import os
import time
from threading import Event
from typing import Literal, Optional, Tuple

from loguru import logger

from mdp_controller.mdp_device import ConnectCancelled

logger.warning("You are using the simulated version of MDPBus, for testing only")

_next_fake_idcode = 0x11223344


def sim_connect(device_name: str, idcode: Optional[str], cancel: Optional[Event]):
    """
    Shared connect() body for the simulated devices.

    MDP_SIM_CONNECT_DELAY (seconds) makes the connect take that long,
    honoring cancel the way the real connect() does. MDP_SIM_CONNECT_FAIL
    (comma-separated IDCODEs) makes the listed devices fail once the delay
    has elapsed. Both are read on every call.
    """
    delay = float(os.environ.get("MDP_SIM_CONNECT_DELAY", "0") or 0)
    fail = {
        s.strip().upper()
        for s in os.environ.get("MDP_SIM_CONNECT_FAIL", "").split(",")
        if s.strip()
    }
    if cancel is not None and cancel.is_set():
        raise ConnectCancelled(f"Connect to {device_name} cancelled")
    if delay > 0:
        if cancel is None:
            time.sleep(delay)
        elif cancel.wait(delay):
            raise ConnectCancelled(f"Connect to {device_name} cancelled")
    if (idcode or "").upper() in fail:
        raise Exception(f"Failed to connect to {device_name}")
    logger.success(f"{device_name} Connected")


class SpeedCounter:
    def __init__(self, *args, **kwargs):
        self._speed_Bps = 1024
        self._error_rate = 0.1

    @property
    def bps(self) -> float:
        return self._speed_Bps * 8

    @property
    def Bps(self) -> float:
        return self._speed_Bps

    @property
    def Kbps(self) -> float:
        return self.KBps * 8

    @property
    def KBps(self) -> float:
        return self._speed_Bps / 1024

    @property
    def Mbps(self) -> float:
        return self.MBps * 8

    @property
    def MBps(self) -> float:
        return self._speed_Bps / 1024 / 1024

    @property
    def error_rate(self) -> float:
        return self._error_rate


class MDPBus:
    """
    SIMULATED VERSION, FOR TESTING ONLY

    Device-agnostic: attach()/transfer() don't touch device type at all, so
    any number of simulated devices (P906, L1060, ...) can attach to one sim
    bus at different pipes with no changes needed here.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 921600,
        address: str = "0E:4C:B9:EF:E0",
        freq: int = 2473,
        tx_output_power: Literal[
            "7dBm", "4dBm", "3dBm", "1dBm", "0dBm", "-4dBm", "-6dBm", "-12dBm"
        ] = "4dBm",
        debug: bool = False,
    ):
        logger.info(
            f"MDPBus init params: port={port}, baudrate={baudrate}, address={address}, "
            f"freq={freq}, tx_output_power={tx_output_power}, debug={debug}"
        )
        self.com_timeout = 0.08 if (port or "").startswith("tcp://") else 0.04

    @property
    def speed_counter(self) -> SpeedCounter:
        return SpeedCounter()

    def attach(self, device, pipe: int = 0):
        logger.info(f"Attach device to pipe {pipe}")
        device.address = b"\x00" * 5

    def detach(self, device):
        logger.info("Detach device")

    def transfer(self, owner, packet: bytes, wait_response: bool = True) -> bytes:
        return b""

    def auto_match(self, try_times: int = 3, pipe: Optional[int] = None) -> Tuple[str, int]:
        global _next_fake_idcode
        logger.info("Auto match")
        idcode = f"{_next_fake_idcode:08X}"
        _next_fake_idcode += 1
        return idcode, pipe if pipe is not None else 1

    def close(self):
        logger.info("MDPBus closed")
