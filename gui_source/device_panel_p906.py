import datetime
import math
import os
import random
import time
from typing import List, Tuple

import numpy as np
from loguru import logger
from PyQt5 import QtCore, QtGui, QtWidgets
from simple_pid import PID

from app_context import FPSCounter, set_color
from battery_aux import (
    CHEM_LEAD_ACID,
    CHEM_LI_ION,
    CHEM_LIFEPO4,
    CHEM_NIMH,
    PHASE_CC,
    PHASE_CV,
    PHASE_DONE,
    PHASE_FLOAT,
    PHASE_PRECHARGE,
    REASON_CEILING,
    REASON_CUTOFF,
    REASON_DEVICE_ERROR,
    REASON_DISCONNECTED,
    REASON_MAX_AH,
    REASON_MAX_WH,
    REASON_NDV,
    REASON_OUTPUT_OFF,
    REASON_TIMEOUT,
    REASON_USER_STOP,
    CapacityAccumulator,
    ChargeController,
    ChargeFinished,
    ChargeProfile,
    ChargeSetpoint,
    build_profile,
)
from device_core import ChannelSpec, RecordData
from device_panel import (
    BASE_CHANNEL_SHORT,
    BASE_CHANNELS,
    DEVICE_PANEL_TYPES,
    OPEN_R,
    RECORD_CHANNELS,
    DevicePanelBase,
)
from mdp_controller import MDP_P906
from mdp_custom import CustomInputDialog, CustomMessageBox
from mdp_gui_template import Ui_DevicePanelP906
from settings_model import SETTING_FILE, setting


CHANNELS = BASE_CHANNELS + [
    # Battery-charge running totals (battery_aux.CapacityAccumulator).
    # Zero/flat outside an active charge run, held at the last run's final
    # value in between -- ordinary store channels so the Ah/Wh/Charge Curve
    # graphs get the same scrolling-window/slider behavior as every other
    # channel.
    ChannelSpec("ah", QtCore.QCoreApplication.translate("P906DevicePanel", "安时"), "Ah"),
    ChannelSpec("wh", QtCore.QCoreApplication.translate("P906DevicePanel", "瓦时"), "Wh"),
]
CHANNEL_BY_KEY = {c.key: c for c in CHANNELS}
CHANNEL_SHORT = {**BASE_CHANNEL_SHORT, "ah": "Ah", "wh": "Wh"}
CHARGE_CSV_CHANNELS = [
    CHANNEL_BY_KEY["voltage"],
    CHANNEL_BY_KEY["current"],
    CHANNEL_BY_KEY["power"],
    CHANNEL_BY_KEY["ah"],
    CHANNEL_BY_KEY["wh"],
]
_CHARGE_TICK_MS = 200
# Rows of the charge settings that only apply to some chemistries.
_CHARGE_PRECHARGE_WIDGETS = (
    "label_chargePrechargeV",
    "spinBoxChargePrechargeV",
    "label_chargePrechargeI",
    "spinBoxChargePrechargeI",
)
_CHARGE_CUTOFF_WIDGETS = ("label_chargeCutoff", "spinBoxChargeCutoff")
_CHARGE_FLOAT_WIDGETS = ("checkBoxChargeFloat", "spinBoxChargeFloat")
_CHARGE_NDV_WIDGETS = (
    "label_chargeNdv",
    "spinBoxChargeNdv",
    "label_chargeHoldoff",
    "spinBoxChargeHoldoff",
)


