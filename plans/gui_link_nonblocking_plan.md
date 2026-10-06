# Device link — off the GUI thread

## Context

Clicking a panel's LINK/UNLINK button runs the whole connect sequence on the Qt GUI
thread. If the device is off or out of range, the window freezes for the full 8 s connect
budget. Every linked panel stops updating while that happens.

### Confirmed blocking call path

| Step | Where | What blocks |
|---|---|---|
| Qt slot | `gui_source/mdp_gui.py:390-419` `on_panel_link_toggled()` | calls `link_panel()` inline at :409 and shows the failure box at :416 |
| Bus open (first link only) | `gui_source/connection.py:43-45` → `_build_bus()` → `MDPBus.__init__` | `NRF24Adapter.wait_connected()` at `mdp_controller/bus.py:92`: up to 2.0 s on serial, 4.0 s on TCP. On TCP, `TcpAdapterPort.__init__` also does a synchronous socket connect with a 2.0 s timeout first (`mdp_controller/tcp_port.py:24-45`). |
| Attach | `gui_source/device_panel.py:167` → `bus.attach()` (`bus.py:133-139`) | one `nrf_open_pipe` action, up to 2 s timeout |
| Connect | `gui_source/device_panel.py:170` → `MDPDevice.connect(timeout=8.0)` (`mdp_controller/mdp_device.py:268-308`) | time-based loop to an 8.0 s deadline (:283), `time.sleep(0.1)` between attempts (:296, :305). Each attempt is one or two waited `bus.transfer()` calls. |
| Post-connect | `mdp_device.py:306` → L1060 `_post_connect()` (`mdp_controller/mdp_l1060.py:380-386`) | up to 3 more waited Type 10 gets |
| Failure | `connection.py:51-56` | closes the bus if this call opened it, then re-raises to the slot |

`MDP_L1060.connect()` (`mdp_l1060.py:355-368`) only calls `super().connect(timeout)`, so
both device types run the same loop. The 8 s time-based budget came in with commit `a44a270`.
`plans/device_status_async_polling_plan.md` (Decision 2) deliberately left `connect()`
synchronous and out of scope.

Worst-case cost of one waited `bus.transfer()`, worked out from the code constants rather than
measured: `com_retry` 5 means 6 attempts. Each attempt waits up to `com_timeout` for the local
TX-ack and `com_timeout` again for the reply. That is 6 × (0.04 + 0.04) = **0.48 s wired** and
**0.96 s on TCP**, all of it under `MDPBus._lock` (`bus.py:174-196`).

### Other GUI-thread bus I/O

| Action | Where | Blocking bound (from constants) | This plan |
|---|---|---|---|
| Settings → Match | `gui_source/dialogs.py:180-194` → `connection.py:70-83` → `MDPBus.auto_match()` (`bus.py:241-308`) | bus open (2 s serial, 6 s TCP) plus up to 3 tries × (≤0.08 s + `time.sleep(1)` at :276), so roughly 3.2 s, plus the dispatch | Phase 3, which can be split off |
| Unlink | `mdp_gui.py:394` → `connection.py:58-68` → panel `unlink()` | L1060: `_confirmed_load_off()` → `set_load_on(False)` (`mdp_l1060.py:298-353`): up to 3 × 6 waited Type 10 gets plus 0.075 s settles. P906: output-off confirmation through `_apply_output(False)` (`device_panel_p906.py:327`, `:1513`) when a charge or aux run is active. Then `bus.close()` joins two threads. | stays synchronous (see Decisions) |
| P906 output toggle | `device_panel_p906.py:313-333` → `set_output()` (`mdp_p906.py:198-236`) | 3 × 6 waited gets plus settles | unchanged |
| L1060 load on/off, mode | `device_panel_l1060.py:301-389` | same pattern as above | unchanged |
| Wheel color push | `mdp_gui.py:291` → `refresh_led_color()` (`device_panel.py:204-208`) | one waited transfer, ≤0.48 s | unchanged |

`set_voltage` and `set_current` on both devices are fire-and-forget, so they never block the GUI
thread.

### Threading model (what is safe off the GUI thread)

- **`MDPBus`** (`bus.py`) is Qt-free. Every adapter call that can run concurrently takes
  `self._lock` (an RLock): `attach()` :134, `transfer()` :174 (held across all retries),
  `_send_fire_and_forget()` :210 on the `_send_worker` thread, and `auto_match()` :263.
  `__init__` uses the adapter before any other thread can see the bus. `detach()` also takes
  the lock. `_on_recv()` runs on the adapter worker thread and only does a dict `.get()`
  plus a per-owner `Event.set()`.
