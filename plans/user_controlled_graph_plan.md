# User-controlled graph Start/Stop with auto-start and auto-stop

## Context

Linking a device starts graphing right away. `DevicePanelBase.state_callback` (`gui_source/device_panel.py:191`) writes every sample into `DeviceDataStore`, and `on_panel_link_toggled` (`mdp_gui.py:402`) starts `draw_graph_timer`.

The user wants to control when capture starts and stops:
- A Start/Stop button.
- An optional **auto-start**: begin capturing when one chosen device's voltage or current rises across a threshold.
- An optional **auto-stop**: stop capturing when that device's voltage or current falls across a threshold.

**What the REC button does today** (`on_btnGraphRecord_clicked`, `mdp_gui.py:819`):
- It is unrelated to the graph.
- It records raw full-rate V/I samples (before averaging) for each device into a `RecordData` in memory.
- It writes them to `record_<timestamp>.csv` every 30 s and again on Stop, one file per device when there are several.
- It stops by itself when the last device unlinks.

REC stays unchanged.

### Decisions made with the user

**Timeline**
- Pressing Start when the graph already has data continues the same timeline after a line break.
- CLEAR is the only way to reset to t=0.
- The first Start on an empty graph is t=0.

**Auto-start**
- Triggers on a rising crossing only: the reading goes from below the threshold to at or above it.
- A device that is already above the threshold when it links, or when Stop is pressed, will not trigger until it drops below and rises again.

**Auto-stop**
- Triggers on a falling crossing: the reading goes from at or above the threshold to below it.
- It has its own mode (off / voltage / current) and its own threshold, separate from auto-start's.

**Triggers**
- Both triggers watch a single device chosen in settings. Other devices are captured but never trigger anything.

**Settings location**
- The trigger settings live in the Graphics settings dialog.
- The Start/Stop button shows an "Armed" state.

### Key constraint

Stopping capture cannot simply skip `store.append()`. Panel logic depends on the store even while nothing is being graphed:
- `store.last()` feeds the P906 stable checker, sweep, keep-power and battery-sim (`device_panel_p906.py:412, 692, 870, 1026`).
- The Energy LCD total and the LCD averaging lists are updated inside `append()`.

`append()` therefore keeps doing all of that on every sample. Only the ring-buffer write is gated.

---

## Phase 1 — `GraphCapture` core and store gating (Qt-free)

### `gui_source/device_core.py`: new `GraphCapture`

`state_callback` runs on the serial/TCP worker thread, so all of this state is protected by a `threading.Lock`.

**Fields**

| Field | Meaning |
|---|---|
| `running` | Whether capture is on. |
| `origin` | The shared graph time origin, or `None`. |
| `device_id` | The device the triggers watch. |
| `start_mode`, `start_threshold` | Auto-start trigger: mode is `"off"`, `"voltage"` or `"current"`. |
| `stop_mode`, `stop_threshold` | Auto-stop trigger, same modes. |
| `_last_below` | Watched device's last reading relative to the active trigger's threshold: `True` (below), `False` (at or above), or `None` (no baseline yet). |
| `on_auto_start`, `on_auto_stop` | Callbacks. |

**Methods**

- **`start(t)`**: sets `running`. If `origin is None`, sets `origin = t`. Resets `_last_below = None`, because the stop trigger measures against a different threshold.
- **`stop()`**: clears `running` and resets `_last_below = None`.
- **`clear(t)`**: used by CLEAR. Sets `origin = t` if running, otherwise `None`.
- **`set_triggers(device_id, start_mode, start_threshold, stop_mode, stop_threshold)`**: updates all trigger fields and resets `_last_below`.
- **`forget(device_id)`**: resets `_last_below` if `device_id` is the watched device. Called on link and unlink.
- **`check_start(device_id, voltages, currents, t) -> bool`**
  - Does nothing unless the capture is stopped, `start_mode` is not off, and `device_id` is the watched device.
  - Walks the chosen series in order. The first reading only sets the baseline.
  - A reading below the threshold followed by one at or above it calls `start(t)` and `on_auto_start()`.
  - Crossings inside a single batch count.
- **`check_stop(device_id, voltages, currents) -> bool`**
  - Mirror of `check_start`: does nothing unless the capture is running, `stop_mode` is not off, and `device_id` is the watched device.
  - A reading at or above the threshold followed by one below it clears `running` and calls `on_auto_stop()`.
  - If the watched reading is already below the stop threshold when capture starts, the stop will not fire until the reading rises and falls again.

`GraphCapture` replaces `ConnectionManager.time_origin` / `reset_time_origin()` (`connection.py:24, 70`), which existed only for graph alignment.

### `DeviceDataStore` (same file)

- Its constructor takes `capture: GraphCapture`. `start_time` is removed; times are computed against `capture.origin`.
- **`append()`** always updates:
  - the temporary sample lists
  - `last_time` / `dt`
  - `energy`
  - a new `_latest` dict holding each channel's last value

  It writes `times` / `series` and calls `_advance` only when `capture.running` is true, reading `capture.origin` once.
