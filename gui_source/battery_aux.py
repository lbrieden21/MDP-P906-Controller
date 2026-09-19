"""Qt-free helpers for the battery auxiliary workflows (L1060 Discharge, P906
Battery Charge).

Kept free of PyQt and device-API imports so the integration and charge
state-machine logic here can be exercised by plain unittest without a
running Qt event loop or hardware.
"""

import math
from collections import deque
from typing import Deque, Dict, List, NamedTuple, Optional, Tuple, Union

######### Capacity integration #########


class CapacityRow(NamedTuple):
    elapsed: float
    voltage: float
    current: float
    power: float
    ah: float
    wh: float


class CapacityAccumulator:
    """Integrates Ah/Wh from raw (voltage, current) batches at the rate they
    actually arrive, independent of any LCD/UI polling cadence.

    Each add_batch call supplies the samples collected since the previous
    call plus a monotonic timestamp for the batch. The gap between two
    consecutive batches (dt) is split evenly across the newer batch's
    samples -- matching DeviceDataStore.append's existing energy-integration
    scheme -- so a slow/bursty callback rate doesn't bias the running totals.
    The first batch only seeds the clock; a batch has to be integrated
    against a predecessor before it can contribute charge/energy, so a lone
    startup sample can't be double counted or produce a bogus huge dt.
    """

    ROW_INTERVAL_S = 1.0

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.ah = 0.0
        self.wh = 0.0
        self.rows: List[CapacityRow] = []
        self._start_t = None
        self._last_t = None
        self._row_buf: List[Tuple[float, float, float]] = []
        self._next_row_boundary = self.ROW_INTERVAL_S

    def add_batch(self, samples: List[Tuple[float, float]], t: float) -> None:
        if not samples:
            return
        if self._start_t is None:
            self._start_t = t
            self._last_t = t
            return
        dt = t - self._last_t
        self._last_t = t
        if dt <= 0:
            return
        n = len(samples)
        sub_dt = dt / n
        base_elapsed = self.elapsed
        for idx, (voltage, current) in enumerate(samples):
            power = voltage * current
            self.ah += current * sub_dt / 3600.0
            self.wh += power * sub_dt / 3600.0
            self.elapsed = base_elapsed + sub_dt * (idx + 1)
            self._row_buf.append((voltage, current, power))
            if self.elapsed >= self._next_row_boundary:
                self._flush_row()

    def _flush_row(self) -> None:
        n = len(self._row_buf)
        if n == 0:
            return
        v = sum(s[0] for s in self._row_buf) / n
        i = sum(s[1] for s in self._row_buf) / n
        p = sum(s[2] for s in self._row_buf) / n
        self.rows.append(CapacityRow(self.elapsed, v, i, p, self.ah, self.wh))
        self._row_buf = []
        # First boundary past elapsed, so a batch spanning a long gap is
        # followed by normal one-per-interval rows.
        self._next_row_boundary = (
            math.floor(self.elapsed / self.ROW_INTERVAL_S) + 1
        ) * self.ROW_INTERVAL_S


class BelowThresholdDebounce:
    """Tracks how long a measured value has stayed continuously at or below a
    threshold, so a startup zero or a brief transient dip can't trigger by
    itself -- only a full debounce_s of sustained low readings does."""

    def __init__(self, debounce_s: float = 1.0) -> None:
        self.debounce_s = debounce_s
        self._below_since = None

    def reset(self) -> None:
        self._below_since = None

    def update(self, value: float, threshold: float, t: float) -> bool:
        if value > threshold:
            self._below_since = None
            return False
        if self._below_since is None:
            self._below_since = t
        return (t - self._below_since) >= self.debounce_s


######### Battery charge #########

CHEM_LI_ION = "li_ion"
CHEM_LIFEPO4 = "lifepo4"
CHEM_LEAD_ACID = "lead_acid"
CHEM_NIMH = "nimh"

PHASE_PRECHARGE = "precharge"
PHASE_CC = "cc"
PHASE_CV = "cv"
PHASE_FLOAT = "float"
PHASE_DONE = "done"

REASON_CUTOFF = "cutoff"
REASON_NDV = "ndv"
REASON_CEILING = "ceiling"
REASON_TIMEOUT = "timeout"
REASON_MAX_AH = "max_ah"
REASON_MAX_WH = "max_wh"
REASON_OUTPUT_OFF = "output_off"
REASON_DEVICE_ERROR = "device_error"
REASON_USER_STOP = "user_stop"
REASON_DISCONNECTED = "disconnected"


