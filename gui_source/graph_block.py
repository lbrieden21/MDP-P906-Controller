from typing import Dict, List

import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from mdp_custom import FmtAxisItem


class DeviceChip(QtWidgets.QToolButton):
    """Checkable per-device toggle for one GraphBlock, tinted with that
    device's user-assigned color (settings_model.DeviceSettings.color) so it
    visually matches the curve it controls."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.setCheckable(True)
        self.setChecked(True)
        self.setAutoRaise(True)
        self.setText(panel.display_name)
        self.toggled.connect(lambda _: self.restyle())
        self.restyle()

    def restyle(self):
        color = f"#{self.panel.settings.color.lstrip('#')}"
        if self.isChecked():
            self.setStyleSheet(
                "QToolButton {"
                f"background-color: {color}; color: black; border: 1px solid {color};"
                "border-radius: 3px; padding: 2px 8px; font-weight: bold; }"
            )
        else:
            self.setStyleSheet(
                "QToolButton {"
                f"border: 1px solid {color}; border-radius: 3px; padding: 2px 8px; }}"
            )


class GraphBlock(QtWidgets.QWidget):
    """One data-type graph (voltage/current/power/resistance): a title, a row
    of per-device toggle chips, a PlotWidget with one curve per device, and a
    per-device avg/max/min/pp stats line below it."""

    def __init__(self, channel_key: str, ch_spec, parent=None):
        super().__init__(parent)
        self.channel_key = channel_key
        self.ch_spec = ch_spec
        self.chips: Dict[str, DeviceChip] = {}
        self.curves: Dict[str, pg.PlotDataItem] = {}
        self.pens: Dict[str, object] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.title_label = QtWidgets.QLabel(ch_spec.label)
        font = QtGui.QFont()
        font.setBold(True)
        self.title_label.setFont(font)
        header.addWidget(self.title_label)
        self.chips_layout = QtWidgets.QHBoxLayout()
        self.chips_layout.setContentsMargins(8, 0, 0, 0)
        self.chips_layout.setSpacing(4)
        header.addLayout(self.chips_layout)
        header.addStretch(1)
        layout.addLayout(header)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground(None)
        self.plot_widget.setLabel("left", ch_spec.label, units=ch_spec.unit)
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setMouseEnabled(x=False, y=False)
        self.axis = FmtAxisItem(orientation="left")
        self.plot_widget.setAxisItems(axisItems={"left": self.axis})
        layout.addWidget(self.plot_widget, stretch=1)

        self.stats_label = QtWidgets.QLabel("No Info")
        self.stats_label.setTextFormat(QtCore.Qt.RichText)
        self.stats_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.stats_label)

    def set_panels(self, panels: List) -> None:
        """(Re)build device chips/curves to match the current panel list,
        preserving each surviving device's checked state and color."""
        seen = set()
        for panel in panels:
            seen.add(panel.device_id)
            if panel.device_id not in self.chips:
                chip = DeviceChip(panel)
                self.chips_layout.addWidget(chip)
                self.chips[panel.device_id] = chip
                pen = pg.mkPen(color=f"#{panel.settings.color.lstrip('#')}", width=1)
                self.pens[panel.device_id] = pen
                self.curves[panel.device_id] = self.plot_widget.plot(
                    pen=pen, connect="finite"
                )
        for device_id in list(self.chips):
            if device_id not in seen:
                chip = self.chips.pop(device_id)
                self.chips_layout.removeWidget(chip)
                chip.deleteLater()
                self.plot_widget.removeItem(self.curves.pop(device_id))
                self.pens.pop(device_id, None)

    def refresh_device_color(self, panel) -> None:
        """Re-apply panel.settings.color to this device's chip and curve
        after it's been changed in the Settings dialog (set_panels only
        colors a chip/curve the first time it sees a device_id)."""
        chip = self.chips.get(panel.device_id)
        if chip is not None:
            chip.restyle()
        curve = self.curves.get(panel.device_id)
        if curve is not None:
            pen = pg.mkPen(color=f"#{panel.settings.color.lstrip('#')}", width=1)
            self.pens[panel.device_id] = pen
            curve.setPen(pen)

    def checked_panels(self, panels: List) -> List:
        return [
            p
            for p in panels
            if self.chips.get(p.device_id) is not None and self.chips[p.device_id].isChecked()
        ]

    def set_mouse_enabled(self, enabled: bool) -> None:
        self.plot_widget.setMouseEnabled(x=enabled, y=enabled)

    def clear(self) -> None:
        for curve in self.curves.values():
            curve.setData(x=[], y=[])
        self.stats_label.setText("No Info")
