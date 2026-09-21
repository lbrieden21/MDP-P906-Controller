"""Unit tests for the Qt-free GraphCapture/DeviceDataStore gating in
device_core.py.
"""

import math
import unittest

from device_core import ChannelSpec, DeviceDataStore, GraphCapture

CHANNELS = [
    ChannelSpec("voltage", "Voltage", "V"),
    ChannelSpec("current", "Current", "A"),
    ChannelSpec("power", "Power", "W"),
    ChannelSpec("energy", "Energy", "J"),
]


def append_sample(store, v, i, t1):
    return store.append(
        [(v, i)], {"voltage": [v], "current": [i], "power": [v * i]}, 1, t1
    )


class GraphCaptureStartTriggerTest(unittest.TestCase):
    def _capture(self, threshold=1.0, mode="voltage", device_id="dev"):
        capture = GraphCapture()
        capture.set_triggers(device_id, mode, threshold, device_id, "off", 0.0)
        return capture

    def test_first_reading_at_or_above_does_not_trigger(self):
        capture = self._capture()
        self.assertFalse(capture.check_start("dev", [1.5], [], t=0.0))
        self.assertFalse(capture.running)

    def test_below_then_above_triggers(self):
        capture = self._capture()
        self.assertFalse(capture.check_start("dev", [0.5], [], t=0.0))
        self.assertTrue(capture.check_start("dev", [1.5], [], t=1.0))
        self.assertTrue(capture.running)
        self.assertEqual(capture.origin, 1.0)

    def test_crossing_within_a_single_batch_triggers(self):
        capture = self._capture()
        self.assertTrue(capture.check_start("dev", [0.5, 1.5], [], t=1.0))
        self.assertTrue(capture.running)

    def test_mode_off_never_triggers(self):
        capture = self._capture(mode="off")
        self.assertFalse(capture.check_start("dev", [0.5], [], t=0.0))
        self.assertFalse(capture.check_start("dev", [1.5], [], t=1.0))
        self.assertFalse(capture.running)

    def test_current_mode_ignores_voltage(self):
        capture = self._capture(mode="current")
        # Voltage is already high throughout; only current should matter.
        self.assertFalse(capture.check_start("dev", [5.0], [0.5], t=0.0))
        self.assertTrue(capture.check_start("dev", [5.0], [1.5], t=1.0))
        self.assertTrue(capture.running)

    def test_other_device_never_triggers(self):
        capture = self._capture()
        self.assertFalse(capture.check_start("other", [0.5], [], t=0.0))
        self.assertFalse(capture.check_start("other", [1.5], [], t=1.0))
        self.assertFalse(capture.running)

    def test_after_stop_still_high_does_not_retrigger_but_drop_then_rise_does(self):
        capture = self._capture()
        capture.check_start("dev", [0.5], [], t=0.0)
        capture.check_start("dev", [1.5], [], t=1.0)
        self.assertTrue(capture.running)
        capture.stop()

        # Still high right after Stop: first reading only seeds the
        # baseline, it can't itself trigger.
        self.assertFalse(capture.check_start("dev", [1.5], [], t=2.0))
        self.assertFalse(capture.running)

        # A drop followed by a rise does trigger.
        self.assertFalse(capture.check_start("dev", [0.5], [], t=3.0))
        self.assertTrue(capture.check_start("dev", [1.5], [], t=4.0))
        self.assertTrue(capture.running)