- **`NRF24Adapter`** (`nrf24_adapter.py`) has one worker thread (`_worker`, :270) that reads the
  port, sends ECHO pings and dispatches responses. `_action_event`, `_send_event` and
  `_query_event` are each one shared `Event` with no lock of their own (:249-268, :384-402),
  so they are only correct while callers are serialized. `MDPBus._lock` provides that
  serialization, as listed above.
- **`serial_reader.SerialReaderBuffered`** is only called from `_worker`. **`TcpAdapterPort`**
  has its own socket thread and a lock around its buffer.
- **Conclusion:** `_build_bus()`'s `MDPBus(...)` constructor, `bus.attach()`,
  `api.connect()` and `api.close()` can run on a non-GUI thread as-is. No change is needed in
  `bus.py`, `nrf24_adapter.py`, `serial_reader.py` or `tcp_port.py`. Everything after
  `connect()` returns has to stay on the GUI thread (`device_panel.py:174-191`): callback
  registration, store and capture resets, the three `QTimer.start()` calls,
  `_start_device_timers()`, `update_state()` and `open_state_ui()`. So do the failure message
  box and every `ConnectionManager` state change (`self.bus`, the job tables).
- **Codebase idiom:** `gui_source` has no `QThread` anywhere. Background work is plain
  `threading.Thread(daemon=True)` (bus, adapter, TCP port). Data flows back through a
  lock-protected snapshot polled by a `QTimer` (`device_panel.py:81-85`, `:215-219`). That idiom
  suits recurring data. A link result is a one-shot event that must run widget code exactly
  once, so this plan uses a queued `pyqtSignal` from the worker thread to a QObject that lives on
  the GUI thread instead of a polling timer.

### Re-entrancy hazard

`CustomMessageBox` calls `exec_()` from its constructor (`gui_source/mdp_custom.py:155-157`),
which runs a nested event loop. Any queued link-result signal that arrives while a failure box
is open is processed *inside* that box's loop. Every `ConnectionManager` state change for a
result must therefore be complete before any signal that can open a box is emitted.

### Simulator

Sim mode is chosen at import by `mdp_controller/__init__.py:1-11`: the env var `MDP_SIM_MODE`
(any value) or `--sim` in `sys.argv`. The sim `connect()` returns immediately
(`__sim_mdp_p906.py:252-262`, `__sim_mdp_l1060.py:212-213`), so a slow or failed connect
**cannot** be reproduced today. A small knob is added for that (Phase 1).

Found in passing: the sim `MDPBus.auto_match(self, try_times=3)` (`__sim_bus.py:84`) does not
accept the `pipe=` keyword that `ConnectionManager.match()` passes (`connection.py:80`). The
Settings → Match button therefore raises `TypeError` in sim mode today. This is fixed in
Phase 3, because that phase needs Match working in sim to verify it.

## Decisions made

1. **The 8 s budget is unchanged.** Only the thread it runs on moves.
2. **The worker is a `threading.Thread(daemon=True)` per job, with results sent back through a
   queued signal on `ConnectionManager`.** This matches the rest of the codebase's threads, and
   `QThread` raises "destroyed while running" problems at app close. The signal is connected to
   a bound method with an explicit `Qt.QueuedConnection`, not to a lambda.
3. **All `ConnectionManager` state is changed on the GUI thread only.** Worker threads only
   (a) build an `MDPBus` and return it, or (b) attach, connect and return an api. They never
   assign `self.bus` and never touch the job tables. This replaces `opened_bus` with one rule:
   *close the bus when no panel is linked, no link job is running or waiting, and no bus build
   is in flight* (`_close_bus_if_idle()`). That rule covers the old failed-first-link case and
   every concurrent case.
4. **Concurrent links run in parallel on the shared bus.** Probes interleave per waited
   transfer under `MDPBus._lock`, the same way per-device polling already does. Two devices
   powered on together then both link inside one boot window instead of one after the other.
   The bus is built once. Panels clicked while it is building wait in a queue and start when it
   is ready.
5. **`connect()` gets a cancel `threading.Event`.** Without it, cancelling or closing the app
   would have to wait out the remaining budget.
