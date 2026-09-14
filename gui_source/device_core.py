import time
from threading import Lock, RLock
from typing import Dict, List, Optional

import numpy as np


class ChannelSpec:
    """Describes one plottable/recordable data channel.

    hide_above marks a sentinel value (e.g. the "open circuit" resistance
    reading) that must be excluded from plotting/stats even though it is a
    real, stored sample - matched by exact equality, not a value ceiling.
    """

    def __init__(
        self, key: str, label: str, unit: str, hide_above: Optional[float] = None
    ) -> None:
        self.key = key
        self.label = label
        self.unit = unit
        self.hide_above = hide_above


CSV_UNIT_OVERRIDES = {"Ω": "ohm"}


def csv_unit(unit: str) -> str:
    return CSV_UNIT_OVERRIDES.get(unit, unit)


class GraphCapture:
    """Whether the live graph is capturing, its shared time origin, and the
    optional auto-start/auto-stop crossing triggers that drive it.

    Replaces ConnectionManager.time_origin/reset_time_origin(), which
    existed only for graph alignment. state_callback runs on the
    serial/TCP worker thread, so every field here is protected by a lock --
    an RLock because start()/stop() are called both directly and from
    inside check_start()/check_stop(), which already hold it.
    """

    def __init__(self, on_auto_start=None, on_auto_stop=None) -> None:
        self._lock = RLock()
        self.running = False
        self.origin: Optional[float] = None
        self.device_id: Optional[str] = None
        self.start_mode = "off"
        self.start_threshold = 0.0
        self.stop_mode = "off"
        self.stop_threshold = 0.0
        # Watched device's last reading relative to the *active* trigger's
        # threshold: True (below), False (at or above), None (no baseline
        # yet -- the next reading only seeds it, it can't itself trigger).
        self._last_below: Optional[bool] = None
        self.on_auto_start = on_auto_start if on_auto_start is not None else lambda: None
        self.on_auto_stop = on_auto_stop if on_auto_stop is not None else lambda: None

    def start(self, t: float) -> None:
        with self._lock:
            self.running = True
            if self.origin is None:
                self.origin = t
            # The stop trigger measures against a different threshold, so
            # the start trigger's baseline can't be reused for it.
            self._last_below = None

    def stop(self) -> None:
        with self._lock:
            self.running = False
            self._last_below = None

    def clear(self, t: float) -> None:
        with self._lock:
            self.origin = t if self.running else None

    def set_triggers(
        self,
        device_id: Optional[str],
        start_mode: str,
        start_threshold: float,
        stop_mode: str,
        stop_threshold: float,
    ) -> None:
        with self._lock:
            self.device_id = device_id
            self.start_mode = start_mode
            self.start_threshold = start_threshold
            self.stop_mode = stop_mode
            self.stop_threshold = stop_threshold
            self._last_below = None

    def forget(self, device_id: str) -> None:
        """Drop the crossing baseline for `device_id` -- called on link and
        unlink so a device's history from before it was (or after it
        stops being) watched never contributes a stale crossing."""
        with self._lock:
            if device_id == self.device_id:
                self._last_below = None

    def _check_crossing(self, mode, threshold, voltages, currents, rising: bool) -> bool:
        """Walk `voltages` or `currents` (per `mode`) in order, looking for
        a below/at-or-above crossing in the direction `rising` says.
        Crossings inside a single batch count -- this doesn't just compare
        the batch's first and last readings."""
        series = voltages if mode == "voltage" else currents
        for reading in series:
            below = reading < threshold
            if self._last_below is None:
                # The first reading ever seen only seeds the baseline.
                self._last_below = below
                continue
            crossed = (
                self._last_below and not below
                if rising
                else not self._last_below and below
            )
            self._last_below = below
            if crossed:
                return True
        return False

    def check_start(self, device_id: str, voltages, currents, t: float) -> bool:
        with self._lock:
            if self.running or self.start_mode == "off" or device_id != self.device_id:
                return False
            if self._check_crossing(
                self.start_mode, self.start_threshold, voltages, currents, rising=True
            ):
                self.start(t)
                self.on_auto_start()
                return True
            return False

    def check_stop(self, device_id: str, voltages, currents) -> bool:
        with self._lock:
            if not self.running or self.stop_mode == "off" or device_id != self.device_id:
                return False
            if self._check_crossing(
                self.stop_mode, self.stop_threshold, voltages, currents, rising=False
            ):
                self.stop()
                self.on_auto_stop()
                return True
            return False


