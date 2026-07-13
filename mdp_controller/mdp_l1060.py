import time
from threading import Event
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from loguru import logger

import mdp_controller.mdp_protocal_l1060 as mdp_protocal_l1060
from mdp_controller.nrf24_adapter import NRF24AdapterError

if TYPE_CHECKING:
    from mdp_controller.bus import MDPBus


def _convert_to_rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _hex_to_bytes(s: str) -> bytes:
    s = s.replace("0x", "").replace(":", "").replace(" ", "")
    return bytes.fromhex(s)


class MDP_L1060:
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
        MDP-L1060 Electronic Load driver.

        Args:
            bus (MDPBus): The shared transport this device attaches to (see bus.attach()).
            idcode (Optional[str]): ID code of the MDP-L1060, set to None then call bus.auto_match() to get idcode.
            m01_channel (int): Simulate the MDP-M01, this number shows on top-right of L1060's LCD.
            led_color (Tuple[int, int, int]): Color of the digital wheel of the L1060, in RGB format.
            com_timeout (Optional[float]): Communication timeout in seconds between L1060 and the adapter.
            com_retry (int): Communication retry times when timeout occurs.
            blink (bool): Whether to blink the "under-control" indicator of the L1060.
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
            "ErrFlag": 0,
            "Temperature": 0.0,
            "LoadMode": "CC",
            "LoadActive": None,
            "LoadEnabled": None,
            "InputVoltage": 0.0,
            "Voltage": 0.0,
            "Current": 0.0,
            "AnchorValid": False,
            "RealtimeOutput9": [(0.0, 0.0) for _ in range(9)],
            "Targets": {"CC": 0.0, "CV": 0.0, "CR": 0.0, "CP": 0.0},
            "Protection": None,
            "ProtectionLatched": False,
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
                result = mdp_protocal_l1060.parse_type7_response(data)
                self._status["ErrFlag"] = result["errflag"]
                self._status["Temperature"] = result["temperature"]
                if result["load_mode"] != "Unknown":
                    self._status["LoadMode"] = result["load_mode"]
                self._status["LoadActive"] = result["load_active"]
                if result["input_voltage"] is not None:
                    self._status["InputVoltage"] = result["input_voltage"]
                if result["voltage"] is not None and result["current"] is not None:
                    self._status["Voltage"] = result["voltage"]
                    self._status["Current"] = result["current"]
                    self._status["AnchorValid"] = True
            elif data[0] == 8:
                if not self._status["AnchorValid"]:
                    logger.debug("L1060 Type-8: no valid Type-7 anchor yet, skipping unwrap")
                else:
                    errflag, values = mdp_protocal_l1060.parse_type8_response(
                        data, self._status["Voltage"], self._status["Current"]
                    )
                    self._status["ErrFlag"] = errflag
                    self._status["RealtimeOutput9"] = values
                    if self._rtvalue_callback is not None:
                        self._rtvalue_callback(values)
            elif data[0] == 9:
                idcode, _latched = mdp_protocal_l1060.parse_type9_response(data)
                if idcode != self._idcode:
                    logger.warning(f"Type-9 ID code mismatch: {idcode}!={self._idcode}")
            elif data[0] == 10:
                result = mdp_protocal_l1060.parse_type10_response(data)
                if result["errflag"] is not None:
                    self._status["ErrFlag"] = result["errflag"]
                if result["load_mode"] not in (None, "Unknown"):
                    self._status["LoadMode"] = result["load_mode"]
                if result["load_enabled"] is not None:
                    self._status["LoadEnabled"] = result["load_enabled"]
                if result["input_voltage"] is not None:
                    self._status["InputVoltage"] = result["input_voltage"]
                if result["page"] is not None:
                    self._status["Targets"][result["page"]] = result["target_value"]
                protection_latched = bool(result["errflag"]) and result["load_enabled"] is False
                self._status["ProtectionLatched"] = protection_latched
                self._status["Protection"] = result["protection"] if protection_latched else None
            elif data[0] == 5:
                pass
            elif data[0] == 6:
                logger.info(
                    f"Dispatch device result: {mdp_protocal_l1060.parse_type6_response(data)}"
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
        logger.info("MDP-L1060 closed")

    def get_status(
        self,
    ) -> Tuple[str, Optional[bool], Optional[bool], float, float, float, float, int, Optional[str], bool]:
        """
        Get the status of the device.

        Returns:
            Tuple containing:
            - LoadMode (str): 'CC' / 'CV' / 'CR' / 'CP'
            - LoadActive (Optional[bool]): True if current is actually flowing (Type 7 derived)
            - LoadEnabled (Optional[bool]): True if the load switch is on (Type 10 derived, may lag)
            - Temperature (float): Device temperature
            - InputVoltage (float): USB/power-input rail voltage (NOT load-terminal voltage)
            - Voltage (float): Load-terminal voltage
            - Current (float): Load-terminal current
            - ErrFlag (int): System error flag
            - Protection (Optional[str]): None / "OVP" / "OCP" / "OPP_OR_UVP" when latched
            - ProtectionLatched (bool): True if a protection fault is latched (requires physical Run-button reset)
        """
        assert self._idcode is not None, "Please pair first"
        self._transfer(
            mdp_protocal_l1060.gen_get_type7(
                self._idcode, self._m01_channel, blink=self._blink
            )
        )
        return (
            self._status["LoadMode"],
            self._status["LoadActive"],
            self._status["LoadEnabled"],
            self._status["Temperature"],
            self._status["InputVoltage"],
            self._status["Voltage"],
            self._status["Current"],
            self._status["ErrFlag"],
            self._status["Protection"],
            self._status["ProtectionLatched"],
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
                mdp_protocal_l1060.gen_get_type8(
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
            Should call register_realtime_value_callback() first. Only fires
            the callback once a valid Type-7 anchor exists (see connect()) --
            unwrapping Type-8 samples against a zero/stale anchor would
            silently corrupt the measurement.

        Returns:
            bool: True if success, False if failed.
        """
        assert self._idcode is not None, "Please pair first"
        try:
            self._transfer(
                mdp_protocal_l1060.gen_get_type8(
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
        """
        self._rtvalue_callback = callback

    def request_target_page(self) -> bool:
        """
        Request the next rotating Type-10 target page (fire-and-forget).

        Note:
            High get-loss is expected on this path (bench-observed 40-62%);
            call this on a slow, independent poll cadence and read whichever
            targets have landed via get_targets() -- never block waiting for
            all four pages to refresh.
        """
        assert self._idcode is not None, "Please pair first"
        try:
            self._transfer(
                mdp_protocal_l1060.gen_get_type10(
                    self._idcode, self._m01_channel, blink=self._blink
                ),
                wait_response=False,
            )
            return True
        except (TimeoutError, NRF24AdapterError):
            return False

    def get_targets(self) -> Dict[str, float]:
        """
        Get the last-known target setpoints for all four modes.

        Returns:
            Dict[str, float]: {"CC": amps, "CV": volts, "CR": ohms, "CP": watts}
        """
        return dict(self._status["Targets"])

    def set_current(self, current_a: float):
        """Set the CC-mode target current, in A."""
        assert self._idcode is not None, "Please pair first"
        logger.debug(f"Set L1060 CC target: {current_a}")
        self._transfer(
            mdp_protocal_l1060.gen_set_current(
                self._idcode, current_a, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )

    def set_voltage(self, voltage_v: float):
        """Set the CV-mode target voltage, in V."""
        assert self._idcode is not None, "Please pair first"
        logger.debug(f"Set L1060 CV target: {voltage_v}")
        self._transfer(
            mdp_protocal_l1060.gen_set_voltage(
                self._idcode, voltage_v, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )

    def set_resistance(self, resistance_ohm: float):
        """Set the CR-mode target resistance, in Ohm."""
        assert self._idcode is not None, "Please pair first"
        logger.debug(f"Set L1060 CR target: {resistance_ohm}")
        self._transfer(
            mdp_protocal_l1060.gen_set_resistance(
                self._idcode, resistance_ohm, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )

    def set_power(self, power_w: float):
        """Set the CP-mode target power, in W."""
        assert self._idcode is not None, "Please pair first"
        logger.debug(f"Set L1060 CP target: {power_w}")
        self._transfer(
            mdp_protocal_l1060.gen_set_power(
                self._idcode, power_w, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )

    def select_mode(self, mode: str):
        """Select the active load mode: 'CC' / 'CV' / 'CR' / 'CP'."""
        assert self._idcode is not None, "Please pair first"
        mode = mode.upper()
        assert mode in mdp_protocal_l1060.MODE_CODES, f"Unknown mode {mode!r}"
        logger.debug(f"Select L1060 mode: {mode}")
        self._transfer(
            mdp_protocal_l1060.gen_select_mode(
                self._idcode, mode, self._m01_channel, blink=self._blink
            ),
            wait_response=False,
        )
        self._status["LoadMode"] = mode

    def set_load_on(self, on: bool, retries: int = 3, settle_s: float = 0.05) -> bool:
        """
        Turn the load on/off, with confirm-and-retry on the on-path.

        A dropped load-on packet still looks accepted (fire-and-forget write,
        no per-write ack payload worth trusting), so turning on re-sends the
        current mode select, sends the switch write, then confirms current
        actually flowed (LoadActive, from Type 7) before trusting it --
        resending the whole sequence on failure, not just re-polling. This is
        a second, higher-level retry layer on top of MDPBus.transfer()'s own
        internal retry (which only covers wait_response=True calls and can't
        detect a dropped fire-and-forget switch write at all).

        Turning off is a single fire-and-forget write, no confirmation --
        matches the proven mdp_commander sequence.

        Returns:
            bool: True if confirmed (or off, which isn't confirmed), False if
            refused (protection latched) or not confirmed after retries.
        """
        assert self._idcode is not None, "Please pair first"
        if on and self._status["ProtectionLatched"]:
            logger.error(
                "Cannot enable L1060 load: protection latched, requires a "
                "physical Run-button reset on the device"
            )
            return False
        for attempt in range(retries):
            if on:
                self._transfer(
                    mdp_protocal_l1060.gen_select_mode(
                        self._idcode,
                        self._status["LoadMode"],
                        self._m01_channel,
                        blink=self._blink,
                    ),
                    wait_response=False,
                )
            self._transfer(
                mdp_protocal_l1060.gen_set_load_switch(
                    self._idcode, on, self._m01_channel, blink=self._blink
                ),
                wait_response=False,
            )
            if not on:
                return True
            time.sleep(settle_s)
            try:
                self.get_status()
            except (TimeoutError, NRF24AdapterError):
                continue
            if self._status["LoadActive"]:
                return True
            logger.warning(f"set_load_on(True) not confirmed, retry {attempt+1}/{retries}")
        logger.error(f"set_load_on(True) failed after {retries} retries")
        return False

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
            mdp_protocal_l1060.gen_set_led_color(
                self._idcode, self._led_color, self._m01_channel, blink=self._blink
            )
        )

    def connect(self, retry_times: int = 3):
        """
        Connect to the MDP-L1060.

        Blocks until a valid Type-7 measurement anchor exists (self._status
        ["AnchorValid"]) -- this is what prevents request_realtime_value()'s
        Type-8 samples from being unwrapped against a zero/stale reference at
        link-time. No calibration round-trip is needed (unlike P906): the
        L1060's Type 7/8 decode is exact base-100/wrap-unwrap arithmetic, no
        gain/offset correction.

        Args:
            retry_times (int): The number of times to try to connect to the MDP-L1060.

        Raises:
            Exception: If failed to connect to the MDP-L1060.
        """
        assert self._idcode is not None, "Please pair first"
        for i in range(retry_times + 1):
            try:
                self._transfer(
                    mdp_protocal_l1060.gen_set_led_color(
                        self._idcode, self._led_color, self._m01_channel, blink=self._blink
                    )
                )
                self.get_status()
            except (NRF24AdapterError, TimeoutError, AssertionError) as e:
                if i == retry_times:
                    raise Exception("Failed to connect to MDP-L1060") from e
                logger.error(f"Connect failed, retry - {i+1}/{retry_times}")
                time.sleep(0.1)
                continue
            if self._status["AnchorValid"]:
                break
            if i == retry_times:
                raise Exception(
                    "Failed to connect to MDP-L1060: no valid measurement anchor"
                )
            logger.error(f"Connect got no valid measurement, retry - {i+1}/{retry_times}")
            time.sleep(0.1)
        logger.debug(f"MDP-L1060 init status: {self._status}")
        logger.success("MDP-L1060 Connected")