- **`last(key)`** returns `_latest[key]`.
- **`mark_gap()`** uses `capture.origin`. It skips the write when the store is empty or when `origin is None`.
- **`clear()`** no longer takes a start time.

### Tests: new `gui_source/test_graph_capture.py`

Unittest, in the style of `test_battery_aux.py`.

**Start trigger**
- A first reading at or above the threshold does not trigger.
- A below-then-above sequence triggers, including within one batch.
- Mode off never triggers.
- Current mode ignores voltage.
- A device other than the watched one never triggers.
- After `stop()`, a reading that is still high does not re-trigger. A drop followed by a rise does.

**Stop trigger**
- Above-then-below stops capture and calls `on_auto_stop`.
- A reading that is already below at start does not stop until it rises and falls.
- The stop trigger is ignored while stopped.
- Start on voltage with stop on current behaves independently.

**Origin**
- A fresh `start` sets the origin.
- A `start` after `stop` keeps the existing origin.
- `clear` while stopped resets the origin to `None`.

**Store**
- While stopped, `append()` updates `last()` and `energy`, but `update_count` stays 0.
- While running, `append()` writes to the buffer.
- `mark_gap` after a stop inserts a NaN row.

**Phase 1 check:** `venv/bin/python -m unittest gui_source/test_graph_capture.py gui_source/test_battery_aux.py`

---

## Phase 2 — Panels and connection wiring

### Base panel: `gui_source/device_panel.py`

**`DevicePanelBase.__init__`**
- Takes `capture`, keeps it as `self.capture`, and passes it to the store.
- The P906 and L1060 constructors (`device_panel_p906.py:93`, `device_panel_l1060.py:148`) accept `capture` and pass it through.

**`state_callback`**, after `_preprocess_rtvalues`:
1. If `self.linked`, run `capture.check_start(...)` on the raw samples **before** `store.append`, so the triggering batch is captured.
2. Run `capture.check_stop(...)` **after** `store.append`, so the crossing sample itself lands in the graph.
3. The `self.linked` guard keeps the synthetic `(0, 0)` sample that `close_state_ui(record_disconnect=True)` injects from acting as a trigger.

**`link()`**
- Drops its `time_origin` argument.
- Calls `capture.forget(device_id)`, as does `unlink()`.

### Connection: `gui_source/connection.py`

- Remove `time_origin` and `reset_time_origin()`.
- `link_panel` no longer passes an origin.

### Absolute enable timestamps

These are currently stored relative to `store.start_time`, which is being removed. Switch them to absolute `perf_counter` timestamps:

| Panel | Old name | New name | Lines |
|---|---|---|---|
| P906 | `_charge_enable_rel` | `_charge_enable_t` | `device_panel_p906.py:121, 1517, 1530, 1539` |
| L1060 | `_discharge_enable_rel` | `_discharge_enable_t` | `device_panel_l1060.py:193, 978, 1123` |

The comparison becomes `t1 >= self._..._enable_t`.

---

## Phase 3 — Main-window Start/Stop button

All changes in `gui_source/mdp_gui.py`, plus `mainwindow.ui`.

### Capture object and signals

- Add `graph_auto_started` and `graph_auto_stopped` signals.
- In `__init__`, before panels are built:
  - create `self.capture = GraphCapture(on_auto_start=self.graph_auto_started.emit, on_auto_stop=self.graph_auto_stopped.emit)`
  - call `set_graph_triggers()`
- `_add_panel_for_device` passes `self.capture` into each panel.
- Both signals are emitted from the worker thread, so Qt queues them to the GUI thread.
  - `graph_auto_started` → `_refresh_graph_run_button`
  - `graph_auto_stopped` → `_on_graph_stopped`

### New `btnGraphRun`

Placed in `mainwindow.ui` inside `horizontalLayout_27`, immediately left of `btnGraphRecord`, with the same 60px sizing and font.

- **`on_btnGraphRun_clicked`**: if running, calls `capture.stop()` then `_on_graph_stopped()`. Otherwise calls `capture.start(time.perf_counter())` and refreshes the button.
- **`_on_graph_stopped()`**: calls `panel.store.mark_gap()` for every panel, then refreshes the button.
- **`_refresh_graph_run_button()`** shows one of three states:

| State | Text | Colour (`set_color`) | Tooltip |
|---|---|---|---|
| Running | 停止 | `general_green` | The stop condition, when auto-stop is set |
| Armed: stopped, `start_mode` not off, watched device linked | 待触发 | `general_yellow` | The start condition, e.g. "P906: 电压 ≥ 1.000V 时自动开始" |
| Stopped | 开始 | none | none |

The button is enabled when any panel is linked or capture is running.

### Link, unlink and clear

**`on_panel_link_toggled`**
- The draw timer still starts on the first link. With an empty buffer it draws nothing, but it still drives the FPS label and gated-channel visibility.
- Link and unlink both refresh the button.
- When the last panel unlinks while capture is running, call `capture.stop()` and `_on_graph_stopped()`.

