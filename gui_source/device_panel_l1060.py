import time
from typing import List, Tuple

import numpy as np
from loguru import logger
from PyQt5 import QtCore, QtWidgets

from app_context import DEBUG, FPSCounter, set_color
from device_core import ChannelSpec, RecordData
from device_panel import DEVICE_PANEL_TYPES, DevicePanelBase
from mdp_controller import MDP_L1060
from mdp_gui_template import Ui_DevicePanelL1060
from settings_model import SETTING_FILE, setting

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
    ChannelSpec("energy", QtCore.QCoreApplication.translate("MDPMainwindow", "能量"), "J"),
    ChannelSpec(
        "temperature", QtCore.QCoreApplication.translate("MDPMainwindow", "温度"), "°F"
    ),
]
CHANNEL_BY_KEY = {c.key: c for c in CHANNELS}
CHANNEL_SHORT = {
    "voltage": "V",
    "current": "I",
    "power": "P",
    "resistance": "R",
    "energy": "E",
    "temperature": "T",
}
RECORD_CHANNELS = [CHANNEL_BY_KEY["voltage"], CHANNEL_BY_KEY["current"]]

_TARGET_SPINBOX = {
    "CC": "spinBoxTargetCC",
    "CV": "spinBoxTargetCV",
    "CR": "spinBoxTargetCR",
    "CP": "spinBoxTargetCP",
}
_MODE_BUTTON = {
    "CC": "btnModeCC",
    "CV": "btnModeCV",
    "CR": "btnModeCR",
    "CP": "btnModeCP",
}
_MODE_UNIT = {
    "CC": "A",
    "CV": "V",
    "CR": "Ω",
    "CP": "W",
}
# Mirrors the ranges/steps set on spinBoxTargetCC/CV/CR/CP in the .ui.
_MODE_RANGE = {
    "CC": (0.0, 10.0, 0.001),
    "CV": (0.0, 30.0, 0.001),
    "CR": (0.01, 999.999, 0.1),
    "CP": (0.0, 999.999, 0.1),
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
        self._temp_f = 0.0
        self._load_commanded_on = False

        self._init_timers()
        self._init_mode_buttons()
        self._init_signals()

        for mode, target in self.settings.l1060_targets.items():
            getattr(self.ui, _TARGET_SPINBOX[mode]).setValue(target)
        self._sync_mode_ui(self.settings.l1060_mode)

        self.ui.tabWidget.tabBar().setVisible(False)
        self.ui.labelTab.setText(
            self.ui.tabWidget.tabText(self.ui.tabWidget.currentIndex())
        )
        self.refresh_preset()
        self.get_preset(self.ui.comboPresetEdit.currentText())

        self.set_interp(setting.ui.interp)
        self.close_state_ui()
        # qdarktheme's global stylesheet (applied once, at startup, after this
        # constructor runs) clobbers the .ui-set minimumSize on QLCDNumber
        # widgets. Re-assert it once the event loop is actually running (show()
        # + first layout pass done) so the digits aren't squeezed unreadably
        # thin in the narrower per-device panel column.
        QtCore.QTimer.singleShot(0, self._enforce_lcd_min_width)

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

    def _init_mode_buttons(self):
        self._mode_button_group = QtWidgets.QButtonGroup(self)
        self._mode_button_group.setExclusive(True)
        for mode, name in _MODE_BUTTON.items():
            btn = getattr(self.ui, name)
            self._mode_button_group.addButton(btn)
            btn.clicked.connect(lambda _checked, m=mode: self.on_mode_changed(m))

    def _init_signals(self):
        self.ui.spinBoxTargetCC.editingFinished.connect(lambda: self.on_target_changed("CC"))
        self.ui.spinBoxTargetCV.editingFinished.connect(lambda: self.on_target_changed("CV"))
        self.ui.spinBoxTargetCR.editingFinished.connect(lambda: self.on_target_changed("CR"))
        self.ui.spinBoxTargetCP.editingFinished.connect(lambda: self.on_target_changed("CP"))
        self.ui.btnLoadOn.toggled.connect(self.on_load_on_toggled)
        self.ui.comboPreset.currentTextChanged.connect(self.set_preset)
        self.ui.comboPresetEdit.currentTextChanged.connect(self.get_preset)
        self.ui.comboPresetEditMode.currentTextChanged.connect(self.on_preset_mode_changed)

    @QtCore.pyqtSlot()
    def on_btnLink_clicked(self):
        self.link_toggle_requested.emit()

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

    def _sync_mode_ui(self, mode: str):
        for m, name in _TARGET_SPINBOX.items():
            spin = getattr(self.ui, name)
            spin.setStyleSheet("" if m == mode else "color: gray;")
        btn = getattr(self.ui, _MODE_BUTTON[mode])
        if not btn.isChecked():
            btn.setChecked(True)

    def on_mode_changed(self, mode: str):
        self._sync_mode_ui(mode)
        self.settings.l1060_mode = mode
        if self.api is None:
            return
        if not self._load_commanded_on:
            self.api.select_mode(mode)
            self._push_target(mode, self.settings.l1060_targets[mode])
            return
        # Mimic the front panel's "Turn off before SET" interlock: cycle the
        # load off to apply the mode change, then back on.
        self.api.set_load_on(False)
        self._load_commanded_on = False
        self._flush_pending_targets(mode)
        ok = self.api.set_load_on(True)
        self._load_commanded_on = ok
        if not ok:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)

    def _push_target(self, mode: str, value: float):
        if mode == "CC":
            self.api.set_current(value)
        elif mode == "CV":
            self.api.set_voltage(value)
        elif mode == "CR":
            self.api.set_resistance(value)
        elif mode == "CP":
            self.api.set_power(value)

    def _flush_pending_targets(self, desired_mode: str):
        for mode in ("CC", "CV", "CR", "CP"):
            self._push_target(mode, self.settings.l1060_targets[mode])
        self.api.select_mode(desired_mode)

    def on_target_changed(self, mode: str):
        spin = getattr(self.ui, _TARGET_SPINBOX[mode])
        value = spin.value()
        self.settings.l1060_targets[mode] = value
        if self.api is None:
            return
        # Writing a different mode's register is an implicit mode-select on
        # this firmware and gets fought/reverted while the load is live, so
        # only cross-mode edits are staged. The active mode's own field is
        # not a mode change and pushes immediately.
        if self._load_commanded_on and mode != self.settings.l1060_mode:
            return
        self._push_target(mode, value)

    ######### Auxiliary Functions - Preset Group #########

    def set_preset(self, _):
        text = self.ui.comboPreset.currentText()
        if not text or not text[1].isdigit():
            return
        mode, value = self.settings.l1060_presets[text[1]]
        getattr(self.ui, _TARGET_SPINBOX[mode]).setValue(value)
        self.settings.l1060_targets[mode] = value
        self.on_mode_changed(mode)
        self.ui.comboPreset.setCurrentIndex(0)

    def refresh_preset(self):
        idx = self.ui.comboPreset.currentIndex()
        self.ui.comboPreset.clear()
        self.ui.comboPreset.addItem("[>] " + self.tr("选择预设"))
        self.ui.comboPreset.addItems(
            [
                f"[{k}] {mode} {value:07.3f}{_MODE_UNIT[mode]}"
                for k, (mode, value) in self.settings.l1060_presets.items()
            ]
        )
        self.ui.comboPreset.setCurrentIndex(idx)
        self.ui.comboPreset.setItemData(0, 0, QtCore.Qt.UserRole - 1)
        idx = self.ui.comboPresetEdit.currentIndex()
        self.ui.comboPresetEdit.clear()
        self.ui.comboPresetEdit.addItems(
            [f"Preset-{k}" for k in self.settings.l1060_presets]
        )
        self.ui.comboPresetEdit.setCurrentIndex(idx)

    def get_preset(self, text):
        if "-" not in text:
            return
        mode, value = self.settings.l1060_presets[text.split("-")[1]]
        self.ui.comboPresetEditMode.setCurrentText(mode)
        self.ui.spinBoxPresetTarget.setValue(value)

    @QtCore.pyqtSlot(str)
    def on_preset_mode_changed(self, mode: str):
        lo, hi, step = _MODE_RANGE[mode]
        self.ui.spinBoxPresetTarget.setSuffix(_MODE_UNIT[mode])
        self.ui.spinBoxPresetTarget.setRange(lo, hi)
        self.ui.spinBoxPresetTarget.setSingleStep(step)

    @QtCore.pyqtSlot()
    def on_btnPresetSave_clicked(self):
        preset = self.ui.comboPresetEdit.currentText()
        if not preset:
            return
        preset = preset.split("-")[1]
        mode = self.ui.comboPresetEditMode.currentText()
        value = self.ui.spinBoxPresetTarget.value()
        try:
            self.settings.l1060_presets[preset] = (mode, value)
            setting.save(SETTING_FILE)
            self.ui.btnPresetSave.setText(self.tr("保存成功"))
            self.refresh_preset()
        except Exception:
            logger.exception(self.tr("保存预设失败"))
            self.ui.btnPresetSave.setText(self.tr("保存失败"))
        QtCore.QTimer.singleShot(
            1000, lambda: self.ui.btnPresetSave.setText(self.tr("保存"))
        )

    @QtCore.pyqtSlot(bool)
    def on_load_on_toggled(self, checked: bool):
        if self.api is None:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)
            return
        ok = self.api.set_load_on(checked)
        if not checked:
            self._load_commanded_on = False
            self._flush_pending_targets(self.settings.l1060_mode)
            return
        self._load_commanded_on = ok
        if not ok:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)

    def refresh_led_color(self):
        if self.api is None:
            return
        color_rgb = bytes.fromhex(self.settings.color.lstrip("#"))
        self.api.set_led_color((color_rgb[0], color_rgb[1], color_rgb[2]))

    def link(self, bus, pipe: int = 0, fps: float = 50, session_start_time=None):
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
        self.store.start_time = t if session_start_time is None else session_start_time
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
        self._load_commanded_on = False
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
            "temperature": np.full(len_, self._temp_f),
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
        if LoadMode != self.settings.l1060_mode:
            self._sync_mode_ui(LoadMode)
            self.settings.l1060_mode = LoadMode
        self._temp_f = Temperature * 9 / 5 + 32
        self.ui.labelTemperature.setText(f"{Temperature:.0f}°C/{self._temp_f:.0f}°F")
        self.ui.btnLoadOn.blockSignals(True)
        self.ui.btnLoadOn.setChecked(bool(LoadActive))
        self.ui.btnLoadOn.blockSignals(False)
        if ProtectionLatched:
            self._load_commanded_on = False
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
            self.ui.lcdEnerge.display(f"{store.energy:.{3+setting.ui.interp}f}")
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
            # Only the active mode's spinbox is synced here, and its edits
            # are never staged (on_target_changed pushes them live), so this
            # readback can't clobber a pending cross-mode edit.
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
        self.ui.labelTemperature.setText("")
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
            self.ui.lcdEnerge,
            self.ui.lcdResistance,
        ):
            lcd.display("")

    def open_state_ui(self):
        self.ui.labelLinkState.setText(self.tr("已连接"))
        set_color(self.ui.labelLinkState, setting.get_color("general_green"))
        self.ui.frameOutputSetting.setEnabled(True)
        self.ui.frameSystemState.setEnabled(True)

    def _enforce_lcd_min_width(self):
        for lcd in (
            self.ui.lcdVoltage,
            self.ui.lcdCurrent,
            self.ui.lcdPower,
            self.ui.lcdEnerge,
            self.ui.lcdResistance,
        ):
            lcd.setMinimumWidth(130)

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


DEVICE_PANEL_TYPES["L1060"] = L1060DevicePanel