class ChemistryPreset(NamedTuple):
    """Per-cell voltages and C-rate multipliers for one chemistry. None
    means the stage or stop rule doesn't apply to it."""

    cv_per_cell: float
    cc_c_rate: float
    precharge_below_per_cell: Optional[float]
    precharge_c_rate: Optional[float]
    cutoff_c_rate: Optional[float]
    float_per_cell: Optional[float]
    ndv_per_cell: Optional[float]
    ndv_holdoff_s: Optional[float]


CHEMISTRY_PRESETS: Dict[str, ChemistryPreset] = {
    CHEM_LI_ION: ChemistryPreset(4.20, 0.5, 3.0, 0.1, 0.05, None, None, None),
    CHEM_LIFEPO4: ChemistryPreset(3.65, 0.5, 2.5, 0.1, 0.05, None, None, None),
    CHEM_LEAD_ACID: ChemistryPreset(2.40, 0.2, None, None, 0.05, 2.25, None, None),
    CHEM_NIMH: ChemistryPreset(1.80, 0.5, None, None, None, None, 0.005, 180.0),
}


class ChargeProfile(NamedTuple):
    """Pack-level charge parameters. cv_v is the CV target, or the voltage
    ceiling for NiMH. float_v set means reaching cutoff_a enters FLOAT
    instead of finishing. ndv_v/ndv_holdoff_s enable -dV termination."""

    chemistry: str
    cv_v: float
    cc_a: float
    precharge_below_v: Optional[float]
    precharge_a: Optional[float]
    cutoff_a: Optional[float]
    float_v: Optional[float]
    ndv_v: Optional[float]
    ndv_holdoff_s: Optional[float]
    max_s: Optional[float]
    max_ah: Optional[float]
    max_wh: Optional[float]


def _scaled(per_cell: Optional[float], factor: float) -> Optional[float]:
    return None if per_cell is None else round(per_cell * factor, 3)


def build_profile(chem: str, cells: int, capacity_ah: float) -> ChargeProfile:
    """Scale a chemistry preset to a pack: voltages by cell count, currents
    by capacity. No time/Ah/Wh limits are set. Raises ValueError for an
    unknown chemistry, fewer than one cell, or a non-positive capacity."""
    if chem not in CHEMISTRY_PRESETS:
        raise ValueError(f"unknown chemistry {chem!r}")
    if cells < 1:
        raise ValueError("cells must be at least 1")
    if capacity_ah <= 0:
        raise ValueError("capacity must be positive")
    p = CHEMISTRY_PRESETS[chem]
    return ChargeProfile(
        chemistry=chem,
        cv_v=_scaled(p.cv_per_cell, cells),
        cc_a=_scaled(p.cc_c_rate, capacity_ah),
        precharge_below_v=_scaled(p.precharge_below_per_cell, cells),
        precharge_a=_scaled(p.precharge_c_rate, capacity_ah),
        cutoff_a=_scaled(p.cutoff_c_rate, capacity_ah),
        float_v=_scaled(p.float_per_cell, cells),
        ndv_v=_scaled(p.ndv_per_cell, cells),
        ndv_holdoff_s=p.ndv_holdoff_s,
        max_s=None,
        max_ah=None,
        max_wh=None,
    )


class ChargeSetpoint(NamedTuple):
    v_set: float
    i_set: float


class ChargeFinished(NamedTuple):
    reason: str


ChargeAction = Union[None, ChargeSetpoint, ChargeFinished]