**`on_btnGraphClear_clicked`**
- Calls `self.capture.clear(time.perf_counter())` followed by `store.clear()`.

### Trigger settings

**New slot `set_graph_triggers()`**
- Resolves `setting.ui.graph_trigger_device` against `self.panels`, falling back to the first panel if that device no longer exists.
- Calls `capture.set_triggers(...)` and refreshes the button.

**`rebuild_panels()`**
- Also calls `set_graph_triggers()` so a removed device is never left as the watched one.

---

## Phase 4 — Graphics settings: triggers and device picker

### `settings_model.py` → `UiSettings`

| Setting | Default | Notes |
|---|---|---|
| `graph_trigger_device` | `""` | A device `id`; empty means the first device. |
| `graph_autostart` | `"off"` | `"off"`, `"voltage"` or `"current"` |
| `graph_autostart_threshold` | `1.0` | |
| `graph_autostop` | `"off"` | `"off"`, `"voltage"` or `"current"` |
| `graph_autostop_threshold` | `0.0` | |

### `mdp_gui_template/graphics.ui`

Add rows to the 图表设置 section, after 最小显示点数:

1. **触发设备**: `comboGraphTriggerDevice`
2. **自动开始**: `comboGraphAutostart` (关闭 / 电压 / 电流) and `spinGraphAutostartThreshold`
3. **自动停止**: `comboGraphAutostop` (same items) and `spinGraphAutostopThreshold`

Both threshold spinboxes are `QDoubleSpinBox` with 3 decimals and a range of 0–999. Each is disabled while its mode is off, and its suffix is set in code to V or A.

### `dialogs.py` → `MDPGraphics`

- Add a `graph_triggers_sig` signal.
- **`initValues`**
  - Fills the device combo from `setting.devices`, showing each name with its `id` stored as item data.
  - Selects the saved device, falling back to the first.
  - Loads the other four widgets.
- **Change handlers** for all five widgets write to `setting.ui` and emit `graph_triggers_sig`.
- **`on_btnClose_clicked`** saves all five values.

### Wiring

- In `mdp_gui.py`, connect `DialogGraphics.graph_triggers_sig` to `MainWindow.set_graph_triggers`.

### Generated files and translations

1. Regenerate `mainwindow_ui.py` and `graphics_ui.py` with `venv/bin/pyuic5`.
2. Run `venv/bin/pylupdate5 gui_source/mdp.pro`.
3. Fill in the English entries in `en_US.ts`:

| Chinese | English |
|---|---|
| 开始 | Start |
| 待触发 | Armed |
| 触发设备 | Trigger device |
| 自动开始 | Auto-start |
| 自动停止 | Auto-stop |
| 关闭 | Off |
| 电压 | Voltage |
| 电流 | Current |

   Also translate both tooltip strings.
4. Rebuild `en_US.qm` with `/usr/bin/lrelease`.

---

## Phase 5 — `readme_EN.md`

Only `readme_EN.md` is edited.

**Add a "Graph capture" section covering:**
- Linking a device no longer starts the graph.
- Start/Stop continues the same timeline with a break, and CLEAR resets it.
- Trigger device, auto-start (rising crossing) and auto-stop (falling crossing) are configured in Graphic Settings.
- What the Armed button state means.
- REC is separate from graph capture.

**Adjust existing lines:**
- **Line 143:** change "keep graphing" to "keep capturing while the graph is running".
- **Lines 174 and 182:** note that the live charge, discharge and sweep curves plot only while graph capture is running.

---

## Verification (after Phase 4)

1. **Unit tests:** repeat the Phase 1 check.

2. **Manual Start/Stop:** run `venv/bin/python gui_source/mdp_main.py --sim` and link a P906.
   - The graph stays empty while the LCDs update.
   - Start: curves begin at t≈0.
   - Stop, wait, then Start again: the line shows a break and time kept counting.
   - CLEAR resets the timeline.

3. **Auto-start:** in Graphic Settings, set trigger device P906 and Auto-start Voltage 1 V. The button shows Armed.
   - Raise the output from 0 V to 5 V: capture starts on its own.
   - Stop while still at 5 V: the button stays Armed and capture does not restart.
   - Go 0 V → 5 V: capture restarts.

4. **Auto-stop:** set Auto-stop Voltage 0.5 V.
   - While running at 5 V, drop the output to 0 V: capture stops on its own, the line breaks, and the button returns to Armed.
   - Raise to 5 V: capture restarts automatically.

5. **Watched device only:** link a second sim device (L1060) and make it cross both thresholds. Nothing triggers.

6. **Last unlink:** unlink the last device while running. Capture stops and the data stays on screen.

7. **Panel logic with the graph stopped:**
   - P906 CV/CC detection and keep-power still work.
   - The Energy LCD still accumulates.
   - An L1060 discharge started before Start still counts Ah/Wh correctly after a later Start and after CLEAR.

8. **REC:** still writes its CSV whether or not the graph is running.

9. **Translations:** run `mdp_main.py --sim --english` and confirm the new strings appear in English.
