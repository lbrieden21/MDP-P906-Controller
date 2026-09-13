import os
import sys
import time
import warnings
from functools import partial

os.environ["PYQTGRAPH_QT_LIB"] = "PyQt5"
warnings.filterwarnings("ignore", category=RuntimeWarning)

from loguru import logger

from app_context import (
    ABS_PATH,
    ARG_PATH,
    FONT_PATH,
    ICON_PATH,
    center_window,
    float_str,
    set_color,
    update_pyqtgraph_setting,
)

try:
    from mdp_controller import MDPBus  # noqa: F401 (import fixes up sys.path below for connection.py/device_panel_p906.py)
except ImportError:
    logger.info("Redirecting to repo mdp_controller")
    sys.path.append(os.path.dirname(ABS_PATH))
    sys.path.append(os.path.dirname(os.path.dirname(ABS_PATH)))
    from mdp_controller import MDPBus  # noqa: F401

    logger.success("Found mdp_controller in repo")

from PyQt5 import QtCore, QtGui, QtWidgets
from qframelesswindow import FramelessWindow

import numpy as np
import qdarktheme
from mdp_gui_template import Ui_MainWindow
from superqt.utils import signals_blocked

VERSION = "Ver4.5"
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
qdarktheme.enable_hi_dpi()
app = QtWidgets.QApplication(sys.argv)

# get system language
system_lang = QtCore.QLocale.system().name()
logger.info(f"System language: {system_lang}")
ENGLISH = False
if (
    not system_lang.startswith("zh")
    or os.environ.get("MDP_FORCE_ENGLISH") == "1"
    or "--english" in sys.argv
):
    trans = QtCore.QTranslator()
    trans.load(os.path.join(ABS_PATH, "en_US.qm"))
    app.installTranslator(trans)
    ENGLISH = True

# load custom font
_ = QtGui.QFontDatabase.addApplicationFont(FONT_PATH)
fonts = QtGui.QFontDatabase.applicationFontFamilies(_)
logger.info(f"Loaded custom fonts: {fonts}")
global_font = QtGui.QFont()
global_font.setFamily(fonts[0])
app.setFont(global_font)

from mdp_custom import CustomMessageBox, CustomTitleBar
from settings_model import setting
from device_core import ChannelSpec, csv_unit
from device_panel import DEVICE_PANEL_TYPES
from graph_block import GraphBlock
from device_panel_p906 import CHANNEL_BY_KEY as _P906_CHANNEL_BY_KEY, CHANNEL_SHORT as _P906_CHANNEL_SHORT  # noqa: F401 (registers P906 in DEVICE_PANEL_TYPES)
from device_panel_l1060 import (  # noqa: F401 (registers L1060 in DEVICE_PANEL_TYPES)
    CHANNEL_BY_KEY as _L1060_CHANNEL_BY_KEY,
    CHANNEL_SHORT as _L1060_CHANNEL_SHORT,
)
from connection import ConnectionManager
from dialogs import MDPGraphics, MDPSettings
from aux_windows import ResultGraphWindow, TransparentFloatingWindow

update_pyqtgraph_setting()

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


