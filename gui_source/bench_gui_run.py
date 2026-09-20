"""Timed headless GUI run for adapter bring-up step 6.

Drives the *real* GUI -- the same MDPMainwindow, panels and ConnectionManager
the user runs -- for a fixed duration, then reports per-device sample counts.
This is the code path a driver-level bench_run does not cover: panels poll
asynchronously through request_realtime_value() /
register_realtime_value_callback() rather than synchronous get_status().

The temporary driver used for the Teensy 4.1 bring-up was removed after that
phase; this is the same idea made re-runnable for the remaining boards.

Devices and radio config come from gui_source/settings.json, which is the fixed
bench configuration and is not edited to run this. The adapter transport is the
only thing that varies per board under test, so it is a --port (serial) or
--host (ESP32 WiFi host link, tcp:// port taken from settings.json) argument
here exactly as it is on host_link_test.py and pipe_test.py; the override is
applied to the in-memory setting and never written back.

Usage:
    venv/bin/python gui_source/bench_gui_run.py [seconds] [--port /dev/ttyUSB0]
    venv/bin/python gui_source/bench_gui_run.py [seconds] [--host 192.168.2.106]

Writes gui_source/mdp.log at TRACE level for tools/noack_report.py.
"""
import os
import sys
import time

# Must be set before QApplication is constructed at mdp_gui import time.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _take_port_arg(argv):
    """Pull --port/--port= out of argv, returning the value or None.

    Removing it matters: app_context scans sys.argv for "--debug", and the
    duration below is read positionally, so a stray option would be parsed as
    one of those.
    """
    port = None
    rest = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--port":
            if i + 1 >= len(argv):
                sys.exit("--port needs a value, e.g. --port /dev/ttyUSB0")
            port = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--port="):
            port = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    argv[1:] = rest
    return port


def _take_host_arg(argv):
    """Pull --host/--host= out of argv, returning the value or None.

    Same rationale as _take_port_arg: --host selects the ESP32 WiFi host
    link instead of a serial port, so it has to be gone before the duration
    positional and app_context's "--debug" scan see argv.
    """
    host = None
    rest = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--host":
            if i + 1 >= len(argv):
                sys.exit("--host needs a value, e.g. --host 192.168.2.106")
            host = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--host="):
            host = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    argv[1:] = rest
    return host


PORT = _take_port_arg(sys.argv)
HOST = _take_host_arg(sys.argv)

# app_context reads "--debug" out of sys.argv at import to decide whether to
# add the TRACE-level mdp.log sink, so it has to be present before mdp_gui is
# imported. noack_report.py has nothing to parse without it.
if "--debug" not in sys.argv:
    sys.argv.append("--debug")

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1][0].isdigit() else 60.0

from PyQt5 import QtCore  # noqa: E402

# Imported ahead of mdp_gui so the override is in place before
# ConnectionManager._build_bus() reads it. settings_model load()s and save()s
# the file once at import, both before this point -- settings.json is on disk
# unchanged, and only this process's copy carries the override.
from settings_model import setting  # noqa: E402

if PORT:
    setting.adapter.comport = PORT
    setting.adapter.transport = "serial"
if HOST:
    setting.adapter.host = HOST
    setting.adapter.transport = "tcp"

import mdp_gui  # noqa: E402

app = mdp_gui.app
window = mdp_gui.MainWindow
conn = window.connection

state = {"linked": [], "t0": None, "rc": 0}


def start():
    if setting.adapter.transport == "tcp":
        print(f"adapter tcp://{setting.adapter.host}:{setting.adapter.tcp_port}"
              f"{' (--host override)' if HOST else ' (settings.json)'}")
    else:
        print(f"adapter port {setting.adapter.comport}"
              f"{' (--port override)' if PORT else ' (settings.json)'}")
    print(f"linking {len(window.panels)} panel(s) at {window.data_fps}Hz")
    for panel in window.panels:
        try:
            conn.link_panel(panel, window.data_fps)
        except Exception as e:
            print(f"  FAIL  {panel.display_name}: {e}")
            continue
        state["linked"].append(panel)
        print(f"  linked {panel.display_name} ({panel.model}) on pipe {conn.panels.index(panel) + 1}")

    if not state["linked"]:
        print("no panel linked -- aborting")
        state["rc"] = 1
        app.quit()
        return

    # store.append() only writes the ring buffer -- and so only advances
    # update_count, which is what the sample counts below are read from --
    # while the panel's capture is running. The GUI starts that from the
    # graph view's button or its auto-start trigger, neither of which exists
    # in a headless run, so start it here explicitly.
    t_start = time.perf_counter()
    for panel in state["linked"]:
        if not panel.capture.running:
            panel.capture.start(t_start)

    state["t0"] = time.perf_counter()
    # Sample counts are taken from the moment linking finished, so the
    # connect() handshake and the boot-deaf wait are not charged against the
    # measured window.
    state["base"] = {p.device_id: p.store.update_count for p in state["linked"]}
    print(f"running {DURATION:.0f}s ...")
    QtCore.QTimer.singleShot(int(DURATION * 1000), stop)


def stop():
    elapsed = time.perf_counter() - state["t0"]
    bus = conn.bus
    rows = [
        (p.display_name, p.model, p.store.update_count - state["base"][p.device_id])
        for p in state["linked"]
    ]

    speed = None
    if bus is not None:
        try:
            sc = bus.speed_counter
            speed = (sc.Bps, sc.error_rate)
        except Exception:
            speed = None

    for panel in list(state["linked"]):
        try:
            conn.unlink_panel(panel)
        except Exception as e:
            print(f"  unlink {panel.display_name} failed: {e}")

    print()
    print(f"== {elapsed:.1f}s run ==")
    for name, model, count in rows:
        print(f"  {name:<12} {model:<6} {count:6d} samples   {count / elapsed:6.1f} samples/s")
    if speed is not None:
        print(f"  link {speed[0] / 1024:.1f} KB/s, adapter-reported error rate {speed[1] * 100:.2f}%")
    print()
    # A realtime packet carries several samples, and avgmode collapses a
    # 9-sample batch to 3 or 1, so samples/s is req/s times a per-device,
    # settings-dependent factor. Request rate comes from counting
    # 'NRF received' lines in a TRACE log, not from here.
    print("samples/s is NOT a request rate -- count 'NRF received' lines for that.")
    print("Now run: venv/bin/python tools/noack_report.py gui_source/mdp.log")

    from loguru import logger

    logger.complete()
    sys.stdout.flush()
    # The headless GUI segfaults during interpreter teardown, after the event
    # loop exits and all results have printed -- a PyQt/pyqtgraph artifact,
    # not a run failure. Exiting here keeps that noise out of the result.
    os._exit(state["rc"])


QtCore.QTimer.singleShot(0, start)
sys.exit(app.exec_())
