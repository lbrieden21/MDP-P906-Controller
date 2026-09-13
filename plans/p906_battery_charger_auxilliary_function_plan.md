# P906 Battery Charger auxiliary function

## Context
The P906 panel already has six auxiliary tabs: Preset, Power hold, DC sweep, Function generator, Sequence and Battery simulation. None of them charges a battery, and no charge code exists anywhere in the repo. This plan adds a **Battery Charge** tab. You pick a chemistry preset and a cell count, and it runs a real CC/CV (or NiMH −ΔV) charge. It stops on cutoff current, a max time limit or a max capacity. While running it shows the phase, mAh, Wh and elapsed time, and the result can be saved to CSV. The live V/I data goes to the existing main graph; no new graph channel is added.

Decisions already made:
- **Chemistries:** Li-ion/LiPo, LiFePO4, lead-acid and NiMH/NiCd.
- **Setup:** presets plus cell count, and you can edit every derived value.
- **Lead-acid:** a checkbox chooses between switching to float after absorption and stopping.
- **Testing:** the charge logic is Qt-free and covered by unittest against a synthetic battery model; the sim P906 stays unchanged.

The L1060 Discharge tool is the pattern to copy:
- `gui_source/device_panel_l1060.py:935-1050`, which integrates samples through the `_on_raw_batch` hook at line 1119.
- `gui_source/l1060_aux.py:145-237`, the Qt-free helpers.

## 1. Shared Qt-free helpers: new `gui_source/battery_aux.py`
- **Moved from `l1060_aux.py`, then deleted there (full cutover, no shims):**
  - `DischargeRow` becomes `CapacityRow`.
  - `DischargeAccumulator` becomes `CapacityAccumulator`. Integrating Ah/Wh works the same in either direction, so the logic is unchanged.
  - `VoltageCutoffDebounce` becomes `BelowThresholdDebounce`. The logic is unchanged and already works for current as well as voltage.
- **Updated to the new names:**
  - the imports in `device_panel_l1060.py` (lines 18-22) and the uses at lines 37, 966 and 967;
  - the "Battery discharge" section header and docstring in `l1060_aux.py`.
- **New `ChargeProfile`** (NamedTuple), with per-cell values scaled by cell count:
  - chemistry
  - `cv_v`: CV target, or the voltage ceiling for NiMH
  - `cc_a`
  - `precharge_below_v` and `precharge_a` (both optional)
  - `cutoff_a`
  - `float_v` (optional)
  - `ndv_v` (−ΔV threshold) and `ndv_holdoff_s` (both NiMH only)
  - `max_s`, `max_ah`, `max_wh` (each optional)
- **New `CHEMISTRY_PRESETS` and `build_profile(chem, cells, capacity_ah)`.** The defaults below are all editable in the UI:

  | Chemistry | Per-cell voltages | Default current | Stop rule |
  |---|---|---|---|
  | Li-ion/LiPo | 4.20 V CV; precharge below 3.0 V at C/10 | 0.5C | Cutoff at C/20 |
  | LiFePO4 | 3.65 V CV; precharge below 2.5 V at C/10 | 0.5C | Cutoff at C/20 |
  | Lead-acid | 2.40 V absorption, 2.25 V float | C/5 | Cutoff at C/20 → float or stop |
  | NiMH/NiCd | 1.80 V ceiling | 0.5C (−ΔV needs ≥0.5C to show up) | −ΔV of 5 mV/cell after a 180 s hold-off |

- **New `ChargeController`**, a pure state machine:
  - Phases: `PRECHARGE → CC → CV → FLOAT` (lead-acid, if chosen) `→ DONE`.
  - `start()` returns the first `(v_set, i_set)`.
  - `update(t, v, i, device_state, acc)` returns an action: keep going, set a new V/I, or finish with a reason string key.
  - It moves from CC to CV when the device reports `cv`. It moves from precharge to CC when V rises above `precharge_below_v`, debounced.
  - Cutoff uses `BelowThresholdDebounce` on current, only while in CV. Lead-acid cutoff leads to float or to done, depending on the checkbox.
  - NiMH tracks a smoothed peak voltage (moving average over about 10 s) and finishes when V ≤ peak − `ndv_v` after the hold-off.
  - Time, Ah and Wh limits apply in every phase, float included. A float charge ends only on Stop or a limit.
  - Reasons are plain keys such as `"cutoff"`, `"timeout"`, `"max_ah"`, `"ndv"`, `"output_off"` and `"device_error"`. The panel maps them to `self.tr()` strings, which keeps the module free of Qt.

## 2. UI: `gui_source/mdp_gui_template/device_panel_p906.ui`
Add `tabCharge` (title 电池充电) after `tabBat`, copying `tabBat`'s structure (start button, status row, scroll area):
- **Top of the tab:**
  - `btnCharge` (开始充电 / 停止充电)
  - a status row with `labelChargePhase`, `labelChargeElapsed`, `labelChargeAh` (mAh) and `labelChargeWh`
  - `labelChargeReason`
- **Inside `scrollAreaCharge`:**
  - `comboChargeChem`, `spinBoxChargeCells`, `spinBoxChargeCapacity` (mAh)
  - `btnChargeApplyPreset`, which fills the fields below from the preset
  - `spinBoxChargeCV`, `spinBoxChargeCC`, `spinBoxChargeCutoff`
  - `spinBoxChargeFloat` together with `checkBoxChargeFloat`
  - `spinBoxChargeNdv` and `spinBoxChargeHoldoff` (NiMH only)
  - checkbox + spinbox pairs: `checkBoxChargeMaxTime`/`spinBoxChargeMaxTime` (minutes), `checkBoxChargeMaxAh`/`spinBoxChargeMaxAh`, `checkBoxChargeMaxWh`/`spinBoxChargeMaxWh`
  - `btnChargeSave` (CSV)
