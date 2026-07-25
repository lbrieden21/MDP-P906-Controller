import time
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from loguru import logger

import mdp_controller.mdp_protocal_l1060 as mdp_protocal_l1060
from mdp_controller.mdp_device import MDPDevice
from mdp_controller.nrf24_adapter import NRF24AdapterError

if TYPE_CHECKING:
    from mdp_controller.bus import MDPBus


class MDP_L1060(MDPDevice):
    device_name = "MDP-L1060"

    _connect_ready_error = "no valid measurement anchor"

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
        super().__init__(
            bus,
            idcode=idcode,
            m01_channel=m01_channel,
            led_color=led_color,
            com_timeout=com_timeout,
            com_retry=com_retry,
            blink=blink,
            debug=debug,
        )
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

    def _handle_packet(self, data: bytes) -> bool:
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
            # Type-8 carries wrapped deltas, so it is only decodable against a
            # Type-7 anchor -- unwrapping against a zero/stale reference would
            # silently corrupt the measurement rather than fail. Until connect()
            # has landed an anchor, these samples are dropped, and neither
            # get_realtime_value() nor the request_realtime_value() callback
            # produces anything.
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
        else:
            return False
        return True

    def _refresh_type10(self) -> bool:
        """
        One waited Type 10 get -- the authoritative read of the load-switch
        state (LoadEnabled, sd[4] bit 0, the same field the real M01 polls).
        Also lands one rotating target page as a side effect.

        Returns:
            bool: True if a response arrived and updated the status cache.
        """
        try:
            self._transfer(
                mdp_protocal_l1060.gen_get_type10(
                    self._idcode, self._m01_channel, blink=self._blink
                )
            )
            return True
        except (TimeoutError, NRF24AdapterError):
            return False

    def get_status(
        self,
    ) -> Tuple[str, Optional[bool], Optional[bool], float, float, float, float, int, Optional[str], bool]:
        """
        Get the status of the device.

        Returns:
            Tuple containing:
            - LoadMode (str): 'CC' / 'CV' / 'CR' / 'CP'
            - LoadActive (Optional[bool]): True if current is actually flowing (Type 7
              derived; conduction lags switch-on by ~1 s and can be legitimately False
              while the switch is on, e.g. CV target above the source voltage)
            - LoadEnabled (Optional[bool]): the genuine load-switch state (Type 10
              sd[4] bit 0, the same field the real M01 polls); refreshed whenever a
              Type 10 response arrives, None until the first one does
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

    def set_load_on(self, on: bool, retries: int = 3, settle_s: float = 0.075) -> bool:
        """
        Turn the load on/off, confirming the device-reported switch state
        (LoadEnabled) in both directions.

        A dropped switch packet still looks accepted: the write is
        fire-and-forget, and the radio-level "response" to a switch write is
        a byte-identical stale duplicate of the previous reply, never a fresh
        ack (bench-verified 2026-07-16). So each attempt re-sends the mode
        select (on-path only) and the switch write, then confirms with fresh
        waited Type 10 gets until LoadEnabled reports the commanded state.

        LoadActive (current flowing, Type 7) is NOT the confirm signal:
        conduction lags the switch closing by ~1 s, and is legitimately zero
        in on-states like a CV target above the source voltage -- confirming
        on it reports False for a load that is genuinely on.

        Returns:
            bool: True once LoadEnabled confirms the commanded state, False
            if refused (protection latched) or not confirmed after retries.
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
            # The bit usually flips within one poll, but can trail the write
            # by a few hundred ms on the off-path -- poll a budget covering
            # that lag before charging a retry (which re-sends the switch
            # write).
            for _ in range(6):
                time.sleep(settle_s)
                if self._refresh_type10() and self._status["LoadEnabled"] == on:
                    return True
            logger.warning(f"set_load_on({on}) not confirmed, retry {attempt+1}/{retries}")
        logger.error(f"set_load_on({on}) failed after {retries} retries")
        return False

    def connect(self, timeout: float = 8.0):
        """
        Connect to the MDP-L1060.

        Blocks until a valid Type-7 measurement anchor exists (self._status
        ["AnchorValid"]) -- this is what prevents request_realtime_value()'s
        Type-8 samples from being unwrapped against a zero/stale reference at
        link-time. No calibration round-trip is needed (unlike P906): the
        L1060's Type 7/8 decode is exact base-100/wrap-unwrap arithmetic, no
        gain/offset correction.

        See MDPDevice.connect() for the retry-budget semantics of `timeout`.
        """
        super().connect(timeout)

    def _connect_probe(self):
        self._transfer(
            mdp_protocal_l1060.gen_set_led_color(
                self._idcode, self._led_color, self._m01_channel
            )
        )

    def _connect_ready(self) -> bool:
        return self._status["AnchorValid"]

    def _post_connect(self):
        # Seed the load-switch state so LoadEnabled is known from link time
        # instead of None until the first slow target poll. Best-effort: a
        # lossy link here just leaves it None, it is not a connect failure.
        for _ in range(3):
            if self._refresh_type10():
                break
