import math
import time
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from mdp_controller.__sim_bus import MDPBus, SpeedCounter  # noqa: F401 (re-exported)

logger.warning("You are using the simulated version of MDP-L1060, for testing only")

# Below the protocol's hardware max (set_current only accepts 0 < A < 10) so
# the sim can exercise the latch-and-refuse UI path within the normal in-range
# spinbox values a user could actually enter -- real OCP thresholds are set on
# the device's own front panel, not over RF, so this is an arbitrary in-sim
# value, not a claim about real hardware behavior.
_OCP_LATCH_THRESHOLD_A = 5.0


class MDP_L1060:
    """
    SIMULATED VERSION, FOR TESTING ONLY

    Models an electronic load sinking current from a fixed virtual Thevenin
    source (source_voltage/source_r), the inverse role of P906's sim (which
    pushes current into a virtual resistor).
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
        logger.info(
            f"MDP-L1060 init params: bus={bus}, idcode={idcode}, m01_channel={m01_channel}, "
            f"led_color={led_color}, com_timeout={com_timeout}, com_retry={com_retry}, "
            f"blink={blink}, debug={debug}"
        )
        self._bus = bus
        self._idcode = idcode
        self._rtvalue_callback = None
        self._status_callback = None

        self._mode = "CC"
        self._targets: Dict[str, float] = {"CC": 1.0, "CV": 5.0, "CR": 100.0, "CP": 10.0}
        self._load_on = False
        self._protection_latched = False
        self._protection_reason: Optional[str] = None
        self._temperature = 25.0

        self._source_voltage = 12.0
        self._source_r = 0.3
        self._r_params = [
            (0.2 + 2 * i, 0.01 + 0.02 * i) for i in range(4)
        ]  # sinusoidal wobble, same style as MDP_P906 sim's _r_params

    def _debug_clear_latch(self):
        """Test-only escape hatch -- never call this from GUI code. Real
        hardware needs a physical Run-button press to clear a latch; --sim
        must not offer an RF-reachable way around that."""
        self._protection_latched = False
        self._protection_reason = None

    @property
    def idcode(self) -> Optional[str]:
        return self._idcode

    @property
    def speed_counter(self) -> SpeedCounter:
        return self._bus.speed_counter

    def close(self):
        self._bus.detach(self)
        logger.info("MDP-L1060 closed")

    def _wobble(self) -> float:
        return sum(
            math.sin(time.perf_counter() * p * 2 * math.pi) * a for p, a in self._r_params
        )

    def _simulated_output(self) -> Tuple[float, float]:
        if not self._load_on:
            return 0.0, 0.0
        sv = self._source_voltage + self._wobble()
        sr = self._source_r
        target = self._targets[self._mode]
        if self._mode == "CC":
            i = min(target, sv / sr) if sv > 0 else 0.0
            v = max(sv - i * sr, 0.0)
        elif self._mode == "CV":
            v = min(target, sv)
            i = max((sv - v) / sr, 0.0)
        elif self._mode == "CR":
            # v = sv - i*sr and v = i*target solved simultaneously
            i = sv / (sr + target) if (sr + target) > 0 else 0.0
            v = i * target
        else:  # CP
            # v*i = target and v = sv - i*sr -> sr*i^2 - sv*i + target = 0
            disc = sv * sv - 4 * sr * target
            if disc < 0 or sr <= 0:
                i, v = 0.0, 0.0
            else:
                i = (sv - disc**0.5) / (2 * sr)
                v = max(sv - i * sr, 0.0)
        return round(v, 4), round(i, 4)

    def _tick_temperature(self):
        v, i = self._simulated_output()
        target_temp = 25.0 + (v * i) * 1.5
        self._temperature += (target_temp - self._temperature) * 0.02

    def _check_ocp_latch(self):
        if self._load_on and self._mode == "CC" and self._targets["CC"] > _OCP_LATCH_THRESHOLD_A:
            self._protection_latched = True
            self._protection_reason = "OCP"
            self._load_on = False

    def get_status(
        self,
    ) -> Tuple[str, Optional[bool], Optional[bool], float, float, float, float, int, Optional[str], bool]:
        self._tick_temperature()
        v, i = self._simulated_output()
        load_active = i > 0.005 if self._load_on else False
        return (
            self._mode,
            load_active,
            self._load_on,
            round(self._temperature, 1),
            5.15,
            v,
            i,
            0,
            self._protection_reason,
            self._protection_latched,
        )

    def get_realtime_value(self) -> List[Tuple[float, float]]:
        return [self._simulated_output()] * 9

    def request_realtime_value(self) -> bool:
        if self._rtvalue_callback is not None:
            self._rtvalue_callback(self.get_realtime_value())
            return True
        return False

    def register_realtime_value_callback(self, callback: Callable[[list], None]):
        self._rtvalue_callback = callback

    def request_status(self) -> bool:
        if self._status_callback is not None:
            self._status_callback(self.get_status())
            return True
        return False

    def register_status_callback(self, callback: Callable[[tuple], None]):
        self._status_callback = callback

    def request_target_page(self) -> bool:
        return True

    def get_targets(self) -> Dict[str, float]:
        return dict(self._targets)

    def set_current(self, current_a: float):
        logger.info(f"Set L1060 CC target: {current_a}")
        self._targets["CC"] = current_a
        self._check_ocp_latch()

    def set_voltage(self, voltage_v: float):
        logger.info(f"Set L1060 CV target: {voltage_v}")
        self._targets["CV"] = voltage_v

    def set_resistance(self, resistance_ohm: float):
        logger.info(f"Set L1060 CR target: {resistance_ohm}")
        self._targets["CR"] = resistance_ohm

    def set_power(self, power_w: float):
        logger.info(f"Set L1060 CP target: {power_w}")
        self._targets["CP"] = power_w

    def select_mode(self, mode: str):
        mode = mode.upper()
        logger.info(f"Select L1060 mode: {mode}")
        self._mode = mode

    def set_load_on(self, on: bool, retries: int = 3, settle_s: float = 0.05) -> bool:
        if on and self._protection_latched:
            logger.error(
                "Cannot enable L1060 load: protection latched, requires a "
                "physical Run-button reset on the device"
            )
            return False
        logger.info(f"Set L1060 load on: {on}")
        self._load_on = on
        if on:
            self._check_ocp_latch()
        return not (on and self._protection_latched)

    def set_led_color(self, rgb: Tuple[int, int, int]):
        logger.info(f"Set LED color to: {rgb}")

    def connect(self, timeout: float = 8.0):
        logger.success("MDP-L1060 Connected")
