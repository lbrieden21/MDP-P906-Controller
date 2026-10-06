import threading
import time
from typing import Dict, List, Optional

from loguru import logger
from PyQt5 import QtCore

from app_context import DEBUG
from device_panel import DevicePanelBase
from mdp_controller import ConnectCancelled, MDPBus
from settings_model import setting

# Upper bound shutdown() waits, in total, for in-flight link/match/bus-open
# threads after cancelling them.
_SHUTDOWN_JOIN_S = 2.0


def _error_text(e: Exception) -> str:
    return str(e) or type(e).__name__


class _BusBuild:
    """One in-flight MDPBus open. The worker fills bus or error, then
    emits _bus_built."""

    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.bus: Optional[MDPBus] = None
        self.error = ""


class _LinkJob:
    """One panel's in-flight link. The worker fills api or error, then
    emits _link_done; cancel is the event connect() watches."""

    def __init__(self, panel: DevicePanelBase, pipe: int, params: dict) -> None:
        self.panel = panel
        self.pipe = pipe
        self.params = params
        self.cancel = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.api = None
        self.error = ""


class _MatchJob:
    """One in-flight auto-match on its own throwaway bus."""

    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.idcode = ""
        self.error = ""


class ConnectionManager(QtCore.QObject):
    """
    Owns the shared MDPBus and every panel's link to it.

    Opening the bus, connecting a device and auto-matching all run on
    worker threads; their results come back through queued signals, and
    every change to this object's state (bus, job tables) happens on the GUI
    thread in those signals' slots. The bus is closed once no panel is
    linked and no link or bus open is in flight (_close_bus_if_idle()).
    """

    # (panel, ok, error). ok=False with an empty error means cancelled.
    link_finished = QtCore.pyqtSignal(object, bool, str)
    # (idcode, error). Exactly one of the two is non-empty.
    match_finished = QtCore.pyqtSignal(str, str)

    _bus_built = QtCore.pyqtSignal(object)
    _link_done = QtCore.pyqtSignal(object)
    _match_done = QtCore.pyqtSignal(object)

    def __init__(self, panels: List[DevicePanelBase], parent=None) -> None:
        super().__init__(parent)
        self.panels = panels
        self.bus: Optional[MDPBus] = None
        self._bus_build: Optional[_BusBuild] = None
        self._waiting: List[_LinkJob] = []
        self._running: Dict[DevicePanelBase, _LinkJob] = {}
        self._match: Optional[_MatchJob] = None
        self._bus_built.connect(self._on_bus_built, QtCore.Qt.QueuedConnection)
        self._link_done.connect(self._on_link_done, QtCore.Qt.QueuedConnection)
        self._match_done.connect(self._on_match_done, QtCore.Qt.QueuedConnection)

    @property
    def is_open(self) -> bool:
        return self.bus is not None

    @property
    def busy(self) -> bool:
        """True while the bus is open or anything that may open the adapter
        port is in flight: a bus open, a link, or an auto-match."""
        return (
            self.bus is not None
            or self._bus_build is not None
            or bool(self._waiting)
            or bool(self._running)
            or self._match is not None
        )

    def is_linking(self, panel: DevicePanelBase) -> bool:
        return panel in self._running or any(j.panel is panel for j in self._waiting)

    @staticmethod
    def _bus_kwargs() -> dict:
        if setting.adapter.transport == "tcp":
            port = f"tcp://{setting.adapter.host}:{int(setting.adapter.tcp_port)}"
        else:
            port = setting.adapter.comport
        return dict(
            port=port,
            baudrate=setting.adapter.baudrate,
            address=setting.adapter.address,
            freq=int(setting.adapter.freq),
            tx_output_power=setting.adapter.txpower,
            debug=DEBUG,
        )

    ##########  Link  ##########

    def begin_link(self, panel: DevicePanelBase, fps: float) -> None:
        """
        Start linking a panel in the background; link_finished reports the
        outcome. Opens the shared bus first if it isn't open yet -- panels
        started while it is opening wait for it. Raises synchronously (and
        starts nothing) if the panel's settings are incomplete or an
        auto-match is using the adapter.
        """
        if self._match is not None:
            raise RuntimeError(
                QtCore.QCoreApplication.translate("ConnectionManager", "正在配对, 请稍候")
            )
        params = panel.link_params()
        # Pipes 1-5 only -- pipe 0's RX address is tied to the adapter's
        # own configured address, not independently settable per device
        # (see MDPBus._pipe_address), so it's never used for a device.
        job = _LinkJob(panel, self.panels.index(panel) + 1, params)
        panel.data_fps = fps
        panel.set_linking(False)
        if self.bus is not None:
            self._start_link_job(job, self.bus)
            return
        self._waiting.append(job)
        if self._bus_build is None:
            build = _BusBuild()
            build.thread = threading.Thread(
                target=self._run_bus_build, args=(build, self._bus_kwargs()), daemon=True
            )
            self._bus_build = build
            build.thread.start()

    def cancel_link(self, panel: DevicePanelBase) -> None:
        """Cancel a panel's in-flight link. A link still waiting for the bus
        is dropped at once; a connecting one stops at its next attempt and
        reports through link_finished like any other outcome."""
        for job in self._waiting:
            if job.panel is panel:
                self._waiting.remove(job)
                panel.abort_link()
                self._close_bus_if_idle()
                self.link_finished.emit(panel, False, "")
                return
        job = self._running.get(panel)
        if job is not None:
            job.cancel.set()
            panel.set_linking(True)

    def _run_bus_build(self, build: _BusBuild, kwargs: dict) -> None:
        """Worker thread."""
        try:
            build.bus = MDPBus(**kwargs)
        except Exception as e:
            logger.exception("Failed to open the adapter")
            build.error = _error_text(e)
        self._bus_built.emit(build)

    def _on_bus_built(self, build: _BusBuild) -> None:
        if build is not self._bus_build:
            # Abandoned by shutdown().
            if build.bus is not None:
                build.bus.close()
            return
        self._bus_build = None
        jobs = self._waiting
        self._waiting = []
        if build.bus is None:
            for job in jobs:
                job.panel.abort_link()
            for job in jobs:
                self.link_finished.emit(job.panel, False, build.error)
            return
        self.bus = build.bus
        for job in jobs:
            self._start_link_job(job, self.bus)
        self._close_bus_if_idle()

    def _start_link_job(self, job: _LinkJob, bus: MDPBus) -> None:
        job.thread = threading.Thread(
            target=self._run_link_job, args=(job, bus), daemon=True
        )
        self._running[job.panel] = job
        job.thread.start()

    def _run_link_job(self, job: _LinkJob, bus: MDPBus) -> None:
        """Worker thread."""
        try:
            job.api = job.panel.connect_api(bus, job.pipe, job.params, job.cancel)
        except ConnectCancelled:
            pass
        except Exception as e:
            logger.exception(f"Failed to link device {job.panel.device_id}")
            job.error = _error_text(e)
        self._link_done.emit(job)

    def _on_link_done(self, job: _LinkJob) -> None:
        # All state changes happen before link_finished is emitted: its
        # receiver may open a modal box, whose nested event loop can deliver
        # further results into this slot.
        panel = job.panel
        if self._running.get(panel) is not job:
            # Abandoned by shutdown().
            if job.api is not None:
                job.api.close()
            return
        del self._running[panel]
        error = job.error
        if job.cancel.is_set():
            if job.api is not None:
                job.api.close()
            panel.abort_link()
        elif error:
            panel.abort_link()
        else:
            try:
                panel.finish_link(job.api, job.params)
            except Exception as e:
                logger.exception(f"Failed to link device {panel.device_id}")
                error = _error_text(e)
                if panel.linked:
                    panel.unlink()
                else:
                    job.api.close()
                    panel.abort_link()
        self._close_bus_if_idle()
        self.link_finished.emit(panel, not job.cancel.is_set() and not error, error)

    ##########  Unlink / bus lifetime  ##########

    def unlink_panel(self, panel: DevicePanelBase) -> None:
        """
        Unlink a single panel. Closes the shared bus once no panel is
        linked to it and no link is in flight.
        """
        if panel.linked:
            panel.unlink()
        self._close_bus_if_idle()

    def _close_bus_if_idle(self) -> None:
        if (
            self.bus is None
            or self._bus_build is not None
            or self._waiting
            or self._running
            or any(p.linked for p in self.panels)
        ):
            return
        bus = self.bus
        self.bus = None
        bus.close()

    ##########  Auto-match  ##########

    def begin_match(self, pipe: int) -> None:
        """
        Auto-match a device on a throwaway bus in the background;
        match_finished reports the outcome. Must not be called while busy
        (the throwaway bus opens the same adapter port).

        Args:
            pipe: The real pipe this device will occupy once linked (its
                index in setting.devices + 1) -- see MDPBus.auto_match()'s
                docstring for why this must be passed explicitly rather than
                left to this throwaway bus's own pipe bookkeeping.
        """
        assert not self.busy, "begin_match() while the adapter is in use"
        job = _MatchJob()
        job.thread = threading.Thread(
            target=self._run_match, args=(job, self._bus_kwargs(), pipe), daemon=True
        )
        self._match = job
        job.thread.start()

    def _run_match(self, job: _MatchJob, kwargs: dict, pipe: int) -> None:
        """Worker thread."""
        try:
            bus = MDPBus(**kwargs)
            try:
                job.idcode, _pipe = bus.auto_match(pipe=pipe)
            finally:
                bus.close()
        except Exception as e:
            logger.exception("Auto match failed")
            job.error = _error_text(e)
        self._match_done.emit(job)

    def _on_match_done(self, job: _MatchJob) -> None:
        if job is not self._match:
            return
        self._match = None
        self.match_finished.emit(job.idcode, job.error)

    ##########  App shutdown  ##########

    def shutdown(self) -> None:
        """
        Cancel every in-flight link and wait (bounded by _SHUTDOWN_JOIN_S in
        total) for the link, bus-open and match threads to exit. Results
        they deliver afterwards are discarded. Linked panels, and the bus if
        any panel is linked, are left as they are.
        """
        jobs = list(self._running.values()) + self._waiting
        for job in jobs:
            job.cancel.set()
        threads = [j.thread for j in jobs if j.thread is not None]
        build = self._bus_build
        if build is not None:
            threads.append(build.thread)
        if self._match is not None:
            threads.append(self._match.thread)
        self._waiting = []
        self._running = {}
        self._bus_build = None
        self._match = None
        deadline = time.monotonic() + _SHUTDOWN_JOIN_S
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        # Cleared once closed so a late-delivered result's abandoned-job
        # branch doesn't close the same object twice.
        for job in jobs:
            if job.api is not None:
                api, job.api = job.api, None
                api.close()
        if build is not None and build.bus is not None:
            bus, build.bus = build.bus, None
            bus.close()
        self._close_bus_if_idle()
