import time
from typing import List, Tuple

import numpy as np
from PyQt5 import QtCore, QtWidgets

from app_context import DEBUG, FPSCounter, set_color
from device_core import ChannelSpec, RecordData
from device_panel import DEVICE_PANEL_TYPES, DevicePanelBase
from mdp_controller import MDP_L1060
from mdp_gui_template import Ui_DevicePanelL1060
from settings_model import setting

OPEN_R = 1e7

CHANNELS = [
    ChannelSpec("voltage", QtCore.QCoreApplication.translate("MDPMainwindow", "电压"), "V"),
    ChannelSpec("current", QtCore.QCoreApplication.translate("MDPMainwindow", "电流"), "A"),
    ChannelSpec("power", QtCore.QCoreApplication.translate("MDPMainwindow", "功率"), "W"),
    ChannelSpec(
        "resistance",
        QtCore.QCoreApplication.translate("MDPMainwindow", "阻值"),
        "Ω",
        hide_above=OPEN_R,
    ),
]
CHANNEL_BY_KEY = {c.key: c for c in CHANNELS}
CHANNEL_SHORT = {"voltage": "V", "current": "I", "power": "P", "resistance": "R"}
RECORD_CHANNELS = [CHANNEL_BY_KEY["voltage"], CHANNEL_BY_KEY["current"]]

_TARGET_SPINBOX = {
    "CC": "spinBoxTargetCC",
    "CV": "spinBoxTargetCV",
    "CR": "spinBoxTargetCR",
    "CP": "spinBoxTargetCP",
}
_PROTECTION_LABELS = {
    "OVP": "LATCHED: OVP",
    "OCP": "LATCHED: OCP",
    "OPP_OR_UVP": "LATCHED: OPP or UVP (indistinguishable)",
}


