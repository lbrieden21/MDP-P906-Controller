"""Unit tests for the Qt-free helpers in battery_aux.py.

Covers the L1060-discharge helpers (renamed to CapacityAccumulator /
BelowThresholdDebounce) and the P906 charge state machine. A SyntheticBattery
closes the loop for ChargeController the way a real pack would: it clamps
current once the requested voltage would be exceeded and reports the
resulting "cc"/"cv" device_state, so a run through it exercises the same
phase transitions the real P906 would drive.
"""

import unittest

from battery_aux import (
    CHEM_LEAD_ACID,
    CHEM_LI_ION,
    CHEM_LIFEPO4,
    CHEM_NIMH,
    BelowThresholdDebounce,
    CapacityAccumulator,
    ChargeController,
    ChargeFinished,
    ChargeProfile,
    ChargeSetpoint,
    PHASE_CC,
    PHASE_CV,
    PHASE_FLOAT,
    PHASE_PRECHARGE,
    REASON_CUTOFF,
    REASON_MAX_AH,
    REASON_MAX_WH,
    REASON_NDV,
    REASON_TIMEOUT,
    build_profile,
)


class SyntheticBattery:
    """Open-circuit voltage rises linearly with state of charge toward the
    pack's own CV target, plus a fixed internal resistance. step()
    reproduces the charger's CC/CV clamp -- it caps the requested current
    once terminal voltage would exceed v_set -- and reports the "cc"/"cv"
    device_state a real charger would."""

    def __init__(self, capacity_ah, ocv_min, ocv_max, r_ohm, soc=0.02):
        self.capacity_ah = capacity_ah
        self.ocv_min = ocv_min
        self.ocv_max = ocv_max
        self.r_ohm = r_ohm
        self.soc = soc

    def step(self, dt, v_set, i_set):
        ocv = self.ocv_min + (self.ocv_max - self.ocv_min) * self.soc
        v_cc = ocv + i_set * self.r_ohm
        if v_cc <= v_set:
            v, i, state = v_cc, i_set, "cc"
        else:
            i = max(0.0, (v_set - ocv) / self.r_ohm)
            v, i, state = v_set, i, "cv"
        self.soc += i * dt / 3600.0 / self.capacity_ah
        return v, i, state


def run_charge(profile, battery, dt=1.0, max_steps=20000, ndv_window_s=10.0):
    """Drive a ChargeController against a SyntheticBattery until it
    finishes. Returns (reason, phase_history, acc, elapsed_t)."""
    controller = ChargeController(profile, ndv_window_s=ndv_window_s)
    acc = CapacityAccumulator()
    t = 0.0
    setpoint = controller.start(t)
    v_set, i_set = setpoint.v_set, setpoint.i_set
    v0, i0, _ = battery.step(0.0, v_set, i_set)
    acc.add_batch([(v0, i0)], t)
    phases = [controller.phase]
    for _ in range(max_steps):
        t += dt
        v, i, state = battery.step(dt, v_set, i_set)
        acc.add_batch([(v, i)], t)
        action = controller.update(t, v, i, state, acc)
        if isinstance(action, ChargeSetpoint):
            v_set, i_set = action.v_set, action.i_set
        phases.append(controller.phase)
        if isinstance(action, ChargeFinished):
            return action.reason, phases, acc, t
    raise AssertionError(f"charge did not finish within {max_steps} steps")


class CapacityAccumulatorTest(unittest.TestCase):
    """Renamed from DischargeAccumulator; behaviour unchanged."""

    def test_first_batch_only_seeds_the_clock(self):
        acc = CapacityAccumulator()
        acc.add_batch([(4.0, 1.0)], 0.0)
        self.assertEqual(acc.ah, 0.0)
        self.assertEqual(acc.wh, 0.0)

    def test_integrates_ah_and_wh_over_the_batch_gap(self):
        acc = CapacityAccumulator()
        acc.add_batch([(4.0, 1.0)], 0.0)
        acc.add_batch([(4.0, 1.0), (4.0, 1.0)], 2.0)
        self.assertAlmostEqual(acc.ah, 2 * 1.0 * 1.0 / 3600.0)
        self.assertAlmostEqual(acc.wh, 2 * 4.0 * 1.0 / 3600.0)
        self.assertTrue(acc.rows)
        self.assertAlmostEqual(acc.rows[-1].ah, acc.ah)

    def test_empty_and_nonpositive_dt_batches_are_ignored(self):
        acc = CapacityAccumulator()
        acc.add_batch([], 5.0)
        acc.add_batch([(4.0, 1.0)], 10.0)
        acc.add_batch([(4.0, 1.0)], 20.0)
        ah_after = acc.ah
        acc.add_batch([(4.0, 1.0)], 20.0)  # dt == 0
        acc.add_batch([(4.0, 1.0)], 15.0)  # dt < 0
        self.assertEqual(acc.ah, ah_after)