- **Visibility:** fields that don't apply to the chosen chemistry are hidden.
- **Regenerate** `device_panel_p906_ui.py` with `venv/bin/pyuic5`.

## 3. Panel logic: `gui_source/device_panel_p906.py`
Add a new `######### 辅助功能-电池充电 #########` section, modelled on the L1060 discharge flow.
- **Setup:**
  - `_init_timers`: add `charge_timer` with a 200 ms tick.
  - `_init_combos`: add the chemistry items, plus a `currentTextChanged` handler that shows or hides the chemistry-specific fields.
- **`on_btnCharge_clicked` (start):**
  - If a charge is already active, stop with the "user stopped" reason.
  - Otherwise refuse if `api is None`, the panel is locked, or another P906 aux timer is active. This reuses the timer set already listed in `unlink()`, factored into `_any_aux_active()`.
  - Validate that CV voltage ≤ 30 V and CC current ≤ `spinBoxCurrent.maximum()`, so the P905's 5 A limit is respected. On failure, flash 非法参数 on the button, the same way the other tabs do.
  - Build the profile, create a `CapacityAccumulator`, set `v_set`/`i_set` from `controller.start()`, then `wait_output_stable(...)` and start `charge_timer`.
  - Record `_charge_enable_rel` the same way L1060 does, so samples from before enable are never credited.
- **Mutual exclusion:** the other five start handlers (sweep, wave generator, keep power, sequence, battery sim) refuse to start while a charge is active.
- **`_on_raw_batch` override:** while charging and past `_charge_enable_rel`, call `acc.add_batch(raw, t1)` and store the last V/I.
- **`_charge_tick`:**
  - Update the labels.
  - Abort if `output_state_str == "off"` (the output was switched off manually or by the device) or if a nonzero `ErrFlag` was seen. For this, `update_state` saves `ErrFlag` to `self._err_flag`.
  - Otherwise call `controller.update(...)` and apply the returned `v_set`/`i_set`, or finish.
- **`_finish_charge(reason)`:**
  - Stop the timer, turn output off (`output_state = False`) and re-enable the inputs.
  - Show the translated reason.
  - Float keeps output on only while it is running; when float ends, output turns off like every other end.
- **Other hooks:**
  - `unlink()` also stops the charge.
  - `update_state` keeps `spinBoxVoltage`/`spinBoxCurrent` disabled while a charge is active, so the 100 ms poll can't re-enable them.
- **`on_btnChargeSave_clicked`:** `RecordData` with voltage/current/power/ah/wh from `acc.rows`, following `device_panel_l1060.py:1029`. The P906 module gets its own small `CHARGE_CSV_CHANNELS` list (ah/wh `ChannelSpec`s), so no graph channels are added.

## 4. Translations and docs
- **Translations:** add `battery_aux.py` to `SOURCES` in `gui_source/mdp.pro`. Then run `venv/bin/pylupdate5 mdp.pro`, fill in the English strings in `en_US.ts` (both the `DevicePanelP906` and `P906DevicePanel` contexts), and run `lrelease en_US.ts`. Check `set_english_fonts` for text that overflows.
- **`readme_EN.md` only** (never `readme.md`):
  - Add a "Battery charger" bullet under Features.
  - Add a "P906 Battery Charge" subsection covering the presets, phases, stop conditions, the lead-acid float option, the CSV export, and the fact that the P906 measures voltage at its output terminals.
  - Describe the current state only.

## 5. Tests: new `gui_source/test_battery_aux.py` (unittest)
- **Synthetic battery:** OCV rises with SoC, plus internal resistance. A helper closes the loop by reproducing the PSU's CC/CV clamp and the `device_state` it would report.
- **Test cases:**
  - Li-ion: precharge → CC → CV → cutoff → done, with sensible Ah.
  - LiFePO4: target voltage scales with cell count.
  - Lead-acid: float on continues until the time limit; float off ends at cutoff.
  - NiMH: a −ΔV dip after the hold-off finishes the charge; a dip inside the hold-off doesn't.
  - The time, Ah and Wh limits each fire.
  - A cutoff glitch shorter than the debounce doesn't end the charge.
- **Renamed helpers:** `CapacityAccumulator` and `BelowThresholdDebounce` keep their behaviour (a quick regression check).

## Verification
1. `cd gui_source && ../venv/bin/python -m unittest test_battery_aux -v` passes.
2. Run the GUI with `venv/bin/python gui_source/mdp_main.py --sim`, then:
   - Open the Charge tab and apply each preset.
   - Start a charge and check that V/I are set, the phase shows CC or CV, and mAh/Wh/elapsed count up.
   - With a 1-minute max time, check the charge stops with the "timeout" reason and output goes off.
   - Check that Stop works, that other aux functions refuse to start during a charge, and that CSV save writes rows.
   - Run once in English to check layout.
3. L1060 discharge still works in `--sim` after the helper rename. Also run a grep for the old names, which should return nothing.
4. Real hardware (your call): a single Li-ion cell at low current, to confirm the CC→CV→cutoff transitions against the P906's real `cv` state reporting.
