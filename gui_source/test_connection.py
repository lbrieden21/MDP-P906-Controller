"""Tests for ConnectionManager's background link, cancel and auto-match.

Runs headless against the simulated devices, with MDP_SIM_CONNECT_DELAY /
MDP_SIM_CONNECT_FAIL standing in for a slow or unreachable device. Drives
ConnectionManager directly rather than the main window, whose failure
handler opens a modal box.

Run as a script (settings_model resolves settings.json next to argv[0]):
    venv/bin/python gui_source/test_connection.py
"""

import os
import sys
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MDP_SIM_MODE"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

import device_panel_l1060  # noqa: E402,F401 (registers L1060 in DEVICE_PANEL_TYPES)
import device_panel_p906  # noqa: E402,F401 (registers P906 in DEVICE_PANEL_TYPES)
from connection import ConnectionManager  # noqa: E402
from device_core import GraphCapture  # noqa: E402
from device_panel import DEVICE_PANEL_TYPES  # noqa: E402
from settings_model import DeviceSettings  # noqa: E402

GOOD_P906 = "11110001"
GOOD_L1060 = "11110002"
FAILING = "DEADBEEF"


def make_panel(dtype: str, n: int, idcode: str):
    dev = DeviceSettings(type=dtype, id=f"{dtype.lower()}-{n}", name=f"{dtype} #{n}")
    dev.idcode = idcode
    return DEVICE_PANEL_TYPES[dtype](GraphCapture(), None, dev)


