import time
from typing import List, Tuple

import numpy as np
from PyQt5 import QtCore, QtWidgets

from app_context import DEBUG, set_color
from device_core import ChannelSpec, DeviceDataStore, GraphCapture, RecordData
from settings_model import setting

DEVICE_PANEL_TYPES = {}

OPEN_R = 1e7

# The channels every device type has. A panel module either uses this list
# as-is (P906) or extends it with its own device-specific channels (L1060's
# discharge/sweep totals) -- see each module's CHANNELS.
BASE_CHANNELS = [
    ChannelSpec("voltage", QtCore.QCoreApplication.translate("MDPMainwindow", "电压"), "V"),
    ChannelSpec("current", QtCore.QCoreApplication.translate("MDPMainwindow", "电流"), "A"),
    ChannelSpec("power", QtCore.QCoreApplication.translate("MDPMainwindow", "功率"), "W"),
    ChannelSpec(
        "resistance",
        QtCore.QCoreApplication.translate("MDPMainwindow", "阻值"),
        "Ω",
        hide_above=OPEN_R,
    ),
    ChannelSpec("energy", QtCore.QCoreApplication.translate("MDPMainwindow", "能量"), "J"),
    ChannelSpec(
        "temperature", QtCore.QCoreApplication.translate("MDPMainwindow", "温度"), "°F"
    ),
]
BASE_CHANNEL_SHORT = {
    "voltage": "V",
    "current": "I",
    "power": "P",
    "resistance": "R",
    "energy": "E",
    "temperature": "T",
}
_BASE_CHANNEL_BY_KEY = {c.key: c for c in BASE_CHANNELS}
RECORD_CHANNELS = [
    _BASE_CHANNEL_BY_KEY["voltage"],
    _BASE_CHANNEL_BY_KEY["current"],
]


