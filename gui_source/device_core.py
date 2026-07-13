import time
from threading import Lock
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


class DeviceDataStore:
    def __init__(
        self, channels: List[ChannelSpec], data_length: int, open_r: float = 1e7
    ) -> None:
        self.channels = channels
        self.channel_keys = [c.key for c in channels]
        self._spec_by_key = {c.key: c for c in channels}
        self.data_length = data_length
        self.open_r = open_r
        self.sync_lock = Lock()
        self.voltage_tmp: List[float] = []
        self.current_tmp: List[float] = []
        self.energy = 0.0
        self.update_count = 0
        t = time.perf_counter()
        self.start_time = t
        self.eng_start_time = t
        self.last_time = 0.0
        self.times = np.zeros(data_length, np.float64)
        self.series: Dict[str, np.ndarray] = {
            c.key: np.zeros(data_length, np.float64) for c in channels
        }

    def mark_gap(self) -> None:
        """Insert a broken-line marker at the current head, e.g. on unlink,
        so a later resume doesn't draw a straight line across the time
        nothing was recorded - relies on the curve being plotted with
        connect="finite"."""
        with self.sync_lock:
            if self.update_count == 0:
                return
            if self.update_count >= self.data_length:
                self.times = np.roll(self.times, -1)
                for k in self.channel_keys:
                    self.series[k] = np.roll(self.series[k], -1)
                self.update_count -= 1
            idx = self.update_count
            self.times[idx] = time.perf_counter() - self.start_time
            for k in self.channel_keys:
                self.series[k][idx] = np.nan
            self.update_count += 1

    def clear(self) -> None:
        with self.sync_lock:
            self.times = np.zeros(self.data_length, np.float64)
            self.series = {
                k: np.zeros(self.data_length, np.float64) for k in self.channel_keys
            }
            self.update_count = 0
            t = time.perf_counter()
            self.start_time = t
            self.last_time = t

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
            t = t1 - self.start_time
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
            if self.update_count + len_ > self.data_length:
                offset = self.update_count + len_ - self.data_length
                self.times = np.roll(self.times, -offset)
                for k in self.channel_keys:
                    self.series[k] = np.roll(self.series[k], -offset)
                self.update_count -= offset
            for idx in range(len_):
                self.times[self.update_count + idx] = t - dt + (dt / len_) * (idx + 1)
            for k in self.channel_keys:
                # energy is derived from power+dt here rather than supplied by
                # the caller, since it needs the running total (self.energy)
                # this store already tracks for the LCD/eng_start_time reset.
                values = energy_samples if k == "energy" else values_by_channel[k]
                self.series[k][self.update_count : self.update_count + len_] = values
            self.update_count += len_
            return eng

    def get_series(self, key: Optional[str], display_pts: int, r_offset: int = 0):
        if key is None or key not in self.series:
            return None, None, None, None, None, None, None
        spec = self._spec_by_key[key]
        data = self.series[key][: self.update_count]
        time_ = self.times[: self.update_count]
        if spec.hide_above is not None:
            indexs = np.where(data != spec.hide_above)[0]
            data = data[indexs]
            time_ = time_[indexs]
        if data.size == 0:
            return None, None, None, None, None, None, None
        start_index = max(0, len(data) - display_pts - r_offset)
        to_index = len(data) - r_offset
        eval_data = data[start_index:to_index]
        return (
            data,
            time_,
            start_index,
            to_index,
            np.nanmax(eval_data),
            np.nanmin(eval_data),
            np.nanmean(eval_data),
        )


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