class GraphCaptureStopTriggerTest(unittest.TestCase):
    def _capture(self, threshold=1.0, mode="voltage", device_id="dev", start_t=0.0):
        capture = GraphCapture()
        capture.set_triggers(device_id, "off", 0.0, device_id, mode, threshold)
        capture.start(start_t)
        return capture

    def test_above_then_below_stops_and_calls_on_auto_stop(self):
        calls = []
        capture = GraphCapture(on_auto_stop=lambda: calls.append(1))
        capture.set_triggers("dev", "off", 0.0, "dev", "voltage", 1.0)
        capture.start(0.0)
        self.assertFalse(capture.check_stop("dev", [1.5], []))
        self.assertTrue(capture.check_stop("dev", [0.5], []))
        self.assertFalse(capture.running)
        self.assertEqual(calls, [1])

    def test_already_below_at_start_does_not_stop_until_rise_then_fall(self):
        capture = self._capture()
        self.assertFalse(capture.check_stop("dev", [0.3], []))  # seeds baseline
        self.assertFalse(capture.check_stop("dev", [0.2], []))  # still below
        self.assertFalse(capture.check_stop("dev", [1.5], []))  # rises above
        self.assertTrue(capture.running)
        self.assertTrue(capture.check_stop("dev", [0.5], []))  # falls again
        self.assertFalse(capture.running)

    def test_stop_trigger_ignored_while_stopped(self):
        capture = GraphCapture()
        capture.set_triggers("dev", "off", 0.0, "dev", "voltage", 1.0)
        self.assertFalse(capture.check_stop("dev", [1.5], []))
        self.assertFalse(capture.check_stop("dev", [0.5], []))
        self.assertFalse(capture.running)

    def test_start_on_voltage_with_stop_on_current_behaves_independently(self):
        capture = GraphCapture()
        capture.set_triggers("dev", "voltage", 1.0, "dev", "current", 0.5)

        capture.check_start("dev", [0.5], [10.0], t=0.0)  # seeds voltage baseline
        self.assertTrue(capture.check_start("dev", [1.5], [0.0], t=1.0))
        self.assertTrue(capture.running)

        capture.check_stop("dev", [10.0], [1.0])  # seeds current baseline
        self.assertTrue(capture.check_stop("dev", [0.0], [0.3]))
        self.assertFalse(capture.running)

    def test_start_and_stop_can_watch_different_devices(self):
        capture = GraphCapture()
        capture.set_triggers("dev-a", "voltage", 1.0, "dev-b", "voltage", 1.0)

        # dev-b readings never arm/fire the start trigger.
        self.assertFalse(capture.check_start("dev-b", [0.5], [], t=0.0))
        self.assertFalse(capture.check_start("dev-b", [1.5], [], t=1.0))
        self.assertFalse(capture.running)

        self.assertFalse(capture.check_start("dev-a", [0.5], [], t=2.0))
        self.assertTrue(capture.check_start("dev-a", [1.5], [], t=3.0))
        self.assertTrue(capture.running)

        # dev-a readings never fire the stop trigger once running.
        self.assertFalse(capture.check_stop("dev-a", [1.5], []))
        self.assertFalse(capture.check_stop("dev-a", [0.5], []))
        self.assertTrue(capture.running)

        self.assertFalse(capture.check_stop("dev-b", [1.5], []))
        self.assertTrue(capture.check_stop("dev-b", [0.5], []))
        self.assertFalse(capture.running)


class GraphCaptureOriginTest(unittest.TestCase):
    def test_fresh_start_sets_origin(self):
        capture = GraphCapture()
        capture.start(5.0)
        self.assertEqual(capture.origin, 5.0)
        self.assertTrue(capture.running)

    def test_start_after_stop_keeps_existing_origin(self):
        capture = GraphCapture()
        capture.start(5.0)
        capture.stop()
        capture.start(9.0)
        self.assertEqual(capture.origin, 5.0)
        self.assertTrue(capture.running)

    def test_clear_while_stopped_resets_origin_to_none(self):
        capture = GraphCapture()
        capture.start(5.0)
        capture.stop()
        capture.clear(20.0)
        self.assertIsNone(capture.origin)

    def test_clear_while_running_rebases_origin(self):
        capture = GraphCapture()
        capture.start(5.0)
        capture.clear(20.0)
        self.assertEqual(capture.origin, 20.0)


class DeviceDataStoreCaptureGatingTest(unittest.TestCase):
    def test_append_while_stopped_updates_last_and_energy_but_not_count(self):
        capture = GraphCapture()  # never started
        store = DeviceDataStore(CHANNELS, data_length=10, capture=capture)
        eng = append_sample(store, v=4.0, i=1.0, t1=1.0)
        self.assertEqual(store.last("voltage"), 4.0)
        self.assertEqual(store.last("current"), 1.0)
        self.assertGreater(eng, 0.0)
        self.assertEqual(store.energy, eng)
        self.assertEqual(store.update_count, 0)

    def test_append_while_running_writes_buffer(self):
        capture = GraphCapture()
        capture.start(0.0)
        store = DeviceDataStore(CHANNELS, data_length=10, capture=capture)
        append_sample(store, v=4.0, i=1.0, t1=1.0)
        self.assertEqual(store.update_count, 1)
        with store.sync_lock:
            xs = store.ordered(store.times)
            ys = store.ordered(store.series["voltage"])
        self.assertAlmostEqual(xs[0], 1.0)
        self.assertEqual(ys[0], 4.0)

    def test_mark_gap_after_stop_inserts_nan_row(self):
        capture = GraphCapture()
        capture.start(0.0)
        store = DeviceDataStore(CHANNELS, data_length=10, capture=capture)
        append_sample(store, v=4.0, i=1.0, t1=1.0)
        capture.stop()
        store.mark_gap()
        self.assertEqual(store.update_count, 2)
        with store.sync_lock:
            ys = store.ordered(store.series["voltage"])
        self.assertTrue(math.isnan(ys[-1]))

    def test_mark_gap_on_empty_store_is_a_no_op(self):
        capture = GraphCapture()
        store = DeviceDataStore(CHANNELS, data_length=10, capture=capture)
        store.mark_gap()
        self.assertEqual(store.update_count, 0)


if __name__ == "__main__":
    unittest.main()