6. **Unlink stays synchronous.** Its waits are bounded confirm-off writes that need the live
   panel state. Making unlink asynchronous would need its own "disconnecting" state, which is a
   separate problem. It only changes to close the bus through `_close_bus_if_idle()`.
7. **Settings and fps changes made during a link are applied when it finishes.** Device
   parameters are snapshotted on the GUI thread when the link starts. `finish_link()` reads
   `panel.data_fps`, which `set_data_fps()` already updates even while the panel's timers are
   stopped (`device_panel.py:380-381`).

## Phases

### Phase 1 — Cancellable connect and sim knobs (driver layer)

- `mdp_controller/mdp_device.py`
  - Add `class ConnectCancelled(Exception)`.
  - `connect(self, timeout: float = 8.0, cancel: Optional[threading.Event] = None)`: check
    `cancel` at the top of each attempt, and replace both `time.sleep(0.1)` calls with
    `cancel.wait(0.1)` when `cancel` is given (raise `ConnectCancelled` if it returns `True`).
    The deadline logic is unchanged. Cancel latency is bounded by one attempt: up to 2 waited
    transfers (≤0.96 s wired worst case from the constants).
- `mdp_controller/mdp_l1060.py`: delete the pass-through `connect()` override (:355-368) so
  there is one signature to maintain. Move its anchor explanation into `_connect_ready()`'s
  docstring.
- `mdp_controller/__init__.py`: export `ConnectCancelled` from `mdp_controller.mdp_device` in
  both branches (it is not simulated).
- `mdp_controller/__sim_mdp_p906.py`, `__sim_mdp_l1060.py`: give `connect()` the same signature.
  Add two env knobs read at connect time:
  - `MDP_SIM_CONNECT_DELAY=<seconds>`: wait this long, honoring `cancel` (raise
    `ConnectCancelled`).
  - `MDP_SIM_CONNECT_FAIL=<IDCODE>[,<IDCODE>...]`: after the delay, raise
    `Exception("Failed to connect to MDP-…")` for the listed idcodes.
- `test_main.py` and `test_l1060.py` call `connect()` with no arguments and need no change.

### Phase 2 — Asynchronous link in the GUI

**`gui_source/device_panel.py`.** Split `link()` and remove it. Callers are listed below.

- `link_params() -> dict` (GUI thread): the empty-IDCODE `ValueError` check that `link()` does
  today (:151-156), plus a snapshot of `idcode`, `blink`, `led_color` and `m01_channel` taken
  from `self.settings`.