class MDPMainwindow(QtWidgets.QMainWindow, FramelessWindow):  # QtWidgets.QMainWindow
    close_signal = QtCore.pyqtSignal()
    panels_changed = QtCore.pyqtSignal()
    data_fps = 50
    graph_keep_flag = False
    graph_record_flag = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.panels = []
        self._panel_by_id = {}
        self._init_device_selector()
        for dev in setting.devices:
            self._add_panel_for_device(dev)
        self._rebuild_device_selector()
        self.set_device_layout(setting.ui.device_layout)
        self.connection = ConnectionManager(self.panels)
        self.initSignals()
        self.initGraph()
        self.initTimer()
        self.CustomTitleBar = CustomTitleBar(
            self,
            self.tr("MDP-P906 数控电源上位机") + f" {VERSION}",
        )
        self.CustomTitleBar.set_theme("dark")
        self.ui.comboDataFps.setCurrentText(f"{self.data_fps}Hz")
        self.setTitleBar(self.CustomTitleBar)
        self.close_state_ui()
        center_window(self, 920, 800)
        self.titleBar.raise_()

    def _add_panel_for_device(self, dev):
        panel = DEVICE_PANEL_TYPES[dev.type](self, dev)
        self.ui.layoutDevices.addWidget(panel)
        panel.link_state_changed.connect(self._update_title_for_model)
        panel.link_state_changed.connect(self._refresh_device_selector_style)
        panel.link_toggle_requested.connect(
            lambda p=panel: self.on_panel_link_toggled(p)
        )
        panel.apply_theme()
        if ENGLISH:
            panel.set_english_fonts()
        self.panels.append(panel)
        self._panel_by_id[panel.device_id] = panel
        return panel

    def rebuild_panels(self):
        """Re-derive self.panels from setting.devices - called after the
        Settings dialog adds/removes a device. Refuses while connected: a
        live panel<->bus<->pipe attachment can't be safely rebuilt under
        it, so the dialog itself blocks Add/Remove while connected and this
        is just a defensive backstop."""
        if self.connection.is_open:
            return
        for panel in self.panels:
            self.ui.layoutDevices.removeWidget(panel)
            panel.setParent(None)
            panel.deleteLater()
        self.panels = []
        self._panel_by_id = {}
        for dev in setting.devices:
            self._add_panel_for_device(dev)
        self.connection.panels = self.panels
        self._rebuild_device_selector()
        self.set_device_layout(setting.ui.device_layout)
        for block in self.graph_blocks.values():
            block.set_panels(self.panels)
        self.on_btnGraphClear_clicked(skip_confirm=True)
        self._update_title_for_model()
        self.panels_changed.emit()

    def _init_device_selector(self):
        """Device button row shown above the panels in the "single" device
        layout; one exclusive checkable button per panel."""
        self.device_selector = QtWidgets.QWidget(self.ui.widgetDeviceArea)
        self.device_selector.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed
        )
        self._device_selector_layout = QtWidgets.QHBoxLayout(self.device_selector)
        self._device_selector_layout.setContentsMargins(0, 0, 0, 0)
        self._device_selector_group = QtWidgets.QButtonGroup(self)
        self._device_selector_group.setExclusive(True)
        self._device_selector_buttons = {}
        self._selected_device_id = None
        self.ui.layoutDevices.insertWidget(0, self.device_selector)

    def _rebuild_device_selector(self):
        for btn in self._device_selector_buttons.values():
            self._device_selector_group.removeButton(btn)
            self._device_selector_layout.removeWidget(btn)
            btn.deleteLater()
        self._device_selector_buttons = {}
        if self._selected_device_id not in self._panel_by_id:
            self._selected_device_id = self.panels[0].device_id if self.panels else None
        for panel in self.panels:
            btn = QtWidgets.QPushButton(panel.display_name, self.device_selector)
            btn.setCheckable(True)
            btn.setChecked(panel.device_id == self._selected_device_id)
            btn.clicked.connect(
                lambda _=False, device_id=panel.device_id: self.select_device(device_id)
            )
            self._device_selector_group.addButton(btn)
            self._device_selector_layout.addWidget(btn)
            self._device_selector_buttons[panel.device_id] = btn
        self._refresh_device_selector_style()

    def _refresh_device_selector_style(self):
        """Selected button is filled with the device color, the rest are
        outlined in it; a linked device's button is prefixed with a dot."""
        for device_id, btn in self._device_selector_buttons.items():
            panel = self._panel_by_id[device_id]
            color = f"#{panel.settings.color.lstrip('#')}"
            btn.setText(("● " if panel.linked else "") + panel.display_name)
            if btn.isChecked():
                btn.setStyleSheet(
                    "QPushButton {"
                    f"background-color: {color}; color: black; border: 1px solid {color};"
                    "border-radius: 3px; padding: 2px 8px; font-weight: bold; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton {"
                    f"border: 1px solid {color}; border-radius: 3px; padding: 2px 8px; }}"
                )

    def select_device(self, device_id: str):
        self._selected_device_id = device_id
        self._device_selector_buttons[device_id].setChecked(True)
        self.set_device_layout(setting.ui.device_layout)

    def set_device_layout(self, mode: str):
        """"stacked" shows every panel; "single" shows only the selected one
        plus the device button row. In "single" the device area keeps the
        width the stacked layout would give it, so switching devices or
        layouts never resizes the column."""
        setting.ui.device_layout = mode
        self.device_selector.setVisible(mode == "single")
        self._update_device_area_width()
        # Panel size hints only settle after an event-loop pass (deferred
        # LCD min-width setup, style polish on first real show), so measure
        # once more after that.
        QtCore.QTimer.singleShot(0, self._update_device_area_width)
        self._refresh_device_selector_style()

    def _update_device_area_width(self):
        """Apply panel visibility for the current device layout and, in
        "single", pin the device area to the widest panel's width and give
        the panel's extra height to its aux tab widget so every other
        section keeps its natural size."""
        single = setting.ui.device_layout == "single"
        # A panel that has never been shown reports a narrower, unstyled size
        # hint, so show every panel before measuring and hide afterwards.
        for panel in self.panels:
            panel.set_fills_column(single)
            panel.setVisible(True)
        stacked_width = max((p.sizeHint().width() for p in self.panels), default=0)
        for panel in self.panels:
            panel.setVisible(not single or panel.device_id == self._selected_device_id)
        self.ui.widgetDeviceArea.setMinimumWidth(stacked_width if single else 0)

    def on_device_color_changed(self, device_id: str):
        """A device's wheel color was edited and saved in the Settings
        dialog: push it live to the hardware wheel (if linked) and refresh
        its graph chip/curve, neither of which otherwise gets re-touched
        after the panel/curve was first created."""
        panel = self._panel_by_id.get(device_id)
        if panel is None:
            return
        panel.refresh_led_color()
        for block in self.graph_blocks.values():
            block.refresh_device_color(panel)
        self._refresh_device_selector_style()

    def _update_title_for_model(self):
        if any(p.model == "P905" for p in self.panels):
            self.CustomTitleBar.set_name(
                self.tr("MDP-P906 数控电源上位机") + f" {VERSION} - P905 Mode"
            )
        else:
            self.CustomTitleBar.set_name(
                self.tr("MDP-P906 数控电源上位机") + f" {VERSION}"
            )

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        pos = event.pos()
        for panel in self.panels:
            label = getattr(panel.ui, "labelTab", None)
            if label is None:  # e.g. L1060 panels have no tab bar to scroll-switch
                continue
            if not label.isVisible():  # panel hidden by the single device layout
                continue
            label_geom = label.geometry()
            label_pos = label.mapTo(self, QtCore.QPoint(0, 0))
            label_rect = QtCore.QRect(label_pos, label_geom.size())
            if panel.api is not None and label_rect.contains(pos):
                delta = event.angleDelta().y()
                current_idx = panel.ui.tabWidget.currentIndex()
                if delta > 0:
                    if current_idx > 0:
                        panel.ui.tabWidget.setCurrentIndex(current_idx - 1)
                else:
                    if current_idx < panel.ui.tabWidget.count() - 1:
                        panel.ui.tabWidget.setCurrentIndex(current_idx + 1)
                event.accept()
                return
        event.ignore()

    def closeEvent(self, a0: QtGui.QCloseEvent) -> None:
        self.close_signal.emit()
        return super().closeEvent(a0)

    def initTimer(self):
        self.draw_graph_timer = QtCore.QTimer(self)
        self.draw_graph_timer.timeout.connect(self.draw_graph)
        self.graph_record_save_timer = QtCore.QTimer(self)
        self.graph_record_save_timer.timeout.connect(self.graph_record_save)
        self.link_state_timer = QtCore.QTimer(self)
        self.link_state_timer.timeout.connect(self.update_link_state)
        self.link_state_timer.start(100)

    def update_link_state(self):
        if not self.connection.is_open:
            return
        speed_counter = self.connection.bus.speed_counter
        self.ui.labelComSpeed.setText(f"{speed_counter.KBps:.1f}kBps")
        errrate = speed_counter.error_rate * 100
        self.ui.labelErrRate.setText(f"CON-ERR {errrate:.0f}%")
        clr = (
            setting.get_color("general_red")
            if errrate > 50
            else (setting.get_color("general_yellow") if errrate > 10 else None)
        )
        set_color(self.ui.labelErrRate, clr)
        set_color(self.ui.labelComSpeed, clr)

    def initSignals(self):
        self.ui.comboDataFps.currentTextChanged.connect(self.set_data_fps)
        self.ui.horizontalSlider.sliderMoved.connect(
            self.on_horizontalSlider_sliderMoved
        )

    def switch_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    ##########  基本功能  ##########

    def close_state_ui(self):
        for widget in [self.ui.labelComSpeed, self.ui.labelErrRate]:
            set_color(widget, None)
            widget.setText("[N/A]")
        # Once every panel is unlinked, leave already-graphed data on screen
        # for review (the CLEAR button is the explicit way to discard it)
        # instead of wiping it - only reset to the empty/disabled state if
        # there's nothing to review yet.
        if any(p.store.update_count > 0 for p in self.panels):
            return
        for block in self.graph_blocks.values():
            block.clear()
        self.ui.horizontalSlider.setRange(0, 10)
        self.ui.horizontalSlider.setValue((2, 8))
        self.ui.labelBufferSize.setText("N/A")
        self.ui.labelDisplayRange.setText("N/A")
        set_color(self.ui.labelBufferSize, None)
        self.ui.frameGraphControl.setEnabled(False)

    def open_state_ui(self):
        self.ui.frameGraphControl.setEnabled(True)

    def apply_theme(self, sys_theme):
        for block in self.graph_blocks.values():
            block.plot_widget.setBackground(None)
        self.CustomTitleBar.set_theme(sys_theme)
        self.ui.horizontalSlider.setStyleSheet("background: none;")
        self.ui.horizontalSlider.setBarVisible(False)
        for panel in self.panels:
            panel.apply_theme()
        self._refresh_device_selector_style()

    def on_panel_link_toggled(self, panel):
        try:
            if panel.linked:
                was_last = sum(p.linked for p in self.panels) == 1
                self.connection.unlink_panel(panel)
                panel.store.mark_gap()
                if was_last:
                    # Keep the redraw timer alive if there's data to review
                    # (close_state_ui leaves it on screen instead of
                    # clearing it) so the buffer slider still scrubs the
                    # existing curves; only stop it once there's nothing
                    # left to draw.
                    if not any(p.store.update_count > 0 for p in self.panels):
                        self.draw_graph_timer.stop()
                    if self.graph_record_save_timer.isActive():
                        self.on_btnGraphRecord_clicked()
                    self.close_state_ui()
            else:
                first_link = not self.connection.is_open
                self.connection.link_panel(panel, self.data_fps)
                if first_link:
                    self.draw_graph_timer.start(
                        round(1000 / min(self.data_fps, setting.ui.graph_max_fps))
                    )
                    self.open_state_ui()
        except Exception as e:
            logger.exception(f"Failed to link/unlink {panel.display_name}")
            CustomMessageBox(self, self.tr("连接失败"), str(e))
        self._refresh_device_selector_style()

    def set_data_fps(self, text):
        if text != "":
            self.data_fps = int(text.replace("Hz", ""))
        if self.draw_graph_timer.isActive():
            self.draw_graph_timer.stop()
            self.draw_graph_timer.start(
                round(1000 / min(self.data_fps, setting.ui.graph_max_fps))
            )
        for panel in self.panels:
            panel.set_data_fps(self.data_fps)

    def set_graph_max_fps(self, _):
        self.set_data_fps(self.ui.comboDataFps.currentText())

    def set_state_fps(self, fps):
        self.set_data_fps(self.ui.comboDataFps.currentText())

    def set_data_length(self, length) -> None:
        for panel in self.panels:
            panel.store.data_length = length
        self.on_btnGraphClear_clicked(skip_confirm=True)

    def set_interp_all(self, interp) -> None:
        for panel in self.panels:
            panel.set_interp(interp)

    @QtCore.pyqtSlot()
    def on_btnRecordClear_clicked(self):
        for panel in self.panels:
            with panel.store.sync_lock:
                panel.store.energy = 0
                panel.store.eng_start_time = time.perf_counter()

    ##########  图像绘制  ##########

    def initGraph(self):
        self._graph_auto_scale_flag = True
        self.graph_blocks = {}
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
            mouse_enabled = self.graph_keep_flag or (not self._graph_auto_scale_flag)
            block.set_mouse_enabled(mouse_enabled)
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
        if self.draw_graph_timer.isActive():
            self.draw_graph()

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

    def _series_for_block(self, block, panel, display_pts, r_offset):
        """(xs, ys, mx, mn, avg) for one panel's curve in this block, over
        the exact same scrolling window (display_pts/r_offset, computed once
        per tick above) every other block uses -- must be called with
        panel.store.sync_lock held. "discharge" and "sweep" are the two
        exceptions to a single store.get_series() call: each pairs two
        existing series from that same window (voltage against accumulated
        ah; sweep_target against whichever response channel that panel's
        sweep was run with) instead of one series against time. Still just a
        lookup against the same window everyone else uses, not separate
        windowing logic."""
        if block.channel_key == "discharge":
            ah, _, start_index, to_index, _, _, _ = panel.store.get_series(
                "ah", display_pts, r_offset
            )
            voltage, _, _, _, mx, mn, avg = panel.store.get_series(
                "voltage", display_pts, r_offset
            )
            if ah is None or voltage is None or ah.size == 0:
                return None, None, None, None, None
            return ah[start_index:to_index], voltage[start_index:to_index], mx, mn, avg
        if block.channel_key == "sweep":
            response_key = getattr(panel, "_sweep_response_key", "voltage")
            target, _, start_index, to_index, _, _, _ = panel.store.get_series(
                "sweep_target", display_pts, r_offset
            )
            response, _, _, _, mx, mn, avg = panel.store.get_series(
                response_key, display_pts, r_offset
            )
            if target is None or response is None or target.size == 0:
                return None, None, None, None, None
            return target[start_index:to_index], response[start_index:to_index], mx, mn, avg
        data, time_, start_index, to_index, mx, mn, avg = panel.store.get_series(
            block.channel_key, display_pts, r_offset
        )
        if data is None or data.size == 0:
            return None, None, None, None, None
        return time_[start_index:to_index], data[start_index:to_index], mx, mn, avg

    _left_last = -1

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

    def draw_graph(self):
        self.ui.labelFps.setText(
            " | ".join(f"{p.display_name}: {p.fps_counter.fps:.1f}Hz" for p in self.panels)
        )
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
        needed_panels = []
        seen_ids = set()
        for block in self.graph_blocks.values():
            for panel in block.checked_panels(self.panels):
                if panel.device_id not in seen_ids:
                    seen_ids.add(panel.device_id)
                    needed_panels.append(panel)
        # A panel that's never been linked still logs one throwaway
        # calibration sample at construction (close_state_ui()), so its
        # buffer sits frozen at update_count=1 forever. Picking it as
        # sync_panel would pin display_pts to 1 for every panel's
        # get_series() call below, collapsing everyone's window down to
        # just their single latest sample. Prefer an actually-linked panel
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

        for block in self.graph_blocks.values():
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
                        block, panel, display_pts, r_offset
                    )
                if xs is None or xs.size == 0:
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
                xmin = xs[0] if xmin is None else min(xmin, xs[0])
                xmax = xs[-1] if xmax is None else max(xmax, xs[-1])
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
        for panel in self.panels:
            panel.store.clear()
            panel.clear_aux_data()
        for block in self.graph_blocks.values():
            block.clear()
        set_color(self.ui.labelBufferSize, None)

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
        mouse_enabled = self.graph_keep_flag or (not self._graph_auto_scale_flag)
        self.ui.frameGraphControl.setEnabled(not mouse_enabled)
        self._set_graphs_mouse_enabled(mouse_enabled)

    @QtCore.pyqtSlot()
    def on_btnGraphAutoScale_clicked(self):
        self._graph_auto_scale_flag = not self._graph_auto_scale_flag
        if self._graph_auto_scale_flag:
            self.ui.btnGraphAutoScale.setText(self.tr("适应"))
        else:
            self.ui.btnGraphAutoScale.setText(self.tr("手动"))
        mouse_enabled = self.graph_keep_flag or (not self._graph_auto_scale_flag)
        self.ui.frameGraphControl.setEnabled(not mouse_enabled)
        self._set_graphs_mouse_enabled(mouse_enabled)

    def _device_path(self, base_path: str, panel) -> str:
        """One shared path if there's exactly one panel (byte-identical to
        pre-multi-device naming); otherwise suffix each panel's own file
        with its device_id so N devices don't clobber each other."""
        if len(self.panels) == 1:
            return base_path
        base, ext = os.path.splitext(base_path)
        return f"{base}_{panel.device_id}{ext}"

    @QtCore.pyqtSlot()
    def on_btnGraphRecord_clicked(self):
        self.graph_record_flag = not self.graph_record_flag
        if self.graph_record_flag:
            for panel in self.panels:
                panel.start_record()
            time_str = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
            self.graph_record_basepath = os.path.join(
                ARG_PATH, f"record_{time_str}.csv"
            )
            self.ui.btnGraphRecord.setText(self.tr("停止"))
            self.graph_record_save_timer.start(30000)
        else:
            self.graph_record_save_timer.stop()
            saved = self._save_all_records()
            for panel in self.panels:
                panel.stop_record()

            CustomMessageBox(
                self,
                self.tr("录制完成"),
                self.tr("数据已保存至：")
                + "\n"
                + "\n".join(os.path.basename(p) for p in saved),
                additional_actions=(
                    [
                        (
                            self.tr("打开文件路径"),
                            partial(self._handle_open_filebase, saved[0]),
                        ),
                    ]
                    if saved
                    else []
                ),
            )
            self.ui.btnGraphRecord.setText(self.tr("录制"))

    def _save_all_records(self) -> list:
        saved = []
        for panel in self.panels:
            if panel.record_data is None:
                continue
            out_path = self._device_path(self.graph_record_basepath, panel)
            panel.record_data.to_csv(out_path)
            saved.append(out_path)
        return saved

    @QtCore.pyqtSlot()
    def on_btnGraphDump_clicked(self):
        path, ok = QtWidgets.QFileDialog.getSaveFileName(
            self,
            self.tr("保存数据"),
            os.path.join(ARG_PATH, "mdp_buffer_dump.csv"),
            "CSV Files (*.csv)",
        )
        if not ok:
            return
        saved = []
        for panel in self.panels:
            out_path = self._device_path(path, panel)
            with panel.store.sync_lock:
                times = panel.store.times[: panel.store.update_count]
                cols = [times] + [
                    panel.store.series[ch.key][: panel.store.update_count]
                    for ch in panel.channels
                ]
                header = "time/s," + ",".join(
                    f"{ch.key}/{csv_unit(ch.unit)}" for ch in panel.channels
                )
                np.savetxt(
                    out_path,
                    np.c_[tuple(cols)],
                    delimiter=",",
                    header=header,
                    comments="",
                    fmt="%f",
                )
            saved.append(out_path)
        CustomMessageBox(
            self,
            self.tr("保存完成"),
            self.tr("数据已保存至：") + "\n" + "\n".join(os.path.basename(p) for p in saved),
            additional_actions=[
                (
                    self.tr("打开文件路径"),
                    partial(self._handle_open_filebase, saved[0]),
                ),
            ],
        )

    def _handle_open_filebase(self, file):
        folder = os.path.dirname(file)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
        return True

    def graph_record_save(self):
        if self.graph_record_flag:
            self._save_all_records()
        else:
            self.graph_record_save_timer.stop()


