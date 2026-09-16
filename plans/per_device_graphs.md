# Per-device graphs (Graph Layout: Shared / Per Device)

## Context

Commit b635a14 added **Device Layout** (all panels stacked vs one selected panel). The graph area is still a single shared set:
- one row of channel buttons
- one Start/Stop, AUTO, KEEP and CLEAR
- one set of `GraphBlock`s, with a chip and a curve for each device
- one buffer slider
- one `GraphCapture` (a shared timeline and one trigger device)

This change adds a second setting, **Graph Layout**:
- **Shared** (default): works exactly as today in both device layouts.
- **Per Device**: each device gets its own complete graph section:
  - channel buttons, Start/Stop, AUTO, KEEP and CLEAR
  - its graph blocks, with no device chips
  - its own buffer slider
  - its own `GraphCapture`, so its own timeline and its own auto-start/stop triggers watching that device
  - **All Devices layout:** one section for each device, stacked vertically.
  - **Single Device layout:** only the selected device's section is shown.

These stay global in both modes:
- Data FPS combo and the live FPS label
- REC, Dump, the floating window and energy reset (回零)
- the COM speed and error readouts

Decisions made with the user: per-device controls cover both viewing and capture (Start/Stop, timeline, triggers); per-device sections are stacked vertically.

## Approach

Move everything graph-related out of `MDPMainwindow` into a reusable `GraphView` widget. The main window holds one `GraphView` in Shared mode and one for each device in Per Device mode. Panels are already built from a Designer template (`device_panel_p906.ui`), so `GraphView` follows the same pattern. That keeps the moved widgets looking identical and keeps their `.ui` translations.

### Phase 1 — New template `gui_source/mdp_gui_template/graph_view.ui` (+ `graph_view_ui.py` via `venv/bin/pyuic5`)

Move these widgets out of `mainwindow.ui` without changing them:
- `btnGraphVoltage` … `btnGraphSweep`
- `btnGraphRun`, `btnGraphAutoScale`, `btnGraphKeep`, `btnGraphClear`
- `widgetGraphsContainer`/`layoutGraphs`
- `frameGraphControl` (`labelBufferSize`, `horizontalSlider`, `labelDisplayRange`)

Layout, top to bottom:
1. **Header row:** a new `labelDeviceName` (hidden in Shared mode), the channel buttons, a stretch, then Run/AutoScale/Keep/Clear.
2. **Graphs container** (stretch 1).
3. **Slider frame.**

### Phase 2 — `mainwindow.ui` (+ regenerate `mainwindow_ui.py`)

- Remove the widgets listed above.
- Row 1 keeps `label_29`/`comboDataFps`.
- Row 2 keeps `labelFps`, REC, Dump, FloatWindow and RecordClear.
- Replace `widgetGraphsContainer`, the spacer and `frameGraphControl` with `widgetGraphViews`/`layoutGraphViews` (QVBoxLayout, stretch 1).

### Phase 3 — New `gui_source/graph_view.py`: `class GraphView(QtWidgets.QWidget)`

**State it owns:**
- `self.capture = GraphCapture(on_auto_start=self.auto_started.emit, on_auto_stop=self.auto_stopped.emit)`
- `self.panels`, `graph_blocks`, `_channel_buttons`, `graph_keep_flag`, `_graph_auto_scale_flag`, `_left_last`
- Signals `auto_started`/`auto_stopped`, connected internally to refresh the Run button or run `_on_stopped`. They are queued from the worker thread, as today.

**Moved from `mdp_gui.py` with `self.ui.*` → the view's own ui** (same logic, scoped to `self.panels`):
- `initGraph`
- `_show_graph_block`/`_hide_graph_block`/`_on_channel_toggled`
- `_sync_gated_channels`, `_resync_axes`, `_set_graph_controls_enabled`, `_sync_sweep_axes`, `_series_for_block`
- `on_horizontalSlider_sliderMoved`
- `draw_graph` → `draw()`, without the `labelFps` line
- `on_btnGraphClear_clicked` (confirm, then `capture.clear`, `store.clear()`/`clear_aux_data()` for its own panels only)
- `on_btnGraphRun_clicked`, `_on_graph_stopped`, `_refresh_graph_run_button`, `_graph_trigger_tooltip`
- `on_btnGraphKeep_clicked`, `on_btnGraphAutoScale_clicked`, `_set_graphs_mouse_enabled`
- the graph part of `close_state_ui`/`open_state_ui` → `refresh_link_state()`:
  - Enables the slider frame when any of its panels is linked.
  - Resets to the empty state when none is linked and there is no data.
  - Stops a running capture once all of its panels are unlinked.

**New methods:**
- `set_panels(panels, device_name=None)`:
  - Sets `self.panels` and calls `block.set_panels`.
  - Calls `panel.set_capture(self.capture)` for each panel.
  - A `device_name` shows `labelDeviceName` in the device color and hides the block chips.
- `set_triggers(device_id, dev_settings)` → `capture.set_triggers(...)` from that device's trigger fields, then refreshes the Run button.
- `refresh_device_color(panel)`, `apply_theme()`, `set_data_length(n)`.

Add `graph_view.py` to `gui_source/mdp.pro` SOURCES.

### Phase 4 — `graph_block.py`

- Add `GraphBlock.set_chips_visible(visible)`. Per Device views call it with `False`.
- In Shared mode chips behave as today, including in the Single Device layout.
- `checked_panels` must treat hidden chips as checked. Simplest way: they stay checked, because Per Device views never disable them.

### Phase 5 — `device_panel.py`

Add `DevicePanelBase.set_capture(capture)`, which sets `self.capture` and `self.store.capture`. The constructor keeps its `capture` argument.