- `connect_api(bus, pipe, params, cancel)` (**worker thread, no widget access**): build
  `self.api_class(bus, **params, debug=DEBUG)`, then `bus.attach()` and
  `api.connect(timeout=8.0, cancel=cancel)`. Call `api.close()` on any exception and re-raise.
  Return the api. The moved comment keeps what the code does ("outlasts the devices'
  post-power-on deaf window"). The 3-4.5 s measurement goes in the README, not the comment.
- `finish_link(api, params)` (GUI thread): `device_panel.py:174-191` unchanged, except that it
  uses `self.data_fps` instead of an `fps` argument. If `self.settings.color` changed since the
  snapshot, it also calls `refresh_led_color()`.
- `set_linking(cancelling: bool)`: set `labelLinkState` to the new translatable string
  `连接中...` ("Connecting...") in `general_yellow`. `btnLink` stays enabled while connecting,
  where a click means cancel, and is disabled once `cancelling=True`. Output and state frames
  stay disabled. No `.ui` change is needed, because both widgets are already driven from code.
- `abort_link()`: re-enable `btnLink` and call `close_state_ui()` (the plain variant, with no
  `record_disconnect` sample).

**`gui_source/connection.py` `ConnectionManager`** (GUI thread unless noted)

- Signals: a private `_bus_built = pyqtSignal(object, object)` (bus or None, error message),
  a private `_link_done = pyqtSignal(object)` (job), and a public
  `link_finished = pyqtSignal(object, bool, str)` (panel, ok, error; an empty error means
  cancelled, so no box is shown). The private signals are connected with
  `QtCore.Qt.QueuedConnection` to bound methods.
- `_LinkJob`: `panel`, `pipe`, `params`, `cancel: threading.Event`, `thread`, `api`, `error`.
- State: `bus`, `_bus_thread` (or None), `_waiting: List[_LinkJob]`,
  `_running: Dict[panel, _LinkJob]`.
- `begin_link(panel, fps)` replaces `link_panel()`. It calls `params = panel.link_params()`,
  which raises synchronously on an empty IDCODE so the caller shows the box at once as it does
  today. It then sets `panel.data_fps = fps` and `panel.set_linking(False)`. If the bus is open,
  it starts the job thread. If a build is in flight, it appends the job to `_waiting`.
  Otherwise it appends the job and starts the bus thread with **kwargs snapshotted now** from
  `setting.adapter` (`_build_bus()` becomes `_bus_kwargs()` plus a thread target).
- `is_linking(panel)`: true if the panel is in `_waiting` or `_running`.
- `cancel_link(panel)`: a waiting job is removed at once, and the method calls
  `panel.abort_link()` then `_close_bus_if_idle()`. A running job gets `job.cancel.set()` and
  `panel.set_linking(True)`, and its `_link_done` finishes it.
- `_on_bus_built(bus, err)`: clear `_bus_thread`. On error, take every waiting job and call
  `panel.abort_link()` on each, then emit `link_finished(panel, False, err)` for each.
  Otherwise set `self.bus = bus`, start a thread for each waiting job, then
  `_close_bus_if_idle()` (for when every waiter was cancelled during the build).
- Job thread (worker): `job.api = panel.connect_api(...)`. `ConnectCancelled` is recorded as a
  cancel. Any other exception gets `logger.exception` and is recorded as `job.error = str(e)`.
  The thread always ends with `_link_done.emit(job)`.
- `_on_link_done(job)`, in this order:
  1. Remove the job from `_running`.
  2. If `job.cancel.is_set()`: close `job.api` if set, then `panel.abort_link()`.
     Else if `job.error`: `panel.abort_link()`. Else: `panel.finish_link(job.api, job.params)`.
  3. `_close_bus_if_idle()`.
  4. Emit `link_finished` last. Steps 1-3 never open a box, so the re-entrancy hazard cannot
     leave state half-updated.
- `unlink_panel()`: as today, except that its tail becomes `_close_bus_if_idle()`. If
  another panel is still connecting, the bus stays open.
- `busy` property: the bus is open, a build is in flight, or any job is waiting or running.
  `is_open` keeps meaning "`self.bus` is not None", which is what `update_link_state` and
  `bench_gui_run.stop()` need.
- `shutdown()`: set every job's `cancel` and clear `_waiting`. Join the running job threads and
  the bus thread with one shared bounded timeout (worst cases: one connect attempt, or a bus
  build of up to 2 s serial / 6 s TCP). Threads still alive after the timeout are daemon threads
  and are left alone. After the joins, close any bus a just-finished build left behind with
  nothing linked. Linked panels and their bus are left exactly as they are today on app close.

**`gui_source/mdp_gui.py`**

- `on_panel_link_toggled(panel)` has three branches: linked → unlink path (unchanged);
  `connection.is_linking(panel)` → `connection.cancel_link(panel)`; otherwise
  `connection.begin_link(panel, self.data_fps)`, with the `ValueError` box kept for an empty
  IDCODE.
- New `_on_link_finished(panel, ok, err)`. On ok: start `draw_graph_timer` if it is not active
  (this replaces the `first_link` bookkeeping at :408-413). On failure with a non-empty `err`:
  show the `连接失败` box. In every case: refresh the graph views and the device selector
  style, and call `self.close_state_ui()` if `not self.connection.is_open`, so the
  CON-ERR/kBps labels don't keep showing a closed bus.
- `closeEvent`: call `self.connection.shutdown()` before `close_signal.emit()`.
- `rebuild_panels()` gate (:133): `is_open` → `busy`.

**`gui_source/dialogs.py`**: the Add, Remove and Match gates (:78, :98, :181) change from
`is_open` to `busy`. Removing or renaming panels, or pairing, while a link is in flight is
refused just as it is while linked.

**`gui_source/bench_gui_run.py`** (:135-142 depends on the synchronous call): `start()` calls
`conn.begin_link()` for each panel and connects `conn.link_finished` to a handler that records
linked or failed, prints the same lines as today, and runs the rest of today's `start()` (from
"no panel linked" onward) once every panel has reported. `stop()` is unchanged.

**`gui_source/en_US.ts`**: add `连接中...` → `Connecting...` under `DevicePanelBase`, using
`pylupdate5` and then `lrelease` for `en_US.qm`.

