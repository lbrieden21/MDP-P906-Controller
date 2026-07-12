import threading
import time
from copy import deepcopy
from typing import Dict, Literal, Optional, Tuple

from loguru import logger

import mdp_controller.mdp_protocal as mdp_protocal
from mdp_controller.nrf24_adapter import (
    NRF24Adapter,
    NRF24AdapterError,
    NRF24AdapterSetting,
    SpeedCounter,
)

# Pipe-address derivation for devices dispatched onto pipes 1-5. Pipe 0 always
# uses the bus's own configured address (matches today's single-device
# behavior exactly, no re-match needed). This prefix byte is otherwise
# unclaimed protocol-wise and just needs to keep every pipe>=1 address
# distinct, which idcode (unique per device) already guarantees.
_PIPE_ADDRESS_PREFIX = 0xE1

_MATCH_COM_TIMEOUT = 0.04


def _hex_to_bytes(s: str) -> bytes:
    s = s.replace("0x", "").replace(":", "").replace(" ", "")
    return bytes.fromhex(s)


class MDPBus:
    """
    Qt-free transport shared by every attached device driver. Owns the single
    NRF24Adapter (one physical UART/radio), routes incoming frames to devices
    by hardware RX pipe number, and serializes all sends behind one lock.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 921600,
        address: str = "AA:BB:CC:DD:EE",
        freq: int = 2442,
        tx_output_power: Literal[
            "7dBm", "4dBm", "3dBm", "1dBm", "0dBm", "-4dBm", "-6dBm", "-12dBm"
        ] = "4dBm",
        debug: bool = False,
    ):
        self._address = _hex_to_bytes(address)
        self._freq = freq
        self._debug = debug

        self._lock = threading.RLock()
        self._pipe_owners: Dict[int, Optional[object]] = {}
        self._current_target: Optional[bytes] = None

        self._match_data = b""
        self._match_wait_header = -1
        self._match_event = threading.Event()

        self._adp = NRF24Adapter(port=port, baudrate=baudrate, debug=debug)
        self._adp.nrf_register_recv_callback(self._on_recv)

        if not self._adp.wait_connected():
            self._adp.close()
            raise Exception("NRF24-Adapter wait connection timeout")

        setting = NRF24AdapterSetting(
            freq=self._freq,
            air_data_rate="2Mbps",
            address_width=5,
            address=self._address,
            tx_output_power=tx_output_power,
            crc_length="crc16",
            payload_length=32,
            auto_retransmit_count=12,
            auto_retransmit_delay=250,
        )
        self._adp.nrf_set_settings(setting)
        self._current_target = self._address
        time.sleep(0.1)

    @property
    def speed_counter(self) -> SpeedCounter:
        return self._adp.speed_counter

    def _pipe_address(self, idcode: bytes, pipe: int) -> bytes:
        if pipe == 0:
            return self._address
        return bytes([_PIPE_ADDRESS_PREFIX]) + idcode

    def _next_free_pipe(self) -> int:
        for pipe in range(6):
            if pipe not in self._pipe_owners:
                return pipe
        raise Exception("No free NRF24 pipe available (max 6 devices)")

    def attach(self, device, pipe: int):
        with self._lock:
            address = self._pipe_address(device.idcode, pipe)
            if pipe != 0:
                self._adp.nrf_open_pipe(pipe, address)
            device.address = address
            self._pipe_owners[pipe] = device
        logger.info(f"Attached device (idcode: {device.idcode.hex().upper()}) to pipe {pipe}")

    def detach(self, device):
        with self._lock:
            for pipe, owner in list(self._pipe_owners.items()):
                if owner is device:
                    del self._pipe_owners[pipe]

    def _on_recv(self, pipe: int, data: bytes):
        owner = self._pipe_owners.get(pipe)
        if owner is not None:
            owner._on_packet(data)
        elif self._debug:
            logger.debug(f"Unclaimed pipe {pipe}: {data.hex(' ').upper()}")

        if data and data[0] == self._match_wait_header:
            self._match_data = data
            self._match_wait_header = -1
            self._match_event.set()

    def transfer(
        self,
        owner,
        packet: bytes,
        wait_response: bool = True,
        _retry: Optional[int] = None,
    ) -> bytes:
        with self._lock:
            if self._current_target != owner.address:
                self._adp.nrf_set_tx_target(owner.address)
                self._current_target = owner.address

            if not wait_response:
                self._adp.nrf_send(packet, timeout=owner.com_timeout)
                return b""

            if _retry is None:
                _retry = owner.com_retry
            owner._transfer_data = b""
            owner._transfer_wait_header = packet[0]
            owner._transfer_event.clear()
            try:
                self._adp.nrf_send(packet, timeout=owner.com_timeout)
            except NRF24AdapterError:
                if _retry > 0:
                    return self.transfer(owner, packet, wait_response, _retry - 1)
                raise
            if not owner._transfer_event.wait(owner.com_timeout):
                if _retry > 0:
                    return self.transfer(owner, packet, wait_response, _retry - 1)
                raise TimeoutError("NRF24 timeout")
            return owner._transfer_data

    def _match_transfer(self, packet: bytes) -> Optional[bytes]:
        self._match_data = b""
        self._match_wait_header = packet[0]
        self._match_event.clear()
        try:
            self._adp.nrf_send(packet, timeout=_MATCH_COM_TIMEOUT)
        except NRF24AdapterError:
            return None
        if not self._match_event.wait(_MATCH_COM_TIMEOUT):
            return None
        return self._match_data

    def auto_match(self, try_times: int = 3) -> Tuple[str, int]:
        """
        Discover an unmatched device via broadcast and dispatch it onto the
        next free pipe.

        Returns:
            Tuple[str, int]: (idcode hex string, pipe number it was dispatched to).
        """
        with self._lock:
            setting = self._adp.nrf_get_settings()
            setting_old = deepcopy(setting)
            setting.address = b"\xff\xff\xff\xff\xff"  # broadcast address
            setting.freq = 2478
            self._adp.nrf_set_settings(setting)
            self._current_target = None

            idcode = None
            for i in range(try_times):
                logger.info(f"Auto matching - {i+1}/{try_times}")
                data = self._match_transfer(mdp_protocal.gen_call_for_id())
                if data is None:
                    time.sleep(1)
                    continue
                if data[0] == 0x05:
                    idcode = mdp_protocal.parse_type5_response(data)
                    logger.info(f"Found device - {idcode.hex().upper()}")
                    break
                logger.warning(f"Unhandled response - {data.hex(' ').upper()}")

            if idcode is None:
                self._adp.nrf_set_settings(setting_old)
                self._current_target = setting_old.address
                raise Exception("Failed to auto match with MDP-P906")

            pipe = self._next_free_pipe()
            address = self._pipe_address(idcode, pipe)
            self._match_transfer(
                mdp_protocal.gen_dispatch_ch_addr(address, self._freq - 2400)
            )
            logger.info(
                f"Dispatched device to address {address.hex(':').upper()} "
                f"with freq {self._freq} Mhz on pipe {pipe}"
            )

            self._adp.nrf_set_settings(setting_old)
            self._current_target = setting_old.address
            if pipe != 0:
                self._adp.nrf_open_pipe(pipe, address)
            self._pipe_owners.setdefault(pipe, None)

        logger.success(
            f"Successfully auto matched (idcode: {idcode.hex().upper()}, pipe: {pipe})"
        )
        return idcode.hex().upper(), pipe

    def close(self):
        self._adp.close()
        logger.info("MDPBus closed")