class L1060DevicePanel(DevicePanelBase):
    def __init__(self, parent=None, device_settings=None):
        device_settings = device_settings or setting.devices[0]
        super().__init__(
            device_id=device_settings.id,
            display_name=device_settings.name,
            channels=CHANNELS,
            data_length=setting.ui.data_pts,
            open_r=OPEN_R,
            parent=parent,
        )
        self.settings = device_settings
        self.ui = Ui_DevicePanelL1060()
        self.ui.setupUi(self)
        self.ui.labelDeviceName.setText(self.display_name)

        self.api = None
        self.data_fps = 50
        self.model = "L1060"
        self.open_r = OPEN_R
        self.fps_counter = FPSCounter()
        self._updating_ui = False

        self._init_timers()
        self._init_signals()

        self.ui.comboBoxMode.setCurrentText(self.settings.l1060_mode)
        for mode, target in self.settings.l1060_targets.items():
            getattr(self.ui, _TARGET_SPINBOX[mode]).setValue(target)
        self._highlight_active_target(self.settings.l1060_mode)

        self.close_state_ui()

    def set_english_fonts(self):
        pass

    def _init_timers(self):
        self.state_request_sender_timer = QtCore.QTimer(self)
        self.state_request_sender_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.state_request_sender_timer.timeout.connect(self.request_state)
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.timeout.connect(self.update_state)
        self.target_poll_timer = QtCore.QTimer(self)
        self.target_poll_timer.timeout.connect(self.request_targets)
        self.state_lcd_timer = QtCore.QTimer(self)
        self.state_lcd_timer.timeout.connect(self.update_state_lcd)

    def _init_signals(self):
        self.ui.comboBoxMode.currentTextChanged.connect(self.on_mode_changed)
        self.ui.spinBoxTargetCC.editingFinished.connect(lambda: self.on_target_changed("CC"))
        self.ui.spinBoxTargetCV.editingFinished.connect(lambda: self.on_target_changed("CV"))
        self.ui.spinBoxTargetCR.editingFinished.connect(lambda: self.on_target_changed("CR"))
        self.ui.spinBoxTargetCP.editingFinished.connect(lambda: self.on_target_changed("CP"))
        self.ui.btnLoadOn.toggled.connect(self.on_load_on_toggled)

    @QtCore.pyqtSlot()
    def on_btnLink_clicked(self):
        self.link_toggle_requested.emit()

    def _highlight_active_target(self, mode: str):
        for m, name in _TARGET_SPINBOX.items():
            getattr(self.ui, name).setEnabled(m == mode)

    @QtCore.pyqtSlot(str)
    def on_mode_changed(self, mode: str):
        self._highlight_active_target(mode)
        if self._updating_ui:
            return
        self.settings.l1060_mode = mode
        if self.api is not None:
            self.api.select_mode(mode)

    def on_target_changed(self, mode: str):
        spin = getattr(self.ui, _TARGET_SPINBOX[mode])
        value = spin.value()
        self.settings.l1060_targets[mode] = value
        if self.api is None:
            return
        if mode == "CC":
            self.api.set_current(value)
        elif mode == "CV":
            self.api.set_voltage(value)
        elif mode == "CR":
            self.api.set_resistance(value)
        elif mode == "CP":
            self.api.set_power(value)

    @QtCore.pyqtSlot(bool)
    def on_load_on_toggled(self, checked: bool):
        if self.api is None:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)
            return
        ok = self.api.set_load_on(checked)
        if checked and not ok:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)

    def link(self, bus, pipe: int = 0, fps: float = 50):
        if not self.settings.idcode:
            raise ValueError(self.tr("IDCODE为空, 请先完成连接设置"))
        color_rgb = bytes.fromhex(self.settings.color.lstrip("#"))
        api = MDP_L1060(
            bus,
            idcode=self.settings.idcode,
            blink=self.settings.blink,
            led_color=(color_rgb[0], color_rgb[1], color_rgb[2]),
            m01_channel=int(self.settings.m01ch[3]),
            debug=DEBUG,
        )
        try:
            bus.attach(api, pipe)
            api.connect(retry_times=2)
        except Exception:
            api.close()
            raise
        self.api = api
        self.api.register_realtime_value_callback(self.state_callback)
        t = time.perf_counter()
        self.store.start_time = t
        self.store.eng_start_time = t
        self.store.last_time = t
        self.store.energy = 0
        self.data_fps = fps
        self.fps_counter.clear()
        self.status_timer.start(100)
        self.state_request_sender_timer.start(round(1000 / fps))
        self.state_lcd_timer.start(round(1000 / min(fps, setting.ui.state_fps)))
        self.target_poll_timer.start(400)
        self.linked = True
        self.update_state()
        self.open_state_ui()

    def unlink(self):
        self.state_request_sender_timer.stop()
        self.status_timer.stop()
        self.state_lcd_timer.stop()
        self.target_poll_timer.stop()
        api = self.api
        self.api = None
        api.close()
        self.linked = False
        self.close_state_ui()
        self.link_state_changed.emit()

    def request_state(self):
        if self.api is not None:
            self.api.request_realtime_value()

    def request_targets(self):
        if self.api is not None:
            self.api.request_target_page()

    def start_record(self):
        self.record_data = RecordData(RECORD_CHANNELS)
        self.record_flag = True

    def stop_record(self):
        self.record_flag = False
        data = self.record_data
        self.record_data = None
        return data

    def state_callback(self, rtvalues: List[Tuple[float, float]]):
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
        }
        self.store.append(raw_rtvalues, values, len_, t1)
        self.fps_counter.tick()

    def update_state(self):
        if self.api is None:
            return
        (
            LoadMode,
            LoadActive,
            _LoadEnabled,
            Temperature,
            _InputVoltage,
            _Voltage,
            _Current,
            _ErrFlag,
            Protection,
            ProtectionLatched,
        ) = self.api.get_status()
        if LoadMode != self.settings.l1060_mode and not self.ui.comboBoxMode.hasFocus():
            self._updating_ui = True
            self.ui.comboBoxMode.setCurrentText(LoadMode)
            self._updating_ui = False
            self.settings.l1060_mode = LoadMode
        self.ui.labelTemperature.setText(f"{Temperature:.1f}°C")
        self.ui.btnLoadOn.blockSignals(True)
        self.ui.btnLoadOn.setChecked(bool(LoadActive))
        self.ui.btnLoadOn.blockSignals(False)
        if ProtectionLatched:
            self.ui.labelProtection.setText(_PROTECTION_LABELS.get(Protection, "LATCHED"))
            self.ui.labelProtection.setToolTip(
                self.tr("需要在设备上物理按下 Run 按钮才能解除保护锁存")
            )
            set_color(self.ui.labelProtection, setting.get_color("general_red"))
            self.ui.btnLoadOn.setEnabled(False)
        else:
            self.ui.labelProtection.setText("")
            self.ui.labelProtection.setToolTip("")
            self.ui.btnLoadOn.setEnabled(True)

    def update_state_lcd(self):
        store = self.store
        if len(store.voltage_tmp) == 0 or len(store.current_tmp) == 0:
            return
        with store.sync_lock:
            vavg = sum(store.voltage_tmp) / len(store.voltage_tmp)
            iavg = sum(store.current_tmp) / len(store.current_tmp)
            store.voltage_tmp.clear()
            store.current_tmp.clear()
        power = vavg * iavg
        if iavg >= 0.002:
            resistance = vavg / iavg
        else:
            resistance = self.open_r
        r_text = f"{resistance:.2f}" if resistance < self.open_r / 100 else "--"
        self.ui.lcdVoltage.display(f"{vavg:.3f}")
        self.ui.lcdCurrent.display(f"{iavg:.3f}")
        self.ui.lcdPower.display(f"{power:.3f}")
        self.ui.lcdResistance.display(r_text)
        self.values_signal.emit(vavg, iavg, power)
        if self.api is not None:
            mode = self.settings.l1060_mode
            targets = self.api.get_targets()
            if mode in targets:
                self.settings.l1060_targets[mode] = targets[mode]
                spin = getattr(self.ui, _TARGET_SPINBOX[mode])
                if not spin.hasFocus():
                    spin.blockSignals(True)
                    spin.setValue(targets[mode])
                    spin.blockSignals(False)

    def set_data_fps(self, fps: float):
        self.data_fps = fps
        if self.state_request_sender_timer.isActive():
            self.state_request_sender_timer.stop()
            self.state_request_sender_timer.start(round(1000 / fps))
        if self.state_lcd_timer.isActive():
            self.state_lcd_timer.stop()
            self.state_lcd_timer.start(round(1000 / min(fps, setting.ui.state_fps)))
        self.fps_counter.clear()

    def close_state_ui(self):
        self.ui.labelLinkState.setText(self.tr("未连接"))
        set_color(self.ui.labelLinkState, None)
        self.ui.frameOutputSetting.setEnabled(False)
        self.ui.frameSystemState.setEnabled(False)
        self.ui.labelTemperature.setText("[N/A]")
        self.ui.labelProtection.setText("")
        set_color(self.ui.labelProtection, None)
        self.ui.btnLoadOn.blockSignals(True)
        self.ui.btnLoadOn.setChecked(False)
        self.ui.btnLoadOn.blockSignals(False)
        self.state_callback([(0.0, 0.0)])
        for lcd in (
            self.ui.lcdVoltage,
            self.ui.lcdCurrent,
            self.ui.lcdPower,
            self.ui.lcdResistance,
        ):
            lcd.display("--")

    def open_state_ui(self):
        self.ui.labelLinkState.setText(self.tr("已连接"))
        set_color(self.ui.labelLinkState, setting.get_color("general_green"))
        self.ui.frameOutputSetting.setEnabled(True)
        self.ui.frameSystemState.setEnabled(True)

    def apply_theme(self):
        set_color(self.ui.lcdVoltage, setting.get_color("lcd_voltage"))
        set_color(self.ui.lcdCurrent, setting.get_color("lcd_current"))
        set_color(self.ui.lcdPower, setting.get_color("lcd_power"))
        set_color(self.ui.lcdResistance, setting.get_color("lcd_resistance"))


DEVICE_PANEL_TYPES["L1060"] = L1060DevicePanel