**`readme_EN.md`** (the multi-device paragraph at :156): connecting runs in the background,
the panel shows *Connecting...*, clicking LINK/UNLINK again cancels, and a connect waits up to
8 s, which covers the roughly 3-4.5 s after power-on when a device does not answer the radio.

### Phase 3 — Asynchronous pairing (optional, can be split off)

Same job pattern, with one throwaway bus per match job (the bus is built and closed inside the
worker, so `self.bus` is never touched).

- `ConnectionManager.begin_match(pipe)` and `match_finished = pyqtSignal(str, str)` (idcode,
  error). Refused while `busy`, and counts toward `busy` while running.
- `dialogs.py:180-194`: the Match button is disabled and shows a new string `配对中...`
  ("Pairing...") until `match_finished` arrives. Success and failure boxes are shown from that
  handler.
- `__sim_bus.py:84`: `auto_match(self, try_times=3, pipe=None)` returns the given pipe.

`auto_match()` itself is not cancellable (bounded at about 3.2 s plus the bus open).
`shutdown()` joins it with the same bounded timeout.

## Edge cases

| Case | Handling |
|---|---|
| First link fails | The job ends with an error, then `abort_link()`, then `_close_bus_if_idle()` closes the bus (no panel linked, nothing pending), and then the box is shown. This matches `link_panel`'s `opened_bus` behavior. |
| Second panel fails while the first stays linked | `_close_bus_if_idle()` sees a linked panel and keeps the bus open, as today. |
| Bus build fails (port missing, adapter silent) | Every waiting panel gets `abort_link()` and one `link_finished(..., False, err)`, so two queued panels show two boxes, one after the other. |
| Two panels clicked together, bus closed | The first click starts the build and the second queues behind it. Both connect in parallel once the bus is ready. Pipes are distinct (`panels.index + 1`). |
| Panel B clicked while A is mid-connect on an open bus | B's job starts at once. Probes interleave under `MDPBus._lock`. |
| LINK/UNLINK clicked on a connecting panel | Cancel. A waiting job is dropped at once. A running job's label stays *Connecting...* with the button disabled until the worker exits (one attempt at most). The panel then returns to *Disconnected* with no box. |
| Cancel clicked after `connect()` succeeded but before the result is handled | `_on_link_done` sees `cancel` set, closes the api and aborts. The panel is never half-linked. |
| Unlink another, linked panel during a connect | That panel unlinks synchronously. The bus stays open because a job is running. |
| Settings Add/Remove/Match during a connect | Refused through `busy` (the existing "请先断开连接" box). |
| Settings Apply (adapter or IDCODE fields) during a connect | Takes effect on the next link, because bus kwargs and device params were snapshotted at start. This matches the existing "重新连接生效" wording. |
| Wheel color changed during a connect | `refresh_led_color()` is a no-op while `api is None`. `finish_link()` pushes the new color if it differs from the snapshot. |
| Data fps changed during a connect | `set_data_fps()` already updates `panel.data_fps`, and `finish_link()` starts the timers from it. |
| `rebuild_panels` during a connect | Refused through `busy`, so no panel can be deleted while its job holds a reference to it. |
| App closed during a connect or bus build | `shutdown()` cancels, then joins with a bounded wait. Any daemon thread left over cannot keep the process alive. |
| Failure box open when another result arrives | Handled inside the nested loop. State was already final before the box opened (`_on_link_done` ordering). |
| A linked panel's GUI-thread waited write (output toggle, load on/off, color) during another panel's connect | It can wait for `MDPBus._lock` for up to one connect transfer (≤0.48 s wired). This stall is new: today the whole GUI is frozen instead. |
| A linked panel's fire-and-forget polls during another panel's connect | `_send_worker` shares `_lock` with the probe transfers. The hardware check below watches for a drop in the linked panel's rate. |

## Verification

### Without hardware

