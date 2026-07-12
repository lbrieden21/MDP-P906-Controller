from typing import List

from loguru import logger
from PyQt5 import QtCore

from app_context import DEBUG
from device_panel import DevicePanelBase
from mdp_controller import MDPBus
from settings_model import setting


class ConnectionManager(QtCore.QObject):
    def __init__(self, panels: List[DevicePanelBase], parent=None) -> None:
        super().__init__(parent)
        self.panels = panels
        self.bus = None

    @property
    def is_open(self) -> bool:
        return self.bus is not None

    def _build_bus(self) -> MDPBus:
        return MDPBus(
            port=setting.adapter.comport,
            baudrate=setting.adapter.baudrate,
            address=setting.adapter.address,
            freq=int(setting.adapter.freq),
            tx_output_power=setting.adapter.txpower,
            debug=DEBUG,
        )

    def link_panel(self, panel: DevicePanelBase, fps: float = 50) -> None:
        """
        Link a single panel onto the shared bus, opening the bus first if
        this is the first device to link. If the link fails and this call
        was the one that opened the bus, the bus is torn back down so a
        failed first link doesn't leave a dangling open adapter.
        """
        opened_bus = self.bus is None
        if opened_bus:
            self.bus = self._build_bus()
        try:
            panel.link(self.bus, self.panels.index(panel), fps)
        except Exception:
            logger.exception(f"Failed to link device {panel.device_id}")
            if opened_bus:
                self.bus.close()
                self.bus = None
            raise

    def unlink_panel(self, panel: DevicePanelBase) -> None:
        """
        Unlink a single panel. Closes the shared bus once no panel is
        linked to it anymore.
        """
        if panel.linked:
            panel.unlink()
        if self.bus is not None and not any(p.linked for p in self.panels):
            bus = self.bus
            self.bus = None
            bus.close()

    def match(self) -> str:
        bus = self._build_bus()
        try:
            idcode, _pipe = bus.auto_match()
        finally:
            bus.close()
        return idcode