class DeviceDataStore:
    """Fixed-capacity per-channel sample history, used for the live graph.

    The backing arrays are a true circular buffer: `head` is the next
    physical write index and `update_count` is the number of valid samples
    (capped at data_length). append()/mark_gap() only ever write the new
    samples at `head` and advance it - no existing data is shifted, so their
    cost is O(len_) regardless of data_length. The physical layout is
    unrolled into oldest-to-newest order on read instead (see ordered()),
    which is where the O(data_length) cost now lives - paid on the
    frame-rate-bounded render/export path rather than the
    sample-rate-bounded write path.
    """

    def __init__(
        self,
        channels: List[ChannelSpec],
        data_length: int,
        capture: "GraphCapture",
        open_r: float = 1e7,
    ) -> None:
        self.channels = channels
        self.channel_keys = [c.key for c in channels]
        self._spec_by_key = {c.key: c for c in channels}
        self.data_length = data_length
        self.capture = capture
        self.open_r = open_r
        self.sync_lock = Lock()
        self.voltage_tmp: List[float] = []
        self.current_tmp: List[float] = []
        self.energy = 0.0
        self.update_count = 0
        self.head = 0
        t = time.perf_counter()
        self.eng_start_time = t
        self.last_time = 0.0
        self.times = np.zeros(data_length, np.float64)
        self.series: Dict[str, np.ndarray] = {
            c.key: np.zeros(data_length, np.float64) for c in channels
        }
        # Each channel's most recent value, kept even while capture is
        # stopped and the ring buffer isn't being written -- panel logic
        # (stable-state checks, sweep, keep-power, battery-sim) reads this
        # through last() regardless of whether anything is being graphed.
        self._latest: Dict[str, float] = {c.key: 0.0 for c in channels}

    def _write(self, arr: np.ndarray, values) -> None:
        """Write `values` into `arr` starting at `head`, wrapping around the
        end of the buffer as needed. Does not advance `head` - callers write
        every array for a batch at the same `head` before calling _advance()
        once, so they all land at matching indices."""
        n = len(values)
        if n >= self.data_length:
            arr[:] = values[-self.data_length :]
            return
        end = self.head + n
        if end <= self.data_length:
            arr[self.head : end] = values
        else:
            first = self.data_length - self.head
            arr[self.head :] = values[:first]
            arr[: end - self.data_length] = values[first:]

    def _advance(self, n: int) -> None:
        self.head = 0 if n >= self.data_length else (self.head + n) % self.data_length
        self.update_count = min(self.update_count + n, self.data_length)

    def ordered(self, arr: np.ndarray) -> np.ndarray:
        """Return `arr`'s valid samples in oldest-to-newest order. Caller
        must hold sync_lock. A view (no copy) while the buffer hasn't
        wrapped yet; a copy once it has, since the data is then split across
        the physical end of the array."""
        n = self.update_count
        if n < self.data_length:
            return arr[:n]
        return np.concatenate((arr[self.head :], arr[: self.head]))

    def last(self, key: str) -> float:
        """Most recently appended value for `key`, whether or not capture is
        running -- undefined before the first sample lands."""
        return self._latest[key]

    def mark_gap(self) -> None:
        """Insert a broken-line marker at the current head, e.g. on a
        capture stop, so a later resume doesn't draw a straight line across
        the time nothing was recorded - relies on the curve being plotted
        with connect="finite"."""
        with self.sync_lock:
            origin = self.capture.origin
            if self.update_count == 0 or origin is None:
                return
            self._write(self.times, [time.perf_counter() - origin])
            for k in self.channel_keys:
                self._write(self.series[k], [np.nan])
            self._advance(1)

    def clear(self) -> None:
        """Empty the buffer. The time axis rebases itself on capture.origin,
        which CLEAR resets separately (GraphCapture.clear)."""
        with self.sync_lock:
            self.times = np.zeros(self.data_length, np.float64)
            self.series = {
                k: np.zeros(self.data_length, np.float64) for k in self.channel_keys
            }
            self.update_count = 0
            self.head = 0
            self.last_time = time.perf_counter()

    def append(self, raw_samples, values_by_channel, len_: int, t1: float) -> float:
        """Store one batch of samples.

        raw_samples: list of (voltage, current) full-rate samples, appended to
        voltage_tmp/current_tmp for LCD averaging - independent of avg-mode
        reduction, which the caller applies before building values_by_channel.
        values_by_channel: dict[key] -> sequence of len_ values to store in
        the channel series, already reduced to the target sample rate.
        Returns the energy (Wh-equivalent, integrated in caller's time units)
        added by this batch.
        """
        with self.sync_lock:
            for v, i in raw_samples:
                self.voltage_tmp.append(v)
                self.current_tmp.append(i)
            dt = t1 - self.last_time
            self.last_time = t1
            eng = 0.0
            energy_samples = None
            if "power" in values_by_channel:
                per_sample_eng = np.asarray(values_by_channel["power"], np.float64) * (
                    dt / len_
                )
                eng = float(np.sum(per_sample_eng))
                if "energy" in self.channel_keys:
                    energy_samples = self.energy + np.cumsum(per_sample_eng)
                self.energy += eng
            for k in self.channel_keys:
                # energy is derived from power+dt here rather than supplied by
                # the caller, since it needs the running total (self.energy)
                # this store already tracks for the LCD/eng_start_time reset.
                values = energy_samples if k == "energy" else values_by_channel[k]
                self._latest[k] = values[-1]
            if self.capture.running:
                origin = self.capture.origin
                t = t1 - origin
                times_batch = np.fromiter(
                    (t - dt + (dt / len_) * (idx + 1) for idx in range(len_)),
                    np.float64,
                    count=len_,
                )
                self._write(self.times, times_batch)
                for k in self.channel_keys:
                    values = energy_samples if k == "energy" else values_by_channel[k]
                    self._write(self.series[k], values)
                self._advance(len_)
            return eng

    def window(self, t_lo: float, t_hi: float) -> tuple[int, int]:
        """(start, stop) indices into the ordered() samples whose times fall
        in [t_lo, t_hi]. Caller must hold sync_lock. Relies on sample times
        being non-decreasing between clears."""
        times = self.ordered(self.times)
        return (
            int(np.searchsorted(times, t_lo, side="left")),
            int(np.searchsorted(times, t_hi, side="right")),
        )

    def get_series(
        self, key: Optional[str], start: int, stop: int, x_key: Optional[str] = None
    ):
        """(xs, ys, max, min, mean) for `key` over ordered samples
        [start, stop), plotted against time or, given x_key, against that
        series. Samples equal to the channel's hide_above sentinel are
        dropped from both axes. Caller must hold sync_lock."""
        if key is None or key not in self.series:
            return None, None, None, None, None
        if x_key is not None and x_key not in self.series:
            return None, None, None, None, None
        spec = self._spec_by_key[key]
        data = self.ordered(self.series[key])[start:stop]
        xs = self.ordered(self.times if x_key is None else self.series[x_key])[start:stop]
        if spec.hide_above is not None:
            keep = data != spec.hide_above
            data = data[keep]
            xs = xs[keep]
        if data.size == 0:
            return None, None, None, None, None
        return xs, data, np.nanmax(data), np.nanmin(data), np.nanmean(data)


class RecordData:
    def __init__(self, channels: List[ChannelSpec]) -> None:
        self.channels = list(channels)
        self.series: Dict[str, List[float]] = {c.key: [] for c in self.channels}
        self.times: List[float] = []
        self.start_time = 0
        self.last_time = 0

    def add_values(self, values: Dict[str, float], t: float) -> None:
        for c in self.channels:
            self.series[c.key].append(values[c.key])
        self.times.append(t)

    def to_csv(self, filename: str) -> None:
        cols = [self.times] + [self.series[c.key] for c in self.channels]
        header = "time/s," + ",".join(
            f"{c.key}/{csv_unit(c.unit)}" for c in self.channels
        )
        data = np.array(cols).T
        np.savetxt(filename, data, delimiter=",", fmt="%f", header=header, comments="")
