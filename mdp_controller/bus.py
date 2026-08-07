import threading
import time
from copy import deepcopy
from typing import Dict, Literal, Optional, Tuple

from loguru import logger

import mdp_controller.mdp_protocal as mdp_protocal
from mdp_controller.mdp_device import _hex_to_bytes
from mdp_controller.nrf24_adapter import (
    NRF24Adapter,
    NRF24AdapterError,
    NRF24AdapterSetting,
    SpeedCounter,
)

# Pipe-address derivation: every device gets an address that shares its
# upper 4 bytes with the bus's own configured address and differs only in
# the last byte (one of _PIPE_ADDRESS_LSBS, indexed by pipe 1-5) -- the same
# base_addr + (0xE1+k) scheme the real MDP-M01 hub uses. This isn't a style
# choice: real nRF24L01+ hardware only gives pipes 1 and 0 a fully
# independent 5-byte RX address; pipes 2-5 only have a single configurable
# LSB register apiece and silently share pipe 1's upper 4 bytes for
# reception (confirmed against this repo's own firmware,
# nrf24l01p_open_rx_pipe() in nrf_adapter_source/core/nrf24l01p.c --
# pipe==1 writes all 5 bytes, pipe>1 writes only addr[0]).
# A per-device *idcode*-derived address (this file's previous scheme) broke
# exactly this: two devices' idcodes essentially never share upper bytes, so
# only whichever device landed on pipe 1 would actually be reachable.
# Because the address only depends on (bus's own configured address, pipe
# number) -- never on idcode or which bus instance computes it -- a
# throwaway match-only MDPBus (Settings dialog's "Match" button) and the
# real session bus agree on a device's address as long as they're given the
# same target pipe. Pipe 0 itself is never used for a device: RX_ADDR_P0 is
# the adapter's own identity address, shared with TX_ADDR for ShockBurst
# auto-ack, so it can't be reassigned per-device either.
_PIPE_ADDRESS_PREFIX = 0xE1

_MATCH_COM_TIMEOUT = 0.04


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
        # WiFi adds a network connect in front of the adapter's 1s ECHO
        # cadence, on top of a few ms of per-request jitter -- see
        # plans/nrf_adapter_esp32_wifi_link_plan.md, "Host side: the timeout
        # budget". Both the connect wait and every device's default
        # com_timeout are widened for a tcp:// port; a serial port keeps the
        # tighter wired-link budgets unchanged.
        self._is_tcp = bool(port) and port.startswith("tcp://")
        self.com_timeout = 0.08 if self._is_tcp else 0.04

        self._lock = threading.RLock()
        self._pipe_owners: Dict[int, Optional[object]] = {}
        self._current_target: Optional[bytes] = None

        self._match_data = b""
        self._match_wait_header = -1
        self._match_event = threading.Event()

        self._adp = NRF24Adapter(port=port, baudrate=baudrate, debug=debug)
        self._adp.nrf_register_recv_callback(self._on_recv)

        if not self._adp.wait_connected(timeout=4.0 if self._is_tcp else 2.0):
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

    def _pipe_address(self, pipe: int) -> bytes:
        assert 1 <= pipe <= 5, f"pipe must be 1-5, got {pipe}"
        return self._address[:4] + bytes([_PIPE_ADDRESS_PREFIX + pipe])

    def _next_free_pipe(self) -> int:
        for pipe in range(1, 6):
            if pipe not in self._pipe_owners:
                return pipe
        raise Exception("No free NRF24 pipe available (max 5 devices)")

    def attach(self, device, pipe: int):
        with self._lock:
            address = self._pipe_address(pipe)
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

            context = (
                f"pipe addr ..{owner.address[-1]:02X}, type 0x{packet[0]:02X}, "
                f"{'waited' if wait_response else 'fire-and-forget'}"
            )

            if not wait_response:
                if _retry is None:
                    _retry = owner.com_retry
                try:
                    self._adp.nrf_send(packet, timeout=owner.com_timeout, context=context)
                except NRF24AdapterError:
                    if _retry > 0:
                        return self.transfer(owner, packet, wait_response, _retry - 1)
                    raise
                return b""

            if _retry is None:
                _retry = owner.com_retry
            owner._transfer_data = b""
            owner._transfer_wait_header = packet[0]
            owner._transfer_event.clear()
            try:
                self._adp.nrf_send(packet, timeout=owner.com_timeout, context=context)
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

    def auto_match(self, try_times: int = 3, pipe: Optional[int] = None) -> Tuple[str, int]:
        """
        Discover an unmatched device via broadcast and dispatch it onto the
        given pipe.

        Args:
            pipe: Target pipe (1-5) to dispatch the device onto. Pass the
                device's real, eventual pipe explicitly (e.g. its index in
                setting.devices + 1) whenever this call's result needs to
                agree with a later attach() on a *different* MDPBus instance
                (e.g. the Settings dialog's "Match" button uses a throwaway
                bus) -- since address is a pure function of (this bus's
                configured address, pipe number), not of idcode, two bus
                instances only compute the same address for a device if
                they're given the same pipe. Defaults to this bus's own next
                free pipe, which is only correct when there's no other bus
                instance's pipe assignment to stay consistent with (e.g. a
                single-device standalone script).

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

            if pipe is None:
                pipe = self._next_free_pipe()
            address = self._pipe_address(pipe)
            self._match_transfer(
                mdp_protocal.gen_dispatch_ch_addr(address, self._freq - 2400)
            )
            logger.info(
                f"Dispatched device to address {address.hex(':').upper()} "
                f"with freq {self._freq} Mhz on pipe {pipe}"
            )

            self._adp.nrf_set_settings(setting_old)
            self._current_target = setting_old.address
            self._adp.nrf_open_pipe(pipe, address)
            self._pipe_owners.setdefault(pipe, None)

        logger.success(
            f"Successfully auto matched (idcode: {idcode.hex().upper()}, pipe: {pipe})"
        )
        return idcode.hex().upper(), pipe

    def close(self):
        self._adp.close()
        logger.info("MDPBus closed")
