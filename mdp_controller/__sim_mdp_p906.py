import math
import random
import time
from typing import Callable, List, Optional, Tuple

from loguru import logger

from mdp_controller.__sim_bus import MDPBus, SpeedCounter  # noqa: F401 (re-exported)

logger.warning("You are using the simulated version of MDP-P906, for testing only")


class MDP_P906:
    """
    SIMULATED VERSION, FOR TESTING ONLY
    """

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
        Initialize MDP-P906 Digital Power Supply Controller.

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
        logger.info(
            f"MDP-P906 init params: bus={bus}, idcode={idcode}, m01_channel={m01_channel}, "
            f"led_color={led_color}, com_timeout={com_timeout}, com_retry={com_retry}, "
            f"blink={blink}, debug={debug}"
        )
        self._bus = bus
        self._idcode = idcode
        self._rtvalue_callback = None
        self._output_state = False
        self._voltage_set = 5
        self._current_set = 5
        self._r_params = [
            (5 + random.random() * 5, random.random() * 0.003) for _ in range(10)
        ]
        self._r_base = 5

    @property
    def _simulated_r(self) -> float:
        return self._r_base + sum(
            math.sin(time.perf_counter() * p * 2 * math.pi) * a
            for p, a in self._r_params
        )

    @property
    def _simulated_output(self) -> Tuple[float, float]:
        i = min(self._current_set, self._voltage_set / self._simulated_r)
        v = min(self._voltage_set, self._current_set * self._simulated_r)
        return v, i

    @property
    def idcode(self) -> Optional[str]:
        return self._idcode

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
        return (
            "off"
            if not self._output_state
            else (
                "cv"
                if self._voltage_set < self._current_set * self._simulated_r
                else "cc"
            ),
            False,
            self._voltage_set,
            self._current_set,
            20,
            0.4,
            22.5,
            0,
            [self._simulated_output] * 4 if self._output_state else [(0, 0)] * 4,
            "P906",
        )

    @property
    def speed_counter(self) -> SpeedCounter:
        return self._bus.speed_counter

    def get_realtime_value(self) -> List[Tuple[float, float]]:
        """
        Get the realtime values of output in sync mode.

        Returns:
            List[Tuple[float, float]]: A 9-value list of (voltage/V, current/A)

        Note:
            return [] if failed.
        """
        return [self._simulated_output] * 9 if self._output_state else [(0, 0)] * 9

    def request_realtime_value(self) -> bool:
        """
        Request the realtime values of output in async mode.

        Note:
            Should call register_realtime_value_callback() first.

        Returns:
            bool: True if success, False if failed.
        """
        if self._rtvalue_callback is not None:
            self._rtvalue_callback(self.get_realtime_value())
            return True
        return False

    def register_realtime_value_callback(self, callback: Callable[[list], None]):
        """
        Register a callback function to handle the realtime values of output in async mode.

        Args:
            callback (Callable[[list], None]): A function that takes a list of (voltage in mV, current in mA) as input.

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
        logger.info(f"Set output: {state}")
        self._output_state = state

    def set_voltage(self, voltage_set: float):
        """
        Set the output voltage of the MDP-P906.

        Args:
            voltage_set (float): The voltage to be set, in V, steps of 0.001V.
        """
        logger.info(f"Set VOUT: {voltage_set}")
        self._voltage_set = voltage_set

    def set_current(self, current_set: float):
        """
        Set the output current of the MDP-P906.

        Args:
            current_set (float): The current to be set, in A, steps of 0.001A.
        """
        logger.info(f"Set IOUT: {current_set}")
        self._current_set = current_set

    def get_set_voltage_current(self) -> Tuple[float, float]:
        """
        Get the set voltage and current of the MDP-P906.

        Returns:
            Tuple[float, float]: A tuple of (voltage/V, current/A).
        """
        return self._voltage_set, self._current_set

    def set_led_color(self, rgb: Tuple[int, int, int]):
        """
        Set the LED color of the digital wheel.

        Args:
            rgb (Tuple[int, int, int]): The color of the LED, in the form of (R, G, B).
        """
        logger.info(f"Set LED color to: {rgb}")

    def connect(self, retry_times: int = 3):
        """
        Connect to the MDP-P906 and prepare information for calibration.

        Args:
            retry_times (int): The number of times to try to connect to the MDP-P906.

        Raises:
            Exception: If failed to connect to the MDP-P906.
        """
        logger.success("MDP-P906 Connected")

    def update_gain_offset(self) -> Tuple[int, int, int, int]:
        """
        Update the gain and offset of the ADCs.

        Note:
            Called by connect(), doesn't need to be called again.

        Returns:
            Tuple[int, int, int, int]: A tuple containing HVzero16, HVgain16, HCzero04, HCgain04.
        """
        logger.info("Update gain and offset")
        return (0, 0, 0, 0)