MainWindow = MDPMainwindow()


DialogSettings = MDPSettings(MainWindow.connection)
DialogGraphics = MDPGraphics()
DialogResult = ResultGraphWindow(global_font)
FloatingWindow = TransparentFloatingWindow()
MainWindow.ui.btnSettings.clicked.connect(DialogSettings.show)
MainWindow.ui.btnGraphics.clicked.connect(DialogGraphics.show)
DialogGraphics.set_max_fps_sig.connect(MainWindow.set_graph_max_fps)
DialogGraphics.state_fps_sig.connect(MainWindow.set_state_fps)
DialogGraphics.set_data_len_sig.connect(MainWindow.set_data_length)
DialogGraphics.set_interp_sig.connect(MainWindow.set_interp_all)
DialogGraphics.theme_requested.connect(lambda theme: set_theme(theme))
DialogGraphics.device_layout_requested.connect(MainWindow.set_device_layout)
MainWindow.ui.btnRecordFloatWindow.clicked.connect(FloatingWindow.switch_visibility)
DialogSettings.devices_changed.connect(MainWindow.rebuild_panels)
DialogSettings.device_color_changed.connect(MainWindow.on_device_color_changed)


def wire_panels():
    FloatingWindow.set_devices(MainWindow.panels)
    for _panel in MainWindow.panels:
        _panel.display_data_signal.connect(DialogResult.showData)
        _panel.highlight_point_signal.connect(DialogResult.highlightPoint)