### Phase 6 — `settings_model.py`

- **`UiSettings.graph_layout`:** new setting, `"shared"` (default) or `"separate"`.
- **Move the trigger settings** `graph_autostart`, `graph_autostart_threshold`, `graph_autostop`, `graph_autostop_threshold` from `UiSettings` to `DeviceSettings`. Every device then has its own triggers.
  - **Shared mode:** the view uses the settings of the device named by `ui.graph_trigger_device`.
  - **Per Device mode:** each view uses its own device's settings.
- No migration of the old `ui.*` keys (no backwards compatibility).

### Phase 7 — `mdp_gui.py`: `MDPMainwindow`

- **Construction:** build panels with a placeholder `GraphCapture` (any capture works, since `set_panels` rebinds it right away), then call `_rebuild_graph_views()`.
- **`_rebuild_graph_views()`:**
  1. Stop captures and delete the existing views.
  2. **Shared mode:** create one `GraphView(panels)`.
  3. **Per Device mode:** create one view per panel, keyed by `device_id`, each with `device_name`.
  4. Add the views to `layoutGraphViews` (stretch 1).
  5. Clear every store, as `set_data_length` already does.
  6. Apply triggers, then update visibility.

  It is called from `__init__`, `rebuild_panels`, and a new `set_graph_layout(mode)` slot. `set_graph_layout` saves `setting.ui.graph_layout` first. Switching modes clears graph data, because per-device timelines can't be merged into one shared timeline.
- **`_update_graph_view_visibility()`:** in Per Device mode with the Single Device layout, show only the selected device's view. Call it from `set_device_layout` and `select_device`. When a view becomes visible, call `view.draw()` right away.
- **`draw_graph` timer tick:** update `labelFps`, then call `draw()` on each visible view. `_sync_gated_channels` runs inside `draw()`, so each view gates Discharge/Sweep on its own panels.
- **`on_panel_link_toggled`:**
  - The first overall link still starts `draw_graph_timer`.
  - The last overall unlink still stops REC and the COM readouts, and stops the timer only when no store has data.
  - Per-view capture stop and slider state move to `view.refresh_link_state()` for every view.
- **`set_graph_triggers`:** Shared mode uses the chosen device; Per Device mode calls `view.set_triggers(own id, own settings)` for each view.
- **Fan-out to every view:** `on_device_color_changed`, `apply_theme`, `set_data_length`.
- **Remove:** every method listed in Phase 3 from the main window.
- **Wiring:** connect `DialogGraphics.graph_layout_requested` to `MainWindow.set_graph_layout`.

### Phase 8 — Graphics dialog (`graphics.ui` + `graphics_ui.py`, `dialogs.py`)

- **New control:** add `labelGraphLayout`/`comboGraphLayout` after Device Layout, using the same pattern as `comboDeviceLayout`. Items are `共享` / `每设备独立`. The combo emits `graph_layout_requested("shared"|"separate")`.
- **Triggers:** the existing trigger device combo now also selects whose trigger values the four controls show and edit.
  - Changing the device reloads the controls from that `DeviceSettings`, with signals blocked.
  - Editing a control writes to that device.
  - In Per Device mode, relabel it "Trigger settings for" (`触发设置设备`). In Shared mode it keeps `触发设备`.
  - `initValues` and `on_btnClose_clicked` are updated to match.

### Phase 9 — Translations and docs

- **Translations:** run `venv/bin/pylupdate5 gui_source/mdp.pro`.
  - Moved `.ui` strings get a new context (`GraphView`), and the `tr()` strings move from `MDPMainwindow` to `GraphView`. Carry their existing English over.
  - Add English for the new strings: Graph Layout / Shared / Per Device / Trigger settings for.
  - Rebuild `en_US.qm` with `/usr/bin/lrelease`.
- **`readme_EN.md`** (not `readme.md`):
  - Graph capture section: triggers are per device.
  - Multi-device section: add a paragraph on **Graphic Settings → Graph Layout**, how it combines with Device Layout, and that switching clears the graph.

## Verification

1. Run `venv/bin/python -m unittest gui_source/test_graph_capture.py gui_source/test_battery_aux.py`. `GraphCapture` itself is unchanged.
2. Run `venv/bin/python gui_source/mdp_main.py --sim` with a P906 and an L1060 configured.
3. **Shared + All Devices:** matches today's behavior.
   - one header row and one slider
   - chips on each block
   - Start/Stop, KEEP, AUTO and CLEAR affect both devices
   - auto-start watches the trigger device
4. **Shared + Single Device:** the graph area is unchanged when switching devices.
5. **Per Device + All Devices:** two stacked sections, each with a name label, its own buttons and slider, and no chips.
   - Start only device A: B's graph stays empty.
   - Channel toggles, KEEP, AUTO, CLEAR and slider scrubbing on A leave B untouched.
   - Each device's timeline begins at its own Start.
6. **Per Device + Single Device:** only the selected device's section shows, and switching devices swaps it. A hidden device that is capturing still has its data when shown.
7. **Per-device triggers:** set A's auto-start to V ≥ 1 V and B's to I ≥ 0.5 A. Each fires only its own capture, and each Run button shows Armed separately.
8. **Unlink:** unlinking A while B runs stops only A's capture. After the last unlink, data stays on screen and REC stops.
9. **Switching Graph Layout** while running clears the graphs and stops capture, and nothing crashes. Adding or removing a device in Connection Settings rebuilds the views in both modes.
10. **Discharge/Sweep gating in Per Device mode:** run a sweep on the L1060. Only its section reveals the Sweep button.
11. **English:** `mdp_main.py --sim --english` shows the moved and new strings in English.