class BelowThresholdDebounceTest(unittest.TestCase):
    """Renamed from VoltageCutoffDebounce; behaviour unchanged, and already
    used here for current as well as voltage."""

    def test_requires_the_full_debounce_window(self):
        d = BelowThresholdDebounce(5.0)
        self.assertFalse(d.update(1.0, 2.0, 0.0))
        self.assertFalse(d.update(1.0, 2.0, 4.0))
        self.assertTrue(d.update(1.0, 2.0, 5.0))

    def test_rising_above_threshold_resets_it(self):
        d = BelowThresholdDebounce(5.0)
        self.assertFalse(d.update(1.0, 2.0, 0.0))
        self.assertFalse(d.update(3.0, 2.0, 2.0))  # rose above -> reset
        self.assertFalse(d.update(1.0, 2.0, 3.0))  # clock restarts here
        self.assertFalse(d.update(1.0, 2.0, 7.0))  # only 4s since restart
        self.assertTrue(d.update(1.0, 2.0, 8.0))  # 5s since restart


class BuildProfileTest(unittest.TestCase):
    def test_lifepo4_voltages_scale_with_cell_count(self):
        p1 = build_profile(CHEM_LIFEPO4, 1, 2.0)
        p4 = build_profile(CHEM_LIFEPO4, 4, 2.0)
        self.assertAlmostEqual(p1.cv_v, 3.65)
        self.assertAlmostEqual(p4.cv_v, 3.65 * 4)
        self.assertAlmostEqual(p4.precharge_below_v, 2.5 * 4)
        # Current scales with capacity, not cell count.
        self.assertAlmostEqual(p1.cc_a, p4.cc_a)

    def test_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            build_profile("bogus", 1, 1.0)
        with self.assertRaises(ValueError):
            build_profile(CHEM_LI_ION, 0, 1.0)
        with self.assertRaises(ValueError):
            build_profile(CHEM_LI_ION, 1, 0.0)


class ChargeControllerLimitsTest(unittest.TestCase):
    """Time/Ah/Wh limits apply regardless of phase, so these drive the
    controller directly at a constant, never-tapering V/I."""

    def _flat_profile(self, **overrides):
        fields = dict(
            chemistry=CHEM_LI_ION,
            cv_v=100.0,
            cc_a=1.0,
            precharge_below_v=None,
            precharge_a=None,
            cutoff_a=None,
            float_v=None,
            ndv_v=None,
            ndv_holdoff_s=None,
            max_s=None,
            max_ah=None,
            max_wh=None,
        )
        fields.update(overrides)
        return ChargeProfile(**fields)

    def _run_until_finished(self, profile, v=3.0, i=1.0, dt=1.0, max_steps=100000):
        controller = ChargeController(profile)
        acc = CapacityAccumulator()
        t = 0.0
        controller.start(t)
        acc.add_batch([(v, i)], t)
        for _ in range(max_steps):
            t += dt
            acc.add_batch([(v, i)], t)
            action = controller.update(t, v, i, "cc", acc)
            if isinstance(action, ChargeFinished):
                return action.reason, t
        raise AssertionError("did not finish")

    def test_time_limit_fires(self):
        reason, t = self._run_until_finished(self._flat_profile(max_s=100.0))
        self.assertEqual(reason, REASON_TIMEOUT)
        self.assertEqual(t, 100.0)

    def test_ah_limit_fires(self):
        reason, t = self._run_until_finished(
            self._flat_profile(max_ah=1.0 / 3600.0), v=3.0, i=1.0
        )
        self.assertEqual(reason, REASON_MAX_AH)
        self.assertEqual(t, 1.0)

    def test_wh_limit_fires(self):
        reason, t = self._run_until_finished(
            self._flat_profile(max_wh=3.0 / 3600.0), v=3.0, i=1.0
        )
        self.assertEqual(reason, REASON_MAX_WH)
        self.assertEqual(t, 1.0)


class CutoffDebounceTest(unittest.TestCase):
    def test_short_glitch_survives_but_sustained_cutoff_finishes(self):
        profile = build_profile(CHEM_LI_ION, cells=1, capacity_ah=1.0)._replace(
            precharge_below_v=None, precharge_a=None
        )
        controller = ChargeController(profile, cutoff_debounce_s=5.0)
        acc = CapacityAccumulator()
        t = 0.0
        controller.start(t)  # no precharge fields -> starts in CC
        acc.add_batch([(profile.cv_v, profile.cc_a)], t)

        t = 1.0
        acc.add_batch([(profile.cv_v, profile.cc_a)], t)
        action = controller.update(t, profile.cv_v, profile.cc_a, "cv", acc)
        self.assertIsNone(action)
        self.assertEqual(controller.phase, PHASE_CV)

        # A 2s dip below cutoff_a, shorter than the 5s debounce.
        for _ in range(2):
            t += 1.0
            acc.add_batch([(profile.cv_v, profile.cutoff_a / 2)], t)
            action = controller.update(t, profile.cv_v, profile.cutoff_a / 2, "cv", acc)
            self.assertIsNone(action)

        # Recovering above cutoff_a for a tick resets the debounce.
        t += 1.0
        acc.add_batch([(profile.cv_v, profile.cc_a)], t)
        action = controller.update(t, profile.cv_v, profile.cc_a, "cv", acc)
        self.assertIsNone(action)

        # A full 5s below cutoff_a now finishes the charge.
        for _ in range(6):
            t += 1.0
            acc.add_batch([(profile.cv_v, profile.cutoff_a / 2)], t)
            action = controller.update(t, profile.cv_v, profile.cutoff_a / 2, "cv", acc)
        self.assertIsInstance(action, ChargeFinished)
        self.assertEqual(action.reason, REASON_CUTOFF)


