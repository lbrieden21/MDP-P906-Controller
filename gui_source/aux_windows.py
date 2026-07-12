import numpy as np
import pyqtgraph as pg
from loguru import logger
from PyQt5 import QtCore, QtGui, QtWidgets
from qframelesswindow import FramelessWindow

from app_context import center_window, set_color
from mdp_custom import CustomTitleBar
from settings_model import setting


class DeviceValueBlock(QtWidgets.QWidget):
    """One device's title + V/I/P readout, stacked N-high inside
    TransparentFloatingWindow. Mirrors the layout the floating window used
    to build directly for its single hardcoded device."""

    BLOCK_HEIGHT_EXPANDED = 140
    BLOCK_HEIGHT_COLLAPSED = 80

    def __init__(self, display_name, parent=None):
        super().__init__(parent)

        font_title = QtGui.QFont()
        font_title.setFamily("Sarasa Fixed SC SemiBold")
        font_title.setPointSize(10)

        font_value = QtGui.QFont()
        font_value.setFamily("Sarasa Fixed SC SemiBold")
        font_value.setPointSize(12)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.name_label = QtWidgets.QLabel(f" {display_name} ", self)
        self.name_label.setFont(font_title)
        set_color(self.name_label, "rgb(200, 200, 200)")
        layout.addWidget(self.name_label, alignment=QtCore.Qt.AlignCenter)

        self.v_label = QtWidgets.QLabel(self.tr("电压 U"), self)
        self.v_label.setFont(font_title)
        set_color(self.v_label, setting.get_color("general_red", "dark"))
        layout.addWidget(self.v_label)
        self.voltage_label = QtWidgets.QLabel("", self)
        self.voltage_label.setFont(font_value)
        set_color(self.voltage_label, setting.get_color("general_red", "dark"))
        layout.addWidget(self.voltage_label)

        self.i_label = QtWidgets.QLabel(self.tr("电流 I"), self)
        self.i_label.setFont(font_title)
        set_color(self.i_label, setting.get_color("general_green", "dark"))
        layout.addWidget(self.i_label)
        self.current_label = QtWidgets.QLabel("", self)
        self.current_label.setFont(font_value)
        set_color(self.current_label, setting.get_color("general_green", "dark"))
        layout.addWidget(self.current_label)

        self.p_label = QtWidgets.QLabel(self.tr("功率 P"), self)
        self.p_label.setFont(font_title)
        set_color(self.p_label, setting.get_color("general_blue", "dark"))
        layout.addWidget(self.p_label)
        self.power_label = QtWidgets.QLabel("", self)
        self.power_label.setFont(font_value)
        set_color(self.power_label, setting.get_color("general_blue", "dark"))
        layout.addWidget(self.power_label)

        self.update_values(0, 0, 0)

    def update_values(self, u, i, p):
        self.voltage_label.setText(f"{u:06.3f} V")
        self.current_label.setText(f"{i:06.3f} A")
        if p < 100:
            self.power_label.setText(f"{p:06.3f} W")
        else:
            self.power_label.setText(f"{p:06.2f} W")

    def set_labels_visible(self, visible):
        self.v_label.setVisible(visible)
        self.i_label.setVisible(visible)
        self.p_label.setVisible(visible)


class TransparentFloatingWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()

        # 设置窗口为无边框和置顶
        self.setWindowFlags(
            QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.FramelessWindowHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)

        self.setWindowTitle("MDP-P906 Floating Monitor")
        self.setWindowOpacity(0.95)

        # 创建主窗口布局
        window_layout = QtWidgets.QVBoxLayout(self)
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.setSpacing(0)

        # 创建Frame
        frame = QtWidgets.QFrame(self)
        frame.setStyleSheet("QFrame { background-color: rgba(17, 17, 21, 100); }")
        window_layout.addWidget(frame)

        # 每设备一个 DeviceValueBlock，纵向堆叠
        self.blocks_layout = QtWidgets.QVBoxLayout(frame)
        self.blocks_layout.setContentsMargins(0, 0, 0, 0)
        self.blocks_layout.setSpacing(0)

        self.label_visable = True
        self._blocks = {}

        self.dragging = False
        self.offset = None

        self._recompute_size()

    def set_devices(self, panels):
        for block in self._blocks.values():
            self.blocks_layout.removeWidget(block)
            block.deleteLater()
        self._blocks = {}
        for panel in panels:
            block = DeviceValueBlock(panel.display_name, self)
            block.set_labels_visible(self.label_visable)
            self.blocks_layout.addWidget(block)
            self._blocks[panel.device_id] = block
            panel.values_signal.connect(
                lambda v, i, p, device_id=panel.device_id: self._update_block(
                    device_id, v, i, p
                )
            )
        self._recompute_size()

    def _update_block(self, device_id, u, i, p):
        block = self._blocks.get(device_id)
        if block is not None:
            block.update_values(u, i, p)

    def _recompute_size(self):
        n = max(1, len(self._blocks))
        per_block = (
            DeviceValueBlock.BLOCK_HEIGHT_EXPANDED
            if self.label_visable
            else DeviceValueBlock.BLOCK_HEIGHT_COLLAPSED
        )
        self.setFixedSize(75, per_block * n)

    def center_window(self):
        self.screen = QtWidgets.QApplication.primaryScreen().geometry()
        x = self.screen.width() - self.width() - 10
        y = self.screen.height() // 2 - self.height() // 2
        self.move(x, y)
        self.setWindowOpacity(0.95)

    def switch_visibility(self):
        if self.isVisible():
            self.close()
        else:
            self.center_window()
            self.show()

    def wheelEvent(self, event):
        current_opacity = self.windowOpacity()
        if event.angleDelta().y() > 0:
            new_opacity = min(1.0, current_opacity + 0.1)
        else:
            new_opacity = max(0.1, current_opacity - 0.1)
        self.setWindowOpacity(new_opacity)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.dragging = True
            self.offset = event.pos()

    def mouseMoveEvent(self, event):
        if self.dragging:
            move = event.globalPos() - self.offset
            x = max(0, min(move.x(), self.screen.width() - self.width()))
            y = max(0, min(move.y(), self.screen.height() - self.height()))
            self.move(x, y)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.dragging = False

    def mouseDoubleClickEvent(self, event):
        self.switch_label_visibility()

    def switch_label_visibility(self):
        self.label_visable = not self.label_visable
        for block in self._blocks.values():
            block.set_labels_visible(self.label_visable)
        self._recompute_size()

    def contextMenuEvent(self, event):
        menu = QtWidgets.QMenu(self)

        toggle_action = menu.addAction(
            self.tr("折叠") if self.label_visable else self.tr("展开")
        )
        close_action = menu.addAction(self.tr("关闭"))

        action = menu.exec_(event.globalPos())

        if action == toggle_action:
            self.switch_label_visibility()
        elif action == close_action:
            self.close()


