import time
from functools import partial

import numpy as np
from PyQt5 import QtCore, QtWidgets
from superqt.utils import signals_blocked

from app_context import float_str, set_color
from device_core import ChannelSpec, GraphCapture
from device_panel_l1060 import (
    CHANNEL_BY_KEY as _L1060_CHANNEL_BY_KEY,
    CHANNEL_SHORT as _L1060_CHANNEL_SHORT,
)
from device_panel_p906 import (
    CHANNEL_BY_KEY as _P906_CHANNEL_BY_KEY,
    CHANNEL_SHORT as _P906_CHANNEL_SHORT,
)
from graph_block import GraphBlock
from mdp_custom import CustomMessageBox
from mdp_gui_template.graph_view_ui import Ui_GraphView
from settings_model import SETTING_FILE, setting

CHANNEL_ORDER = [
    "voltage", "current", "resistance", "power", "energy", "temperature", "ah", "wh", "discharge", "sweep",
]
DEFAULT_ACTIVE_CHANNELS = {"voltage", "current"}
# Only meaningful for device types that support the discharge/sweep workflows
# (currently just L1060); each group's buttons stay hidden until some panel
# actually has that workflow's data, rather than cluttering the row
# unconditionally like the always-applicable channels above. Kept as two
# separate groups (not one combined set) so running Sweep doesn't also
# reveal the unrelated Discharge/Ah/Wh buttons, and vice versa.
DISCHARGE_GATED_CHANNELS = {"ah", "wh", "discharge"}
SWEEP_GATED_CHANNELS = {"sweep"}
GATED_CHANNELS = DISCHARGE_GATED_CHANNELS | SWEEP_GATED_CHANNELS

# Merged so channels that only one device type has (e.g. L1060's Ah/Wh
# discharge totals) still resolve here -- every panel type's blocks are
# built from this one shared lookup.
CHANNEL_BY_KEY = {**_P906_CHANNEL_BY_KEY, **_L1060_CHANNEL_BY_KEY}
CHANNEL_SHORT = {**_P906_CHANNEL_SHORT, **_L1060_CHANNEL_SHORT}
# Not a real store channel/series -- this pairs two existing L1060 series
# (voltage against accumulated ah) for the conventional discharge curve, so
# it only needs a label/unit for the block's title and left axis.
CHANNEL_BY_KEY["discharge"] = ChannelSpec(
    "discharge", QtCore.QCoreApplication.translate("MDPMainwindow", "放电曲线"), "V"
)
CHANNEL_SHORT["discharge"] = "V"
# Also not a real store series -- pairs L1060's sweep_target (x) against
# whichever response channel (voltage/current/power/resistance) that panel's
# sweep was last run with (y). Unlike "discharge" above, that response and
# its unit vary per run, so the left/bottom axis labels can't be fixed here
# and are instead kept in sync in _sync_sweep_axes().
CHANNEL_BY_KEY["sweep"] = ChannelSpec(
    "sweep", QtCore.QCoreApplication.translate("MDPMainwindow", "扫描曲线"), ""
)
CHANNEL_SHORT["sweep"] = ""


