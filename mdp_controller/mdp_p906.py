import time
from threading import Event
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from loguru import logger

import mdp_controller.mdp_protocal as mdp_protocal
from mdp_controller.nrf24_adapter import NRF24AdapterError

if TYPE_CHECKING:
    from mdp_controller.bus import MDPBus


def _convert_to_rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _hex_to_bytes(s: str) -> bytes:
    s = s.replace("0x", "").replace(":", "").replace(" ", "")
    return bytes.fromhex(s)


class MDP_P906:
    def __init__(
        self,
        bus: "MDPBus",
        idcode: Optional[str] = None,
        m01_channel: int = 0,
        led_color: Tuple[int, int, int] = (0x66, 0xCC, 0xFF),
        com_timeout: Optional[float] = 0.04,
        com_retry: int = 5,
        blink: bool = True,
        debug: bool = False,
    ):
        """
        MDP-P906 Digital Power Supply Controller driver.

        Args:
            bus (MDPBus): The shared transport this device attaches to (see bus.attach()).
            idcode (Optional[str]): ID code of the MDP-P906, set to None then call bus.auto_match() to get idcode.
            m01_channel (int): Simulate the MDP-M01, this number shows on top-right of P906's LCD.
            led_color (Tuple[int, int, int]): Color of the digital wheel of the P906, in RGB format.
            com_timeout (Optional[float]): Communication timeout in seconds between P906 and the adapter.
            com_retry (int): Communication retry times when timeout occurs.
            blink (bool): Whether to blink the "under-control" indicator of the P906.
            debug (bool): Show debug info.
        """
        self._bus = bus
        self.address: Optional[bytes] = None
        self._idcode = _hex_to_bytes(idcode) if idcode is not None else None
        self._m01_channel = m01_channel
        self._led_color = _convert_to_rgb565(*led_color)
        self._com_timeout = com_timeout
        self._com_retry = com_retry
        self._blink = blink
        self._debug = debug
        self._status = {
            "Model": "Unknown",
            "HVzero16": 0.0,
            "HVgain16": 0.0,
            "HCzero04": 0.0,
            "HCgain04": 0.0,
            "SetVoltage": 0.0,
            "SetCurrent": 0.0,
            "InputVoltage": 0.0,
            "InputCurrent": 0.0,
            "ErrFlag": 0,
            "Locked": False,
            "State": "off",
            "Temperature": 0.0,
            "RealtimeOutput4": [0.0 for _ in range(4)],
            "RealtimeOutput9": [0.0 for _ in range(9)],
        }

        self._transfer_data = b""
        self._transfer_wait_header = -1
        self._transfer_event = Event()

        self._rtvalue_callback: Optional[Callable[[list], None]] = None

    @property
    def idcode(self) -> Optional[bytes]:
        return self._idcode

    @property
    def com_timeout(self) -> Optional[float]:
        return self._com_timeout

    @property
    def com_retry(self) -> int:
        return self._com_retry

    @property
    def speed_counter(self):
        return self._bus.speed_counter

    def _on_packet(self, data: bytes):
        try:
            if data[0] == 7:
                (
                    errflag,
                    input_volt,
                    input_curr,
                    voltage,
                    current,
                    locked,
                    state,
                    temperature,
                    realtime_adc,
                ) = mdp_protocal.parse_type7_response(
                    data,
                    self._status["HVzero16"],
                    self._status["HVgain16"],
                    self._status["HCzero04"],
                    self._status["HCgain04"],
                )
                self._status["ErrFlag"] = errflag
                self._status["InputVoltage"] = input_volt
                self._status["InputCurrent"] = input_curr
                self._status["SetVoltage"] = voltage
                self._status["SetCurrent"] = current
                self._status["Locked"] = bool(locked)
                self._status["State"] = {0: "off", 1: "cc", 2: "cv", 3: "on"}[state]
                self._status["Temperature"] = temperature
                self._status["RealtimeOutput4"] = realtime_adc
            elif data[0] == 9:
                idcode, HVzero16, HVgain16, HCzero04, HCgain04, model = (
                    mdp_protocal.parse_type9_response(data)
                )
                if idcode != self._idcode:
                    logger.warning(f"Type-9 ID code mismatch: {idcode}!={self._idcode}")
                self._status["HVzero16"] = HVzero16
                self._status["HVgain16"] = HVgain16
                self._status["HCzero04"] = HCzero04
                self._status["HCgain04"] = HCgain04
                self._status["Model"] = {1: "P905", 2: "P906"}.get(model, "Unknown")
            elif data[0] == 8:
                errflag, values = mdp_protocal.parse_type8_response(
                    data,
                    self._status["HVzero16"],
                    self._status["HVgain16"],
                    self._status["HCzero04"],
                    self._status["HCgain04"],
                )
                self._status["ErrFlag"] = errflag
                self._status["RealtimeOutput9"] = values
                if self._rtvalue_callback is not None:
                    self._rtvalue_callback(values)
            elif data[0] == 4:
                self._status["SetCurrent"], self._status["SetVoltage"] = (
                    mdp_protocal.parse_type4_response(data)
                )
            elif data[0] == 5:
                pass
            elif data[0] == 6:
                logger.info(
                    f"Dispatch device result: {mdp_protocal.parse_type6_response(data)}"
                )
            else:
                logger.warning(f"Unhandled Type-{data[0]}: {data.hex(' ').upper()}")

            if self._debug:
                logger.trace(
                    f"Type-{data[0]}: {data.hex(' ').upper()} -> {self._status}"
                )

        except Exception:
            logger.exception("Parse error")

        if data[0] == self._transfer_wait_header:
            self._transfer_data = data
            self._transfer_wait_header = -1
            self._transfer_event.set()

    def _transfer(self, packet: bytes, wait_response: bool = True):
        return self._bus.transfer(self, packet, wait_response)

    def close(self):
        self._bus.detach(self)
        logger.info("MDP-P906 closed")

    def get_status(
        self,
    ) -> Tuple[
        str,
        bool,
        float,
        float,
        float,
        float,
        float,
        int,
        List[Tuple[float, float]],
        str,
    ]:
        """
        Get the status of the device.

        Returns:
            Tuple containing:
            - State (str): 'off' / 'cc' / 'cv' / 'on' (output unstable)
            - Locked (bool): True if the device is locked
            - SetVoltage (float): Set voltage value
            - SetCurrent (float): Set current value
            - InputVoltage (float): Input voltage value
            - InputCurrent (float): Input current value
            - Temperature (float): Device temperature
            - ErrFlag (int): System error flag
            - RealtimeOutput (List[Tuple[float, float]]): 4-value list of (voltage/V, current/A)
            - Model (str): "P905" / "P906" / "Unknown"

        Note:
            If SetVoltage or SetCurrent is -1, it means the value is unstable.
        """
        assert self._idcode is not None, "Please pair first"
        self._transfer(
            mdp_protocal.gen_get_type7(
                self._idcode, self._m01_channel, blink=self._blink
            )
        )
        return (
            self._status["State"],
            self._status["Locked"],
            self._status["SetVoltage"],
            self._status["SetCurrent"],
            self._status["InputVoltage"],
            self._status["InputCurrent"],
            self._status["Temperature"],
            self._status["ErrFlag"],
            self._status["RealtimeOutput4"],
            self._status["Model"],
        )

    def get_realtime_value(self) -> List[Tuple[float, float]]:
        """
        Get the realtime values of output in sync mode.

        Returns:
            List[Tuple[float, float]]: A 9-value list of (voltage/V, current/A)

        Note:
            return [] if failed.
        """
        assert self._idcode is not None, "Please pair first"
        try:
            self._transfer(
                mdp_protocal.gen_get_type8(
                    self._idcode, self._m01_channel, blink=self._blink
                )
            )
            return self._status["RealtimeOutput9"]
        except (TimeoutError, NRF24AdapterError):
            return []

    def request_realtime_value(self) -> bool:
        """
        Request the realtime values of output in async mode.

        Note:
            Should call register_realtime_value_callback() first.

        Returns:
            bool: True if success, False if failed.
        """
        assert self._idcode is not None, "Please pair first"
        try:
            self._transfer(
                mdp_protocal.gen_get_type8(
                    self._idcode, self._m01_channel, blink=self._blink
                ),
                wait_response=False,
            )
            return True
        except (TimeoutError, NRF24AdapterError):
            return False

    def register_realtime_value_callback(self, callback: Callable[[list], None]):
        """
        Register a callback function to handle the realtime values of output in async mode.

        Args:
            callback (Callable[[list], None]): A function that takes a list of (voltage in V, current in A) as input.

        Note:
            The callback will be called in a separate thread. get_realtime_value() will also trigger the callback like request_realtime_value(), but in blocking mode.
        """
        self._rtvalue_callback = callback

    def set_output(self, state: bool):
        """
        Set the output state of the MDP-P906.

        Args:
            state (bool): True for on, False for off.
        """
        assert self._idcode is not None, "Please pair first"
        logger.debug(f"Set output: {state}")
        self._transfer(
            mdp_protocal.gen_set_output(
                self._idcode, state, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )

    def set_voltage(self, voltage_set: float):
        """
        Set the output voltage of the MDP-P906.

        Args:
            voltage_set (float): The voltage to be set, in V, steps of 0.001V.
        """
        assert self._idcode is not None, "Please pair first"
        assert 0 <= voltage_set <= 30, "Voltage limit is 0~30V"
        logger.debug(f"Set VOUT: {voltage_set}")
        self._transfer(
            mdp_protocal.gen_set_voltage(
                self._idcode, voltage_set, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )

    def set_current(self, current_set: float):
        """
        Set the output current of the MDP-P906.

        Args:
            current_set (float): The current to be set, in A, steps of 0.001A.
        """
        assert self._idcode is not None, "Please pair first"
        if self._status["Model"] == "P905":
            assert 0 <= current_set <= 5, "P905 current limit is 0~5A"
        elif self._status["Model"] == "P906":
            assert 0 <= current_set <= 10, "P906 current limit is 0~10A"
        logger.debug(f"Set IOUT: {current_set}")
        self._transfer(
            mdp_protocal.gen_set_current(
                self._idcode, current_set, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )

    def get_set_voltage_current(self) -> Tuple[float, float]:
        """
        Get the set voltage and current of the MDP-P906.

        Returns:
            Tuple[float, float]: A tuple of (voltage/V, current/A).
        """
        assert self._idcode is not None, "Please pair first"
        self._transfer(mdp_protocal.gen_get_volt_cur())
        return self._status["SetVoltage"], self._status["SetCurrent"]

    def set_led_color(self, rgb: Tuple[int, int, int]):
        """
        Set the LED color of the digital wheel.

        Args:
            rgb (Tuple[int, int, int]): The color of the LED, in the form of (R, G, B).
        """
        assert self._idcode is not None, "Please pair first"
        rgb565 = _convert_to_rgb565(*rgb)
        self._led_color = rgb565
        logger.debug(f"Set LED color to: {rgb565}")
        self._transfer(
            mdp_protocal.gen_set_led_color(
                self._idcode, self._led_color, self._m01_channel, blink=self._blink
            )
        )

    def connect(self, timeout: float = 8.0):
        """
        Connect to the MDP-P906 and prepare information for calibration.

        Args:
            timeout (float): Total retry budget in seconds. Time-based, not
                attempt-based: a freshly powered-on device ACKs nothing at
                the radio level for the first ~3-4.5 s (measured on real
                hardware for both P906 and L1060), so the budget must
                outlast that boot window for a connect racing a power-on.

        Raises:
            Exception: If failed to connect to the MDP-P906 within the budget.
        """
        assert self._idcode is not None, "Please pair first"
        deadline = time.monotonic() + timeout
        last_log = 0.0
        while True:
            try:
                self.update_gain_offset()
                self.get_status()
            except (NRF24AdapterError, TimeoutError, AssertionError) as e:
                now = time.monotonic()
                if now >= deadline:
                    raise Exception("Failed to connect to MDP-P906") from e
                if now - last_log >= 1.0:
                    logger.error(f"Connect failed, retrying for {deadline - now:.1f}s more")
                    last_log = now
                time.sleep(0.1)
                continue
            break
        logger.debug(f"MDP init status: {self._status}")
        logger.success("MDP-P906 Connected")

    def update_gain_offset(self) -> Tuple[int, int, int, int]:
        """
        Update the gain and offset of the ADCs.

        Note:
            Called by connect(), doesn't need to be called again.

        Returns:
            Tuple[int, int, int, int]: A tuple containing HVzero16, HVgain16, HCzero04, HCgain04.
        """
        assert self._idcode is not None, "Please pair first"
        self._transfer(
            mdp_protocal.gen_set_led_color(
                self._idcode, self._led_color, self._m01_channel
            )
        )
        return (
            self._status["HVzero16"],
            self._status["HVgain16"],
            self._status["HCzero04"],
            self._status["HCgain04"],
        )