class ResultGraphWindow(QtWidgets.QDialog, FramelessWindow):
    def __init__(self, global_font, parent=None):
        super().__init__(parent)
        # 创建主布局
        self.main_layout = QtWidgets.QVBoxLayout()

        # padding
        label = QtWidgets.QLabel("")
        label.setFixedHeight(25)
        self.main_layout.addWidget(label)
        self.setLayout(self.main_layout)

        # 设置自定义标题栏
        self.setWindowTitle("MDP-P906 Result Graph Window")
        self.CustomTitleBar = CustomTitleBar(self, "MDP-P906 Result Graph Window")
        self.CustomTitleBar.set_theme("dark")
        self.CustomTitleBar.set_allow_double_toggle_max(False)
        self.CustomTitleBar.set_full_btn_enabled(False)
        self.setTitleBar(self.CustomTitleBar)

        # 创建图表组件
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground(None)
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setLabel("left", "Y-Axis")
        self.plot_widget.setLabel("bottom", "X-Axis")

        # 创建曲线
        self.pen = pg.mkPen(color=setting.get_color("line1"), width=2)
        self.fit_pen = pg.mkPen(color=setting.get_color("line2"), width=2)
        self.curve = self.plot_widget.plot(pen=self.pen)
        self.curve.setData([], [])
        self.curve_fit = self.plot_widget.plot(pen=self.fit_pen)
        self.curve_fit.setData([], [])
        self.plot_widget.autoRange()
        self.main_layout.addWidget(self.plot_widget)

        self.hlayout = QtWidgets.QHBoxLayout()

        self.btnXYSwap = QtWidgets.QPushButton("X/Y")
        self.hlayout.addWidget(self.btnXYSwap)
        self.btnXYSwap.clicked.connect(self.swapXY)

        label = QtWidgets.QLabel(self.tr("多项式拟合次数:"))
        label.setFont(global_font)
        self.hlayout.addWidget(label)

        self.spinBoxFit = QtWidgets.QSpinBox()
        self.spinBoxFit.setRange(0, 8)
        self.spinBoxFit.setValue(0)
        self.hlayout.addWidget(self.spinBoxFit)
        self.spinBoxFit.valueChanged.connect(self.update_fit_result)

        self.labelFitResult = QtWidgets.QLabel("N/A")
        self.labelFitResult.setFont(global_font)
        self.hlayout.addWidget(self.labelFitResult)

        self.hlayout.setStretch(0, 0)
        self.hlayout.setStretch(1, 0)
        self.hlayout.setStretch(2, 0)
        self.hlayout.setStretch(3, 1)

        self.main_layout.addLayout(self.hlayout)

        self.x_data = []
        self.y_data = []

    def apply_theme(self, sys_theme):
        self.pen.setColor(QtGui.QColor(setting.get_color("line1")))

    def format_fit_result(self, result):
        result = list(result)
        for i in range(len(result)):
            if abs(result[i]) < 1e-9:
                result[i] = 0
        text = "y = ("
        for i, coef in enumerate(result):
            if i != 0:
                text += " + ("
            text += f"{coef:0.4g}"
            ml = len(result) - i - 1
            if ml > 1:
                text += f"x^{ml}"
            elif ml == 1:
                text += "x"
            text += ")"
        return text

    def update_fit_result(self, _=None):
        if len(self.x_data) + len(self.y_data) < 4 or self.spinBoxFit.value() < 1:
            self.labelFitResult.setText("N/A")
            self.curve_fit.setData([], [])
            return
        logger.info(
            f"fit {len(self.x_data)} points with {self.spinBoxFit.value()} order"
        )
        z = np.polyfit(self.x_data, self.y_data, self.spinBoxFit.value())

        y_pred = np.polyval(z, self.x_data)
        rmse = np.sqrt(np.mean((np.array(self.y_data) - y_pred) ** 2))
        logger.info(f"fit result: {z} with rmse={rmse:0.4g}")
        self.labelFitResult.setText(f"rmse={rmse:0.4f} {self.format_fit_result(z)}")
        SAMPLE_N = 1000
        x_fit = np.linspace(min(self.x_data), max(self.x_data), SAMPLE_N)
        y_fit = np.polyval(z, x_fit)
        self.curve_fit.setData(x_fit, y_fit)

    def showData(self, x, y, x_label, y_label, x_unit, y_unit, title, disable_swap):
        self.x_data = x
        self.y_data = y
        self.x_label = x_label
        self.y_label = y_label
        self.x_unit = x_unit
        self.y_unit = y_unit
        self.title = title
        self.curve.setData(x, y)
        self.plot_widget.setLabel("bottom", x_label, units=x_unit)
        self.plot_widget.setLabel("left", y_label, units=y_unit)
        self.plot_widget.autoRange()
        self.CustomTitleBar.set_name(title)
        if disable_swap:
            self.btnXYSwap.setVisible(False)
        else:
            self.btnXYSwap.setVisible(True)
        if hasattr(self, "vLine"):
            self.plot_widget.removeItem(self.vLine)
        if hasattr(self, "hLine"):
            self.plot_widget.removeItem(self.hLine)
        if not self.isVisible():
            self.spinBoxFit.setValue(0)
        self.update_fit_result()
        if not self.isVisible():
            self.show()
            center_window(self, 800, 600)

    def swapXY(self):
        self.x_data, self.y_data = self.y_data, self.x_data
        self.x_label, self.y_label = self.y_label, self.x_label
        self.x_unit, self.y_unit = self.y_unit, self.x_unit
        self.showData(
            self.x_data,
            self.y_data,
            self.x_label,
            self.y_label,
            self.x_unit,
            self.y_unit,
            self.title,
            False,
        )

    def highlightPoint(self, x, y):
        if hasattr(self, "vLine"):
            self.plot_widget.removeItem(self.vLine)
        if hasattr(self, "hLine"):
            self.plot_widget.removeItem(self.hLine)

        self.vLine = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen(setting.get_color("line2")),
        )
        self.hLine = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen(setting.get_color("line2")),
        )
        self.vLine.setPos(x)
        self.hLine.setPos(y)
        self.plot_widget.addItem(self.vLine)
        self.plot_widget.addItem(self.hLine)
