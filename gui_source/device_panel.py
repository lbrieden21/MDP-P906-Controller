from typing import List

from PyQt5 import QtCore, QtWidgets

from device_core import ChannelSpec, DeviceDataStore

DEVICE_PANEL_TYPES = {}


class DevicePanelBase(QtWidgets.QWidget):
    values_signal = QtCore.pyqtSignal(float, float, float)
    display_data_signal = QtCore.pyqtSignal(list, list, str, str, str, str, str, bool)
    highlight_point_signal = QtCore.pyqtSignal(float, float)
    link_state_changed = QtCore.pyqtSignal()
    link_toggle_requested = QtCore.pyqtSignal()

    def __init__(
        self,
        device_id: str,
        display_name: str,
        channels: List[ChannelSpec],
        data_length: int,
        open_r: float = 1e7,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self.display_name = display_name
        self.channels = channels
        self.store = DeviceDataStore(channels, data_length, open_r=open_r)
        self.linked = False
        self.record_flag = False
        self.record_data = None

    def link(self, bus, pipe: int, fps: float = 50) -> None:
        raise NotImplementedError

    def unlink(self) -> None:
        raise NotImplementedError

    def set_data_fps(self, fps: float) -> None:
        raise NotImplementedError

    def apply_theme(self) -> None:
        raise NotImplementedError

    def start_record(self) -> None:
        raise NotImplementedError

    def stop_record(self):
        raise NotImplementedError

    def has_discharge_data(self) -> bool:
        """Whether this panel is currently running, or still holds the
        results of, a discharge workflow -- used to reveal the Ah/Wh graph
        channels (which only apply to device types supporting the discharge
        workflow, currently only L1060) once they'd actually show
        something, and to hide them again once clear_aux_data() drops that
        result (and no run is active)."""
        return False

    def has_sweep_data(self) -> bool:
        """Whether this panel is currently running, or still holds the
        results of, a sweep workflow -- used to reveal the Sweep graph
        channel (which only applies to device types supporting the sweep
        workflow, currently only L1060) once it'd actually show something,
        and to hide it again once clear_aux_data() drops that result (and
        no run is active)."""
        return False

    def clear_aux_data(self) -> None:
        """Drop any held discharge/sweep results so has_discharge_data()/
        has_sweep_data() go back to False, unless that workflow is actively
        running right now. Called by the main window's Clear-buffer action
        alongside store.clear() -- a no-op for device types (e.g. P906)
        that don't support these workflows."""