def spin(seconds: float, until=None) -> bool:
    """Run the event loop for up to `seconds`, stopping early once
    until() is true. Returns whether until() became true."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.002)
    return until is not None and until()


class ConnectionManagerTest(unittest.TestCase):
    def setUp(self):
        os.environ["MDP_SIM_CONNECT_DELAY"] = "0"
        os.environ["MDP_SIM_CONNECT_FAIL"] = FAILING
        self.p906 = make_panel("P906", 1, GOOD_P906)
        self.l1060 = make_panel("L1060", 2, GOOD_L1060)
        self.bad = make_panel("P906", 3, FAILING)
        self.conn = ConnectionManager([self.p906, self.l1060, self.bad])
        self.results = []
        self.conn.link_finished.connect(lambda p, ok, err: self.results.append((p, ok, err)))
        self.matches = []
        self.conn.match_finished.connect(lambda idc, err: self.matches.append((idc, err)))

    def tearDown(self):
        self.conn.shutdown()
        for panel in self.conn.panels:
            self.conn.unlink_panel(panel)
        spin(0.05)
        for panel in self.conn.panels:
            panel.deleteLater()
        spin(0.05)

    def result_for(self, panel):
        for p, ok, err in self.results:
            if p is panel:
                return ok, err
        return None

    def link_and_wait(self, panel, timeout=5.0):
        self.conn.begin_link(panel, 10)
        self.assertTrue(spin(timeout, lambda: self.result_for(panel) is not None))
        return self.result_for(panel)

    def test_event_loop_keeps_running_during_connect(self):
        os.environ["MDP_SIM_CONNECT_DELAY"] = "1.0"
        ticks = []
        timer = QtCore.QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(20)
        self.conn.begin_link(self.p906, 10)
        self.assertTrue(self.conn.is_linking(self.p906))
        self.assertTrue(spin(3.0, lambda: self.result_for(self.p906) is not None))
        timer.stop()
        self.assertEqual(self.result_for(self.p906), (True, ""))
        self.assertGreaterEqual(len(ticks), 30)
        self.assertTrue(self.p906.linked)
        self.assertTrue(self.conn.is_open)
        self.assertTrue(self.p906.update_state_timer.isActive())
        self.conn.unlink_panel(self.p906)
        self.assertFalse(self.conn.is_open)

    def test_failed_first_link_closes_bus(self):
        os.environ["MDP_SIM_CONNECT_DELAY"] = "0.2"
        ok, err = self.link_and_wait(self.bad)
        self.assertFalse(ok)
        self.assertTrue(err)
        self.assertFalse(self.bad.linked)
        self.assertIsNone(self.bad.api)
        self.assertTrue(self.bad.ui.btnLink.isEnabled())
        self.assertIsNone(self.conn.bus)
        self.assertFalse(self.conn.busy)

    def test_failure_keeps_other_panel_linked(self):
        self.assertEqual(self.link_and_wait(self.p906), (True, ""))
        os.environ["MDP_SIM_CONNECT_DELAY"] = "0.3"
        ok, err = self.link_and_wait(self.bad)
        self.assertFalse(ok)
        self.assertTrue(err)
        self.assertTrue(self.p906.linked)
        self.assertTrue(self.p906.update_state_timer.isActive())
        self.assertTrue(self.conn.is_open)
        self.conn.unlink_panel(self.p906)
        self.assertFalse(self.conn.is_open)

    def test_cancel_running_link(self):
        os.environ["MDP_SIM_CONNECT_DELAY"] = "2.0"
        self.conn.begin_link(self.p906, 10)
        spin(0.2)
        self.assertTrue(self.p906.ui.btnLink.isEnabled())
        t0 = time.monotonic()
        self.conn.cancel_link(self.p906)
        self.assertFalse(self.p906.ui.btnLink.isEnabled())
        self.assertTrue(spin(2.0, lambda: self.result_for(self.p906) is not None))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(self.result_for(self.p906), (False, ""))
        self.assertFalse(self.p906.linked)
        self.assertTrue(self.p906.ui.btnLink.isEnabled())
        self.assertIsNone(self.conn.bus)
        self.assertFalse(self.conn.busy)

    def test_cancel_while_bus_opening(self):
        self.conn.begin_link(self.p906, 10)
        # The bus open's result has not been delivered yet, so the link is
        # still waiting for it.
        self.conn.cancel_link(self.p906)
        self.assertEqual(self.result_for(self.p906), (False, ""))
        self.assertTrue(spin(2.0, lambda: not self.conn.busy))
        self.assertIsNone(self.conn.bus)

    def test_parallel_links(self):
        os.environ["MDP_SIM_CONNECT_DELAY"] = "1.0"
        t0 = time.monotonic()
        self.conn.begin_link(self.p906, 10)
        self.conn.begin_link(self.l1060, 10)
        self.assertTrue(spin(4.0, lambda: len(self.results) == 2))
        self.assertLess(time.monotonic() - t0, 1.7)
        self.assertEqual(self.result_for(self.p906), (True, ""))
        self.assertEqual(self.result_for(self.l1060), (True, ""))
        self.assertTrue(self.p906.linked and self.l1060.linked)

    def test_unlink_other_panel_while_linking_keeps_bus(self):
        self.assertEqual(self.link_and_wait(self.p906), (True, ""))
        os.environ["MDP_SIM_CONNECT_DELAY"] = "0.5"
        self.conn.begin_link(self.l1060, 10)
        spin(0.1)
        self.conn.unlink_panel(self.p906)
        self.assertTrue(self.conn.is_open)
        self.assertTrue(spin(2.0, lambda: self.result_for(self.l1060) is not None))
        self.assertEqual(self.result_for(self.l1060), (True, ""))
        self.assertTrue(self.l1060.linked)
        self.assertTrue(self.conn.is_open)

    def test_empty_idcode_raises_synchronously(self):
        self.p906.settings.idcode = ""
        with self.assertRaises(ValueError):
            self.conn.begin_link(self.p906, 10)
        self.assertFalse(self.conn.busy)
        self.assertFalse(self.conn.is_linking(self.p906))

    def test_shutdown_returns_promptly(self):
        os.environ["MDP_SIM_CONNECT_DELAY"] = "5.0"
        self.conn.begin_link(self.p906, 10)
        spin(0.2)
        t0 = time.monotonic()
        self.conn.shutdown()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(self.conn.busy)
        spin(0.1)
        self.assertEqual(self.results, [])

    def test_match(self):
        self.conn.begin_match(1)
        self.assertTrue(self.conn.busy)
        with self.assertRaises(RuntimeError):
            self.conn.begin_link(self.p906, 10)
        self.assertTrue(spin(2.0, lambda: bool(self.matches)))
        idcode, err = self.matches[0]
        self.assertTrue(idcode)
        self.assertEqual(err, "")
        self.assertFalse(self.conn.busy)


if __name__ == "__main__":
    unittest.main()
