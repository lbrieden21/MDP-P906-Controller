"""Timed headless GUI run for adapter bring-up step 6.

Drives the *real* GUI -- the same MDPMainwindow, panels and ConnectionManager
the user runs -- for a fixed duration, then reports per-device sample counts.
This is the code path a driver-level bench_run does not cover: panels poll
asynchronously through request_realtime_value() /
register_realtime_value_callback() rather than synchronous get_status().

The temporary driver used for the Teensy 4.1 bring-up was removed after that
phase; this is the same idea made re-runnable for the remaining boards.

Devices, adapter port and radio config all come from gui_source/settings.json,
so point that at the adapter under test first (and put it back afterwards --
it is gitignored, so git will not remind you).

Usage:
    venv/bin/python gui_source/bench_gui_run.py [seconds]

Writes gui_source/mdp.log at TRACE level for tools/noack_report.py.
"""
import os
import sys
import time

# Must be set before QApplication is constructed at mdp_gui import time.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# app_context reads "--debug" out of sys.argv at import to decide whether to
# add the TRACE-level mdp.log sink, so it has to be present before mdp_gui is
# imported. noack_report.py has nothing to parse without it.
if "--debug" not in sys.argv:
    sys.argv.append("--debug")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1][0].isdigit() else 60.0

from PyQt5 import QtCore  # noqa: E402

import mdp_gui  # noqa: E402

app = mdp_gui.app
window = mdp_gui.MainWindow
conn = window.connection

state = {"linked": [], "t0": None, "rc": 0}


def start():
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
        print(f"  {name:<12} {model:<6} {count:6d} samples   {count / elapsed:6.1f}/s")
    if speed is not None:
        print(f"  link {speed[0] / 1024:.1f} KB/s, adapter-reported error rate {speed[1] * 100:.2f}%")
    print()
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