class DevicePanelBase(QtWidgets.QWidget):
    values_signal = QtCore.pyqtSignal(float, float, float)
    display_data_signal = QtCore.pyqtSignal(list, list, str, str, str, str, str, bool)
    highlight_point_signal = QtCore.pyqtSignal(float, float)
    link_state_changed = QtCore.pyqtSignal()
    link_toggle_requested = QtCore.pyqtSignal()

    # Set by each subclass: the mdp_controller device class link() builds,
    # and the channel list start_record() records.
    api_class = None
    record_channels: List[ChannelSpec] = []

    def __init__(
        self,
        device_id: str,
        display_name: str,
        channels: List[ChannelSpec],
        data_length: int,
        capture: GraphCapture,
        open_r: float = OPEN_R,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self.display_name = display_name
        self.channels = channels
        self.open_r = open_r
        self.capture = capture
        self.store = DeviceDataStore(channels, data_length, capture, open_r=open_r)
        self.linked = False
        self.record_flag = False
        self.record_data = None

    def set_capture(self, capture: GraphCapture) -> None:
        """Bind this panel and its store to a different GraphCapture. The
        store's swap is taken under sync_lock because append() reads
        capture.running and capture.origin together under that lock on the
        worker thread."""
        self.capture = capture
        with self.store.sync_lock:
            self.store.capture = capture

    ##########  Tab navigation  ##########

    @QtCore.pyqtSlot(int)
    def on_tabWidget_currentChanged(self, index):
        self.ui.labelTab.setText(self.ui.tabWidget.tabText(index))
        if index == 0:
            self.ui.pushButtonLastTab.setEnabled(False)
        elif index == self.ui.tabWidget.count() - 1:
            self.ui.pushButtonNextTab.setEnabled(False)
        else:
            self.ui.pushButtonLastTab.setEnabled(True)
            self.ui.pushButtonNextTab.setEnabled(True)

    @QtCore.pyqtSlot()
    def on_pushButtonLastTab_clicked(self):
        idx = self.ui.tabWidget.currentIndex()
        if idx > 0:
            self.ui.tabWidget.setCurrentIndex(idx - 1)

    @QtCore.pyqtSlot()
    def on_pushButtonNextTab_clicked(self):
        idx = self.ui.tabWidget.currentIndex()
        if idx < self.ui.tabWidget.count() - 1:
            self.ui.tabWidget.setCurrentIndex(idx + 1)

    ##########  Link / unlink  ##########

    @QtCore.pyqtSlot()
    def on_btnLink_clicked(self):
        self.link_toggle_requested.emit()

    def link(self, bus, pipe: int, fps: float):
        if not self.settings.idcode:
            raise ValueError(
                QtCore.QCoreApplication.translate(
                    "DevicePanelBase", "IDCODE为空, 请先完成连接设置"
                )
            )
        color_rgb = bytes.fromhex(self.settings.color.lstrip("#"))
        api = self.api_class(
            bus,
            idcode=self.settings.idcode,
            blink=self.settings.blink,
            led_color=(color_rgb[0], color_rgb[1], color_rgb[2]),
            m01_channel=int(self.settings.m01ch[3]),
            com_timeout=bus.com_timeout,
            debug=DEBUG,
        )
        try:
            bus.attach(api, pipe)
            # Both device types are radio-deaf for ~3-4.5s after power-on, so
            # this has to outlast that window rather than fail fast.
            api.connect(timeout=8.0)
        except Exception:
            api.close()
            raise
        self.api = api
        self.api.register_realtime_value_callback(self.state_callback)
        t = time.perf_counter()
        self.capture.forget(self.device_id)
        self.store.eng_start_time = t
        self.store.last_time = t
        self.store.energy = 0
        self.data_fps = fps
        self.fps_counter.clear()
        self._on_link_reset(t)
        self.update_state_timer.start(100)
        self.state_request_sender_timer.start(round(1000 / fps))
        self.state_lcd_timer.start(round(1000 / min(fps, setting.ui.state_fps)))
        self._start_device_timers()
        self.linked = True
        self.update_state()
        self.open_state_ui()

    def _on_link_reset(self, t):
        """Device-specific per-link state resets, alongside the shared store
        reset above."""

    def _start_device_timers(self):
        """Start any device-specific polling timers, after the three shared
        ones and before the panel is marked linked."""

    def unlink(self) -> None:
        raise NotImplementedError

    def refresh_led_color(self):
        if self.api is None:
            return
        color_rgb = bytes.fromhex(self.settings.color.lstrip("#"))
        self.api.set_led_color((color_rgb[0], color_rgb[1], color_rgb[2]))

    def request_state(self):
        if self.api is not None:
            self.api.request_realtime_value()

    def update_state(self) -> None:
        raise NotImplementedError

    ##########  Recording  ##########

    def start_record(self):
        self.record_data = RecordData(self.record_channels)
        self.record_flag = True

    def stop_record(self):
        self.record_flag = False
        data = self.record_data
        self.record_data = None
        return data

    ##########  Realtime data  ##########

    def state_callback(self, rtvalues: List[Tuple[float, float]]):
        rtvalues = self._preprocess_rtvalues(rtvalues)
        len_ = len(rtvalues)
        t1 = time.perf_counter()
        raw_rtvalues = rtvalues
        if self.record_flag:
            rd = self.record_data
            if rd.start_time == 0:
                rd.start_time = t1
                rd.last_time = t1
            else:
                t = t1 - rd.start_time
                dt = t1 - rd.last_time
                rd.last_time = t1
                for idx, (v, i) in enumerate(raw_rtvalues):
                    rd.add_values(
                        {"voltage": v, "current": i}, t - dt + (dt / len_) * (idx + 1)
                    )
        self._on_raw_batch(raw_rtvalues, t1)
        if self.linked:
            # Checked on the raw, pre-avgmode batch and before store.append()
            # so a crossing here starts capture in time for this same batch
            # to land in the graph. Guarded on self.linked so the synthetic
            # (0, 0) sample close_state_ui(record_disconnect=True) injects
            # after unlink() can't itself act as a trigger.
            self.capture.check_start(
                self.device_id,
                [v for v, i in raw_rtvalues],
                [i for v, i in raw_rtvalues],
                t1,
            )
        if len(rtvalues) == 9:
            if self.settings.avgmode == 1:
                rtvalues = np.array(rtvalues)
                rtvalues = np.reshape(rtvalues, [3, 3, 2])
                rtvalues = np.mean(rtvalues, axis=(1))
                len_ = 3
            elif self.settings.avgmode == 2:
                rtvalues = np.array(rtvalues)
                rtvalues = np.reshape(rtvalues, [1, 9, 2])
                rtvalues = np.mean(rtvalues, axis=(1))
                len_ = 1
        voltages = np.array([v for v, i in rtvalues], dtype=np.float64)
        currents = np.array([i for v, i in rtvalues], dtype=np.float64)
        values = {
            "voltage": voltages,
            "current": currents,
            "power": voltages * currents,
            "resistance": np.where(currents != 0, voltages / currents, self.open_r),
            "temperature": np.full(len_, self._temp_f),
        }
        values.update(self._extra_channel_values(len_))
        eng = self.store.append(raw_rtvalues, values, len_, t1)
        if self.linked:
            # After store.append() so the crossing sample itself is already
            # in the graph by the time capture stops.
            self.capture.check_stop(self.device_id, voltages, currents)
        self._on_samples_appended(eng)
        self.fps_counter.tick()

    def _preprocess_rtvalues(self, rtvalues):
        """Adjust the incoming (voltage, current) samples before anything
        else sees them (e.g. calibration). Must preserve length."""
        return rtvalues

    def _on_raw_batch(self, raw_rtvalues, t1):
        """Hook for device-specific integration over the raw, pre-averaging
        batch -- runs after the record block, before the avgmode reshape."""

    def _extra_channel_values(self, len_):
        """Per-sample arrays for any channels beyond BASE_CHANNELS that this
        device type declares."""
        return {}

    def _on_samples_appended(self, eng):
        """Hook receiving the joules the store just accumulated."""

    ##########  LCD readouts  ##########

    def _update_common_lcds(self):
        """Refresh the five shared LCDs from the pending sample window.
        Returns the (voltage, current, power) averages just displayed, or
        None if there was no data -- device-specific tails are gated on
        that same availability."""
        store = self.store
        if len(store.voltage_tmp) == 0 or len(store.current_tmp) == 0:
            return None
        with store.sync_lock:
            vavg = sum(store.voltage_tmp) / len(store.voltage_tmp)
            iavg = sum(store.current_tmp) / len(store.current_tmp)
            store.voltage_tmp.clear()
            store.current_tmp.clear()
            self.ui.lcdEnerge.display(f"{store.energy:.{3+setting.ui.interp}f}")
        power = vavg * iavg
        if iavg >= 0.002:  # 致敬P906的愚蠢adc
            resistance = vavg / iavg
        else:
            resistance = self.open_r
        r_text = f"{resistance:.2f}" if resistance < self.open_r / 100 else "--"
        self.ui.lcdVoltage.display(f"{vavg:.3f}")
        self.ui.lcdCurrent.display(f"{iavg:.3f}")
        self.ui.lcdResistance.display(r_text)
        self.ui.lcdPower.display(f"{power:.3f}")
        self.values_signal.emit(vavg, iavg, power)
        return vavg, iavg, power

    def set_interp(self, interp):
        self.ui.lcdVoltage.setDigitCount(6)
        self.ui.lcdCurrent.setDigitCount(6)
        self.ui.lcdResistance.setDigitCount(8)
        self.ui.lcdPower.setDigitCount(6)
        self.ui.lcdEnerge.setDigitCount(6 + interp)

    def apply_theme(self):
        set_color(self.ui.lcdVoltage, setting.get_color("lcd_voltage"))
        set_color(self.ui.lcdCurrent, setting.get_color("lcd_current"))
        set_color(self.ui.lcdPower, setting.get_color("lcd_power"))
        set_color(self.ui.lcdEnerge, setting.get_color("lcd_energy"))
        set_color(self.ui.labelTemperature, setting.get_color("lcd_temperature"))
        set_color(self.ui.lcdResistance, setting.get_color("lcd_resistance"))
        self._enforce_lcd_min_width()

    def _enforce_lcd_min_width(self):
        for lcd in (
            self.ui.lcdVoltage,
            self.ui.lcdCurrent,
            self.ui.lcdPower,
            self.ui.lcdEnerge,
            self.ui.lcdResistance,
        ):
            lcd.setMinimumWidth(130)

    def set_fills_column(self, fill: bool):
        """fill=True when this panel is the only one shown in the device
        column: the aux tab widget takes all extra height so every other
        section keeps its natural size."""
        policy = self.ui.tabWidget.sizePolicy()
        policy.setVerticalPolicy(
            QtWidgets.QSizePolicy.Expanding if fill else QtWidgets.QSizePolicy.Preferred
        )
        self.ui.tabWidget.setSizePolicy(policy)

    def set_data_fps(self, fps: float):
        self.data_fps = fps
        if self.state_request_sender_timer.isActive():
            self.state_request_sender_timer.stop()
            self.state_request_sender_timer.start(round(1000 / fps))
        if self.state_lcd_timer.isActive():
            self.state_lcd_timer.stop()
            self.state_lcd_timer.start(round(1000 / min(fps, setting.ui.state_fps)))
        self.fps_counter.clear()

    def set_english_fonts(self):
        """Re-font any widget whose Chinese-width layout doesn't fit its
        English label. No-op for panels whose labels already fit."""

    ##########  Connected/disconnected UI  ##########

    def open_state_ui(self):
        self.ui.labelLinkState.setText(
            QtCore.QCoreApplication.translate("DevicePanelBase", "已连接")
        )
        set_color(self.ui.labelLinkState, setting.get_color("general_green"))
        self.ui.frameOutputSetting.setEnabled(True)
        self.ui.frameSystemState.setEnabled(True)

    def close_state_ui(self, record_disconnect: bool = False):
        self.ui.labelLinkState.setText(
            QtCore.QCoreApplication.translate("DevicePanelBase", "未连接")
        )
        set_color(self.ui.labelLinkState, None)
        self.ui.frameOutputSetting.setEnabled(False)
        self.ui.frameSystemState.setEnabled(False)
        # Must run before the record_disconnect sample below: that (0, 0)
        # sample is what repaints device widgets (e.g. P906's progress bars)
        # after they're reset here.
        self._close_state_ui_device(record_disconnect)
        if record_disconnect:
            # Marks the graph history with an explicit drop to (0, 0) right
            # before unlink()'s mark_gap() breaks the line, so a reviewed
            # chart shows the device's output actually falling away instead
            # of flat-lining at its last real reading. Not wanted here on
            # the plain init call - there's no real reading to mark as lost
            # yet, and it would otherwise permanently seed this panel's
            # buffer with one sample even if it's never linked all session.
            self.state_callback([(0.0, 0.0)])
        for lcd in (
            self.ui.lcdVoltage,
            self.ui.lcdCurrent,
            self.ui.lcdPower,
            self.ui.lcdEnerge,
            self.ui.lcdResistance,
        ):
            lcd.display("")

    def _close_state_ui_device(self, record_disconnect: bool):
        """Reset the device-specific widgets of the system-state area."""

    ##########  Auxiliary-workflow results  ##########

    def has_discharge_data(self) -> bool:
        """Whether this panel is currently running, or still holds the
        results of, a discharge workflow -- used to reveal the Ah/Wh and
        Discharge Curve graph channels (which only apply to device types
        supporting the discharge workflow, currently only L1060) once
        they'd actually show something, and to hide them again once
        clear_aux_data() drops that result (and no run is active)."""
        return False

    def has_charge_data(self) -> bool:
        """Whether this panel is currently running, or still holds the
        results of, a battery charge workflow -- used to reveal the Ah/Wh
        and Charge Curve graph channels (which only apply to device types
        supporting the charge workflow, currently only P906) once they'd
        actually show something, and to hide them again once
        clear_aux_data() drops that result (and no run is active)."""
        return False

    def has_sweep_data(self) -> bool:
        """Whether this panel is currently running, or still holds the
        results of, a sweep workflow -- used to reveal the Sweep graph
        channel (which only applies to device types supporting the sweep
        workflow, currently only L1060) once it'd actually show something,
        and to hide it again once clear_aux_data() drops that result (and
        no run is active)."""
        return False

    def clear_aux_data(self) -> None:
        """Drop any held discharge/charge/sweep results so the matching
        has_*_data() goes back to False, unless that workflow is actively
        running right now. Called by the Clear-buffer action alongside
        store.clear() -- a no-op for device types that don't support any
        of these workflows."""
