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

from mdp_custom import CustomMessageBox, CustomTitleBar
from settings_model import setting
from device_core import GraphCapture, csv_unit
from device_panel import DEVICE_PANEL_TYPES
import device_panel_p906  # noqa: F401 (registers P906 in DEVICE_PANEL_TYPES)
import device_panel_l1060  # noqa: F401 (registers L1060 in DEVICE_PANEL_TYPES)
from graph_view import GraphView
from connection import ConnectionManager
from dialogs import MDPGraphics, MDPSettings
from aux_windows import ResultGraphWindow, TransparentFloatingWindow

update_pyqtgraph_setting()


class MDPMainwindow(QtWidgets.QMainWindow, FramelessWindow):  # QtWidgets.QMainWindow
    close_signal = QtCore.pyqtSignal()
    panels_changed = QtCore.pyqtSignal()
    data_fps = 50
    graph_record_flag = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.panels = []
        self._panel_by_id = {}
        # Keyed by device_id in the "separate" graph layout; the single
        # shared view is keyed by None.
        self.graph_views = {}
        self.initTimer()
        self._init_device_selector()
        for dev in setting.devices:
            self._add_panel_for_device(dev)
        self._rebuild_device_selector()
        self._rebuild_graph_views()
        self.set_device_layout(setting.ui.device_layout)
        self.connection = ConnectionManager(self.panels)
        self.initSignals()
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
        # Placeholder capture: _rebuild_graph_views() binds each panel to
        # its graph view's capture.
        panel = DEVICE_PANEL_TYPES[dev.type](GraphCapture(), self, dev)
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
        self._rebuild_graph_views()
        self.set_device_layout(setting.ui.device_layout)
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
        self._update_graph_view_visibility()
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

    def _rebuild_graph_views(self):
        """Replace the graph views to match setting.ui.graph_layout and the
        current panels: one view for every panel in "shared", one view per
        panel in "separate". Graph data is cleared, since separate
        per-device timelines can't be merged into one shared timeline."""
        for view in self.graph_views.values():
            view.capture.stop()
            self.ui.layoutGraphViews.removeWidget(view)
            view.setParent(None)
            view.deleteLater()
        self.graph_views = {}
        if setting.ui.graph_layout == "separate":
            for panel in self.panels:
                self.graph_views[panel.device_id] = GraphView(
                    [panel], panel.display_name, self.ui.widgetGraphViews
                )
        else:
            self.graph_views[None] = GraphView(self.panels, parent=self.ui.widgetGraphViews)
        for view in self.graph_views.values():
            view.redraw_requested.connect(partial(self._redraw_view, view))
            self.ui.layoutGraphViews.addWidget(view, stretch=1)
            view.on_btnGraphClear_clicked(skip_confirm=True)
            view.refresh_link_state()
        self._update_graph_view_visibility()

    def set_graph_layout(self, mode: str):
        """"shared" draws every device in one graph section; "separate"
        gives each device its own section with its own capture."""
        setting.ui.graph_layout = mode
        self._rebuild_graph_views()

    def _update_graph_view_visibility(self):
        """In the "separate" graph layout with the "single" device layout,
        show only the selected device's view; otherwise show every view. A
        view that becomes visible is drawn immediately rather than on the
        next timer tick."""
        single = (
            setting.ui.graph_layout == "separate"
            and setting.ui.device_layout == "single"
        )
        for device_id, view in self.graph_views.items():
            visible = not single or device_id == self._selected_device_id
            became_visible = visible and view.isHidden()
            view.setVisible(visible)
            if became_visible and self.draw_graph_timer.isActive():
                view.draw()

    def _redraw_view(self, view):
        if self.draw_graph_timer.isActive() and view.isVisible():
            view.draw()

    def on_device_color_changed(self, device_id: str):
        """A device's wheel color was edited and saved in the Settings
        dialog: push it live to the hardware wheel (if linked) and refresh
        its graph chip/curve, neither of which otherwise gets re-touched
        after the panel/curve was first created."""
        panel = self._panel_by_id.get(device_id)
        if panel is None:
            return
        panel.refresh_led_color()
        for view in self.graph_views.values():
            view.refresh_device_color(panel)
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

    def apply_theme(self, sys_theme):
        for view in self.graph_views.values():
            view.apply_theme()
        self.CustomTitleBar.set_theme(sys_theme)
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
                    # (GraphView.refresh_link_state leaves it on screen
                    # instead of clearing it) so the buffer slider still
                    # scrubs the existing curves; only stop it once there's
                    # nothing left to draw.
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
        except Exception as e:
            logger.exception(f"Failed to link/unlink {panel.display_name}")
            CustomMessageBox(self, self.tr("连接失败"), str(e))
        for view in self.graph_views.values():
            view.refresh_link_state()
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
        for view in self.graph_views.values():
            view.set_data_length(length)

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

    def draw_graph(self):
        self.ui.labelFps.setText(
            " | ".join(f"{p.display_name}: {p.fps_counter.fps:.1f}Hz" for p in self.panels)
        )
        for view in self.graph_views.values():
            if view.isVisible():
                view.draw()

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
                times = panel.store.ordered(panel.store.times)
                cols = [times] + [
                    panel.store.ordered(panel.store.series[ch.key])
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
DialogGraphics.graph_layout_requested.connect(MainWindow.set_graph_layout)
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