class ChargeController:
    """CC/CV charge state machine: PRECHARGE -> CC -> CV -> FLOAT -> DONE.

    start(t) returns the first setpoint. Each update() call returns None to
    keep the current setpoint, a ChargeSetpoint to apply, or a
    ChargeFinished carrying a REASON_* key; once finished, every later call
    returns the same ChargeFinished.

    PRECHARGE (only when both precharge fields are set) holds precharge_a
    until the voltage stays above precharge_below_v for
    precharge_debounce_s. CC becomes CV when the device reports "cv". CV
    ends when current stays at or below cutoff_a for cutoff_debounce_s, going
    to FLOAT if float_v is set. FLOAT only ends on a limit. With ndv_v set,
    the charge finishes when the ndv_window_s moving-average voltage falls
    ndv_v below its peak, with the peak tracked only after ndv_holdoff_s;
    in that mode a "cv" report sustained for cutoff_debounce_s means the
    voltage ceiling was reached and finishes the charge. Output off and the
    time/Ah/Wh limits end any phase.
    """

    def __init__(
        self,
        profile: ChargeProfile,
        precharge_debounce_s: float = 2.0,
        cutoff_debounce_s: float = 5.0,
        ndv_window_s: float = 10.0,
    ) -> None:
        self.profile = profile
        self.phase = PHASE_PRECHARGE
        self.reason: Optional[str] = None
        self.ndv_window_s = ndv_window_s
        self._start_t: Optional[float] = None
        # Debounces "voltage above threshold" by negating both sides.
        self._precharge_debounce = BelowThresholdDebounce(precharge_debounce_s)
        self._cutoff_debounce = BelowThresholdDebounce(cutoff_debounce_s)
        self._ceiling_debounce = BelowThresholdDebounce(cutoff_debounce_s)
        self._ndv_samples: Deque[Tuple[float, float]] = deque()
        self._ndv_first_t: Optional[float] = None
        self._ndv_peak: Optional[float] = None

    @property
    def _has_precharge(self) -> bool:
        p = self.profile
        return p.precharge_below_v is not None and p.precharge_a is not None

    @property
    def _uses_ndv(self) -> bool:
        return self.profile.ndv_v is not None

    def start(self, t: float) -> ChargeSetpoint:
        self._start_t = t
        if self._has_precharge:
            self.phase = PHASE_PRECHARGE
            return ChargeSetpoint(self.profile.cv_v, self.profile.precharge_a)
        self.phase = PHASE_CC
        return ChargeSetpoint(self.profile.cv_v, self.profile.cc_a)

    def update(
        self,
        t: float,
        v: float,
        i: float,
        device_state: str,
        acc: CapacityAccumulator,
    ) -> ChargeAction:
        if self.phase == PHASE_DONE:
            return ChargeFinished(self.reason)
        if device_state == "off":
            return self._finish(REASON_OUTPUT_OFF)
        limit = self._check_limits(t, acc)
        if limit is not None:
            return self._finish(limit)

        p = self.profile
        if self._uses_ndv:
            return self._update_ndv(t, v, device_state)

        if self.phase == PHASE_PRECHARGE:
            if self._precharge_debounce.update(-v, -p.precharge_below_v, t):
                self.phase = PHASE_CC
                return ChargeSetpoint(p.cv_v, p.cc_a)
            return None

        if self.phase == PHASE_CC:
            if device_state == "cv":
                self.phase = PHASE_CV
            return None

        if self.phase == PHASE_CV:
            if p.cutoff_a is not None and self._cutoff_debounce.update(i, p.cutoff_a, t):
                if p.float_v is not None:
                    self.phase = PHASE_FLOAT
                    return ChargeSetpoint(p.float_v, p.cc_a)
                return self._finish(REASON_CUTOFF)
            return None

        return None

    def _update_ndv(self, t: float, v: float, device_state: str) -> ChargeAction:
        p = self.profile
        # 0.0 while the device reports "cv", so the debounce times that run.
        if self._ceiling_debounce.update(float(device_state != "cv"), 0.0, t):
            return self._finish(REASON_CEILING)

        if t - self._start_t < p.ndv_holdoff_s:
            return None
        if self._ndv_first_t is None:
            self._ndv_first_t = t
        self._ndv_samples.append((t, v))
        while t - self._ndv_samples[0][0] > self.ndv_window_s:
            self._ndv_samples.popleft()
        if t - self._ndv_first_t < self.ndv_window_s:
            return None
        avg = sum(s[1] for s in self._ndv_samples) / len(self._ndv_samples)
        if self._ndv_peak is None or avg > self._ndv_peak:
            self._ndv_peak = avg
            return None
        if avg <= self._ndv_peak - p.ndv_v:
            return self._finish(REASON_NDV)
        return None

    def _check_limits(self, t: float, acc: CapacityAccumulator) -> Optional[str]:
        p = self.profile
        if p.max_s is not None and t - self._start_t >= p.max_s:
            return REASON_TIMEOUT
        if p.max_ah is not None and acc.ah >= p.max_ah:
            return REASON_MAX_AH
        if p.max_wh is not None and acc.wh >= p.max_wh:
            return REASON_MAX_WH
        return None

    def _finish(self, reason: str) -> ChargeFinished:
        self.phase = PHASE_DONE
        self.reason = reason
        return ChargeFinished(reason)