wire_panels()
MainWindow.panels_changed.connect(wire_panels)
MainWindow.close_signal.connect(FloatingWindow.close)
MainWindow.close_signal.connect(DialogResult.close)
MainWindow.close_signal.connect(DialogGraphics.close)
MainWindow.close_signal.connect(DialogSettings.close)
app.setWindowIcon(QtGui.QIcon(ICON_PATH))


def set_theme(theme):
    setting.ui.theme = theme
    if theme not in ("dark", "light"):
        sys_theme = setting.ui.color_palette[theme]["based_on_dark_or_light"]
    else:
        sys_theme = theme
    additional_qss = (
        "QToolTip {"
        "   color: rgb(228, 231, 235);"
        "   background-color: rgb(32, 33, 36);"
        "   border: 1px solid rgb(63, 64, 66);"
        "   border-radius: 4px;"
        "}"
        "QSlider::add-page:horizontal {"
        "   background: #36ff8888;"
        "}"
        "QSlider::sub-page:horizontal {"
        "   background: #368888ff;"
        "}"
        if sys_theme == "dark"
        else "QToolTip {"
        "   color: rgb(32, 33, 36);"
        "   background-color: white;"
        "   border: 1px solid rgb(218, 220, 224);"
        "   border-radius: 4px;"
        "}"
        "QSlider::add-page:horizontal {"
        "   background: #36880000;"
        "}"
        "QSlider::sub-page:horizontal {"
        "   background: #36000088;"
        "}"
    )
    qdarktheme.setup_theme(
        sys_theme,
        additional_qss=additional_qss,
    )
    MainWindow.apply_theme(sys_theme)
    DialogSettings.apply_theme(sys_theme)
    DialogGraphics.apply_theme(sys_theme)
    DialogResult.apply_theme(sys_theme)


set_theme(setting.ui.theme)


def show_app():
    MainWindow.showMaximized()
    MainWindow.activateWindow()
    sys.exit(app.exec_())


if __name__ == "__main__":
    show_app()