1. **Unit test**, new file `gui_source/test_connection.py` (`unittest`, matching the other
   `gui_source/test_*.py`). It sets `QT_QPA_PLATFORM=offscreen` and `MDP_SIM_MODE=1` before
   any import, builds real P906 and L1060 panels from `DEVICE_PANEL_TYPES`, and drives
   `ConnectionManager` directly. It does not drive `MDPMainwindow`, because the
   `CustomMessageBox` `exec_()` call would block a headless test. The test spins the event loop
   until `link_finished` arrives. Cases:
   - With `DELAY=1.0`, a 20 ms `QTimer` keeps ticking during the connect (at least about 40
     ticks), which shows the GUI thread was not blocked.
   - A fail-listed idcode gives `ok=False` and a non-empty error, the panel ends not linked,
     and `conn.bus is None` and `not conn.busy` (the old `opened_bus` behavior).
   - A cancel 0.2 s into a 2 s delay gives `ok=False` with an empty error, finished within
     about 0.3 s, and the bus is closed.
   - Two panels started together with `DELAY=1.0` are both linked after about 1 s, not 2 s.
   - With A linked and B failing, A is still linked, the bus stays open, and A's
     `update_state_timer` stays active.
   - `shutdown()` during a 5 s delay returns in under 1 s.
   - Run it with
     `cd gui_source && ../venv/bin/python -m unittest test_connection -v`.
2. **Manual sim run** (both knobs are read at connect time):
   `MDP_SIM_MODE=1 MDP_SIM_CONNECT_DELAY=8 MDP_SIM_CONNECT_FAIL=<one panel's IDCODE> venv/bin/python gui_source/mdp_main.py`.
   Link the good panel and drag the window while it shows *Connecting...*. Then link the
   fail-listed panel and confirm the first panel's LCDs and graph keep moving for the full
   8 s before the box appears. Also try a cancel click, and closing the window mid-connect.
3. With Phase 3, Settings → Match in sim returns a fake IDCODE (it raises `TypeError` today).

### Short real-hardware check

Same bench setup as usual (P906 and L1060 on the shared adapter, idle adapters unplugged).
Run with `--debug` so `gui_source/mdp.log` has TRACE lines.

1. **Failed link next to a live one.** Link the P906 with output on and a visible reading,
   with the L1060 powered **off**. Click the L1060's LINK. Expected: the label shows
   *Connecting...*, the P906 LCDs and graph keep updating, the window can be moved, and the
   failure box appears after about 8 s. The L1060 returns to *Disconnected* and the P906 is
   still linked. Afterwards, count `NRF received` lines for the P906 pipe in the log during the
   8 s window against the 8 s before it. They should be in the same range. This is the
   `_send_worker` contention check, and it is the real req/s rather than the GUI samples/s.
2. **Failed first link releases the port.** With nothing linked and the L1060 off, click LINK
   and wait for the failure. The bus should close: the CON-ERR and kBps labels go back to
   `[N/A]`, and Settings → Match (with a device in pairing mode, or just watching that it does
   not report a port-in-use error) can open the adapter again.
3. **Boot race.** With the L1060 off, click LINK, then power the L1060 on within about 2 s.
   Expected: it links within the 8 s budget, and the P906 keeps updating throughout.
4. **Cancel.** With the L1060 off, click LINK, then click it again after about 2 s. The panel
   returns to *Disconnected* within about 1 s, with no box. If the P906 is unlinked as well,
   the adapter closes.
5. **Close mid-connect.** With the L1060 off, click LINK, then close the window about 2 s
   later. The app should exit within about a second, with no traceback in the terminal.
6. **Bench script cutover.** Run `venv/bin/python gui_source/bench_gui_run.py 30` with both
   devices on. Both should link and report sample counts as before. Don't judge a single run's
   no-ack rate.

## Files touched

| File | Phase |
|---|---|
| `mdp_controller/mdp_device.py` | 1 |
| `mdp_controller/mdp_l1060.py` | 1 |
| `mdp_controller/__init__.py` | 1 |
| `mdp_controller/__sim_mdp_p906.py` | 1 |
| `mdp_controller/__sim_mdp_l1060.py` | 1 |
| `gui_source/device_panel.py` | 2 |
| `gui_source/connection.py` | 2, 3 |
| `gui_source/mdp_gui.py` | 2 |
| `gui_source/dialogs.py` | 2, 3 |
| `gui_source/bench_gui_run.py` | 2 |
| `gui_source/en_US.ts`, `gui_source/en_US.qm` | 2, 3 |
| `gui_source/test_connection.py` (new) | 2 |
| `readme_EN.md` | 2 |
| `mdp_controller/__sim_bus.py` | 3 |

Not touched: `bus.py`, `nrf24_adapter.py`, `serial_reader.py`, `tcp_port.py`, the `.ui` files
and the generated `*_ui.py`, `test_main.py`, `test_l1060.py`, and `readme.md`.
