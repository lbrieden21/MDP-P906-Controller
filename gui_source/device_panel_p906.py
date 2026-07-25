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

from app_context import DEBUG, FPSCounter, set_color
from device_core import ChannelSpec, RecordData
from device_panel import DEVICE_PANEL_TYPES, DevicePanelBase
from mdp_controller import MDP_P906
from mdp_custom import CustomInputDialog, CustomMessageBox
from mdp_gui_template import Ui_DevicePanelP906
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


class P906DevicePanel(DevicePanelBase):
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
        self.open_r = OPEN_R
        self._temp_f = 0.0
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
        # QTabWidget sizes itself to its largest tab (Battery Sim/Sequence are
        # much taller than Preset), which otherwise reserves that much room
        # even while a short tab is showing and starves the L1060 panel's own
        # aux area, which is stacked in the same column. Capping this leaves
        # enough of the shared vertical budget for L1060's Presets tab to fit
        # without scrolling; P906's own shorter tabs (like Preset) now rely on
        # their existing internal scroll areas the same way its taller tabs
        # already did.
        self.ui.tabWidget.setMaximumHeight(210)

        self.set_interp(setting.ui.interp)
        self.refresh_preset()
        self.get_preset("1")
        self.close_state_ui()
        self.load_battery_model(
            os.path.join(os.path.dirname(__file__), "Li-ion.csv")
        )
        # qdarktheme's global stylesheet (applied once, at startup, after this
        # constructor runs) clobbers the .ui-set minimumSize on QLCDNumber
        # widgets. Re-assert it once the event loop is actually running (show()
        # + first layout pass done) so the digits aren't squeezed unreadably
        # thin in the narrower per-device panel column.
        QtCore.QTimer.singleShot(0, self._enforce_lcd_min_width)

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
        self.stable_checker_timer = QtCore.QTimer(self)
        self.stable_checker_timer.timeout.connect(self.stable_checker)

    def _init_combos(self):
        self.ui.comboSweepRecord.setItemData(0, None)
        for idx, ch in enumerate(CHANNELS):
            self.ui.comboSweepRecord.setItemData(idx + 1, ch.key)
        self.ui.comboSweepTarget.setItemData(0, "voltage")
        self.ui.comboSweepTarget.setItemData(1, "current")

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
        ) = self.api.get_status()
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
        self.ui.spinBoxVoltage.setEnabled(not self.locked)
        self.ui.spinBoxCurrent.setEnabled(not self.locked)
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
                self.store.series["voltage"][self.store.update_count - 1] - self.v_set
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

    def close_state_ui(self, record_disconnect: bool = False):
        self.ui.labelLinkState.setText(self.tr("未连接"))
        set_color(self.ui.labelLinkState, None)
        self.ui.frameOutputSetting.setEnabled(False)
        self.ui.frameSystemState.setEnabled(False)
        self.ui.progressBarCurrent.setValue(0)
        self.ui.progressBarVoltage.setValue(0)
        if record_disconnect:
            # Marks the graph history with an explicit drop to (0, 0) right
            # before unlink()'s mark_gap() breaks the line, so a reviewed
            # chart shows the device's output actually falling away instead
            # of flat-lining at its last real reading. Not wanted here on
            # the plain init call - there's no real reading to mark as lost
            # yet, and it would otherwise permanently seed this panel's
            # buffer with one sample even if it's never linked all session.
            self.state_callback([(0, 0)])
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
        for widget in [
            self.ui.lcdVoltage,
            self.ui.lcdCurrent,
            self.ui.lcdResistence,
            self.ui.lcdPower,
            self.ui.lcdEnerge,
        ]:
            widget.display("")

    def open_state_ui(self):
        self.ui.labelLinkState.setText(self.tr("已连接"))
        set_color(self.ui.labelLinkState, setting.get_color("general_green"))
        self.ui.frameOutputSetting.setEnabled(True)
        self.ui.frameSystemState.setEnabled(True)

    @QtCore.pyqtSlot()
    def on_btnLink_clicked(self):
        self.link_toggle_requested.emit()

    def _enforce_lcd_min_width(self):
        for lcd in (
            self.ui.lcdVoltage,
            self.ui.lcdCurrent,
            self.ui.lcdPower,
            self.ui.lcdEnerge,
            self.ui.lcdResistence,
        ):
            lcd.setMinimumWidth(130)

    def apply_theme(self):
        set_color(self.ui.lcdVoltage, setting.get_color("lcd_voltage"))
        set_color(self.ui.lcdCurrent, setting.get_color("lcd_current"))
        set_color(self.ui.lcdPower, setting.get_color("lcd_power"))
        set_color(self.ui.lcdEnerge, setting.get_color("lcd_energy"))
        set_color(self.ui.labelTemperature, setting.get_color("lcd_temperature"))
        set_color(self.ui.lcdResistence, setting.get_color("lcd_resistance"))
        self._enforce_lcd_min_width()

    def refresh_led_color(self):
        if self.api is None:
            return
        color_rgb = bytes.fromhex(self.settings.color.lstrip("#"))
        self.api.set_led_color((color_rgb[0], color_rgb[1], color_rgb[2]))

    def link(self, bus, pipe=0, fps=50, session_start_time=None):
        if not self.settings.idcode:
            raise ValueError(self.tr("IDCODE为空, 请先完成连接设置"))
        color_rgb = bytes.fromhex(self.settings.color.lstrip("#"))
        api = MDP_P906(
            bus,
            idcode=self.settings.idcode,
            blink=self.settings.blink,
            led_color=(color_rgb[0], color_rgb[1], color_rgb[2]),
            m01_channel=int(self.settings.m01ch[3]),
            debug=DEBUG,
        )
        try:
            bus.attach(api, pipe)
            api.connect(timeout=8.0)
        except Exception:
            api.close()
            raise
        self.api = api
        self.api.register_realtime_value_callback(self.state_callback)
        t = time.perf_counter()
        self._last_state_change_t = t
        self.store.start_time = t if session_start_time is None else session_start_time
        self.store.eng_start_time = t
        self.store.last_time = t
        self.store.energy = 0
        self.continuous_energy_counter = 0
        self.data_fps = fps
        self.fps_counter.clear()
        self.update_state_timer.start(100)
        self.state_request_sender_timer.start(round(1000 / fps))
        self.state_lcd_timer.start(round(1000 / min(fps, setting.ui.state_fps)))
        self.linked = True
        self.update_state()
        self.open_state_ui()

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
        self.close_state_ui(record_disconnect=True)
        self.link_state_changed.emit()

    def request_state(self):
        if self.api is not None:
            self.api.request_realtime_value()

    def start_record(self):
        self.record_data = RecordData(RECORD_CHANNELS)
        self.record_flag = True

    def stop_record(self):
        self.record_flag = False
        data = self.record_data
        self.record_data = None
        return data

    def state_callback(self, rtvalues: List[Tuple[float, float]]):
        vt, it = self.settings.v_threshold, self.settings.i_threshold
        if self.settings.cali.use:
            rtvalues = [
                (
                    v * self.settings.cali.v_k + self.settings.cali.v_b,
                    i * self.settings.cali.i_k + self.settings.cali.i_b,
                )
                for v, i in rtvalues
            ]
        len_ = len(rtvalues)
        rtvalues = [(v if v > vt else 0.0, i if i > it else 0.0) for v, i in rtvalues]
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
        eng = self.store.append(raw_rtvalues, values, len_, t1)
        self.continuous_energy_counter += eng
        self.fps_counter.tick()

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
        if iavg >= 0.002:  # 致敬P906的愚蠢adc
            resistance = vavg / iavg
        else:
            resistance = self.open_r
        r_text = f"{resistance:.2f}" if resistance < self.open_r / 100 else "--"
        self.ui.lcdVoltage.display(f"{vavg:.3f}")
        self.ui.lcdCurrent.display(f"{iavg:.3f}")
        self.ui.lcdResistence.display(r_text)
        self.ui.lcdPower.display(f"{power:.3f}")
        self.values_signal.emit(vavg, iavg, power)
        v_value = round(vavg / self.v_set * 1000) if self.v_set != 0 else 0
        i_value = round(iavg / self.i_set * 1000) if self.i_set != 0 else 0
        self.ui.progressBarVoltage.setValue(min(v_value, 1000))
        self.ui.progressBarCurrent.setValue(min(i_value, 1000))
        self.ui.progressBarVoltage.update()
        self.ui.progressBarCurrent.update()

    def set_interp(self, interp):
        self.ui.lcdVoltage.setDigitCount(6)
        self.ui.lcdCurrent.setDigitCount(6)
        self.ui.lcdResistence.setDigitCount(8)
        self.ui.lcdPower.setDigitCount(6)
        self.ui.lcdEnerge.setDigitCount(6 + interp)

    def set_data_fps(self, fps):
        self.data_fps = fps
        if self.state_request_sender_timer.isActive():
            self.state_request_sender_timer.stop()
            self.state_request_sender_timer.start(round(1000 / fps))
        if self.state_lcd_timer.isActive():
            self.state_lcd_timer.stop()
            self.state_lcd_timer.start(round(1000 / min(fps, setting.ui.state_fps)))
        self.fps_counter.clear()

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
                self.store.series[self._sweep_target][self.store.update_count - 1]
            )
            self._sweep_response_data_y.append(
                self.store.series[self._sweep_response_type][self.store.update_count - 1]
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
                self.store.series["power"][self.store.update_count - 1]
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
            * self.store.series["current"][self.store.update_count - 1]
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
        if cnt == 0:
            return
        self.seq_btn_disable()
        self.start_seq(loop=False)
        if not self.output_state:
            self.ui.btnOutput.click()

    @QtCore.pyqtSlot()
    def on_btnSeqLoop_clicked(self):
        cnt = self.ui.listSeq.count()
        if cnt == 0:
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


DEVICE_PANEL_TYPES["P906"] = P906DevicePanel