class GraphView(QtWidgets.QWidget):
    """One complete graph section: channel buttons, Start/Stop, AUTO, KEEP
    and CLEAR, the graph blocks, and the buffer slider, all bound to its own
    GraphCapture and drawn from its own panels. With a device_name it shows
    that name in the device color and hides the per-device block chips."""

    auto_started = QtCore.pyqtSignal()
    auto_stopped = QtCore.pyqtSignal()
    # Emitted when a channel toggle changes which blocks are shown, so the
    # owner can redraw immediately if its draw timer is running.
    redraw_requested = QtCore.pyqtSignal()

    def __init__(self, panels, device_name=None, parent=None):
        super().__init__(parent)
        self.ui = Ui_GraphView()
        self.ui.setupUi(self)
        self.panels = []
        self._panel_by_id = {}
        self._device_name = None
        self.graph_keep_flag = False
        self._graph_auto_scale_flag = True
        self._left_last = -1
        self.graph_blocks = {}
        # Both fire on the serial/TCP worker thread (GraphCapture's
        # callbacks); Qt queues them across to the GUI thread since these
        # are real signals, not direct calls.
        self.capture = GraphCapture(
            on_auto_start=self.auto_started.emit,
            on_auto_stop=self.auto_stopped.emit,
        )
        self.auto_started.connect(self._refresh_graph_run_button)
        self.auto_stopped.connect(self._on_graph_stopped)
        self.ui.horizontalSlider.sliderMoved.connect(
            self.on_horizontalSlider_sliderMoved
        )
        self.set_panels(panels, device_name)
        self.initGraph()
        self.apply_theme()
        self.refresh_link_state()

    def set_panels(self, panels, device_name=None):
        self.panels = list(panels)
        self._panel_by_id = {p.device_id: p for p in self.panels}
        self._device_name = device_name
        for panel in self.panels:
            panel.set_capture(self.capture)
        for block in self.graph_blocks.values():
            block.set_panels(self.panels)
            block.set_chips_visible(device_name is None)
        self.ui.labelDeviceName.setVisible(device_name is not None)
        if device_name is not None:
            self.ui.labelDeviceName.setText(device_name)
            self._refresh_device_name_color()
        self._populate_trigger_device_combo()
        self._load_trigger_controls()

    def _refresh_device_name_color(self):
        if self._device_name is None or not self.panels:
            return
        set_color(
            self.ui.labelDeviceName, f"#{self.panels[0].settings.color.lstrip('#')}"
        )

    def set_triggers(self, device_id, dev_settings):
        self.capture.set_triggers(
            device_id,
            dev_settings.graph_autostart,
            dev_settings.graph_autostart_threshold,
            dev_settings.graph_autostop,
            dev_settings.graph_autostop_threshold,
        )
        self._refresh_graph_run_button()

    def _populate_trigger_device_combo(self):
        """comboGraphTriggerDevice only disambiguates which panel's triggers
        are being edited in the shared view, where one GraphView holds every
        device -- a single-device view has nothing to disambiguate."""
        combo = self.ui.comboGraphTriggerDevice
        shared = self._device_name is None
        self.ui.labelGraphTriggerDevice.setVisible(shared)
        combo.setVisible(shared)
        if not shared:
            return
        with signals_blocked(combo):
            combo.clear()
            for panel in self.panels:
                combo.addItem(panel.display_name, panel.device_id)
            idx = combo.findData(setting.ui.graph_trigger_device)
            combo.setCurrentIndex(max(0, idx))

    def _trigger_device(self):
        """(device_id, DeviceSettings) of the panel whose triggers the
        trigger controls currently show and edit: the panel picked in
        comboGraphTriggerDevice in the shared view, or this view's one panel
        otherwise."""
        if not self.panels:
            return None, None
        if self._device_name is None:
            device_id = self.ui.comboGraphTriggerDevice.currentData()
            panel = self._panel_by_id.get(device_id) or self.panels[0]
        else:
            panel = self.panels[0]
        return panel.device_id, panel.settings

    def _load_trigger_controls(self):
        device_id, dev = self._trigger_device()
        if dev is None:
            return
        controls = (
            self.ui.comboGraphAutostart,
            self.ui.spinGraphAutostartThreshold,
            self.ui.comboGraphAutostop,
            self.ui.spinGraphAutostopThreshold,
        )
        with signals_blocked(*controls):
            self.ui.comboGraphAutostart.setCurrentIndex(
                {"off": 0, "voltage": 1, "current": 2}.get(dev.graph_autostart, 0)
            )
            self.ui.spinGraphAutostartThreshold.setValue(dev.graph_autostart_threshold)
            self.ui.comboGraphAutostop.setCurrentIndex(
                {"off": 0, "voltage": 1, "current": 2}.get(dev.graph_autostop, 0)
            )
            self.ui.spinGraphAutostopThreshold.setValue(dev.graph_autostop_threshold)
        self._update_trigger_controls_enabled()
        self.set_triggers(device_id, dev)

    def _update_trigger_controls_enabled(self):
        start_mode = self.ui.comboGraphAutostart.currentIndex()
        self.ui.spinGraphAutostartThreshold.setEnabled(start_mode != 0)
        self.ui.spinGraphAutostartThreshold.setSuffix("A" if start_mode == 2 else "V")
        stop_mode = self.ui.comboGraphAutostop.currentIndex()
        self.ui.spinGraphAutostopThreshold.setEnabled(stop_mode != 0)
        self.ui.spinGraphAutostopThreshold.setSuffix("A" if stop_mode == 2 else "V")

    def _apply_trigger_from_controls(self):
        device_id, dev = self._trigger_device()
        if dev is None:
            return
        dev.graph_autostart = {0: "off", 1: "voltage", 2: "current"}[
            self.ui.comboGraphAutostart.currentIndex()
        ]
        dev.graph_autostart_threshold = self.ui.spinGraphAutostartThreshold.value()
        dev.graph_autostop = {0: "off", 1: "voltage", 2: "current"}[
            self.ui.comboGraphAutostop.currentIndex()
        ]
        dev.graph_autostop_threshold = self.ui.spinGraphAutostopThreshold.value()
        self.set_triggers(device_id, dev)
        setting.save(SETTING_FILE)

    @QtCore.pyqtSlot(int)
    def on_comboGraphTriggerDevice_currentIndexChanged(self, index):
        setting.ui.graph_trigger_device = self.ui.comboGraphTriggerDevice.itemData(index) or ""
        self._load_trigger_controls()
        setting.save(SETTING_FILE)

    @QtCore.pyqtSlot(int)
    def on_comboGraphAutostart_currentIndexChanged(self, _index):
        self._update_trigger_controls_enabled()
        self._apply_trigger_from_controls()

    @QtCore.pyqtSlot(float)
    def on_spinGraphAutostartThreshold_valueChanged(self, _=None):
        self._apply_trigger_from_controls()

    @QtCore.pyqtSlot(int)
    def on_comboGraphAutostop_currentIndexChanged(self, _index):
        self._update_trigger_controls_enabled()
        self._apply_trigger_from_controls()

    @QtCore.pyqtSlot(float)
    def on_spinGraphAutostopThreshold_valueChanged(self, _=None):
        self._apply_trigger_from_controls()

    def refresh_device_color(self, panel):
        if panel.device_id not in self._panel_by_id:
            return
        for block in self.graph_blocks.values():
            block.refresh_device_color(panel)
        self._refresh_device_name_color()

    def apply_theme(self):
        for block in self.graph_blocks.values():
            block.plot_widget.setBackground(None)
        self.ui.horizontalSlider.setStyleSheet("background: none;")
        self.ui.horizontalSlider.setBarVisible(False)

    def set_data_length(self, length):
        for panel in self.panels:
            panel.store.data_length = length
        self.on_btnGraphClear_clicked(skip_confirm=True)

    def refresh_link_state(self):
        """Enable the slider once any of this view's panels is linked; once
        none is, stop a running capture and, if there's no data left to
        review, reset to the empty state."""
        if any(p.linked for p in self.panels):
            self.ui.frameGraphControl.setEnabled(not self._mouse_enabled())
        else:
            if self.capture.running:
                self.capture.stop()
                self._on_graph_stopped()
            # Leave already-graphed data on screen for review (the CLEAR
            # button is the explicit way to discard it) instead of wiping it
            # - only reset to the empty/disabled state if there's nothing to
            # review yet.
            if not any(p.store.update_count > 0 for p in self.panels):
                for block in self.graph_blocks.values():
                    block.clear()
                self.ui.horizontalSlider.setRange(0, 10)
                self.ui.horizontalSlider.setValue((2, 8))
                self.ui.labelBufferSize.setText("N/A")
                self.ui.labelDisplayRange.setText("N/A")
                set_color(self.ui.labelBufferSize, None)
                self.ui.frameGraphControl.setEnabled(False)
        self._refresh_graph_run_button()

    ##########  图像绘制  ##########

    def initGraph(self):
        self._channel_buttons = {
            "voltage": self.ui.btnGraphVoltage,
            "current": self.ui.btnGraphCurrent,
            "resistance": self.ui.btnGraphResistance,
            "power": self.ui.btnGraphPower,
            "energy": self.ui.btnGraphEnergy,
            "temperature": self.ui.btnGraphTemp,
            "ah": self.ui.btnGraphAh,
            "wh": self.ui.btnGraphWh,
            "discharge": self.ui.btnGraphDischarge,
            "sweep": self.ui.btnGraphSweep,
        }
        for key, button in self._channel_buttons.items():
            button.setChecked(key in DEFAULT_ACTIVE_CHANNELS)
            button.toggled.connect(partial(self._on_channel_toggled, key))
        for key in CHANNEL_ORDER:
            if key in DEFAULT_ACTIVE_CHANNELS:
                self._show_graph_block(key)

    def _show_graph_block(self, key):
        block = self.graph_blocks.get(key)
        if block is None:
            block = GraphBlock(key, CHANNEL_BY_KEY[key])
            if key == "discharge":
                # Only the label differs here -- the window/curve update path
                # is the same as every other block (see _series_for_block()).
                block.plot_widget.setLabel("bottom", "Ah")
            elif key == "sweep":
                self._sync_sweep_axes(block)
            block.set_panels(self.panels)
            block.set_chips_visible(self._device_name is None)
            block.set_mouse_enabled(self._mouse_enabled())
            self.graph_blocks[key] = block
        insert_at = sum(
            1
            for k in CHANNEL_ORDER[: CHANNEL_ORDER.index(key)]
            if self._channel_buttons[k].isChecked()
        )
        self.ui.layoutGraphs.insertWidget(insert_at, block, stretch=1)
        block.show()
        self._resync_axes()

    def _hide_graph_block(self, key):
        block = self.graph_blocks.get(key)
        if block is None:
            return
        self.ui.layoutGraphs.removeWidget(block)
        block.setParent(None)
        self._resync_axes()

    def _on_channel_toggled(self, key, checked):
        if checked:
            self._show_graph_block(key)
        else:
            self._hide_graph_block(key)
        self.redraw_requested.emit()

    def _sync_gated_channels(self, keys, visible):
        for key in keys:
            button = self._channel_buttons[key]
            if visible and not button.isVisible():
                # Edge-triggered on the hidden->visible transition (not
                # every tick while already visible) so auto-selecting a
                # freshly revealed button doesn't fight a user who later
                # manually unchecks it while data is still present.
                button.setChecked(True)
            elif not visible and button.isChecked():
                # Unchecking (rather than just hiding the button) routes
                # through _on_channel_toggled so the now-stale block is
                # actually hidden too, not left on screen with no visible
                # toggle to hide it again.
                button.setChecked(False)
            button.setVisible(visible)

    def _resync_axes(self):
        group = []
        for key in CHANNEL_ORDER:
            if self._channel_buttons[key].isChecked():
                block = self.graph_blocks.get(key)
                if block is not None:
                    block.axis.syncWith(group, left_spacing=True)

    def _set_graph_controls_enabled(self, enabled):
        for button in self._channel_buttons.values():
            button.setEnabled(enabled)
        for block in self.graph_blocks.values():
            for chip in block.chips.values():
                chip.setEnabled(enabled)

    def _sync_sweep_axes(self, block):
        """Unlike every other block's fixed left/bottom axis (set once at
        construction from its ChannelSpec), Sweep's response channel and
        mode -- and so its axis units -- can change from run to run, so
        these must be re-applied whenever they might have changed rather
        than just once. Not attempting per-panel-differing axes if multiple
        L1060 panels have swept with different responses/modes (accepted
        edge case) -- just label off the first panel that has swept."""
        panel = next((p for p in self.panels if p.has_sweep_data()), None)
        response_key = getattr(panel, "_sweep_response_key", "voltage") if panel else "voltage"
        response_spec = CHANNEL_BY_KEY.get(response_key, CHANNEL_BY_KEY["voltage"])
        target_unit = getattr(panel, "_sweep_target_unit", "") if panel else ""
        block.plot_widget.setLabel("left", response_spec.label, units=response_spec.unit)
        block.plot_widget.setLabel("bottom", self.tr("目标"), units=target_unit)

    def _series_for_block(self, block, panel, t_lo, t_hi):
        """(xs, ys, mx, mn, avg) for one panel's curve in this block, over
        the time span [t_lo, t_hi] (computed once per tick in draw()) every
        panel and block shares -- must be called with panel.store.sync_lock
        held. "discharge" and "sweep" plot one series against another
        (voltage against accumulated ah; the sweep response channel against
        sweep_target) instead of against time."""
        start, stop = panel.store.window(t_lo, t_hi)
        if block.channel_key == "discharge":
            return panel.store.get_series("voltage", start, stop, x_key="ah")
        if block.channel_key == "sweep":
            response_key = getattr(panel, "_sweep_response_key", "voltage")
            return panel.store.get_series(response_key, start, stop, x_key="sweep_target")
        return panel.store.get_series(block.channel_key, start, stop)

    def on_horizontalSlider_sliderMoved(self, values):
        left = int(values[0])
        right = int(values[1])
        left_moved = left != self._left_last
        if right - left < setting.ui.display_pts:
            if left_moved:
                right = min(
                    left + setting.ui.display_pts, self.ui.horizontalSlider._maximum
                )
                if right - left < setting.ui.display_pts:
                    left = max(right - setting.ui.display_pts, 0)
            else:
                left = max(right - setting.ui.display_pts, 0)
                if right - left < setting.ui.display_pts:
                    right = min(
                        left + setting.ui.display_pts, self.ui.horizontalSlider._maximum
                    )
            with signals_blocked(self.ui.horizontalSlider):
                self.ui.horizontalSlider.setValue((left, right))
        self._left_last = left

    def draw(self):
        # Visible while a run is active or its result is still held; hidden
        # again once Clear drops that result (panel.clear_aux_data()) and no
        # run is running, at which point the toggle is also force-unchecked
        # so its now-hidden block doesn't stay stuck on screen.
        self._sync_gated_channels(DISCHARGE_GATED_CHANNELS, any(p.has_discharge_data() for p in self.panels))
        self._sync_gated_channels(SWEEP_GATED_CHANNELS, any(p.has_sweep_data() for p in self.panels))
        sweep_block = self.graph_blocks.get("sweep")
        if sweep_block is not None:
            self._sync_sweep_axes(sweep_block)
        if self.graph_keep_flag:
            return
        if not self.panels:
            return
        shown_blocks = [
            self.graph_blocks[key]
            for key in CHANNEL_ORDER
            if self._channel_buttons[key].isChecked() and key in self.graph_blocks
        ]
        needed_panels = []
        seen_ids = set()
        for block in shown_blocks:
            for panel in block.checked_panels(self.panels):
                if panel.device_id not in seen_ids:
                    seen_ids.add(panel.device_id)
                    needed_panels.append(panel)
        # A panel that's never been linked still logs one throwaway
        # calibration sample at construction (close_state_ui()), so its
        # buffer sits frozen at update_count=1 forever. Picking it as
        # sync_panel would collapse the shared time span below down to that
        # single stale sample. Prefer an actually-linked panel
        # as the reference; only fall back to an unlinked one (e.g. to keep
        # reviewing a just-disconnected device's history) if nothing is
        # currently linked.
        linked_panels = [p for p in needed_panels if p.linked]
        if linked_panels:
            sync_panel = max(linked_panels, key=lambda p: p.store.update_count)
        elif needed_panels:
            sync_panel = max(needed_panels, key=lambda p: p.store.update_count)
        else:
            sync_panel = self.panels[0]
        with sync_panel.store.sync_lock:
            update_count = sync_panel.store.update_count
            if update_count > setting.ui.display_pts + 5:
                left, right = self.ui.horizontalSlider.sliderPosition()
                max_ = self.ui.horizontalSlider._maximum
                syncing = right == max_
                allfit = syncing and (left == 0)
                if not self.ui.horizontalSlider.isEnabled():
                    self.ui.horizontalSlider.setEnabled(True)
                if allfit:
                    display_pts = update_count
                else:
                    display_pts = max(int(right) - int(left), setting.ui.display_pts)
                if not syncing:
                    r_offset = update_count - int(right)
                else:
                    r_offset = 0
                right = update_count - r_offset
                if not allfit:
                    left = max(0, right - display_pts)
                else:
                    left = 0
                self.ui.horizontalSlider.setRange(0, update_count)
            else:
                display_pts = update_count
                r_offset = 0
                syncing = True
                allfit = False
                left = 0
                right = update_count
                self.ui.horizontalSlider.setEnabled(False)
                self.ui.horizontalSlider.setRange(0, 10)
            if syncing:
                self.ui.horizontalSlider.setValue((left, update_count))
            # The slider indexes sync_panel's samples; that index window is
            # turned into a time span so every panel is drawn over the same
            # stretch of time, whatever amount of history each one holds.
            sync_times = sync_panel.store.ordered(sync_panel.store.times)
            start_index = max(0, update_count - display_pts - r_offset)
            to_index = update_count - r_offset
            t_lo = -np.inf if start_index == 0 else sync_times[start_index]
            t_hi = np.inf if r_offset == 0 or to_index <= 0 else sync_times[to_index - 1]

        self.ui.labelBufferSize.setText(
            f"{update_count/sync_panel.store.data_length*100:.1f}%"
        )
        self.ui.labelBufferSize.setToolTip(
            self.tr("数据缓冲区占用率") + f"\n{update_count} / {sync_panel.store.data_length}"
        )
        self.ui.labelDisplayRange.setText(str(display_pts))
        if update_count > sync_panel.store.data_length * 0.95:
            set_color(
                self.ui.labelBufferSize,
                setting.get_color("general_yellow"),
            )

        for block in shown_blocks:
            checked_ids = {p.device_id for p in block.checked_panels(self.panels)}
            if block.channel_key == "sweep":
                sweep_panel = next((p for p in self.panels if p.has_sweep_data()), None)
                response_key = (
                    getattr(sweep_panel, "_sweep_response_key", "voltage")
                    if sweep_panel
                    else "voltage"
                )
                short = CHANNEL_SHORT.get(response_key, "")
            else:
                short = CHANNEL_SHORT.get(block.channel_key, "")
            stats_lines = []
            vmin = vmax = xmin = xmax = None
            for panel in self.panels:
                curve = block.curves.get(panel.device_id)
                if curve is None:
                    continue
                if panel.device_id not in checked_ids:
                    curve.setData(x=[], y=[])
                    continue
                with panel.store.sync_lock:
                    xs, ys, mx, mn, avg = self._series_for_block(
                        block, panel, t_lo, t_hi
                    )
                # A window holding only gap markers (NaN, e.g. when every
                # resistance sample was the open-circuit value) has nothing
                # to plot, and its NaN max/min can't be used as a range.
                if xs is None or xs.size == 0 or np.isnan(mx):
                    curve.setData(x=[], y=[])
                    continue
                # Plot exactly the same window the stats below are computed
                # from - plotting the full buffer here while vmin/vmax only
                # covered this window let old out-of-window samples (e.g. a
                # peak recorded before the load was switched off) stay
                # visibly drawn even after the axis had already narrowed to
                # the current window's range.
                curve.setData(x=xs, y=ys)
                vmin = mn if vmin is None else min(vmin, mn)
                vmax = mx if vmax is None else max(vmax, mx)
                if block.channel_key in ("discharge", "sweep"):
                    # x is another series rather than time, so it carries
                    # the NaN gap markers and its first/last samples aren't
                    # its bounds.
                    x_lo, x_hi = np.nanmin(xs), np.nanmax(xs)
                else:
                    x_lo, x_hi = xs[0], xs[-1]
                xmin = x_lo if xmin is None else min(xmin, x_lo)
                xmax = x_hi if xmax is None else max(xmax, x_hi)
                color = f"#{panel.settings.color.lstrip('#')}"
                stats_lines.append(
                    f'<span style="color:{color}">{panel.display_name}</span> '
                    f"{short}avg: {float_str(avg)}  {short}max: {float_str(mx)}  "
                    f"{short}min: {float_str(mn)}  {short}pp: {float_str(mx - mn)}"
                )
            block.stats_label.setText(
                "&nbsp;&nbsp;|&nbsp;&nbsp;".join(stats_lines) if stats_lines else "No Info"
            )
            if (
                self._graph_auto_scale_flag
                and vmax is not None
                and vmax != np.inf
                and vmin != -np.inf
            ):
                add = max(0.01, (vmax - vmin) * 0.05)
                block.plot_widget.setYRange(vmin - add, vmax + add)
                block.plot_widget.setXRange(xmin, xmax)

    @QtCore.pyqtSlot()
    def on_btnGraphClear_clicked(self, _=None, skip_confirm=False):
        if not skip_confirm and not CustomMessageBox.question(
            self,
            self.tr("警告"),
            self.tr("确定要清空数据缓冲区吗？"),
        ):
            return
        self.capture.clear(time.perf_counter())
        for panel in self.panels:
            panel.store.clear()
            panel.clear_aux_data()
        for block in self.graph_blocks.values():
            block.clear()
        set_color(self.ui.labelBufferSize, None)

    @QtCore.pyqtSlot()
    def on_btnGraphRun_clicked(self):
        if self.capture.running:
            self.capture.stop()
            self._on_graph_stopped()
        else:
            self.capture.start(time.perf_counter())
            self._refresh_graph_run_button()

    def _on_graph_stopped(self):
        for panel in self.panels:
            panel.store.mark_gap()
        self._refresh_graph_run_button()

    def _refresh_graph_run_button(self):
        btn = self.ui.btnGraphRun
        btn.setEnabled(any(p.linked for p in self.panels) or self.capture.running)
        if self.capture.running:
            btn.setText(self.tr("停止"))
            set_color(btn, setting.get_color("general_green"))
            btn.setToolTip(self._graph_trigger_tooltip(self.capture.stop_mode, self.capture.stop_threshold, self.tr("时自动停止"), "<"))
            return
        watched = self._panel_by_id.get(self.capture.device_id)
        armed = self.capture.start_mode != "off" and watched is not None and watched.linked
        if armed:
            btn.setText(self.tr("待触发"))
            set_color(btn, setting.get_color("general_yellow"))
            btn.setToolTip(self._graph_trigger_tooltip(self.capture.start_mode, self.capture.start_threshold, self.tr("时自动开始"), "≥"))
        else:
            btn.setText(self.tr("开始"))
            set_color(btn, None)
            btn.setToolTip("")

    def _graph_trigger_tooltip(self, mode, threshold, suffix, symbol):
        if mode == "off":
            return ""
        watched = self._panel_by_id.get(self.capture.device_id)
        if watched is None:
            return ""
        kind = self.tr("电压") if mode == "voltage" else self.tr("电流")
        unit = "V" if mode == "voltage" else "A"
        return f"{watched.display_name}: {kind} {symbol} {threshold:.3f}{unit} {suffix}"

    def _mouse_enabled(self):
        return self.graph_keep_flag or (not self._graph_auto_scale_flag)

    def _set_graphs_mouse_enabled(self, enabled):
        for block in self.graph_blocks.values():
            block.set_mouse_enabled(enabled)

    @QtCore.pyqtSlot()
    def on_btnGraphKeep_clicked(self):
        self.graph_keep_flag = not self.graph_keep_flag
        if self.graph_keep_flag:
            self.ui.btnGraphKeep.setText(self.tr("解除"))
        else:
            self.ui.btnGraphKeep.setText(self.tr("保持"))
        self._set_graph_controls_enabled(not self.graph_keep_flag)
        mouse_enabled = self._mouse_enabled()
        self.ui.frameGraphControl.setEnabled(not mouse_enabled)
        self._set_graphs_mouse_enabled(mouse_enabled)

    @QtCore.pyqtSlot()
    def on_btnGraphAutoScale_clicked(self):
        self._graph_auto_scale_flag = not self._graph_auto_scale_flag
        if self._graph_auto_scale_flag:
            self.ui.btnGraphAutoScale.setText(self.tr("适应"))
        else:
            self.ui.btnGraphAutoScale.setText(self.tr("手动"))
        mouse_enabled = self._mouse_enabled()
        self.ui.frameGraphControl.setEnabled(not mouse_enabled)
        self._set_graphs_mouse_enabled(mouse_enabled)
