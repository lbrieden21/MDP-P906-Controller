import datetime
import time

import numpy as np
from loguru import logger
from PyQt5 import QtCore, QtGui, QtWidgets

from app_context import FPSCounter, set_color
from device_core import ChannelSpec, RecordData
from device_panel import (
    BASE_CHANNEL_SHORT,
    BASE_CHANNELS,
    DEVICE_PANEL_TYPES,
    OPEN_R,
    RECORD_CHANNELS,
    DevicePanelBase,
)
from battery_aux import BelowThresholdDebounce, CapacityAccumulator
from l1060_aux import (
    DelayAction,
    SetAction,
    WaitAction,
    build_sweep_targets,
    format_delay_action,
    format_set_action,
    format_wait_action,
    parse_sequence_line,
    parse_sequence_lines,
)
from mdp_controller import MDP_L1060
from mdp_custom import CustomInputDialog, CustomMessageBox
from mdp_gui_template import Ui_DevicePanelL1060
from settings_model import SETTING_FILE, setting

CHANNELS = BASE_CHANNELS + [
    # Discharge-workflow running totals (battery_aux.CapacityAccumulator).
    # Zero/flat outside an active discharge run, held at the last run's
    # final value in between -- included as ordinary store channels (rather
    # than a bespoke plot) so the main window's Ah/Wh graph buttons get the
    # exact same scrolling-window/slider behavior every other channel has.
    ChannelSpec("ah", QtCore.QCoreApplication.translate("MDPMainwindow", "安时"), "Ah"),
    ChannelSpec("wh", QtCore.QCoreApplication.translate("MDPMainwindow", "瓦时"), "Wh"),
    # Sweep-workflow commanded target (l1060_aux.build_sweep_targets). Held
    # flat at the currently commanded target while a sweep runs, at the last
    # run's final target in between -- same rationale as ah/wh above, and
    # this is the x-axis series for the main window's Sweep graph block.
    ChannelSpec(
        "sweep_target", QtCore.QCoreApplication.translate("MDPMainwindow", "扫描目标"), ""
    ),
]
CHANNEL_BY_KEY = {c.key: c for c in CHANNELS}
DISCHARGE_CHANNELS = [
    CHANNEL_BY_KEY["voltage"],
    CHANNEL_BY_KEY["current"],
    CHANNEL_BY_KEY["power"],
    CHANNEL_BY_KEY["ah"],
    CHANNEL_BY_KEY["wh"],
]
# sweep_target deliberately has no short entry -- it labels the Sweep
# block's x-axis, which never uses the short form.
CHANNEL_SHORT = {**BASE_CHANNEL_SHORT, "ah": "Ah", "wh": "Wh"}

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
    "CC": (0.001, 9.999, 0.001),
    "CV": (0.0, 30.0, 0.001),
    "CR": (0.01, 999.999, 0.1),
    "CP": (0.0, 999.999, 0.1),
}
_PROTECTION_LABELS = {
    "OVP": "LATCHED: OVP",
    "OCP": "LATCHED: OCP",
    "OPP_OR_UVP": "LATCHED: OPP or UVP (indistinguishable)",
}
_AUX_LEAVE_ON_CHECKBOX = {
    "sweep": "checkBoxSweepLeaveOn",
    "discharge": "checkBoxDischargeLeaveOn",
    "sequence": "checkBoxSequenceLeaveOn",
}
_SWEEP_RESPONSE_KEYS = ["voltage", "current", "power", "resistance"]
_SWEEP_INPUT_WIDGETS = (
    "comboSweepMode",
    "spinBoxSweepStart",
    "spinBoxSweepStop",
    "spinBoxSweepStep",
    "spinBoxSweepDwell",
    "comboSweepResponse",
)
_SEQUENCE_INPUT_WIDGETS = (
    "listSequence",
    "btnSequenceDelay",
    "btnSequenceWait",
    "btnSequenceSetCC",
    "btnSequenceSetCV",
    "btnSequenceSetCR",
    "btnSequenceSetCP",
    "btnSequenceSave",
    "btnSequenceLoad",
    "btnSequenceSingle",
    "btnSequenceLoop",
)
_DISCHARGE_INPUT_WIDGETS = (
    "comboDischargeMode",
    "spinBoxDischargeTarget",
    "spinBoxDischargeCutoff",
    "checkBoxDischargeMaxAh",
    "checkBoxDischargeMaxWh",
    "checkBoxDischargeMaxDuration",
)
# Each optional limit's own spinbox is only enabled while its checkbox is
# checked -- both at rest (via the checkbox's toggled slot) and when
# _set_discharge_inputs_enabled() re-enables the tab after a run ends.
_DISCHARGE_LIMIT_SPINBOX = {
    "checkBoxDischargeMaxAh": "spinBoxDischargeMaxAh",
    "checkBoxDischargeMaxWh": "spinBoxDischargeMaxWh",
    "checkBoxDischargeMaxDuration": "spinBoxDischargeMaxDuration",
}
# Polling cadence for live elapsed/Ah/Wh readouts and termination checks.
# Full-rate Ah/Wh integration itself happens in state_callback(), not here.
_DISCHARGE_TICK_MS = 200
# Polling cadence for Delay/Wait boundaries and cross-mode SET writes -- not
# the P906 1 ms busy timer (see l1060_workflows_plan.md Phase 3).
_SEQUENCE_TICK_MS = 50


