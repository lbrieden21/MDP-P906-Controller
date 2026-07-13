from PyQt5 import QtCore, QtGui, QtWidgets
from loguru import logger
from qframelesswindow import FramelessWindow
from serial.tools.list_ports import comports
from superqt.utils import signals_blocked

from app_context import NUMBA_ENABLED, OPENGL_AVALIABLE, center_window, set_color, update_pyqtgraph_setting
from mdp_custom import CustomMessageBox, CustomTitleBar
from mdp_gui_template import Ui_DialogGraphics, Ui_DialogSettings
from settings_model import SETTING_FILE, DeviceSettings, setting


class MDPSettings(QtWidgets.QDialog, FramelessWindow):
    devices_changed = QtCore.pyqtSignal()
    device_color_changed = QtCore.pyqtSignal(str)

    def __init__(self, connection_manager, parent=None):
        super().__init__(parent)
        self.connection_manager = connection_manager
        self._prev_device_idx = 0

        self.ui = Ui_DialogSettings()
        self.ui.setupUi(self)
        self.CustomTitleBar = CustomTitleBar(self, self.tr("连接设置"))
        self.CustomTitleBar.set_theme("dark")
        self.CustomTitleBar.set_allow_double_toggle_max(False)
        self.CustomTitleBar.set_min_btn_enabled(False)
        self.CustomTitleBar.set_max_btn_enabled(False)
        self.CustomTitleBar.set_full_btn_enabled(False)
        self.CustomTitleBar.set_close_btn_enabled(False)
        self.setTitleBar(self.CustomTitleBar)
        # self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint)
        for lineedit in [
            self.ui.lineEditAddr1,
            self.ui.lineEditAddr2,
            self.ui.lineEditAddr3,
            self.ui.lineEditAddr4,
            self.ui.lineEditAddr5,
            self.ui.lineEditColor,
            self.ui.lineEditIdcode,
        ]:
            lineedit.textChanged.connect(
                lambda t=None, le=lineedit: self.check_hex_input(le)
            )

    def apply_theme(self, sys_theme):
        self.CustomTitleBar.set_theme(sys_theme)

    def _current_device(self) -> DeviceSettings:
        idx = self.ui.comboBoxDevice.currentIndex()
        if not (0 <= idx < len(setting.devices)):
            idx = 0
        return setting.devices[idx]

    def refresh_device_combo(self, select_index=None):
        if select_index is None:
            select_index = self.ui.comboBoxDevice.currentIndex()
        select_index = max(0, min(select_index, len(setting.devices) - 1))
        with signals_blocked(self.ui.comboBoxDevice):
            self.ui.comboBoxDevice.clear()
            for dev in setting.devices:
                self.ui.comboBoxDevice.addItem(f"{dev.name} ({dev.id})")
            self.ui.comboBoxDevice.setCurrentIndex(select_index)
        self._prev_device_idx = select_index
        self.ui.btnDeviceRemove.setEnabled(len(setting.devices) > 1)

    @QtCore.pyqtSlot(int)
    def on_comboBoxDevice_currentIndexChanged(self, index):
        if index < 0:
            return
        if 0 <= self._prev_device_idx < len(setting.devices):
            self.save_device_settings(setting.devices[self._prev_device_idx])
        self._prev_device_idx = index
        self.initValues()

    @QtCore.pyqtSlot()
    def on_btnDeviceAdd_clicked(self):
        if self.connection_manager.is_open:
            CustomMessageBox(self, self.tr("错误"), self.tr("请先断开连接"))
            return
        self.save_device_settings(self._current_device())
        dtype = self.ui.comboBoxNewDeviceType.currentText()
        prefix = dtype.lower()
        existing_ids = {d.id for d in setting.devices}
        n = len(setting.devices) + 1
        new_id = f"{prefix}-{n}"
        while new_id in existing_ids:
            n += 1
            new_id = f"{prefix}-{n}"
        setting.devices.append(DeviceSettings(type=dtype, id=new_id, name=f"{dtype} #{n}"))
        setting.save(SETTING_FILE)
        self.refresh_device_combo(select_index=len(setting.devices) - 1)
        self.initValues()
        self.devices_changed.emit()

    @QtCore.pyqtSlot()
    def on_btnDeviceRemove_clicked(self):
        if self.connection_manager.is_open:
            CustomMessageBox(self, self.tr("错误"), self.tr("请先断开连接"))
            return
        if len(setting.devices) <= 1:
            return
        if not CustomMessageBox.question(
            self, self.tr("警告"), self.tr("确定要删除该设备吗？")
        ):
            return
        idx = self.ui.comboBoxDevice.currentIndex()
        del setting.devices[idx]
        setting.save(SETTING_FILE)
        self.refresh_device_combo(select_index=max(0, idx - 1))
        self.initValues()
        self.devices_changed.emit()

    def check_hex_input(self, lineedit: QtWidgets.QLineEdit):
        text = lineedit.text()
        new_text = ""
        for char in text:
            if char in "0123456789ABCDEFabcdef":
                new_text += char.upper()
            else:
                new_text += ""
        lineedit.setText(new_text)

    def initValues(self):
        dev = self._current_device()
        self.ui.spinBoxBaud.setValue(setting.adapter.baudrate)
        self.ui.lineEditAddr1.setText(setting.adapter.address.split(":")[0])
        self.ui.lineEditAddr2.setText(setting.adapter.address.split(":")[1])
        self.ui.lineEditAddr3.setText(setting.adapter.address.split(":")[2])
        self.ui.lineEditAddr4.setText(setting.adapter.address.split(":")[3])
        self.ui.lineEditAddr5.setText(setting.adapter.address.split(":")[4])
        self.ui.spinBoxFreq.setValue(setting.adapter.freq)
        self.ui.comboBoxPower.setCurrentText(setting.adapter.txpower)
        self.ui.lineEditIdcode.setText(dev.idcode)
        self.ui.lineEditColor.setText(dev.color)
        self.ui.spinBoxM01.setValue(int(dev.m01ch[3]))
        self.ui.comboBoxPort.setCurrentText(
            setting.adapter.comport if setting.adapter.comport else self.tr("自动")
        )
        self.ui.comboBoxBlink.setCurrentText(
            self.tr("闪烁") if dev.blink else self.tr("常亮")
        )
        self.ui.checkBoxOutputWarn.setChecked(dev.output_warning)
        self.ui.checkBoxSetLock.setChecked(dev.lock_when_output)
        self.ui.checkBoxIgnoreHWLock.setChecked(dev.ignore_hw_lock)
        self.ui.btnColorIndicator.setStyleSheet(
            f"background-color: #{dev.color.lstrip('#')}"
        )

    def refreshPorts(self):
        self.ui.comboBoxPort.clear()
        self.ui.comboBoxPort.addItem(self.tr("自动"))
        ports = []
        for port in comports():
            self.ui.comboBoxPort.addItem(port.device)
            ports.append(port.device)
        if setting.adapter.comport not in ports:
            self.ui.comboBoxPort.setCurrentText(self.tr("自动"))
        else:
            self.ui.comboBoxPort.setCurrentText(setting.adapter.comport)

    @QtCore.pyqtSlot()
    def on_btnMatch_clicked(self):
        if self.connection_manager.is_open:
            CustomMessageBox(self, self.tr("错误"), self.tr("请先断开连接"))
            return
        self.save_settings()
        pipe = max(0, self.ui.comboBoxDevice.currentIndex()) + 1
        try:
            idcode = self.connection_manager.match(pipe)
        except Exception as e:
            logger.exception(self.tr("自动配对失败"))
            CustomMessageBox(self, self.tr("自动配对失败"), str(e))
            return
        CustomMessageBox(self, self.tr("自动配对成功"), f"IDCODE: {idcode}")
        self.ui.lineEditIdcode.setText(idcode)
        self.save_settings()

    def show(self) -> None:
        self.refresh_device_combo()
        self.initValues()
        self.refreshPorts()
        center_window(self)
        super().show()

    @QtCore.pyqtSlot()
    def on_lineEditColor_editingFinished(self):
        color = self.ui.lineEditColor.text().lstrip("#")
        try:
            _ = bytes.fromhex(color)
            self.ui.btnColorIndicator.setStyleSheet(f"background-color: #{color}")
        except Exception:
            CustomMessageBox(
                self,
                self.tr("颜色格式错误"),
                self.tr("请输入16进制RGB颜色代码(例如: 66CCFF)"),
            )
            self.ui.lineEditColor.setText(self._current_device().color)

    @QtCore.pyqtSlot()
    def on_btnColorIndicator_clicked(self):
        current = QtGui.QColor(f"#{self.ui.lineEditColor.text().lstrip('#')}")
        if not current.isValid():
            current = QtGui.QColor(f"#{self._current_device().color.lstrip('#')}")
        picked = QtWidgets.QColorDialog.getColor(current, self, self.tr("选择颜色"))
        if not picked.isValid():
            return
        hex6 = picked.name().lstrip("#").upper()
        self.ui.lineEditColor.setText(hex6)
        self.ui.btnColorIndicator.setStyleSheet(f"background-color: #{hex6}")

    def save_adapter_settings(self):
        setting.adapter.baudrate = int(self.ui.spinBoxBaud.value())
        setting.adapter.address = ":".join(
            [
                self.ui.lineEditAddr1.text(),
                self.ui.lineEditAddr2.text(),
                self.ui.lineEditAddr3.text(),
                self.ui.lineEditAddr4.text(),
                self.ui.lineEditAddr5.text(),
            ]
        )
        setting.adapter.freq = int(self.ui.spinBoxFreq.value())
        setting.adapter.txpower = self.ui.comboBoxPower.currentText()
        setting.adapter.comport = (
            self.ui.comboBoxPort.currentText()
            if self.ui.comboBoxPort.currentText() != self.tr("自动")
            else ""
        )

    def save_device_settings(self, dev: DeviceSettings):
        old_color = dev.color
        dev.idcode = self.ui.lineEditIdcode.text()
        dev.color = self.ui.lineEditColor.text()
        dev.m01ch = f"CH-{int(self.ui.spinBoxM01.value())}"
        dev.blink = self.ui.comboBoxBlink.currentText() == self.tr("闪烁")
        dev.output_warning = self.ui.checkBoxOutputWarn.isChecked()
        dev.lock_when_output = self.ui.checkBoxSetLock.isChecked()
        dev.ignore_hw_lock = self.ui.checkBoxIgnoreHWLock.isChecked()
        if dev.color != old_color:
            self.device_color_changed.emit(dev.id)

    def save_settings(self):
        self.save_adapter_settings()
        self.save_device_settings(self._current_device())
        setting.save(SETTING_FILE)

    @QtCore.pyqtSlot()
    def on_btnSave_clicked(self):
        self.save_settings()
        self.ui.btnSave.setText(self.tr("重新连接生效"))
        QtCore.QTimer.singleShot(1000, self._reset_btn_text)

    def _reset_btn_text(self):
        self.ui.btnSave.setText(self.tr("应用 / Apply"))

    @QtCore.pyqtSlot()
    def on_btnOk_clicked(self):
        self.save_settings()
        self.close()

    @QtCore.pyqtSlot(int)
    def on_checkBoxIgnoreHWLock_stateChanged(self, state: int):
        self._current_device().ignore_hw_lock = state == QtCore.Qt.CheckState.Checked

    @QtCore.pyqtSlot(int)
    def on_checkBoxSetLock_stateChanged(self, state: int):
        self._current_device().lock_when_output = state == QtCore.Qt.CheckState.Checked

    @QtCore.pyqtSlot(int)
    def on_checkBoxOutputWarn_stateChanged(self, state: int):
        self._current_device().output_warning = state == QtCore.Qt.CheckState.Checked


