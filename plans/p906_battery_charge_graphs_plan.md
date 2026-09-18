# P906 Battery Charge graphs: Ah, Wh, Charge Curve

## Context
The L1060 discharge aux function shows three graph types (Ah, Wh, Discharge Curve) that appear only while a discharge is running or its result is still held. The P906 Battery Charge aux function already calculates the same totals (`battery_aux.CapacityAccumulator` in `self._charge_acc`), but only shows them in labels and the CSV export. `CHARGE_CSV_CHANNELS` in `device_panel_p906.py:62` says so directly: "ah/wh are not store channels, so nothing is added to the graph". The goal is to give charging the same graph treatment: Ah, Wh, and a Charge Curve (voltage against Ah).

## Approach
Use the same mechanism the L1060 already uses:
- ah and wh become ordinary store channels, fed through `_extra_channel_values()`.
- A has-data predicate gates the graph buttons.
- A synthetic graph key plots voltage against ah.

The Ah and Wh blocks are shared. The store channel keys are the same (`"ah"`/`"wh"`), so one Ah block shows L1060 discharge and P906 charge curves side by side, with a chip for each device. Only the curve block is new.

### 1. `gui_source/device_panel.py` (base)
- Add `has_charge_data() -> bool` returning False, next to `has_discharge_data()`/`has_sweep_data()`, with a matching docstring.
- Update the `has_discharge_data` and `clear_aux_data` docstrings. They currently say P906 has no such workflows, which will no longer be true.

### 2. `gui_source/device_panel_p906.py`
- `CHANNELS = BASE_CHANNELS + [ChannelSpec("ah", …), ChannelSpec("wh", …)]`, following `device_panel_l1060.py:35-43`. `CHANNEL_SHORT = {**BASE_CHANNEL_SHORT, "ah": "Ah", "wh": "Wh"}`. Update the stale module comment at lines 56-58.
- Build `CHARGE_CSV_CHANNELS` from `CHANNEL_BY_KEY["ah"/"wh"]` instead of defining its own ChannelSpecs, and remove the "not store channels" comment.
- Add `_extra_channel_values(len_)`. It returns `np.full(len_, acc.ah/acc.wh)`, or 0.0 when `_charge_acc is None`, held flat between runs just like L1060 (`device_panel_l1060.py:1130`).
- Add `has_charge_data()`, which returns `self._charge_acc is not None`.
- Add `clear_aux_data()`, which sets `_charge_acc = None` only when `not self._charge_active`. This matches `_clear_discharge_data`.
- `record_channels` stays `RECORD_CHANNELS`, so the normal recording CSV is unchanged.

### 3. `gui_source/graph_view.py`
- `CHANNEL_ORDER`: insert `"charge"` after `"discharge"`.
- Split the gating groups:
  - `CAPACITY_GATED_CHANNELS = {"ah", "wh"}`: visible if any panel has discharge **or** charge data.
  - `DISCHARGE_GATED_CHANNELS = {"discharge"}`
  - `CHARGE_GATED_CHANNELS = {"charge"}`
  - `SWEEP_GATED_CHANNELS` is unchanged.
  
  Update `draw()` to call `_sync_gated_channels` for each group, and update the header comment.
- Add a synthetic `CHANNEL_BY_KEY["charge"] = ChannelSpec("charge", translate("MDPMainwindow", "充电曲线"), "V")` and `CHANNEL_SHORT["charge"] = "V"`, alongside "discharge".
- `initGraph`: map `"charge": self.ui.btnGraphCharge`. `_show_graph_block`: set the bottom label to "Ah" for `key in ("discharge", "charge")`.
- `_series_for_block`: handle `"charge"` the same way as `"discharge"`, with `get_series("voltage", …, x_key="ah")`. `draw()`'s nanmin/nanmax x-bounds branch becomes `("discharge", "charge", "sweep")`.
- **Per-panel curve filter.** P906 panels will now have an `ah` series, so without a filter the Discharge Curve block would also draw a P906's charge curve (and the reverse). Add a small helper `_panel_has_block_data(key, panel)`:
  - discharge uses `has_discharge_data()`
  - charge uses `has_charge_data()`
  - ah/wh use either one
  - every other block returns True
  
  In `draw()`'s per-panel loop, a panel failing the check is cleared (`curve.setData(x=[], y=[])`) the same way unchecked panels are. Side effect: idle panels no longer draw a flat zero line in Ah/Wh.

### 4. `gui_source/mdp_gui_template/graph_view.ui` → regenerate `graph_view_ui.py`
- Add `btnGraphCharge` right after `btnGraphDischarge`, copying its properties: max width 90, font, `visible=false`, checkable. Text `充电曲线`, tooltip `显示/隐藏充电曲线(电压-安时)`.
- Make the Ah/Wh tooltips generic: `显示/隐藏放电安时曲线` → `显示/隐藏安时曲线`, and the same for 瓦时.
- Regenerate with `venv/bin/pyuic5 gui_source/mdp_gui_template/graph_view.ui -o gui_source/mdp_gui_template/graph_view_ui.py`. The path matches the existing header.

### 5. Translations
- `venv/bin/pylupdate5 gui_source/mdp.pro`, then fill in en_US.ts: 充电曲线 → "Chg Curve" (matching the existing "DisChr Curve" abbreviation). Also the new tooltips: "Show/hide the charge curve (voltage vs. Ah)", "Show/hide the Ah graph", "Show/hide the Wh graph".
- `lrelease gui_source/en_US.ts` to update en_US.qm.

### 6. `readme_EN.md`
Update the Battery Charge section to mention the Ah/Wh/Charge Curve graphs. English readme only.

## Verification
- `venv/bin/python gui_source/mdp_main.py --sim` (or however `--sim` is invoked). Link a sim P906 and start Battery Charge.
  - Ah, Wh and Charge Curve buttons appear and are auto-checked, and the curves grow.
  - After Stop, they hold the final values.
  - After graph CLEAR (with no charge running), the buttons hide again.
- With a sim L1060 also linked in the shared view, run a discharge. Discharge Curve shows only the L1060, Charge Curve only the P906, and Ah/Wh show both, each on its own chip.
- Per-device graph layout: each device's view shows only its own gated buttons.
- Charge CSV save still produces the same columns.
- `venv/bin/python -m pytest gui_source/test_battery_aux.py gui_source/test_graph_capture.py`