class L1060DevicePanel(DevicePanelBase):
    api_class = MDP_L1060
    record_channels = RECORD_CHANNELS

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
        self.fps_counter = FPSCounter()
        self._temp_f = 0.0
        self._load_commanded_on = False
        self._protection_latched = False
        # Auxiliary-workflow run coordinator (Sweep/Discharge/Sequence, added
        # in later phases). _active_aux is None or one of
        # _AUX_LEAVE_ON_CHECKBOX's keys. _aux_start_widgets is populated by
        # each workflow's own init as its Start button is wired up, so
        # _begin_aux can disable every *other* tool's Start control without
        # this coordinator needing to know about tools that don't exist yet.
        self._active_aux = None
        self._aux_start_widgets = {}
        self._sweep_started = False
        self._sweep_last_target = 0.0
        self._sweep_target_unit = ""
        self._seq_actions = []
        self._seq_index = 0
        self._seq_loop = False
        self._seq_delay_deadline = 0.0
        self._discharge_acc = None
        self._discharge_cutoff_debounce = None
        self._discharge_cutoff = 0.0
        self._discharge_max_ah = None
        self._discharge_max_wh = None
        self._discharge_max_duration = None
        self._discharge_armed = False
        self._discharge_last_voltage = None
        self._discharge_enable_rel = 0.0

        self._init_timers()
        self._init_mode_buttons()
        self._init_signals()
        self._init_aux_combos()
        self.register_aux_start_widget("sweep", self.ui.btnSweepRun)
        self.register_aux_start_widget("sequence", self.ui.btnSequenceSingle)
        self.register_aux_start_widget("sequence", self.ui.btnSequenceLoop)
        self.register_aux_start_widget("discharge", self.ui.btnDischargeRun)
        self.ui.btnSequenceStop.hide()

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

    def _init_timers(self):
        self.state_request_sender_timer = QtCore.QTimer(self)
        self.state_request_sender_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.state_request_sender_timer.timeout.connect(self.request_state)
        self.update_state_timer = QtCore.QTimer(self)
        self.update_state_timer.timeout.connect(self.update_state)
        self.target_poll_timer = QtCore.QTimer(self)
        self.target_poll_timer.timeout.connect(self.request_targets)
        self.state_lcd_timer = QtCore.QTimer(self)
        self.state_lcd_timer.timeout.connect(self.update_state_lcd)
        self.sweep_timer = QtCore.QTimer(self)
        self.sweep_timer.setSingleShot(True)
        self.sweep_timer.timeout.connect(self._sweep_dwell_elapsed)
        self.sequence_timer = QtCore.QTimer(self)
        self.sequence_timer.timeout.connect(self._sequence_tick)
        self.discharge_timer = QtCore.QTimer(self)
        self.discharge_timer.timeout.connect(self._discharge_tick)

    def _init_aux_combos(self):
        for idx, key in enumerate(_SWEEP_RESPONSE_KEYS):
            self.ui.comboSweepResponse.setItemData(idx, key)
        # Qt's connectSlotsByName only wires these signal handlers up after
        # setupUi() has already set the combos' initial index -- that first,
        # designer-time selection never reaches on_combo*_currentTextChanged,
        # so the Start/Stop/Target spinboxes are left with the .ui file's raw
        # designer ranges (and, for Start, its equally raw 0.000 default
        # value) instead of the selected mode's real bounds. Call both once
        # here to apply the correct range/value for whichever mode is
        # initially selected.
        self.on_comboSweepMode_currentTextChanged(self.ui.comboSweepMode.currentText())
        self.on_comboDischargeMode_currentTextChanged(
            self.ui.comboDischargeMode.currentText()
        )

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
        if not self.api.set_load_on(False):
            # Off never confirmed -- the load may still be live. Leave the
            # interlock engaged; the staged mode/targets apply on the next
            # confirmed off.
            return
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

    def _apply_mode_target(self, mode: str, value: float) -> bool:
        """Apply a workflow-commanded mode/target change. Sweep/Sequence/
        Discharge all route their mode/target writes through here so they
        share the same confirmed off/change/on interlock on_mode_changed
        uses for manual UI edits, instead of each reimplementing it (and
        risking a live mode change that fights the firmware). Returns
        whether the change was applied/confirmed; False means the run
        driving this call should stop rather than continue on stale state.
        """
        self.settings.l1060_targets[mode] = value
        spin = getattr(self.ui, _TARGET_SPINBOX[mode])
        if not spin.hasFocus():
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        if self.api is None:
            return False
        same_mode = mode == self.settings.l1060_mode
        if not self._load_commanded_on:
            if not same_mode:
                self.api.select_mode(mode)
                self.settings.l1060_mode = mode
                self._sync_mode_ui(mode)
            self._push_target(mode, value)
            return True
        if same_mode:
            self._push_target(mode, value)
            return True
        # Mode must change under a live load -- cycle load off, flush the
        # now-current targets (including this one), then back on, mirroring
        # on_mode_changed's manual-UI interlock.
        if not self.api.set_load_on(False):
            return False
        self._load_commanded_on = False
        self._flush_pending_targets(mode)
        self.settings.l1060_mode = mode
        self._sync_mode_ui(mode)
        ok = self.api.set_load_on(True)
        self._load_commanded_on = ok
        if not ok:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)
        return ok

    def _confirmed_load_off(self) -> bool:
        if self.api is None:
            return False
        ok = self.api.set_load_on(False)
        if ok:
            self._load_commanded_on = False
            self._flush_pending_targets(self.settings.l1060_mode)
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)
        return ok

    ######### Auxiliary Functions - Run Coordinator #########

    def register_aux_start_widget(self, name: str, widget: QtWidgets.QWidget):
        """Called by each workflow (Sweep/Discharge/Sequence) once its own
        Start control(s) exist, so _begin_aux can disable every other tool's
        Start button(s) during a run without hardcoding widget names here.
        A tool may register more than one Start widget (e.g. Sequence's
        Single and Loop buttons)."""
        self._aux_start_widgets.setdefault(name, []).append(widget)

    def _set_core_controls_enabled(self, enabled: bool):
        self.ui.comboPreset.setEnabled(enabled)
        for name in _MODE_BUTTON.values():
            getattr(self.ui, name).setEnabled(enabled)
        for name in _TARGET_SPINBOX.values():
            getattr(self.ui, name).setEnabled(enabled)

    def _begin_aux(self, name: str) -> bool:
        """Claim the run coordinator for auxiliary tool `name`. Returns False
        (and changes nothing) if the panel isn't ready, protection is
        latched, or another tool is already running -- callers must not
        start their timer/state machine unless this returns True."""
        if self.api is None or not self.linked or self._protection_latched:
            return False
        if self._active_aux is not None:
            return False
        self._active_aux = name
        self._set_core_controls_enabled(False)
        for other, widgets in self._aux_start_widgets.items():
            if other != name:
                for widget in widgets:
                    widget.setEnabled(False)
        return True

    def _finish_aux(self, name: str, leave_on: bool):
        """Release the run coordinator and apply the final load-state
        policy: leave_on comes from the tool's own checkbox on normal
        completion/Stop, or False when startup failed before the load was
        ever confirmed on."""
        if self._active_aux != name:
            return
        self._active_aux = None
        self._set_core_controls_enabled(True)
        for widgets in self._aux_start_widgets.values():
            for widget in widgets:
                widget.setEnabled(True)
        if not leave_on:
            self._confirmed_load_off()

    def _aux_leave_on(self, name: str) -> bool:
        return getattr(self.ui, _AUX_LEAVE_ON_CHECKBOX[name]).isChecked()

    def _stop_aux_for_fault(self):
        """Stop whichever auxiliary tool is running because protection just
        latched. leave_on is always False here regardless of the tool's own
        checkbox -- a latch is not a normal completion/Stop."""
        if self._active_aux == "sweep":
            self._finish_sweep(leave_on=False)
        elif self._active_aux == "sequence":
            self._finish_sequence(leave_on=False)
        elif self._active_aux == "discharge":
            self._finish_discharge(leave_on=False, reason=self.tr("保护锁存"))

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

    ######### Auxiliary Functions - Sweep #########

    @QtCore.pyqtSlot(str)
    def on_comboSweepMode_currentTextChanged(self, mode: str):
        if mode not in _MODE_RANGE:
            return
        lo, hi, step = _MODE_RANGE[mode]
        unit = _MODE_UNIT[mode]
        for name in ("spinBoxSweepStart", "spinBoxSweepStop"):
            spin = getattr(self.ui, name)
            spin.setSuffix(unit)
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
        step_spin = self.ui.spinBoxSweepStep
        step_spin.setSuffix(unit)
        step_spin.setRange(0.001, hi - lo)
        step_spin.setSingleStep(step)

    def _set_sweep_inputs_enabled(self, enabled: bool):
        for name in _SWEEP_INPUT_WIDGETS:
            getattr(self.ui, name).setEnabled(enabled)

    @QtCore.pyqtSlot()
    def on_btnSweepRun_clicked(self):
        if self._active_aux == "sweep":
            self._finish_sweep(self._aux_leave_on("sweep"))
            return
        mode = self.ui.comboSweepMode.currentText()
        lo, hi, _ = _MODE_RANGE[mode]
        start = self.ui.spinBoxSweepStart.value()
        stop = self.ui.spinBoxSweepStop.value()
        step = self.ui.spinBoxSweepStep.value()
        try:
            targets = build_sweep_targets(start, stop, step, lo, hi)
        except ValueError:
            self.ui.btnSweepRun.setText(self.tr("非法参数"))
            QtCore.QTimer.singleShot(
                1000, lambda: self.ui.btnSweepRun.setText(self.tr("开始扫描"))
            )
            return
        if not self._begin_aux("sweep"):
            return
        self._sweep_mode = mode
        self._sweep_targets = targets
        self._sweep_index = 0
        self._sweep_dwell = self.ui.spinBoxSweepDwell.value()
        self._sweep_response_key = self.ui.comboSweepResponse.currentData()
        self._sweep_started = True
        self._sweep_last_target = targets[0]
        self._sweep_target_unit = _MODE_UNIT[mode]
        self._set_sweep_inputs_enabled(False)
        self.ui.btnSweepRun.setText(self.tr("停止扫描"))
        if not self._apply_mode_target(mode, targets[0]) or not self._confirm_load_on():
            self._finish_sweep(leave_on=False)
            return
        self._arm_sweep_dwell()

    def _confirm_load_on(self) -> bool:
        if self._load_commanded_on:
            return True
        ok = self.api.set_load_on(True)
        self._load_commanded_on = ok
        self.ui.btnLoadOn.blockSignals(True)
        self.ui.btnLoadOn.setChecked(ok)
        self.ui.btnLoadOn.blockSignals(False)
        return ok

    def _arm_sweep_dwell(self):
        self.sweep_timer.start(max(1, round(self._sweep_dwell * 1000)))

    def _sweep_dwell_elapsed(self):
        if self._active_aux != "sweep":
            return
        self._sweep_index += 1
        if self._sweep_index >= len(self._sweep_targets):
            self._finish_sweep(leave_on=self._aux_leave_on("sweep"))
            return
        target = self._sweep_targets[self._sweep_index]
        if not self._apply_mode_target(self._sweep_mode, target):
            self._finish_sweep(leave_on=False)
            return
        self._sweep_last_target = target
        self._arm_sweep_dwell()

    def _finish_sweep(self, leave_on: bool):
        self.sweep_timer.stop()
        self._set_sweep_inputs_enabled(True)
        self.ui.btnSweepRun.setText(self.tr("开始扫描"))
        self._finish_aux("sweep", leave_on)

    def has_sweep_data(self) -> bool:
        return self._sweep_started

    def _clear_sweep_data(self):
        if self._active_aux != "sweep":
            self._sweep_started = False
            self._sweep_last_target = 0.0
            self._sweep_target_unit = ""

    def clear_aux_data(self):
        self._clear_discharge_data()
        self._clear_sweep_data()

    ######### Auxiliary Functions - Sequence #########

    def _set_sequence_inputs_enabled(self, enabled: bool):
        for name in _SEQUENCE_INPUT_WIDGETS:
            getattr(self.ui, name).setEnabled(enabled)

    def _sequence_insert_row(self) -> int:
        row = self.ui.listSequence.currentRow()
        return row if row != -1 else self.ui.listSequence.count() - 1

    def _sequence_insert_text(self, text: str):
        row = self._sequence_insert_row()
        self.ui.listSequence.insertItem(row + 1, text)
        self.ui.listSequence.setCurrentRow(row + 1)

    @QtCore.pyqtSlot()
    def on_btnSequenceDelay_clicked(self):
        ms, ok = CustomInputDialog.getInt(
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
        self._sequence_insert_text(format_delay_action(ms))

    @QtCore.pyqtSlot()
    def on_btnSequenceWait_clicked(self):
        wait_time, ok = CustomInputDialog.getDateTime(
            self,
            self.tr("添加动作"),
            self.tr("请输入等待时间:") + "\n" + self.tr("格式: 年-月-日 时:分:秒"),
        )
        if not ok:
            return
        self._sequence_insert_text(format_wait_action(wait_time.toPyDateTime()))

    def _add_sequence_set_action(self, mode: str):
        lo, hi, step = _MODE_RANGE[mode]
        unit = _MODE_UNIT[mode]
        value, ok = CustomInputDialog.getDouble(
            self,
            self.tr("添加动作"),
            self.tr("请输入目标值:"),
            default_value=self.settings.l1060_targets[mode],
            min_value=lo,
            max_value=hi,
            decimals=3,
            step=step,
            suffix=unit,
        )
        if not ok:
            return
        self._sequence_insert_text(format_set_action(mode, value, unit))

    @QtCore.pyqtSlot()
    def on_btnSequenceSetCC_clicked(self):
        self._add_sequence_set_action("CC")

    @QtCore.pyqtSlot()
    def on_btnSequenceSetCV_clicked(self):
        self._add_sequence_set_action("CV")

    @QtCore.pyqtSlot()
    def on_btnSequenceSetCR_clicked(self):
        self._add_sequence_set_action("CR")

    @QtCore.pyqtSlot()
    def on_btnSequenceSetCP_clicked(self):
        self._add_sequence_set_action("CP")

    def _sequence_edit_current_item(self):
        row = self.ui.listSequence.currentRow()
        if row == -1:
            return
        item = self.ui.listSequence.item(row)
        try:
            action = parse_sequence_line(item.text(), _MODE_RANGE, _MODE_UNIT)
        except ValueError:
            CustomMessageBox(self, self.tr("错误"), self.tr("无法识别动作"))
            return
        if isinstance(action, DelayAction):
            ms, ok = CustomInputDialog.getInt(
                self,
                self.tr("编辑动作"),
                self.tr("请输入延时时间:"),
                default_value=action.ms,
                min_value=0,
                max_value=100000,
                step=1,
                suffix="ms",
            )
            if not ok:
                return
            item.setText(format_delay_action(ms))
        elif isinstance(action, WaitAction):
            wait_time, ok = CustomInputDialog.getDateTime(
                self,
                self.tr("编辑动作"),
                self.tr("请输入等待时间:"),
                default_value=QtCore.QDateTime(action.at),
            )
            if not ok:
                return
            item.setText(format_wait_action(wait_time.toPyDateTime()))
        elif isinstance(action, SetAction):
            mode = action.mode
            lo, hi, step = _MODE_RANGE[mode]
            unit = _MODE_UNIT[mode]
            value, ok = CustomInputDialog.getDouble(
                self,
                self.tr("编辑动作"),
                self.tr("请输入目标值:"),
                default_value=action.value,
                min_value=lo,
                max_value=hi,
                decimals=3,
                step=step,
                suffix=unit,
            )
            if not ok:
                return
            item.setText(format_set_action(mode, value, unit))

    def _sequence_delete_current_item(self):
        row = self.ui.listSequence.currentRow()
        if row == -1:
            return
        self.ui.listSequence.takeItem(row)
        self.ui.listSequence.setCurrentRow(min(row, self.ui.listSequence.count() - 1))

    def _sequence_clear_all(self):
        if self.ui.listSequence.count() == 0:
            return
        if CustomMessageBox.question(
            self, self.tr("警告"), self.tr("确定要清空序列吗？")
        ):
            self.ui.listSequence.clear()

    @QtCore.pyqtSlot(QtCore.QPoint)
    def on_listSequence_customContextMenuRequested(self, pos):
        if self.ui.listSequence.currentRow() == -1:
            return
        menu = QtWidgets.QMenu()
        menu.addAction(self.tr("编辑"), self._sequence_edit_current_item)
        menu.addAction(self.tr("删除"), self._sequence_delete_current_item)
        menu.addAction(self.tr("清空"), self._sequence_clear_all)
        menu.exec_(QtGui.QCursor.pos())

    @QtCore.pyqtSlot(QtWidgets.QListWidgetItem)
    def on_listSequence_itemDoubleClicked(self, item):
        self._sequence_edit_current_item()

    @QtCore.pyqtSlot()
    def on_btnSequenceSave_clicked(self):
        if self.ui.listSequence.count() == 0:
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.tr("保存"), "", self.tr("文本文件 (*.txt)")
        )
        if not filename:
            return
        lines = [
            self.ui.listSequence.item(i).text()
            for i in range(self.ui.listSequence.count())
        ]
        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    @QtCore.pyqtSlot()
    def on_btnSequenceLoad_clicked(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.tr("打开"), "", self.tr("文本文件 (*.txt)")
        )
        if not filename:
            return
        with open(filename, "r", encoding="utf-8") as f:
            lines = [line for line in f.read().strip().split("\n") if line]
        try:
            parse_sequence_lines(lines, _MODE_RANGE, _MODE_UNIT)
        except ValueError:
            # One malformed line must not leave a partially loaded sequence
            # -- the whole file is validated before the list is touched.
            CustomMessageBox(self, self.tr("错误"), self.tr("序列文件包含非法动作"))
            return
        self.ui.listSequence.clear()
        self.ui.listSequence.addItems(lines)

    def _start_sequence(self, loop: bool):
        if self.ui.listSequence.count() == 0:
            return
        lines = [
            self.ui.listSequence.item(i).text()
            for i in range(self.ui.listSequence.count())
        ]
        try:
            actions = parse_sequence_lines(lines, _MODE_RANGE, _MODE_UNIT)
        except ValueError:
            CustomMessageBox(self, self.tr("错误"), self.tr("序列包含非法动作"))
            return
        if not self._begin_aux("sequence"):
            return
        self._seq_actions = actions
        self._seq_loop = loop
        self._seq_index = 0
        self._set_sequence_inputs_enabled(False)
        self.ui.btnSequenceStop.show()
        if not self._confirm_load_on():
            self._finish_sequence(leave_on=False)
            return
        self.sequence_timer.start(_SEQUENCE_TICK_MS)
        self._sequence_process()

    @QtCore.pyqtSlot()
    def on_btnSequenceSingle_clicked(self):
        self._start_sequence(loop=False)

    @QtCore.pyqtSlot()
    def on_btnSequenceLoop_clicked(self):
        self._start_sequence(loop=True)

    @QtCore.pyqtSlot()
    def on_btnSequenceStop_clicked(self):
        self._finish_sequence(leave_on=self._aux_leave_on("sequence"))

    def _sequence_process(self):
        """Advance through actions synchronously until landing on an
        unresolved Delay/Wait (left for _sequence_tick to poll) or the
        sequence ends. Consecutive SET actions apply back-to-back in the
        same call instead of one per timer tick."""
        while True:
            if self._seq_index >= len(self._seq_actions):
                if self._seq_loop:
                    self._seq_index = 0
                    continue
                self._finish_sequence(leave_on=self._aux_leave_on("sequence"))
                return
            action = self._seq_actions[self._seq_index]
            self.ui.listSequence.setCurrentRow(self._seq_index)
            if isinstance(action, DelayAction):
                self._seq_delay_deadline = time.perf_counter() + action.ms / 1000
                return
            if isinstance(action, WaitAction):
                return
            if not self._apply_mode_target(action.mode, action.value):
                self._finish_sequence(leave_on=False)
                return
            self._seq_index += 1

    def _sequence_tick(self):
        if self._active_aux != "sequence":
            return
        action = self._seq_actions[self._seq_index]
        if isinstance(action, DelayAction):
            if time.perf_counter() < self._seq_delay_deadline:
                return
        elif isinstance(action, WaitAction):
            if datetime.datetime.now() < action.at:
                return
        else:
            return
        self._seq_index += 1
        self._sequence_process()

    def _finish_sequence(self, leave_on: bool):
        self.sequence_timer.stop()
        self._set_sequence_inputs_enabled(True)
        self.ui.btnSequenceStop.hide()
        self._finish_aux("sequence", leave_on)

    ######### Auxiliary Functions - Discharge #########

    @QtCore.pyqtSlot(str)
    def on_comboDischargeMode_currentTextChanged(self, mode: str):
        if mode not in _MODE_RANGE:
            return
        lo, hi, step = _MODE_RANGE[mode]
        spin = self.ui.spinBoxDischargeTarget
        spin.setSuffix(_MODE_UNIT[mode])
        spin.setRange(lo, hi)
        spin.setSingleStep(step)

    @QtCore.pyqtSlot(bool)
    def on_checkBoxDischargeMaxAh_toggled(self, checked: bool):
        self.ui.spinBoxDischargeMaxAh.setEnabled(checked)

    @QtCore.pyqtSlot(bool)
    def on_checkBoxDischargeMaxWh_toggled(self, checked: bool):
        self.ui.spinBoxDischargeMaxWh.setEnabled(checked)

    @QtCore.pyqtSlot(bool)
    def on_checkBoxDischargeMaxDuration_toggled(self, checked: bool):
        self.ui.spinBoxDischargeMaxDuration.setEnabled(checked)

    def _set_discharge_inputs_enabled(self, enabled: bool):
        for name in _DISCHARGE_INPUT_WIDGETS:
            getattr(self.ui, name).setEnabled(enabled)
        for checkbox_name, spin_name in _DISCHARGE_LIMIT_SPINBOX.items():
            checked = getattr(self.ui, checkbox_name).isChecked()
            getattr(self.ui, spin_name).setEnabled(enabled and checked)

    @QtCore.pyqtSlot()
    def on_btnDischargeRun_clicked(self):
        if self._active_aux == "discharge":
            self._finish_discharge(self._aux_leave_on("discharge"), self.tr("用户停止"))
            return
        cutoff = self.ui.spinBoxDischargeCutoff.value()
        if cutoff <= 0:
            self.ui.btnDischargeRun.setText(self.tr("需要截止电压"))
            QtCore.QTimer.singleShot(
                1000, lambda: self.ui.btnDischargeRun.setText(self.tr("开始放电"))
            )
            return
        if not self._begin_aux("discharge"):
            return
        mode = self.ui.comboDischargeMode.currentText()
        target = self.ui.spinBoxDischargeTarget.value()
        self._discharge_cutoff = cutoff
        self._discharge_max_ah = (
            self.ui.spinBoxDischargeMaxAh.value()
            if self.ui.checkBoxDischargeMaxAh.isChecked()
            else None
        )
        self._discharge_max_wh = (
            self.ui.spinBoxDischargeMaxWh.value()
            if self.ui.checkBoxDischargeMaxWh.isChecked()
            else None
        )
        self._discharge_max_duration = (
            self.ui.spinBoxDischargeMaxDuration.value() * 60.0
            if self.ui.checkBoxDischargeMaxDuration.isChecked()
            else None
        )
        self._discharge_acc = CapacityAccumulator()
        self._discharge_cutoff_debounce = BelowThresholdDebounce(1.0)
        self._discharge_armed = False
        self._discharge_last_voltage = None
        self._set_discharge_inputs_enabled(False)
        self.ui.btnDischargeRun.setText(self.tr("停止放电"))
        self.ui.labelDischargeReason.setText("")
        self.ui.labelDischargeElapsed.setText("00:00:00")
        self.ui.labelDischargeAh.setText("0.0000 Ah")
        self.ui.labelDischargeWh.setText("0.0000 Wh")
        if not self._apply_mode_target(mode, target) or not self._confirm_load_on():
            self._finish_discharge(leave_on=False, reason=self.tr("使能失败"))
            return
        self._discharge_enable_rel = time.perf_counter() - self.store.start_time
        self.discharge_timer.start(_DISCHARGE_TICK_MS)

    def _discharge_check_termination(self):
        acc = self._discharge_acc
        if self._discharge_max_duration is not None and acc.elapsed >= self._discharge_max_duration:
            return self.tr("已达到最大时长")
        if self._discharge_max_ah is not None and acc.ah >= self._discharge_max_ah:
            return self.tr("已达到最大安时")
        if self._discharge_max_wh is not None and acc.wh >= self._discharge_max_wh:
            return self.tr("已达到最大瓦时")
        if self._discharge_last_voltage is not None:
            now = time.perf_counter()
            if self._discharge_cutoff_debounce.update(
                self._discharge_last_voltage, self._discharge_cutoff, now
            ):
                return self.tr("已达到截止电压")
        return None

    def _discharge_tick(self):
        if self._active_aux != "discharge":
            return
        acc = self._discharge_acc
        elapsed_i = int(acc.elapsed)
        h, rem = divmod(elapsed_i, 3600)
        m, s = divmod(rem, 60)
        self.ui.labelDischargeElapsed.setText(f"{h:02d}:{m:02d}:{s:02d}")
        self.ui.labelDischargeAh.setText(f"{acc.ah:.4f} Ah")
        self.ui.labelDischargeWh.setText(f"{acc.wh:.4f} Wh")
        if not self._discharge_armed:
            return
        reason = self._discharge_check_termination()
        if reason is not None:
            self._finish_discharge(leave_on=self._aux_leave_on("discharge"), reason=reason)

    def _finish_discharge(self, leave_on: bool, reason: str):
        self.discharge_timer.stop()
        self._set_discharge_inputs_enabled(True)
        self.ui.btnDischargeRun.setText(self.tr("开始放电"))
        self.ui.labelDischargeReason.setText(reason)
        self._finish_aux("discharge", leave_on)

    def has_discharge_data(self) -> bool:
        return self._discharge_acc is not None

    def _clear_discharge_data(self):
        if self._active_aux != "discharge":
            self._discharge_acc = None

    @QtCore.pyqtSlot()
    def on_btnDischargeSave_clicked(self):
        if self._discharge_acc is None or not self._discharge_acc.rows:
            CustomMessageBox(self, self.tr("错误"), self.tr("放电记录为空"))
            return
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.tr("保存"), "", self.tr("CSV文件 (*.csv)")
        )
        if not filename:
            return
        rd = RecordData(DISCHARGE_CHANNELS)
        for row in self._discharge_acc.rows:
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

    @QtCore.pyqtSlot(bool)
    def on_load_on_toggled(self, checked: bool):
        if self.api is None:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)
            return
        if not checked:
            # A manual Load Off during automation is an immediate stop, not
            # a normal completion -- it must not be re-enabled afterwards,
            # so leave_on is False regardless of the tool's own checkbox.
            if self._active_aux == "sweep":
                self._finish_sweep(leave_on=False)
                return
            if self._active_aux == "sequence":
                self._finish_sequence(leave_on=False)
                return
            if self._active_aux == "discharge":
                self._finish_discharge(leave_on=False, reason=self.tr("用户停止"))
                return
            if not self._confirmed_load_off():
                # Off never confirmed -- the load may still be live, so keep
                # the interlock engaged and don't write staged targets into
                # a live load. update_state() re-syncs from LoadEnabled.
                self.ui.btnLoadOn.blockSignals(True)
                self.ui.btnLoadOn.setChecked(True)
                self.ui.btnLoadOn.blockSignals(False)
            return
        ok = self.api.set_load_on(True)
        self._load_commanded_on = ok
        if not ok:
            self.ui.btnLoadOn.blockSignals(True)
            self.ui.btnLoadOn.setChecked(False)
            self.ui.btnLoadOn.blockSignals(False)

    def _start_device_timers(self):
        self.target_poll_timer.start(400)

    def unlink(self):
        self.state_request_sender_timer.stop()
        self.update_state_timer.stop()
        self.state_lcd_timer.stop()
        self.target_poll_timer.stop()
        # Stop any running auxiliary tool first (leave_on=True so its own
        # _finish_aux doesn't attempt the off itself -- the explicit,
        # logged attempt right below covers that exactly once, whether or
        # not a tool was running).
        if self._active_aux == "sweep":
            self._finish_sweep(leave_on=True)
        elif self._active_aux == "sequence":
            self._finish_sequence(leave_on=True)
        elif self._active_aux == "discharge":
            self._finish_discharge(leave_on=True, reason=self.tr("已断开连接"))
        if not self._confirmed_load_off():
            logger.warning(f"{self.display_name}: load-off not confirmed before disconnect")
        api = self.api
        self.api = None
        api.close()
        self._load_commanded_on = False
        self.linked = False
        self.close_state_ui(record_disconnect=True)
        self.link_state_changed.emit()

    def request_targets(self):
        if self.api is not None:
            self.api.request_target_page()

    def _on_raw_batch(self, raw_rtvalues, t1):
        if self._active_aux == "discharge" and self._discharge_acc is not None:
            # Only integrate samples from after the enable was confirmed, so
            # a batch straddling the enable moment can't credit Ah/Wh (or
            # arm the cutoff debounce) against pre-load readings.
            if t1 - self.store.start_time >= self._discharge_enable_rel:
                self._discharge_acc.add_batch(raw_rtvalues, t1)
                self._discharge_last_voltage = raw_rtvalues[-1][0]
                self._discharge_armed = True

    def _extra_channel_values(self, len_):
        # Held flat at the running (or, between runs, final) total rather
        # than only supplied while a discharge is active -- every channel
        # needs a value each call, and freezing here is what makes the
        # graph hold its last reading after Stop instead of gapping out.
        ah_total = self._discharge_acc.ah if self._discharge_acc is not None else 0.0
        wh_total = self._discharge_acc.wh if self._discharge_acc is not None else 0.0
        return {
            "ah": np.full(len_, ah_total),
            "wh": np.full(len_, wh_total),
            "sweep_target": np.full(len_, self._sweep_last_target),
        }

    def update_state(self):
        if self.api is None:
            return
        (
            LoadMode,
            LoadActive,
            LoadEnabled,
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
        # LoadEnabled is the device-reported switch state (authoritative);
        # syncing the interlock flag from it heals any desync left by an
        # unconfirmed set_load_on. LoadActive is only conduction -- it lags
        # switch-on by ~1 s -- so it's just the fallback while LoadEnabled
        # is still unknown.
        if LoadEnabled is not None:
            self._load_commanded_on = LoadEnabled
        self.ui.btnLoadOn.blockSignals(True)
        self.ui.btnLoadOn.setChecked(
            bool(LoadActive) if LoadEnabled is None else LoadEnabled
        )
        self.ui.btnLoadOn.blockSignals(False)
        was_latched = self._protection_latched
        self._protection_latched = ProtectionLatched
        if ProtectionLatched:
            if not was_latched:
                # Only on the edge into latch, not every status tick --
                # _begin_aux already refuses new runs once _protection_latched
                # is set above, so this fires at most once per latch event.
                self._stop_aux_for_fault()
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
        if self._update_common_lcds() is None:
            return
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

    def _close_state_ui_device(self, record_disconnect: bool):
        self.ui.labelTemperature.setText("")
        self.ui.labelProtection.setText("")
        set_color(self.ui.labelProtection, None)
        self.ui.btnLoadOn.blockSignals(True)
        self.ui.btnLoadOn.setChecked(False)
        self.ui.btnLoadOn.blockSignals(False)


DEVICE_PANEL_TYPES["L1060"] = L1060DevicePanel
