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

import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
from qframelesswindow import FramelessWindow

import numpy as np
import qdarktheme
from mdp_gui_template import Ui_MainWindow
from superqt.utils import signals_blocked

VERSION = "Ver4.5"
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

from mdp_custom import CustomMessageBox, CustomTitleBar, FmtAxisItem
from settings_model import setting
from device_core import csv_unit
from device_panel import DEVICE_PANEL_TYPES
from device_panel_p906 import CHANNEL_BY_KEY, CHANNEL_SHORT  # noqa: F401 (registers P906 in DEVICE_PANEL_TYPES)
from device_panel_l1060 import (  # noqa: F401 (registers L1060 in DEVICE_PANEL_TYPES)
    CHANNEL_BY_KEY as _L1060_CHANNEL_BY_KEY,
    CHANNEL_SHORT as _L1060_CHANNEL_SHORT,
)
from connection import ConnectionManager
from dialogs import MDPGraphics, MDPSettings
from aux_windows import ResultGraphWindow, TransparentFloatingWindow

update_pyqtgraph_setting()


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
        for dev in setting.devices:
            self._add_panel_for_device(dev)
        self.connection = ConnectionManager(self.panels)
        self.initDataCombos()
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
        self.initDataCombos()
        self.on_btnGraphClear_clicked(skip_confirm=True)
        self._update_title_for_model()
        self.panels_changed.emit()

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
            label_geom = panel.ui.labelTab.geometry()
            label_pos = panel.ui.labelTab.mapTo(self, QtCore.QPoint(0, 0))
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

    def initDataCombos(self):
        for combo in (self.ui.comboGraph1Data, self.ui.comboGraph2Data):
            with signals_blocked(combo):
                combo.clear()
                for panel in self.panels:
                    for ch in panel.channels:
                        combo.addItem(
                            f"{panel.display_name}: {ch.label}",
                            (panel.device_id, ch.key),
                        )
                combo.addItem(self.tr("无"), None)
        if self.ui.comboGraph2Data.count() > 1:
            with signals_blocked(self.ui.comboGraph2Data):
                self.ui.comboGraph2Data.setCurrentIndex(1)

    def initSignals(self):
        self.ui.comboDataFps.currentTextChanged.connect(self.set_data_fps)
        self.ui.comboGraph1Data.currentIndexChanged.connect(
            lambda _: self.set_graph1_data(self.ui.comboGraph1Data.currentData())
        )
        self.ui.comboGraph2Data.currentIndexChanged.connect(
            lambda _: self.set_graph2_data(self.ui.comboGraph2Data.currentData())
        )
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
        self.ui.frameGraph.setEnabled(False)
        for widget in [self.ui.labelComSpeed, self.ui.labelErrRate]:
            set_color(widget, None)
            widget.setText("[N/A]")
        self.curve1.setData(x=[], y=[])
        self.curve2.setData(x=[], y=[])
        self.ui.labelGraphInfo.setText("No Info")
        self.ui.horizontalSlider.setRange(0, 10)
        self.ui.horizontalSlider.setValue((2, 8))
        self.ui.labelBufferSize.setText("N/A")
        self.ui.labelDisplayRange.setText("N/A")
        set_color(self.ui.labelBufferSize, None)
        self.ui.frameGraphControl.setEnabled(False)

    def open_state_ui(self):
        self.ui.frameGraph.setEnabled(True)
        self.ui.frameGraphControl.setEnabled(True)

    def apply_theme(self, sys_theme):
        self.ui.widgetGraph1.setBackground(None)
        self.ui.widgetGraph2.setBackground(None)
        self.CustomTitleBar.set_theme(sys_theme)
        self.update_pen()
        self.ui.horizontalSlider.setStyleSheet("background: none;")
        self.ui.horizontalSlider.setBarVisible(False)
        for panel in self.panels:
            panel.apply_theme()

    def on_panel_link_toggled(self, panel):
        try:
            if panel.linked:
                was_last = sum(p.linked for p in self.panels) == 1
                self.connection.unlink_panel(panel)
                if was_last:
                    self.draw_graph_timer.stop()
                    if self.graph_record_save_timer.isActive():
                        self.on_btnGraphRecord_clicked()
                    self.close_state_ui()
            else:
                first_link = not self.connection.is_open
                self.connection.link_panel(panel, self.data_fps)
                panel.store.clear()
                if first_link:
                    self.draw_graph_timer.start(
                        round(1000 / min(self.data_fps, setting.ui.graph_max_fps))
                    )
                    self.open_state_ui()
        except Exception as e:
            logger.exception(f"Failed to link/unlink {panel.display_name}")
            CustomMessageBox(self, self.tr("连接失败"), str(e))

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
        self.ui.widgetGraph1.setBackground(None)
        self.ui.widgetGraph2.setBackground(None)
        self.ui.widgetGraph1.setLabel(
            "left", CHANNEL_BY_KEY["voltage"].label, units=CHANNEL_BY_KEY["voltage"].unit
        )
        self.ui.widgetGraph2.setLabel(
            "left", CHANNEL_BY_KEY["current"].label, units=CHANNEL_BY_KEY["current"].unit
        )
        self.ui.widgetGraph1.showGrid(x=True, y=True)
        self.ui.widgetGraph2.showGrid(x=True, y=True)
        self.ui.widgetGraph1.setMouseEnabled(x=False, y=False)
        self.ui.widgetGraph2.setMouseEnabled(x=False, y=False)
        self.pen1 = pg.mkPen(color=setting.get_color("line1"), width=1)
        self.pen2 = pg.mkPen(color=setting.get_color("line2"), width=1)
        self.curve1 = self.ui.widgetGraph1.plot(pen=self.pen1, clear=True)
        self.curve2 = self.ui.widgetGraph2.plot(pen=self.pen2, clear=True)
        self._graph_auto_scale_flag = True
        axis1 = FmtAxisItem(orientation="left")
        axis2 = FmtAxisItem(orientation="left")
        axis1.syncWith(axis2, left_spacing=True)
        axis2.syncWith(axis1, left_spacing=True)
        self.ui.widgetGraph1.setAxisItems(axisItems={"left": axis1})
        self.ui.widgetGraph2.setAxisItems(axisItems={"left": axis2})
        if self.panels:
            self.set_graph1_data((self.panels[0].device_id, "voltage"), skip_update=True)
            self.set_graph2_data((self.panels[0].device_id, "current"), skip_update=True)

    def update_pen(self):
        self.pen1.setColor(QtGui.QColor(setting.get_color("line1")))
        self.pen2.setColor(QtGui.QColor(setting.get_color("line2")))

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
        if self.graph_keep_flag:
            return
        data1 = self.ui.comboGraph1Data.currentData()
        data2 = self.ui.comboGraph2Data.currentData()
        panel1 = self._panel_by_id.get(data1[0]) if data1 else None
        panel2 = self._panel_by_id.get(data2[0]) if data2 else None
        key1 = data1[1] if data1 else None
        key2 = data2[1] if data2 else None
        sync_panel = panel1 or panel2 or (self.panels[0] if self.panels else None)
        if sync_panel is None:
            return
        with sync_panel.store.sync_lock:
            update_count = sync_panel.store.update_count
            if update_count > setting.ui.display_pts + 5:
                left, right = self.ui.horizontalSlider.sliderPosition()
                max_ = self.ui.horizontalSlider._maximum
                syncing = right == max_
                allfit = syncing and (left == 0)
                if not self.ui.horizontalSlider.isEnabled():
                    self.ui.horizontalSlider.setEnabled(True)
                    allfit = False
                    left = max(1, right - setting.ui.display_pts)
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
            # Fetch whichever series belong to sync_panel here, still under its
            # lock - for the common case (panel1/panel2 both resolve to the
            # same store) this keeps the slider math and the series read in
            # one critical section, exactly like the single-store original.
            series1 = (
                sync_panel.store.get_series(key1, display_pts, r_offset)
                if panel1 is sync_panel
                else None
            )
            series2 = (
                sync_panel.store.get_series(key2, display_pts, r_offset)
                if panel2 is sync_panel
                else None
            )
        if series1 is None:
            if panel1 is None:
                series1 = (None,) * 7
            else:
                with panel1.store.sync_lock:
                    series1 = panel1.store.get_series(key1, display_pts, r_offset)
        if series2 is None:
            if panel2 is None:
                series2 = (None,) * 7
            else:
                with panel2.store.sync_lock:
                    series2 = panel2.store.get_series(key2, display_pts, r_offset)
        data1, time1, start_index1, to_index1, max1, min1, avg1 = series1
        data2, time2, start_index2, to_index2, max2, min2, avg2 = series2
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

        _ = CHANNEL_SHORT.get(key1)
        if data1 is not None and data1.size > 0:
            self.curve1.setData(x=time1, y=data1)
            text1 = f"{_}avg: {float_str(avg1)}  {_}max: {float_str(max1)}  {_}min: {float_str(min1)}  {_}pp: {float_str(max1 - min1)}"
        else:
            self.curve1.setData(x=[], y=[])
            text1 = f"{_}avg: N/A  {_}max: N/A  {_}min: N/A  {_}pp: N/A"
        _ = CHANNEL_SHORT.get(key2)
        if data2 is not None and data2.size > 0:
            self.curve2.setData(x=time2, y=data2)
            text2 = f"{_}avg: {float_str(avg2)}  {_}max: {float_str(max2)}  {_}min: {float_str(min2)}  {_}pp: {float_str(max2 - min2)}"
        else:
            self.curve2.setData(x=[], y=[])
            text2 = f"{_}avg: N/A  {_}max: N/A  {_}min: N/A  {_}pp: N/A"
        if data1 is not None and data2 is not None:
            text = text1 + "  |  " + text2
        elif data1 is not None:
            text = text1
        elif data2 is not None:
            text = text2
        else:
            text = "No Info"
        self.ui.labelGraphInfo.setText(text)
        if self._graph_auto_scale_flag:
            if data1 is not None and time1.size != 0:
                if max1 != np.inf and min1 != -np.inf:
                    add1 = max(0.01, (max1 - min1) * 0.05)
                    self.ui.widgetGraph1.setYRange(min1 - add1, max1 + add1)
                    self.ui.widgetGraph1.setXRange(
                        time1[start_index1], time1[to_index1 - 1]
                    )
            if data2 is not None and time2.size != 0:
                if max2 != np.inf and min2 != -np.inf:
                    add2 = max(0.01, (max2 - min2) * 0.05)
                    self.ui.widgetGraph2.setYRange(min2 - add2, max2 + add2)
                    self.ui.widgetGraph2.setXRange(
                        time2[start_index2], time2[to_index2 - 1]
                    )

    def set_graph1_data(self, data, skip_update=False):
        if data is None:
            self.ui.widgetGraph1.hide()
            return
        self.ui.widgetGraph1.show()
        _, key = data
        ch = CHANNEL_BY_KEY[key]
        self.ui.widgetGraph1.setLabel("left", ch.label, units=ch.unit)
        if not skip_update and self.draw_graph_timer.isActive():  # force update axis
            self.ui.widgetGraph1.setYRange(0, 0)
            self.ui.widgetGraph2.setYRange(0, 0)
            self.draw_graph()

    def set_graph2_data(self, data, skip_update=False):
        if data is None:
            self.ui.widgetGraph2.hide()
            return
        self.ui.widgetGraph2.show()
        _, key = data
        ch = CHANNEL_BY_KEY[key]
        self.ui.widgetGraph2.setLabel("left", ch.label, units=ch.unit)
        if not skip_update and self.draw_graph_timer.isActive():
            self.ui.widgetGraph1.setYRange(0, 0)
            self.ui.widgetGraph2.setYRange(0, 0)
            self.draw_graph()

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
        self.curve1.setData(x=[], y=[])
        self.curve2.setData(x=[], y=[])
        set_color(self.ui.labelBufferSize, None)

    @QtCore.pyqtSlot()
    def on_btnGraphKeep_clicked(self):
        self.graph_keep_flag = not self.graph_keep_flag
        if self.graph_keep_flag:
            self.ui.btnGraphKeep.setText(self.tr("解除"))
            self.ui.comboGraph1Data.setEnabled(False)
            self.ui.comboGraph2Data.setEnabled(False)
        else:
            self.ui.btnGraphKeep.setText(self.tr("保持"))
            self.ui.comboGraph1Data.setEnabled(True)
            self.ui.comboGraph2Data.setEnabled(True)
        mouse_enabled = self.graph_keep_flag or (not self._graph_auto_scale_flag)
        self.ui.frameGraphControl.setEnabled(not mouse_enabled)
        self.ui.widgetGraph1.setMouseEnabled(x=mouse_enabled, y=mouse_enabled)
        self.ui.widgetGraph2.setMouseEnabled(x=mouse_enabled, y=mouse_enabled)

    @QtCore.pyqtSlot()
    def on_btnGraphAutoScale_clicked(self):
        self._graph_auto_scale_flag = not self._graph_auto_scale_flag
        if self._graph_auto_scale_flag:
            self.ui.btnGraphAutoScale.setText(self.tr("适应"))
        else:
            self.ui.btnGraphAutoScale.setText(self.tr("手动"))
        mouse_enabled = self.graph_keep_flag or (not self._graph_auto_scale_flag)
        self.ui.frameGraphControl.setEnabled(not mouse_enabled)
        self.ui.widgetGraph1.setMouseEnabled(x=mouse_enabled, y=mouse_enabled)
        self.ui.widgetGraph2.setMouseEnabled(x=mouse_enabled, y=mouse_enabled)

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
MainWindow.ui.btnRecordFloatWindow.clicked.connect(FloatingWindow.switch_visibility)
DialogSettings.devices_changed.connect(MainWindow.rebuild_panels)


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
    MainWindow.show()
    MainWindow.activateWindow()
    sys.exit(app.exec_())


if __name__ == "__main__":
    show_app()