class P906DevicePanel(DevicePanelBase):
    api_class = MDP_P906
    record_channels = RECORD_CHANNELS

    def __init__(self, capture, parent=None, device_settings=None):
        device_settings = device_settings or setting.devices[0]
        super().__init__(
            device_id=device_settings.id,
            display_name=device_settings.name,
            channels=CHANNELS,
            data_length=setting.ui.data_pts,
            capture=capture,
            open_r=OPEN_R,
            parent=parent,
        )
        self.settings = device_settings
        self.ui = Ui_DevicePanelP906()
        self.ui.setupUi(self)
        self.ui.labelDeviceName.setText(self.display_name)

        self.api = None
        self.data_fps = 50
        self.locked = False
        self._v_set = 0.0
        self._i_set = 0.0
        self._output_state = False
        self.output_state_str = ""
        self._temp_f = 0.0
        self._err_flag = 0
        self._charge_controller = None
        self._charge_acc = None
        self._charge_last_vi = None
        self._charge_start_t = 0.0
        self._charge_enable_t = math.inf
        self.continuous_energy_counter = 0
        self.model = "Unknown"
        self.fps_counter = FPSCounter()
        self._last_state_change_t = time.perf_counter()

        self._init_timers()
        self._init_combos()
        self._init_signals()

        self.ui.progressBarVoltage.setMaximum(1000)
        self.ui.progressBarCurrent.setMaximum(1000)
        self.ui.btnSeqStop.hide()
        self.ui.listSeq.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.ui.spinBoxVoltage.setSingleStep(0.001)
        self.ui.spinBoxCurrent.setSingleStep(0.001)
        # Only step actions (arrows/wheel) should apply live via valueChanged;
        # typed edits must wait for editingFinished (Enter/focus-loss), not
        # fire on every keystroke.
        self.ui.spinBoxVoltage.setKeyboardTracking(False)
        self.ui.spinBoxCurrent.setKeyboardTracking(False)
        self.ui.tabWidget.tabBar().setVisible(False)
        self.ui.labelTab.setText(
            self.ui.tabWidget.tabText(self.ui.tabWidget.currentIndex())
        )
        self.set_fills_column(False)

        self.set_interp(setting.ui.interp)
        self.refresh_preset()
        self.get_preset("1")
        self.close_state_ui()
        battery_curve_dir = os.path.join(os.path.dirname(__file__), "battery_curves")
        for chem in (
            "LiFePO4",
            "Lead-acid",
            "NiMH",
            "Alkaline",
            "Zinc-carbon",
            "Li-ion",
        ):
            self.load_battery_model(os.path.join(battery_curve_dir, f"{chem}.csv"))
        # qdarktheme's global stylesheet (applied once, at startup, after this
        # constructor runs) clobbers the .ui-set minimumSize on QLCDNumber
        # widgets. Re-assert it once the event loop is actually running (show()
        # + first layout pass done) so the digits aren't squeezed unreadably
        # thin in the narrower per-device panel column.
        QtCore.QTimer.singleShot(0, self._enforce_lcd_min_width)

    def set_fills_column(self, fill: bool):
        super().set_fills_column(fill)
        # QTabWidget sizes itself to its largest tab (Battery Sim/Sequence are
        # much taller than Preset), which otherwise reserves that much room
        # even while a short tab is showing and starves the aux area of any
        # panel stacked in the same column. Capping this leaves enough of the
        # shared vertical budget for L1060's Presets tab to fit without
        # scrolling; P906's own tabs rely on their internal scroll areas. A
        # panel alone in the column has no one to share with, so it's uncapped.
        self.ui.tabWidget.setMaximumHeight(QtWidgets.QWIDGETSIZE_MAX if fill else 210)

    def set_english_fonts(self):
        c_font = QtGui.QFont()
        c_font.setFamily("Sarasa Fixed SC SemiBold")
        c_font.setPointSize(7)
        self.ui.btnSeqCurrent.setFont(c_font)
        self.ui.btnSeqCurrent.setText("I-SET")
        self.ui.btnSeqVoltage.setFont(c_font)
        self.ui.btnSeqVoltage.setText("V-SET")
        self.ui.btnSeqDelay.setFont(c_font)
        self.ui.btnSeqWaitTime.setFont(c_font)
        self.ui.btnSeqSingle.setFont(c_font)
        self.ui.btnSeqSingle.setText("Once")
        self.ui.btnSeqLoop.setFont(c_font)
        self.ui.btnSeqSave.setFont(c_font)
        self.ui.btnSeqLoad.setFont(c_font)

    def _init_timers(self):
        self.state_request_sender_timer = QtCore.QTimer(self)
        self.state_request_sender_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.state_request_sender_timer.timeout.connect(self.request_state)
        self.update_state_timer = QtCore.QTimer(self)
        self.update_state_timer.timeout.connect(self.update_state)
        self.state_lcd_timer = QtCore.QTimer(self)
        self.state_lcd_timer.timeout.connect(self.update_state_lcd)
        self.func_sweep_timer = QtCore.QTimer(self)
        self.func_sweep_timer.timeout.connect(self.func_sweep)
        self.func_wave_gen_timer = QtCore.QTimer(self)
        self.func_wave_gen_timer.timeout.connect(self.func_wave_gen)
        self.func_keep_power_timer = QtCore.QTimer(self)
        self.func_keep_power_timer.timeout.connect(self.func_keep_power)
        self.func_seq_timer = QtCore.QTimer(self)
        self.func_seq_timer.timeout.connect(self.func_seq)
        self.func_bat_sim_timer = QtCore.QTimer(self)
        self.func_bat_sim_timer.timeout.connect(self.func_bat_sim)
        self.charge_timer = QtCore.QTimer(self)
        self.charge_timer.timeout.connect(self._charge_tick)
        self.stable_checker_timer = QtCore.QTimer(self)
        self.stable_checker_timer.timeout.connect(self.stable_checker)

    def _init_combos(self):
        self.ui.comboSweepRecord.setItemData(0, None)
        for idx, ch in enumerate(CHANNELS):
            self.ui.comboSweepRecord.setItemData(idx + 1, ch.key)
        self.ui.comboSweepTarget.setItemData(0, "voltage")
        self.ui.comboSweepTarget.setItemData(1, "current")
        for text, chem in (
            (self.tr("锂离子"), CHEM_LI_ION),
            (self.tr("磷酸铁锂"), CHEM_LIFEPO4),
            (self.tr("铅酸"), CHEM_LEAD_ACID),
            (self.tr("镍氢/镍镉"), CHEM_NIMH),
        ):
            self.ui.comboChargeChem.addItem(text, chem)
        self.ui.comboChargeChem.currentTextChanged.connect(self._on_charge_chem_changed)
        self._on_charge_chem_changed()

    def _init_signals(self):
        self.ui.comboPreset.currentTextChanged.connect(self.set_preset)
        self.ui.comboPresetEdit.currentTextChanged.connect(self.get_preset)
        self.ui.spinBoxVoltage.valueChanged.connect(self.voltage_changed)
        self.ui.spinBoxCurrent.valueChanged.connect(self.current_changed)
        self.ui.comboWaveGenType.currentTextChanged.connect(self.set_wavegen_type)
        self.ui.spinBoxVoltage.lineEdit().cursorPositionChanged.connect(
            lambda *args: self.set_step(self.ui.spinBoxVoltage, *args)
        )
        self.ui.spinBoxCurrent.lineEdit().cursorPositionChanged.connect(
            lambda *args: self.set_step(self.ui.spinBoxCurrent, *args)
        )

    def set_step(self, spin: QtWidgets.QDoubleSpinBox, f, t):
        if not setting.ui.bitadjust:
            # enable adaptive step size
            spin.setStepType(
                QtWidgets.QAbstractSpinBox.StepType.AdaptiveDecimalStepType
            )
            return
        spin.setStepType(QtWidgets.QAbstractSpinBox.StepType.DefaultStepType)
        if QtWidgets.QApplication.mouseButtons() == QtCore.Qt.NoButton:
            # Cursor moved because of typing, not a click picking a digit to
            # step. Re-selecting here would hijack normal keyboard entry
            # (each keystroke would overwrite a single re-selected digit
            # instead of composing the typed number).
            return
        STEPS = {
            0: 0.001,
            -1: 0.001,
            -2: 0.01,
            -3: 0.1,
        }
        if spin.lineEdit().hasSelectedText():
            return
        text = spin.lineEdit().text()
        tt = t - len(spin.lineEdit().text())
        t -= 1
        if tt == 0:
            t -= 1
        if text[t] == ".":
            if f < t:
                t += 1
            else:
                t -= 1
        if t <= 0:
            t = 0
        if spin.value() >= 10 and t == 0:
            t = 1
        spin.lineEdit().setSelection(t, 1)
        spin.setSingleStep(STEPS.get(tt, 1))

    ##########  基本功能  ##########

    @property
    def v_set(self):
        return self._v_set

    @v_set.setter
    def v_set(self, value):
        if self.api is None or self.locked:
            return
        value = max(0, min(value, 30))
        self._v_set = value
        self.ui.spinBoxVoltage.setValue(value)
        if self.settings.cali.use:
            value = value * self.settings.cali.vset_k + self.settings.cali.vset_b
        self.api.set_voltage(value)
        self._last_state_change_t = time.perf_counter()

    @property
    def i_set(self):
        return self._i_set

    @i_set.setter
    def i_set(self, value):
        if self.api is None or self.locked:
            return
        value = max(0, min(value, 10))
        self._i_set = value
        self.ui.spinBoxCurrent.setValue(value)
        if self.settings.cali.use:
            value = value * self.settings.cali.iset_k + self.settings.cali.iset_b
        self.api.set_current(value)
        self._last_state_change_t = time.perf_counter()

    @property
    def output_state(self):
        return self._output_state

    @output_state.setter
    def output_state(self, value):
        if self.api is None or value == self._output_state:
            return
        if self.settings.output_warning and not self._output_state:
            ok = CustomMessageBox.question(
                self,
                self.tr("警告"),
                self.tr("确定要打开输出?")
                + f"\n{self.tr('电压')}: {self.v_set:.3f}V"
                + f"\n{self.tr('电流')}: {self.i_set:.3f}A",
            )
            if not ok:
                return
        self._output_state = value
        self._last_state_change_t = time.perf_counter()
        self.api.set_output(self._output_state)
        self.update_state()

    def update_state(self):
        if self.api is None:
            return
        with self._status_lock:
            status = self._latest_status
        if status is None:
            return
        (
            State,
            Locked,
            SetVoltage,
            SetCurrent,
            InputVoltage,
            InputCurrent,
            Temperature,
            ErrFlag,
            _,
            Model,
        ) = status
        if Model != self.model:
            self.model = Model
            if Model == "P905":
                self.ui.spinBoxCurrent.setRange(0, 5)
            elif Model == "P906":
                self.ui.spinBoxCurrent.setRange(0, 10)
            self.link_state_changed.emit()
        self.ui.btnOutput.setText(f"-  {State.upper()}  -")
        set_color(
            self.ui.btnOutput,
            setting.get_color(State),
        )
        self._output_state = State != "off"
        self.output_state_str = State
        self.locked = False
        if not self.settings.ignore_hw_lock and Locked:
            self.locked = True
        self.ui.frameOutputSetting.setEnabled(not self.locked)
        if self.settings.lock_when_output and self._output_state:
            self.locked = True
        self.ui.spinBoxVoltage.setEnabled(not self.locked and not self._charge_active)
        self.ui.spinBoxCurrent.setEnabled(not self.locked and not self._charge_active)
        if SetVoltage >= 0:
            if self.settings.cali.use:
                SetVoltage = (SetVoltage - self.settings.cali.vset_b) / self.settings.cali.vset_k
            self._v_set = SetVoltage
            if not self.ui.spinBoxVoltage.hasFocus():
                self.ui.spinBoxVoltage.setValue(SetVoltage)
        if SetCurrent >= 0:
            if self.settings.cali.use:
                SetCurrent = (SetCurrent - self.settings.cali.iset_b) / self.settings.cali.iset_k
            self._i_set = SetCurrent
            if not self.ui.spinBoxCurrent.hasFocus():
                self.ui.spinBoxCurrent.setValue(SetCurrent)
        self.ui.labelLockState.setText("[LOCKED]" if Locked else "UNLOCKED")
        set_color(
            self.ui.labelLockState,
            setting.get_color("general_red") if Locked else None,
        )
        self.ui.labelInputVals.setText(f"{InputVoltage:.2f}V {InputCurrent:.2f}A")
        self._err_flag = ErrFlag
        self._temp_f = Temperature * 9 / 5 + 32
        self.ui.labelTemperature.setText(f"{Temperature:.0f}°C/{self._temp_f:.0f}°F")
        self.ui.labelError.setText(
            f"ERROR-{ErrFlag:02X}" if ErrFlag != 0 else "NO ERROR"
        )
        set_color(self.ui.labelError, "red" if ErrFlag != 0 else None)

    @QtCore.pyqtSlot()
    def on_btnOutput_clicked(self):
        self.output_state = not self.output_state

    _stable_callback = None
    _stable_start_t = 0

    def stable_checker(self):
        dt = time.perf_counter() - self._stable_start_t
        stable = (
            self.output_state_str == "cc"
            or abs(
                self.store.last("voltage") - self.v_set
            )
            < 0.1
            or dt > 10
        )
        if stable:
            logger.info(f"Output is stable after {dt:.3f}s")
            if self._stable_callback is not None:
                self._stable_callback()
                self._stable_callback = None
            self.stable_checker_timer.stop()

    def wait_output_stable(self, func):
        self.output_state = True
        self._stable_callback = func
        self._stable_start_t = time.perf_counter()
        self.stable_checker_timer.start(50)

    def _close_state_ui_device(self, record_disconnect: bool):
        self.ui.progressBarCurrent.setValue(0)
        self.ui.progressBarVoltage.setValue(0)
        self.ui.btnOutput.setText("[N/A]")
        set_color(self.ui.btnOutput, None)
        for widget in [
            self.ui.labelLockState,
            self.ui.labelInputVals,
            self.ui.labelError,
        ]:
            set_color(widget, None)
            widget.setText("[N/A]")
        set_color(self.ui.labelTemperature, None)
        self.ui.labelTemperature.setText("")

    def _on_link_reset(self, t):
        self._last_state_change_t = t
        self.continuous_energy_counter = 0

    def unlink(self):
        self.state_request_sender_timer.stop()
        self.update_state_timer.stop()
        self.state_lcd_timer.stop()
        if self.func_sweep_timer.isActive():
            self.stop_func_sweep()
        if self.func_wave_gen_timer.isActive():
            self.stop_func_wave_gen()
        if self.func_keep_power_timer.isActive():
            self.stop_func_keep_power()
        if self.func_seq_timer.isActive():
            self.stop_func_seq()
        if self.func_bat_sim_timer.isActive():
            self.stop_func_bat_sim()
        if self._charge_active:
            self._finish_charge(REASON_DISCONNECTED)
        if self.stable_checker_timer.isActive():
            self.stable_checker_timer.stop()
        api = self.api
        self.api = None
        api.close()
        self.v_set = 0.0
        self.i_set = 0.0
        self.model = "Unknown"
        self.ui.spinBoxCurrent.setRange(0, 10)
        self.linked = False
        self.capture.forget(self.device_id)
        self.close_state_ui(record_disconnect=True)
        self.link_state_changed.emit()

    def _preprocess_rtvalues(self, rtvalues: List[Tuple[float, float]]):
        vt, it = self.settings.v_threshold, self.settings.i_threshold
        if self.settings.cali.use:
            rtvalues = [
                (
                    v * self.settings.cali.v_k + self.settings.cali.v_b,
                    i * self.settings.cali.i_k + self.settings.cali.i_b,
                )
                for v, i in rtvalues
            ]
        return [(v if v > vt else 0.0, i if i > it else 0.0) for v, i in rtvalues]

    def _on_samples_appended(self, eng):
        self.continuous_energy_counter += eng

    def update_state_lcd(self):
        avgs = self._update_common_lcds()
        if avgs is None:
            return
        vavg, iavg, _power = avgs
        v_value = round(vavg / self.v_set * 1000) if self.v_set != 0 else 0
        i_value = round(iavg / self.i_set * 1000) if self.i_set != 0 else 0
        self.ui.progressBarVoltage.setValue(min(v_value, 1000))
        self.ui.progressBarCurrent.setValue(min(i_value, 1000))
        self.ui.progressBarVoltage.update()
        self.ui.progressBarCurrent.update()

    @QtCore.pyqtSlot()
    def on_spinBoxVoltage_editingFinished(self):
        v_set = self.ui.spinBoxVoltage.value()
        self.ui.spinBoxVoltage.setSingleStep(0.001)
        self.v_set = v_set

    @QtCore.pyqtSlot()
    def on_spinBoxCurrent_editingFinished(self):
        i_set = self.ui.spinBoxCurrent.value()
        self.ui.spinBoxCurrent.setSingleStep(0.001)
        self.i_set = i_set

    def voltage_changed(self, value):
        if not self.ui.checkBoxQuickset.isChecked():
            return
        self.v_set = value

    def current_changed(self, value):
        if not self.ui.checkBoxQuickset.isChecked():
            return
        self.i_set = value

    ######### 辅助功能-预设组 #########

    def set_preset(self, _):
        text = self.ui.comboPreset.currentText()
        if not text or not text[1].isdigit():
            return
        voltage, current = self.settings.presets[text[1]]
        self.v_set = voltage
        self.i_set = current
        self.ui.comboPreset.setCurrentIndex(0)

    def refresh_preset(self):
        idx = self.ui.comboPreset.currentIndex()
        self.ui.comboPreset.clear()
        self.ui.comboPreset.addItem("[>] " + self.tr("选择预设"))
        self.ui.comboPreset.addItems(
            [f"[{k}] {v[0]:06.3f}V {v[1]:06.3f}A" for k, v in self.settings.presets.items()]
        )
        self.ui.comboPreset.setCurrentIndex(idx)
        self.ui.comboPreset.setItemData(0, 0, QtCore.Qt.UserRole - 1)
        idx = self.ui.comboPresetEdit.currentIndex()
        self.ui.comboPresetEdit.clear()
        self.ui.comboPresetEdit.addItems(
            [f"Preset-{k}" for k, v in self.settings.presets.items()]
        )
        self.ui.comboPresetEdit.setCurrentIndex(idx)

    @QtCore.pyqtSlot()
    def on_btnPresetSave_clicked(self):
        preset = self.ui.comboPresetEdit.currentText()
        if not preset:
            return
        preset = preset.split("-")[1]
        v_set = self.ui.spinBoxPresetVoltage.value()
        i_set = self.ui.spinBoxPresetCurrent.value()
        try:
            self.settings.presets[preset] = [v_set, i_set]
            setting.save(SETTING_FILE)
            self.ui.btnPresetSave.setText(self.tr("保存成功"))
            self.refresh_preset()
        except Exception:
            logger.exception(self.tr("保存预设失败"))
            self.ui.btnPresetSave.setText(self.tr("保存失败"))
        QtCore.QTimer.singleShot(
            1000, lambda: self.ui.btnPresetSave.setText(self.tr("保存"))
        )

    def get_preset(self, text):
        if "-" not in text:
            return
        voltage, current = self.settings.presets[text.split("-")[1]]
        self.ui.spinBoxPresetVoltage.setValue(voltage)
        self.ui.spinBoxPresetCurrent.setValue(current)

    ######### 辅助功能-参数扫描 #########

    _sweep_response_type = ""
    _sweep_response_data_y = []
    _sweep_response_data_x = []
    _sweep_response_x_label = ""
    _sweep_response_y_label = ""
    _sweep_response_x_unit = ""
    _sweep_response_y_unit = ""
    _sweep_flag = False

    @QtCore.pyqtSlot()
    def on_btnSweep_clicked(self):
        if self.func_sweep_timer.isActive():
            self.stop_func_sweep()
        else:
            if self._refuse_during_charge(self.ui.btnSweep):
                return
            self._sweep_target = self.ui.comboSweepTarget.currentData()
            self._sweep_start = self.ui.spinBoxSweepStart.value()
            self._sweep_stop = self.ui.spinBoxSweepStop.value()
            self._sweep_step = self.ui.spinBoxSweepStep.value()
            self._sweep_delay = self.ui.spinBoxSweepDelay.value()
            try:
                assert self._sweep_step > 0
                assert self._sweep_start != self._sweep_stop
                assert self._sweep_delay > 0
            except Exception:
                self.ui.btnSweep.setText(self.tr("非法参数"))
                QtCore.QTimer.singleShot(
                    1000, lambda: self.ui.btnSweep.setText(self.tr("功能已关闭"))
                )
                return

            self._sweep_temp = None
            if self.ui.comboSweepRecord.currentIndex() == 0:
                self._sweep_response_type = ""
            else:
                self._sweep_response_type = self.ui.comboSweepRecord.currentData()
                target_ch = CHANNEL_BY_KEY[self._sweep_target]
                response_ch = CHANNEL_BY_KEY[self._sweep_response_type]
                self._sweep_response_x_label = target_ch.label
                self._sweep_response_y_label = response_ch.label
                self._sweep_response_x_unit = target_ch.unit
                self._sweep_response_y_unit = response_ch.unit
                self._sweep_response_data_x = []
                self._sweep_response_data_y = []

            self._sweep_flag = True
            self.ui.btnSweep.setText(self.tr("功能已开启"))
            if self._sweep_target == "voltage":
                self.ui.spinBoxVoltage.setEnabled(False)
            elif self._sweep_target == "current":
                self.ui.spinBoxCurrent.setEnabled(False)
            self.ui.scrollAreaSweep.setEnabled(False)

            def start_sweep():
                self.func_sweep_timer.start(round(self._sweep_delay * 1000))

            self.v_set = self._sweep_start
            self.wait_output_stable(start_sweep)

    @QtCore.pyqtSlot()
    def on_btnSweepShowRecord_clicked(self):
        if not self._sweep_response_type:
            CustomMessageBox(self, self.tr("错误"), self.tr("扫描响应记录为空"))
            return
        len_y = len(self._sweep_response_data_y)
        len_x = len(self._sweep_response_data_x)
        min_len = min(len_x, len_y)
        self.display_data_signal.emit(
            self._sweep_response_data_x[:min_len],
            self._sweep_response_data_y[:min_len],
            self._sweep_response_x_label,
            self._sweep_response_y_label,
            self._sweep_response_x_unit,
            self._sweep_response_y_unit,
            self.tr("扫描响应结果曲线"),
            False,
        )

    @QtCore.pyqtSlot(str)
    def on_comboSweepTarget_currentTextChanged(self, text):
        key = self.ui.comboSweepTarget.currentData()
        if key == "voltage":
            self.ui.spinBoxSweepStart.setSuffix("V")
            self.ui.spinBoxSweepStop.setSuffix("V")
            self.ui.spinBoxSweepStep.setSuffix("V")
            self.ui.spinBoxSweepStart.setRange(0, 30)
            self.ui.spinBoxSweepStop.setRange(0, 30)
            self.ui.spinBoxSweepStep.setRange(0.001, 30)
        elif key == "current":
            self.ui.spinBoxSweepStart.setSuffix("A")
            self.ui.spinBoxSweepStop.setSuffix("A")
            self.ui.spinBoxSweepStep.setSuffix("A")
            self.ui.spinBoxSweepStart.setRange(0, 10)
            self.ui.spinBoxSweepStop.setRange(0, 10)
            self.ui.spinBoxSweepStep.setRange(0.001, 10)

    def stop_func_sweep(self):
        self.func_sweep_timer.stop()
        self.ui.btnSweep.setText(self.tr("功能已关闭"))
        if self._sweep_target == "voltage":
            self.ui.spinBoxVoltage.setEnabled(True)
        elif self._sweep_target == "current":
            self.ui.spinBoxCurrent.setEnabled(True)
        self.ui.scrollAreaSweep.setEnabled(True)

    def func_sweep(self):
        if self._sweep_response_type:
            self._sweep_response_data_x.append(
                self.store.last(self._sweep_target)
            )
            self._sweep_response_data_y.append(
                self.store.last(self._sweep_response_type)
            )

        if not self._sweep_flag:
            self.stop_func_sweep()
            return

        if self._sweep_temp is None:
            self._sweep_temp = self._sweep_start
        else:
            if self._sweep_start <= self._sweep_stop:
                self._sweep_temp += self._sweep_step
            else:
                self._sweep_temp -= self._sweep_step

        if (
            self._sweep_start > self._sweep_stop
            and self._sweep_temp <= self._sweep_stop
        ) or (
            self._sweep_start <= self._sweep_stop
            and self._sweep_temp >= self._sweep_stop
        ):
            self._sweep_temp = self._sweep_stop
            self._sweep_flag = False
            self.stop_func_sweep()

        if self._sweep_target == "voltage":
            self.v_set = self._sweep_temp
        elif self._sweep_target == "current":
            self.i_set = self._sweep_temp

    ######### 辅助功能-发生器 #########

    @QtCore.pyqtSlot()
    def on_btnWaveGen_clicked(self):
        if self.func_wave_gen_timer.isActive():
            self.stop_func_wave_gen()
        else:
            if self._refuse_during_charge(self.ui.btnWaveGen):
                return
            self._wavegen_type = self.ui.comboWaveGenType.currentText()
            self._wavegen_period = self.ui.spinBoxWaveGenPeriod.value()
            self._wavegen_highlevel = self.ui.spinBoxWaveGenHigh.value()
            self._wavegen_lowlevel = self.ui.spinBoxWaveGenLow.value()
            self._wavegen_loopfreq = self.ui.spinBoxWaveGenLoopFreq.value()
            try:
                assert self._wavegen_highlevel > self._wavegen_lowlevel
                assert self._wavegen_period > 0
                assert self._wavegen_loopfreq > 0
            except Exception:
                self.ui.btnWaveGen.setText(self.tr("非法参数"))
                QtCore.QTimer.singleShot(
                    1000, lambda: self.ui.btnWaveGen.setText(self.tr("功能已关闭"))
                )
                return
            self.ui.btnWaveGen.setText(self.tr("功能已开启"))
            self.ui.spinBoxWaveGenLoopFreq.setEnabled(False)
            self.ui.spinBoxVoltage.setEnabled(False)
            self._wavegen_start_time = 0

            def start_wave_gen():
                self._wavegen_start_time = time.perf_counter()
                self.func_wave_gen_timer.start(round(1000 / self._wavegen_loopfreq))
                self.v_set = self._wavegen_lowlevel

            self.v_set = self._wavegen_lowlevel
            self.wait_output_stable(start_wave_gen)

    def stop_func_wave_gen(self):
        self.func_wave_gen_timer.stop()
        self.ui.btnWaveGen.setText(self.tr("功能已关闭"))
        self.ui.spinBoxWaveGenLoopFreq.setEnabled(True)
        self.ui.spinBoxVoltage.setEnabled(True)

    def set_wavegen_type(self, _):
        self._wavegen_type = self.ui.comboWaveGenType.currentText()

    @QtCore.pyqtSlot()
    def on_spinBoxWaveGenPeriod_editingFinished(self):
        self._wavegen_period = self.ui.spinBoxWaveGenPeriod.value()

    @QtCore.pyqtSlot()
    def on_spinBoxWaveGenHigh_editingFinished(self):
        self._wavegen_highlevel = self.ui.spinBoxWaveGenHigh.value()

    @QtCore.pyqtSlot()
    def on_spinBoxWaveGenLow_editingFinished(self):
        self._wavegen_lowlevel = self.ui.spinBoxWaveGenLow.value()

    def func_wave_gen(self):
        t = time.perf_counter() - self._wavegen_start_time
        if self._wavegen_type == self.tr("正弦波"):
            voltage = (
                self._wavegen_lowlevel
                + (self._wavegen_highlevel - self._wavegen_lowlevel)
                * (math.sin(2 * math.pi / self._wavegen_period * t) + 1.0)
                / 2
            )
        elif self._wavegen_type == self.tr("方波"):
            voltage = (
                self._wavegen_highlevel
                if math.sin(2 * math.pi / self._wavegen_period * t) > 0
                else self._wavegen_lowlevel
            )
        elif self._wavegen_type == self.tr("三角波"):
            mul = (t / self._wavegen_period) % 2
            mul = mul if mul < 1 else 2 - mul
            voltage = (
                self._wavegen_lowlevel
                + (self._wavegen_highlevel - self._wavegen_lowlevel) * mul
            )
        elif self._wavegen_type == self.tr("锯齿波"):
            voltage = (self._wavegen_highlevel - self._wavegen_lowlevel) * (
                (t / self._wavegen_period) % 1
            ) + self._wavegen_lowlevel
        elif self._wavegen_type == self.tr("噪音"):
            voltage = random.uniform(self._wavegen_lowlevel, self._wavegen_highlevel)
        else:
            voltage = 0
        voltage = max(
            min(voltage, self._wavegen_highlevel), self._wavegen_lowlevel
        )  # 限幅
        self.v_set = voltage

    ######### 辅助功能-功率保持 #########

    @QtCore.pyqtSlot()
    def on_btnKeepPower_clicked(self):
        if self.func_keep_power_timer.isActive():
            self.stop_func_keep_power()
        else:
            if self._refuse_during_charge(self.ui.btnKeepPower):
                return
            self._keep_power_target = self.ui.spinBoxKeepPowerSet.value()
            self._keep_power_loopfreq = self.ui.spinBoxKeepPowerLoopFreq.value()
            self._keep_power_pid_i = self.ui.spinBoxKeepPowerPi.value()
            self._keep_power_pid_max_v = self.ui.spinBoxKeepPowerMaxV.value()
            try:
                assert self._keep_power_loopfreq > 0
                assert self._keep_power_pid_i > 0
            except Exception:
                self.ui.btnKeepPower.setText(self.tr("非法参数"))
                QtCore.QTimer.singleShot(
                    1000, lambda: self.ui.btnKeepPower.setText(self.tr("功能已关闭"))
                )
                return
            self._keep_power_pid = PID(
                0,
                self._keep_power_pid_i,
                0,
                setpoint=self._keep_power_target,
                auto_mode=False,
            )
            self._keep_power_pid.output_limits = (0, self._keep_power_pid_max_v)
            self._keep_power_pid.set_auto_mode(True, last_output=self.v_set)
            self.func_keep_power_timer.start(round(1000 / self._keep_power_loopfreq))
            self.ui.btnKeepPower.setText(self.tr("功能已开启"))
            self.ui.spinBoxVoltage.setEnabled(False)
            self.ui.spinBoxKeepPowerLoopFreq.setEnabled(False)

    def stop_func_keep_power(self):
        self.func_keep_power_timer.stop()
        self.ui.btnKeepPower.setText(self.tr("功能已关闭"))
        self.ui.spinBoxVoltage.setEnabled(True)
        self.ui.spinBoxKeepPowerLoopFreq.setEnabled(True)

    def func_keep_power(self):
        if not self.output_state:
            if self._keep_power_pid.auto_mode:
                self._keep_power_pid.set_auto_mode(False)
            voltage = 0
        else:
            if not self._keep_power_pid.auto_mode:
                self._keep_power_pid.set_auto_mode(True, last_output=self.v_set)
            voltage = self._keep_power_pid(
                self.store.last("power")
            )
        self.v_set = voltage

    @QtCore.pyqtSlot()
    def on_spinBoxKeepPowerSet_editingFinished(self):
        self._keep_power_target = self.ui.spinBoxKeepPowerSet.value()
        if self.func_keep_power_timer.isActive():
            self._keep_power_pid.setpoint = self._keep_power_target

    @QtCore.pyqtSlot()
    def on_spinBoxKeepPowerPi_editingFinished(self):
        self._keep_power_pid_i = self.ui.spinBoxKeepPowerPi.value()
        if self.func_keep_power_timer.isActive():
            self._keep_power_pid.tunings = (0, self._keep_power_pid_i, 0)

    @QtCore.pyqtSlot()
    def on_spinBoxKeepPowerMaxV_editingFinished(self):
        self._keep_power_pid_max_v = self.ui.spinBoxKeepPowerMaxV.value()
        if self.func_keep_power_timer.isActive():
            self._keep_power_pid.output_limits = (0, self._keep_power_pid_max_v)

    ######### 辅助功能-电池模拟 #########

    _battery_models = {}
    _bat_sim_soc = []
    _bat_sim_voltage = []
    _bat_sim_total_energy = 0
    _bat_sim_used_energy = 0
    _bat_sim_stop_energy = 0
    _bat_sim_cells = 1
    _bat_sim_internal_r = 0.0
    _bat_sim_last_e_temp = 0
    _bat_sim_start_time = 0

    def load_battery_model(self, path):
        csv = np.loadtxt(path, delimiter=",", skiprows=1)
        csv_name = os.path.basename(os.path.splitext(path)[0])
        soc = csv[:, 0]
        voltage = csv[:, 1]
        self._battery_models[csv_name] = (soc, voltage)
        if csv_name not in [
            self.ui.comboBatSimCurve.itemText(i)
            for i in range(self.ui.comboBatSimCurve.count())
        ]:
            self.ui.comboBatSimCurve.addItem(csv_name)
        self.ui.comboBatSimCurve.setCurrentText(csv_name)

    @QtCore.pyqtSlot()
    def on_btnBatSimLoad_clicked(self):
        path, ok = QtWidgets.QFileDialog.getOpenFileName(
            self, self.tr("打开"), "", self.tr("CSV文件 (*.csv)")
        )
        if path == "" or not ok:
            return
        self.load_battery_model(path)

    @QtCore.pyqtSlot()
    def on_btnBatSimPreview_clicked(self):
        curve_name = self.ui.comboBatSimCurve.currentText()
        if curve_name not in self._battery_models:
            return
        soc, voltage = self._battery_models[curve_name]
        new_soc = np.arange(0, 100 + 1e-9, 0.1)
        new_voltage = np.interp(new_soc, soc, voltage)
        self.display_data_signal.emit(
            new_soc.tolist(),
            new_voltage.tolist(),
            self.tr("SOC"),
            self.tr("电压"),
            "%",
            "V",
            f"{curve_name} Voltage-SOC (State of Charge) Curve",
            True,
        )

    def set_batsim_widget_enabled(self, enabled):
        for item in self.ui.scrollAreaBatSim.findChildren(QtWidgets.QPushButton):
            if item is not self.ui.btnBatSimPreview:
                item.setEnabled(enabled)
        for item in self.ui.scrollAreaBatSim.findChildren(QtWidgets.QDoubleSpinBox):
            item.setEnabled(enabled)
        for item in self.ui.scrollAreaBatSim.findChildren(QtWidgets.QComboBox):
            item.setEnabled(enabled)

    def stop_func_bat_sim(self):
        self.func_bat_sim_timer.stop()
        self.set_batsim_widget_enabled(True)
        self.ui.btnBatSim.setText(self.tr("功能已关闭"))

    @QtCore.pyqtSlot()
    def on_btnBatSim_clicked(self):
        if self.func_bat_sim_timer.isActive():
            self.stop_func_bat_sim()
        else:
            if self._refuse_during_charge(self.ui.btnBatSim):
                return
            curve_name = self.ui.comboBatSimCurve.currentText()
            if curve_name not in self._battery_models:
                return
            self._bat_sim_soc = self._battery_models[curve_name][0]
            self._bat_sim_voltage = self._battery_models[curve_name][1]
            self._bat_sim_cells = self.ui.spinBoxBatSimCells.value()
            self._bat_sim_total_energy = (
                self.ui.spinBoxBatSimCap.value() * 3600 * self._bat_sim_cells
            )  # Wh->J
            self._bat_sim_used_energy = (
                1 - self.ui.spinBoxBatSimCurrent.value() / 100
            ) * self._bat_sim_total_energy
            self._bat_sim_stop_energy = self._bat_sim_total_energy - (
                self.ui.spinBoxBatSimStop.value() / 100 * self._bat_sim_total_energy
            )
            self._bat_sim_internal_r = (
                self.ui.spinBoxBatSimRes.value() / 1000
            )  # mOhm->Ohm
            self._bat_sim_last_e_temp = self.continuous_energy_counter
            self.ui.labelBatSimTime.setText("Discharge Time: 00:00:00")
            self.ui.btnBatSim.setText(self.tr("功能已开启"))
            self.set_batsim_widget_enabled(False)
            self.display_data_signal.emit(
                self._bat_sim_soc.tolist(),
                self._bat_sim_voltage.tolist(),
                self.tr("SOC"),
                self.tr("电压"),
                "%",
                "V",
                f"{curve_name} Battery Simulation Real-Time Curve",
                True,
            )

            def start_bat_sim():
                self.func_bat_sim_timer.start(
                    round(1000 / self.ui.spinBoxBatSimLoopFreq.value())
                )
                self._bat_sim_start_time = time.perf_counter()

            self.v_set = 0
            self.wait_output_stable(start_bat_sim)

    def func_bat_sim(self):
        add_e = self.continuous_energy_counter - self._bat_sim_last_e_temp
        self._bat_sim_last_e_temp += add_e
        self._bat_sim_used_energy += add_e
        if self._bat_sim_used_energy >= self._bat_sim_stop_energy:
            self.output_state = False
            self.on_btnBatSim_clicked()
        new_percent = (
            (self._bat_sim_total_energy - self._bat_sim_used_energy)
            / self._bat_sim_total_energy
            * 100
        )
        self.ui.spinBoxBatSimCurrent.setValue(new_percent)
        new_volt = np.interp(new_percent, self._bat_sim_soc, self._bat_sim_voltage)
        self.v_set = (
            new_volt
            - self._bat_sim_internal_r
            * self.store.last("current")
        ) * self._bat_sim_cells
        self.highlight_point_signal.emit(new_percent, new_volt)
        self.ui.labelBatSimTime.setText(
            "Discharge Time: "
            + time.strftime(
                "%H:%M:%S", time.gmtime(time.perf_counter() - self._bat_sim_start_time)
            )
        )

    ######### 辅助功能-序列 #########

    def seq_btn_disable(self):
        self.ui.btnSeqSave.hide()
        self.ui.btnSeqLoad.hide()
        self.ui.btnSeqSingle.hide()
        self.ui.btnSeqLoop.hide()
        self.ui.btnSeqDelay.hide()
        self.ui.btnSeqWaitTime.hide()
        self.ui.btnSeqVoltage.hide()
        self.ui.btnSeqCurrent.hide()
        self.ui.listSeq.setEnabled(False)
        self.ui.btnSeqStop.show()

    def seq_btn_enable(self):
        self.ui.btnSeqSave.show()
        self.ui.btnSeqLoad.show()
        self.ui.btnSeqSingle.show()
        self.ui.btnSeqLoop.show()
        self.ui.btnSeqDelay.show()
        self.ui.btnSeqWaitTime.show()
        self.ui.btnSeqVoltage.show()
        self.ui.btnSeqCurrent.show()
        self.ui.listSeq.setEnabled(True)
        self.ui.btnSeqStop.hide()

    @QtCore.pyqtSlot()
    def on_btnSeqSingle_clicked(self):
        cnt = self.ui.listSeq.count()
        if cnt == 0 or self._charge_active:
            return
        self.seq_btn_disable()
        self.start_seq(loop=False)
        if not self.output_state:
            self.ui.btnOutput.click()

    @QtCore.pyqtSlot()
    def on_btnSeqLoop_clicked(self):
        cnt = self.ui.listSeq.count()
        if cnt == 0 or self._charge_active:
            return
        self.seq_btn_disable()
        self.start_seq(loop=True)
        if not self.output_state:
            self.ui.btnOutput.click()

    @QtCore.pyqtSlot()
    def on_btnSeqStop_clicked(self):
        self.func_seq_timer.stop()
        self.seq_btn_enable()

    # listSeq 删除
    def seq_del_item(self):
        row = self.ui.listSeq.currentRow()
        cnt = self.ui.listSeq.count()
        if cnt == 0:
            return
        if row == -1:
            row = cnt - 1
        self.ui.listSeq.takeItem(row)
        self.ui.listSeq.setCurrentRow(max(row - 1, 0))

    def seq_edit_item(self):
        row = self.ui.listSeq.currentRow()
        cnt = self.ui.listSeq.count()
        if cnt == 0:
            return
        if row == -1:
            return
        item = self.ui.listSeq.item(row)
        text = item.text()
        if text.split()[0] == "DELAY":
            delay, ok = CustomInputDialog.getInt(
                self,
                self.tr("编辑动作"),
                self.tr("请输入延时时间:"),
                default_value=int(text.split()[1]),
                min_value=0,
                max_value=100000,
                step=1,
                suffix="ms",
            )
            if not ok:
                return
            item.setText(f"DELAY {delay} ms")
        elif text.split()[0] == "WAIT":
            default_value = QtCore.QDateTime.fromString(
                " ".join(text.split()[1:]), "%Y-%m-%d %H:%M:%S"
            )
            wait_time, ok = CustomInputDialog.getDateTime(
                self,
                self.tr("编辑动作"),
                self.tr("请输入等待时间:"),
                default_value=default_value,
            )
            if not ok:
                return
            item.setText(f"WAIT  {wait_time.toString('yyyy-MM-dd HH:mm:ss')}")
        elif text.split()[0] == "SET-V":
            voltage, ok = CustomInputDialog.getDouble(
                self,
                self.tr("编辑动作"),
                self.tr("请输入电压值:"),
                default_value=float(text.split()[1]),
                min_value=0,
                max_value=30,
                step=0.001,
                decimals=3,
                suffix="V",
            )
            if not ok:
                return
            item.setText(f"SET-V {voltage:.3f} V")
        elif text.split()[0] == "SET-I":
            current, ok = CustomInputDialog.getDouble(
                self,
                self.tr("编辑动作"),
                self.tr("请输入电流值:"),
                default_value=float(text.split()[1]),
                min_value=0,
                max_value=10,
                step=0.001,
                decimals=3,
                suffix="A",
            )
            if not ok:
                return
            item.setText(f"SET-I {current:.3f} A")
        else:
            CustomMessageBox(
                self,
                self.tr("错误"),
                self.tr("无法识别动作"),
            )

    def seq_clear_all(self):
        if CustomMessageBox.question(
            self,
            self.tr("警告"),
            self.tr("确定要清空序列吗？"),
        ):
            self.ui.listSeq.clear()

    # listSeq 右键菜单 (listSeq)
    @QtCore.pyqtSlot(QtCore.QPoint)
    def on_listSeq_customContextMenuRequested(self, pos):
        row = self.ui.listSeq.currentRow()
        cnt = self.ui.listSeq.count()
        if cnt == 0:
            return
        if row == -1:
            return
        menu = QtWidgets.QMenu()
        menu.addAction(self.tr("编辑"), lambda: self.seq_edit_item())
        menu.addAction(self.tr("删除"), lambda: self.seq_del_item())
        menu.addAction(self.tr("清空"), lambda: self.seq_clear_all())
        menu.exec_(QtGui.QCursor.pos())

    # 双击修改
    @QtCore.pyqtSlot(QtWidgets.QListWidgetItem)
    def on_listSeq_itemDoubleClicked(self, item):
        self.seq_edit_item()

    def seq_set_item_font(self, index):
        item = self.ui.listSeq.item(index)
        sfont = QtGui.QFont()
        sfont.setFamily("Sarasa Fixed SC SemiBold")
        sfont.setPointSize(10)
        item.setFont(sfont)

    @QtCore.pyqtSlot()
    def on_btnSeqDelay_clicked(self):
        row = self.ui.listSeq.currentRow()
        delay, ok = CustomInputDialog.getInt(
            self,
            self.tr("添加动作"),
            self.tr("请输入延时时间:"),
            default_value=1000,
            min_value=0,
            max_value=100000,
            step=1,
            suffix="ms",
        )
        if not ok:
            return
        self.ui.listSeq.insertItem(row + 1, f"DELAY {delay} ms")
        self.ui.listSeq.setCurrentRow(row + 1)
        self.seq_set_item_font(row + 1)

    @QtCore.pyqtSlot()
    def on_btnSeqWaitTime_clicked(self):
        row = self.ui.listSeq.currentRow()
        wait_time, ok = CustomInputDialog.getDateTime(
            self,
            self.tr("添加动作"),
            self.tr("请输入等待时间:") + "\n" + self.tr("格式: 年-月-日 时:分:秒"),
        )
        if not ok:
            return
        wait_time_str = wait_time.toString("yyyy-MM-dd HH:mm:ss")
        self.ui.listSeq.insertItem(row + 1, f"WAIT  {wait_time_str}")
        self.ui.listSeq.setCurrentRow(row + 1)
        self.seq_set_item_font(row + 1)

    @QtCore.pyqtSlot()
    def on_btnSeqVoltage_clicked(self):
        row = self.ui.listSeq.currentRow()
        voltage, ok = CustomInputDialog.getDouble(
            self,
            self.tr("添加动作"),
            self.tr("请输入电压值:"),
            default_value=5,
            min_value=0,
            max_value=30,
            step=0.001,
            decimals=3,
            suffix="V",
        )
        if not ok:
            return
        self.ui.listSeq.insertItem(row + 1, f"SET-V {voltage:.3f} V")
        self.ui.listSeq.setCurrentRow(row + 1)
        self.seq_set_item_font(row + 1)

    @QtCore.pyqtSlot()
    def on_btnSeqCurrent_clicked(self):
        row = self.ui.listSeq.currentRow()
        current, ok = CustomInputDialog.getDouble(
            self,
            self.tr("添加动作"),
            self.tr("请输入电流值:"),
            default_value=1,
            min_value=0,
            max_value=10,
            step=0.001,
            decimals=3,
            suffix="A",
        )
        if not ok:
            return
        self.ui.listSeq.insertItem(row + 1, f"SET-I {current:.3f} A")
        self.ui.listSeq.setCurrentRow(row + 1)
        self.seq_set_item_font(row + 1)

    def switch_to_seq(self, index) -> bool:
        if index > self._seq_cnt:
            return False
        item = self.ui.listSeq.item(index)
        if item is None:
            return False
        self._seq_index = index
        self.ui.listSeq.setCurrentRow(index)
        text = item.text()
        self._seq_type = text.split()[0]
        if self._seq_type == "WAIT":
            self._seq_value = datetime.datetime.strptime(
                " ".join(text.split()[1:]), "%Y-%m-%d %H:%M:%S"
            )
        else:
            self._seq_value = float(text.split()[1])
        if self._seq_type in ("DELAY", "WAIT") or self._seq_index == 0:
            self._seq_time = time.perf_counter()
        return True

    def stop_func_seq(self):
        self.func_seq_timer.stop()
        self.seq_btn_enable()

    def start_seq(self, loop=False):
        self._seq_loop = loop
        self._seq_index = 0
        self._seq_cnt = self.ui.listSeq.count()
        self.switch_to_seq(0)
        self.func_seq_timer.start(1)

    def func_seq(self):
        now = time.perf_counter()
        if self._seq_type == "DELAY":
            if now - self._seq_time < self._seq_value / 1000:
                return
        elif self._seq_type == "WAIT":
            now = datetime.datetime.now()
            if now < self._seq_value:
                return
        elif self._seq_type == "SET-V":
            self.v_set = self._seq_value
        elif self._seq_type == "SET-I":
            self.i_set = self._seq_value
        else:
            raise ValueError("Unknown seq type")
        if not self.switch_to_seq(self._seq_index + 1):
            if self._seq_loop:
                self.switch_to_seq(0)
            else:
                self.func_seq_timer.stop()
                self.seq_btn_enable()

    @QtCore.pyqtSlot()
    def on_btnSeqSave_clicked(self):
        if self.ui.listSeq.count() == 0:
            return
        # 保存到文件
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.tr("保存"), "", self.tr("文本文件 (*.txt)")
        )
        if filename == "" or filename is None:
            return
        lines = []
        for i in range(self.ui.listSeq.count()):
            lines.append(self.ui.listSeq.item(i).text())
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    @QtCore.pyqtSlot()
    def on_btnSeqLoad_clicked(self):
        # 从文件加载

        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.tr("打开"), "", self.tr("文本文件 (*.txt)")
        )

        if filename == "" or filename is None:
            return
        with open(filename, "r", encoding="utf-8") as f:
            lines = f.read().strip().split("\n")
        self.ui.listSeq.clear()
        for line in lines:
            try:
                _ = line.split(" ")
                assert len(_) == 3
                assert _[0] in ["WAIT", "DELAY", "SET-V", "SET-I"]
                if _[0] != "WAIT":
                    assert _[2] in ["ms", "V", "A"]
                    float(_[1])
                self.ui.listSeq.addItem(line)
                self.seq_set_item_font(self.ui.listSeq.count() - 1)
            except Exception:
                CustomMessageBox(
                    self, self.tr("错误"), self.tr("数据验证错误: ") + f"{line}"
                )
                return

    ######### 辅助功能-电池充电 #########

    @property
    def _charge_active(self) -> bool:
        return self._charge_controller is not None

    def _any_aux_active(self) -> bool:
        return self._charge_active or any(
            timer.isActive()
            for timer in (
                self.func_sweep_timer,
                self.func_wave_gen_timer,
                self.func_keep_power_timer,
                self.func_seq_timer,
                self.func_bat_sim_timer,
                self.stable_checker_timer,
            )
        )

    def _refuse_during_charge(self, button: QtWidgets.QPushButton) -> bool:
        if not self._charge_active:
            return False
        button.setText(self.tr("充电进行中"))
        QtCore.QTimer.singleShot(1000, lambda: button.setText(self.tr("功能已关闭")))
        return True

    def _flash_charge_button(self, text: str):
        self.ui.btnCharge.setText(text)
        QtCore.QTimer.singleShot(
            1000,
            lambda: self.ui.btnCharge.setText(
                self.tr("停止充电") if self._charge_active else self.tr("开始充电")
            ),
        )

    def _on_charge_chem_changed(self, _=None):
        chem = self.ui.comboChargeChem.currentData()
        for names, visible in (
            (_CHARGE_PRECHARGE_WIDGETS, chem in (CHEM_LI_ION, CHEM_LIFEPO4)),
            (_CHARGE_CUTOFF_WIDGETS, chem != CHEM_NIMH),
            (_CHARGE_FLOAT_WIDGETS, chem == CHEM_LEAD_ACID),
            (_CHARGE_NDV_WIDGETS, chem == CHEM_NIMH),
        ):
            for name in names:
                getattr(self.ui, name).setVisible(visible)

    @QtCore.pyqtSlot(bool)
    def on_checkBoxChargeFloat_toggled(self, checked: bool):
        self.ui.spinBoxChargeFloat.setEnabled(checked)

    @QtCore.pyqtSlot(bool)
    def on_checkBoxChargeMaxTime_toggled(self, checked: bool):
        self.ui.spinBoxChargeMaxTime.setEnabled(checked)

    @QtCore.pyqtSlot(bool)
    def on_checkBoxChargeMaxAh_toggled(self, checked: bool):
        self.ui.spinBoxChargeMaxAh.setEnabled(checked)

    @QtCore.pyqtSlot(bool)
    def on_checkBoxChargeMaxWh_toggled(self, checked: bool):
        self.ui.spinBoxChargeMaxWh.setEnabled(checked)

    @QtCore.pyqtSlot()
    def on_btnChargeApplyPreset_clicked(self):
        profile = build_profile(
            self.ui.comboChargeChem.currentData(),
            self.ui.spinBoxChargeCells.value(),
            self.ui.spinBoxChargeCapacity.value() / 1000,
        )
        self.ui.spinBoxChargeCV.setValue(profile.cv_v)
        self.ui.spinBoxChargeCC.setValue(profile.cc_a)
        for spin, value in (
            (self.ui.spinBoxChargePrechargeV, profile.precharge_below_v),
            (self.ui.spinBoxChargePrechargeI, profile.precharge_a),
            (self.ui.spinBoxChargeCutoff, profile.cutoff_a),
            (self.ui.spinBoxChargeFloat, profile.float_v),
            (self.ui.spinBoxChargeNdv, profile.ndv_v),
            (self.ui.spinBoxChargeHoldoff, profile.ndv_holdoff_s),
        ):
            if value is not None:
                spin.setValue(value)

    def _charge_profile_from_ui(self) -> ChargeProfile:
        chem = self.ui.comboChargeChem.currentData()
        has_precharge = chem in (CHEM_LI_ION, CHEM_LIFEPO4)
        is_nimh = chem == CHEM_NIMH
        use_float = chem == CHEM_LEAD_ACID and self.ui.checkBoxChargeFloat.isChecked()
        return ChargeProfile(
            chemistry=chem,
            cv_v=self.ui.spinBoxChargeCV.value(),
            cc_a=self.ui.spinBoxChargeCC.value(),
            precharge_below_v=self.ui.spinBoxChargePrechargeV.value() if has_precharge else None,
            precharge_a=self.ui.spinBoxChargePrechargeI.value() if has_precharge else None,
            cutoff_a=None if is_nimh else self.ui.spinBoxChargeCutoff.value(),
            float_v=self.ui.spinBoxChargeFloat.value() if use_float else None,
            ndv_v=self.ui.spinBoxChargeNdv.value() if is_nimh else None,
            ndv_holdoff_s=self.ui.spinBoxChargeHoldoff.value() if is_nimh else None,
            max_s=(
                self.ui.spinBoxChargeMaxTime.value() * 60.0
                if self.ui.checkBoxChargeMaxTime.isChecked()
                else None
            ),
            max_ah=(
                self.ui.spinBoxChargeMaxAh.value() / 1000
                if self.ui.checkBoxChargeMaxAh.isChecked()
                else None
            ),
            max_wh=(
                self.ui.spinBoxChargeMaxWh.value()
                if self.ui.checkBoxChargeMaxWh.isChecked()
                else None
            ),
        )

    def _charge_profile_valid(self, profile: ChargeProfile) -> bool:
        i_max = self.ui.spinBoxCurrent.maximum()
        currents = [profile.cc_a]
        if profile.precharge_a is not None:
            currents.append(profile.precharge_a)
        voltages = [profile.cv_v]
        if profile.float_v is not None:
            voltages.append(profile.float_v)
        return all(0 < a <= i_max for a in currents) and all(0 < v <= 30 for v in voltages)

    @QtCore.pyqtSlot()
    def on_btnCharge_clicked(self):
        if self._charge_active:
            self._finish_charge(REASON_USER_STOP)
            return
        if self.api is None or self.locked or self._any_aux_active():
            self._flash_charge_button(self.tr("无法启动"))
            return
        profile = self._charge_profile_from_ui()
        if not self._charge_profile_valid(profile):
            self._flash_charge_button(self.tr("非法参数"))
            return
        self._charge_controller = ChargeController(profile)
        self._charge_acc = CapacityAccumulator()
        self._charge_last_vi = None
        self._charge_enable_t = math.inf
        self._charge_start_t = time.perf_counter()
        setpoint = self._charge_controller.start(self._charge_start_t)
        self.ui.scrollAreaCharge.setEnabled(False)
        self.ui.spinBoxVoltage.setEnabled(False)
        self.ui.spinBoxCurrent.setEnabled(False)
        self.ui.btnCharge.setText(self.tr("停止充电"))
        self.ui.labelChargeReason.setText("")
        self._update_charge_labels()
        self.v_set = setpoint.v_set
        self.i_set = setpoint.i_set
        self.wait_output_stable(self._start_charge_timer)
        # The output is commanded on by now; only samples from here on count.
        self._charge_enable_t = time.perf_counter()

    def _start_charge_timer(self):
        if self._charge_active:
            self.charge_timer.start(_CHARGE_TICK_MS)

    def _on_raw_batch(self, raw_rtvalues, t1):
        if not self._charge_active or not raw_rtvalues:
            return
        if t1 >= self._charge_enable_t:
            self._charge_acc.add_batch(raw_rtvalues, t1)
            self._charge_last_vi = raw_rtvalues[-1]

    def _extra_channel_values(self, len_):
        # Held flat at the running (or, between runs, final) total so the
        # graph keeps its last reading after the run ends.
        acc = self._charge_acc
        return {
            "ah": np.full(len_, acc.ah if acc is not None else 0.0),
            "wh": np.full(len_, acc.wh if acc is not None else 0.0),
        }

    def has_charge_data(self) -> bool:
        return self._charge_acc is not None

    def clear_aux_data(self):
        if not self._charge_active:
            self._charge_acc = None

    def _update_charge_labels(self):
        acc = self._charge_acc
        elapsed = int(time.perf_counter() - self._charge_start_t)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self.ui.labelChargeElapsed.setText(f"{h:02d}:{m:02d}:{s:02d}")
        self.ui.labelChargeAh.setText(f"{acc.ah * 1000:.0f} mAh")
        self.ui.labelChargeWh.setText(f"{acc.wh:.3f} Wh")
        self.ui.labelChargePhase.setText(
            self._charge_phase_text(self._charge_controller.phase)
        )

    def _charge_tick(self):
        if not self._charge_active:
            return
        self._update_charge_labels()
        if self.output_state_str == "off":
            self._finish_charge(REASON_OUTPUT_OFF)
            return
        if self._err_flag != 0:
            self._finish_charge(REASON_DEVICE_ERROR)
            return
        if self._charge_last_vi is None:
            return
        v, i = self._charge_last_vi
        action = self._charge_controller.update(
            time.perf_counter(), v, i, self.output_state_str, self._charge_acc
        )
        if isinstance(action, ChargeFinished):
            self._finish_charge(action.reason)
        elif isinstance(action, ChargeSetpoint):
            self.v_set = action.v_set
            self.i_set = action.i_set
            self._update_charge_labels()

    def _finish_charge(self, reason: str):
        self.charge_timer.stop()
        if self._stable_callback == self._start_charge_timer:
            self.stable_checker_timer.stop()
            self._stable_callback = None
        self._update_charge_labels()
        self._charge_controller = None
        self.output_state = False
        self.ui.scrollAreaCharge.setEnabled(True)
        self.ui.spinBoxVoltage.setEnabled(not self.locked)
        self.ui.spinBoxCurrent.setEnabled(not self.locked)
        self.ui.btnCharge.setText(self.tr("开始充电"))
        self.ui.labelChargePhase.setText(self._charge_phase_text(PHASE_DONE))
        self.ui.labelChargeReason.setText(self._charge_reason_text(reason))

    def _charge_phase_text(self, phase: str) -> str:
        return {
            PHASE_PRECHARGE: self.tr("预充"),
            PHASE_CC: self.tr("恒流"),
            PHASE_CV: self.tr("恒压"),
            PHASE_FLOAT: self.tr("浮充"),
            PHASE_DONE: self.tr("已结束"),
        }[phase]

    def _charge_reason_text(self, reason: str) -> str:
        return {
            REASON_CUTOFF: self.tr("电流已降至截止电流"),
            REASON_NDV: self.tr("检测到-ΔV"),
            REASON_CEILING: self.tr("已达到电压上限"),
            REASON_TIMEOUT: self.tr("已达到最大时长"),
            REASON_MAX_AH: self.tr("已达到最大容量"),
            REASON_MAX_WH: self.tr("已达到最大能量"),
            REASON_OUTPUT_OFF: self.tr("输出已关闭"),
            REASON_DEVICE_ERROR: self.tr("设备错误"),
            REASON_USER_STOP: self.tr("用户停止"),
            REASON_DISCONNECTED: self.tr("已断开连接"),
        }[reason]

    @QtCore.pyqtSlot()
    def on_btnChargeSave_clicked(self):
        if self._charge_acc is None or not self._charge_acc.rows:
            CustomMessageBox(self, self.tr("错误"), self.tr("充电记录为空"))
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.tr("保存"), "", self.tr("CSV文件 (*.csv)")
        )
        if not filename:
            return
        rd = RecordData(CHARGE_CSV_CHANNELS)
        for row in self._charge_acc.rows:
            rd.add_values(
                {
                    "voltage": row.voltage,
                    "current": row.current,
                    "power": row.power,
                    "ah": row.ah,
                    "wh": row.wh,
                },
                row.elapsed,
            )
        rd.to_csv(filename)


DEVICE_PANEL_TYPES["P906"] = P906DevicePanel