class NimhNdvTest(unittest.TestCase):
    """device_state is kept at "cc" throughout so only the -dV path is
    exercised, never the voltage-ceiling path."""

    def _run(self, v_func, steps, dt=1.0):
        profile = build_profile(CHEM_NIMH, cells=1, capacity_ah=1.0)
        controller = ChargeController(profile, ndv_window_s=10.0)
        acc = CapacityAccumulator()
        t = 0.0
        controller.start(t)
        acc.add_batch([(v_func(0.0), profile.cc_a)], t)
        for _ in range(steps):
            t += dt
            v = v_func(t)
            acc.add_batch([(v, profile.cc_a)], t)
            action = controller.update(t, v, profile.cc_a, "cc", acc)
            if isinstance(action, ChargeFinished):
                return action
        return None

    def test_dip_after_holdoff_finishes_the_charge(self):
        peak_v = 1.60 + 0.001 * 190

        def v_func(t):
            if t < 190:
                return 1.60 + 0.001 * t
            if t < 220:
                return peak_v
            return peak_v - 0.002 * (t - 220)

        action = self._run(v_func, steps=400)
        self.assertIsInstance(action, ChargeFinished)
        self.assertEqual(action.reason, REASON_NDV)

    def test_dip_inside_the_holdoff_does_not_finish(self):
        def v_func(t):
            if 80 <= t < 100:
                return 1.5 - 0.02 * (t - 80)  # well within the 180s hold-off
            if t < 180:
                return 1.5
            return 1.5 + 0.0005 * (t - 180)  # gentle rise, never dips again

        action = self._run(v_func, steps=400)
        self.assertIsNone(action)


class LiIonChargeCycleTest(unittest.TestCase):
    def test_precharge_cc_cv_cutoff_done_with_sensible_ah(self):
        capacity_ah = 1.0
        profile = build_profile(CHEM_LI_ION, cells=1, capacity_ah=capacity_ah)
        battery = SyntheticBattery(
            capacity_ah, ocv_min=2.8, ocv_max=profile.cv_v, r_ohm=0.84, soc=0.02
        )
        reason, phases, acc, _ = run_charge(profile, battery)
        self.assertEqual(reason, REASON_CUTOFF)
        self.assertEqual(
            [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]][:-1],
            [PHASE_PRECHARGE, PHASE_CC, PHASE_CV],
        )
        # A full charge from ~2% SoC should land close to, not wildly past
        # or short of, the pack's nameplate capacity.
        self.assertGreater(acc.ah, 0.5 * capacity_ah)
        self.assertLess(acc.ah, capacity_ah)


class LeadAcidFloatTest(unittest.TestCase):
    def _battery(self, cells, capacity_ah, cv_v):
        return SyntheticBattery(
            capacity_ah, ocv_min=1.9 * cells, ocv_max=cv_v, r_ohm=0.5 * cells, soc=0.3
        )

    def test_float_off_ends_at_cutoff(self):
        cells, capacity_ah = 6, 1.0
        profile = build_profile(CHEM_LEAD_ACID, cells, capacity_ah)._replace(
            float_v=None
        )
        reason, phases, _, _ = run_charge(
            profile, self._battery(cells, capacity_ah, profile.cv_v)
        )
        self.assertEqual(reason, REASON_CUTOFF)
        self.assertNotIn(PHASE_FLOAT, phases)

    def test_float_on_continues_until_the_time_limit(self):
        cells, capacity_ah = 6, 1.0
        base = build_profile(CHEM_LEAD_ACID, cells, capacity_ah)

        # Find when the cutoff current would otherwise end the charge -- that
        # instant is exactly when float begins -- then confirm float instead
        # holds past it, until a time limit set comfortably beyond that point.
        no_float = base._replace(float_v=None)
        cutoff_reason, _, _, cutoff_t = run_charge(
            no_float, self._battery(cells, capacity_ah, base.cv_v)
        )
        self.assertEqual(cutoff_reason, REASON_CUTOFF)

        with_float = base._replace(max_s=cutoff_t + 200.0)
        reason, phases, _, t = run_charge(
            with_float, self._battery(cells, capacity_ah, base.cv_v)
        )
        self.assertEqual(reason, REASON_TIMEOUT)
        self.assertIn(PHASE_FLOAT, phases)
        self.assertAlmostEqual(t, cutoff_t + 200.0)


if __name__ == "__main__":
    unittest.main()