class MDPGraphics(QtWidgets.QDialog, FramelessWindow):
    set_max_fps_sig = QtCore.pyqtSignal(float)
    state_fps_sig = QtCore.pyqtSignal(float)
    set_data_len_sig = QtCore.pyqtSignal(int)
    set_interp_sig = QtCore.pyqtSignal(int)
    theme_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_DialogGraphics()
        self.ui.setupUi(self)
        self.CustomTitleBar = CustomTitleBar(self, self.tr("图形设置"))
        self.CustomTitleBar.set_theme("dark")
        self.CustomTitleBar.set_allow_double_toggle_max(False)
        self.CustomTitleBar.set_min_btn_enabled(False)
        self.CustomTitleBar.set_max_btn_enabled(False)
        self.CustomTitleBar.set_full_btn_enabled(False)
        self.CustomTitleBar.set_close_btn_enabled(False)
        self.setTitleBar(self.CustomTitleBar)
        # self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint)
        if NUMBA_ENABLED:
            self.ui.labelNumba.setVisible(True)
        else:
            self.ui.labelNumba.setVisible(False)

        if not OPENGL_AVALIABLE:
            self.ui.checkBoxOpenGL.setEnabled(False)
            self.ui.checkBoxOpenGL.setChecked(False)

        for k in setting.ui.color_palette:
            if k not in ("dark", "light", "modify_this_to_add_your_custom_theme"):
                self.ui.comboTheme.addItem(k)

    def apply_theme(self, sys_theme):
        self.CustomTitleBar.set_theme(sys_theme)
        set_color(self.ui.labelNumba, setting.get_color("general_green"))

    def initValues(self):
        self.ui.spinMaxFps.setValue(setting.ui.graph_max_fps)
        self.ui.spinStateFps.setValue(setting.ui.state_fps)
        self.ui.spinDataLength.setValue(setting.ui.data_pts)
        self.ui.spinDisplayLength.setMaximum(setting.ui.data_pts)
        self.ui.spinDisplayLength.setValue(setting.ui.display_pts)
        self.ui.comboInterp.setCurrentIndex(setting.ui.interp)
        self.ui.comboAvgMode.setCurrentIndex(setting.devices[0].avgmode)
        self.ui.spinStateVThres.setValue(setting.devices[0].v_threshold)
        self.ui.spinStateIThres.setValue(setting.devices[0].i_threshold)
        self.ui.checkBoxUseCali.setChecked(setting.devices[0].cali.use)
        self.ui.spinCaliVk.setValue(setting.devices[0].cali.v_k)
        self.ui.spinCaliVb.setValue(setting.devices[0].cali.v_b)
        self.ui.spinCaliIk.setValue(setting.devices[0].cali.i_k)
        self.ui.spinCaliIb.setValue(setting.devices[0].cali.i_b)
        self.ui.spinCaliVk_2.setValue(setting.devices[0].cali.vset_k)
        self.ui.spinCaliVb_2.setValue(setting.devices[0].cali.vset_b)
        self.ui.spinCaliIk_2.setValue(setting.devices[0].cali.iset_k)
        self.ui.spinCaliIb_2.setValue(setting.devices[0].cali.iset_b)
        self.ui.checkBoxAntialias.setChecked(setting.ui.antialias)
        self.ui.checkBoxOpenGL.setChecked(setting.ui.opengl)
        self.ui.comboTheme.setCurrentIndex(
            {"light": 1, "dark": 0}.get(setting.ui.theme, setting.ui.theme)
        )
        self.ui.comboInput.setCurrentIndex(int(not setting.ui.bitadjust))

    def show(self) -> None:
        self.initValues()
        center_window(self)
        super().show()

    @QtCore.pyqtSlot(int)
    def on_comboInput_currentIndexChanged(self, index):
        setting.ui.bitadjust = index == 0

    @QtCore.pyqtSlot(int)
    def on_comboTheme_currentIndexChanged(self, index):
        self.theme_requested.emit(
            {0: "dark", 1: "light"}.get(index, self.ui.comboTheme.currentText())
        )

    @QtCore.pyqtSlot(int)
    def on_checkBoxAntialias_stateChanged(self, state: int):
        setting.ui.antialias = state == QtCore.Qt.CheckState.Checked
        update_pyqtgraph_setting()

    @QtCore.pyqtSlot(int)
    def on_checkBoxOpenGL_stateChanged(self, state: int):
        setting.ui.opengl = state == QtCore.Qt.CheckState.Checked
        update_pyqtgraph_setting()

    @QtCore.pyqtSlot(float)
    def on_spinMaxFps_valueChanged(self, _=None):
        setting.ui.graph_max_fps = self.ui.spinMaxFps.value()
        self.set_max_fps_sig.emit(self.ui.spinMaxFps.value())

    @QtCore.pyqtSlot(float)
    def on_spinStateFps_valueChanged(self, _=None):
        setting.ui.state_fps = self.ui.spinStateFps.value()
        self.state_fps_sig.emit(self.ui.spinStateFps.value())

    @QtCore.pyqtSlot()
    def on_spinDataLength_editingFinished(self):
        value = self.ui.spinDataLength.value()
        if value == setting.ui.data_pts:
            return
        setting.ui.data_pts = value
        self.set_data_len_sig.emit(value)
        self.ui.spinDisplayLength.setMaximum(value)

    @QtCore.pyqtSlot()
    def on_spinDisplayLength_editingFinished(self):
        setting.ui.display_pts = self.ui.spinDisplayLength.value()

    @QtCore.pyqtSlot(int)
    def on_comboInterp_currentIndexChanged(self, index):
        setting.ui.interp = self.ui.comboInterp.currentIndex()
        self.set_interp_sig.emit(index)

    @QtCore.pyqtSlot(int)
    def on_comboAvgMode_currentIndexChanged(self, index):
        setting.devices[0].avgmode = self.ui.comboAvgMode.currentIndex()

    @QtCore.pyqtSlot(float)
    def on_spinStateVThres_valueChanged(self, _=None):
        setting.devices[0].v_threshold = self.ui.spinStateVThres.value()

    @QtCore.pyqtSlot(float)
    def on_spinStateIThres_valueChanged(self, _=None):
        setting.devices[0].i_threshold = self.ui.spinStateIThres.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliVk_valueChanged(self, _=None):
        setting.devices[0].cali.v_k = self.ui.spinCaliVk.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliVb_valueChanged(self, _=None):
        setting.devices[0].cali.v_b = self.ui.spinCaliVb.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliIk_valueChanged(self, _=None):
        setting.devices[0].cali.i_k = self.ui.spinCaliIk.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliIb_valueChanged(self, _=None):
        setting.devices[0].cali.i_b = self.ui.spinCaliIb.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliVk_2_valueChanged(self, _=None):
        setting.devices[0].cali.vset_k = self.ui.spinCaliVk_2.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliVb_2_valueChanged(self, _=None):
        setting.devices[0].cali.vset_b = self.ui.spinCaliVb_2.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliIk_2_valueChanged(self, _=None):
        setting.devices[0].cali.iset_k = self.ui.spinCaliIk_2.value()

    @QtCore.pyqtSlot(float)
    def on_spinCaliIb_2_valueChanged(self, _=None):
        setting.devices[0].cali.iset_b = self.ui.spinCaliIb_2.value()

    @QtCore.pyqtSlot(int)
    def on_checkBoxUseCali_stateChanged(self, state: int):
        setting.devices[0].cali.use = state == QtCore.Qt.CheckState.Checked

    @QtCore.pyqtSlot()
    def on_btnClose_clicked(self):
        try:
            setting.ui.graph_max_fps = self.ui.spinMaxFps.value()
            setting.ui.state_fps = self.ui.spinStateFps.value()
            setting.ui.data_pts = self.ui.spinDataLength.value()
            setting.ui.display_pts = self.ui.spinDisplayLength.value()
            setting.ui.interp = self.ui.comboInterp.currentIndex()
            setting.devices[0].avgmode = self.ui.comboAvgMode.currentIndex()
            setting.devices[0].v_threshold = self.ui.spinStateVThres.value()
            setting.devices[0].i_threshold = self.ui.spinStateIThres.value()
            setting.devices[0].cali.use = self.ui.checkBoxUseCali.isChecked()
            setting.devices[0].cali.v_k = self.ui.spinCaliVk.value()
            setting.devices[0].cali.v_b = self.ui.spinCaliVb.value()
            setting.devices[0].cali.i_k = self.ui.spinCaliIk.value()
            setting.devices[0].cali.i_b = self.ui.spinCaliIb.value()
            setting.devices[0].cali.vset_k = self.ui.spinCaliVk_2.value()
            setting.devices[0].cali.vset_b = self.ui.spinCaliVb_2.value()
            setting.devices[0].cali.iset_k = self.ui.spinCaliIk_2.value()
            setting.devices[0].cali.iset_b = self.ui.spinCaliIb_2.value()
            setting.ui.antialias = self.ui.checkBoxAntialias.isChecked()
            setting.ui.opengl = self.ui.checkBoxOpenGL.isChecked()
            setting.ui.bitadjust = self.ui.comboInput.currentIndex() == 0
            setting.save(SETTING_FILE)
        except Exception as e:
            logger.error(e)
        self.close()
