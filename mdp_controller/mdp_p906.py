from typing import TYPE_CHECKING, List, Optional, Tuple

from loguru import logger

import mdp_controller.mdp_protocal as mdp_protocal
from mdp_controller.mdp_device import MDPDevice

if TYPE_CHECKING:
    from mdp_controller.bus import MDPBus


class MDP_P906(MDPDevice):
    device_name = "MDP-P906"

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

    def _handle_packet(self, data: bytes) -> bool:
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
            if self._status_callback is not None:
                self._status_callback(self._status_tuple())
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
        else:
            return False
        return True

    def _status_tuple(
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
        return self._status_tuple()

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

    def _connect_probe(self):
        """
        Probe via update_gain_offset(): the round-trip also latches the four
        calibration constants into _status, which every Type-7/8 decode
        depends on.
        """
        self.update_gain_offset()

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
