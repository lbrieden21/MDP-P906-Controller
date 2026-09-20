# Device status polling — off the GUI thread

## Context

A ~1h17m P906 Battery Simulator + L1060 Discharge run left the GUI unresponsive at the
end: scrolling the settings panel and typing a Save-CSV filename lagged 10-40s per
keystroke. The specific incident's root cause is inconclusive — the C6 adapter in use
may have been running pre-fix firmware (a separate, already-fixed bug where a WiFi
latency hiccup could stick the nRF24 in TX mode, see `nrf24l01p.c` commit `88db4ee`), or
a concurrent, mistargeted flash of a different board's firmware at the same port may have
disrupted it mid-run. Neither can be confirmed after the fact.

What *is* confirmed, independent of that incident, is an architectural problem that will
produce the same symptom under any real link degradation:

- The live chart's `HOLD` button (`gui_source/graph_view.py:459-460`,
  `graph_keep_flag`) correctly skips the expensive per-tick redraw work and was ruled out
  as the cause of a freeze that survives clicking it.
- `DevicePanelBase` starts three `QTimer`s per linked panel
  (`gui_source/device_panel.py:156-158`), all on the GUI thread:
  - `update_state_timer` (100ms) → `update_state()` → `self.api.get_status()`
    (`device_panel_p906.py:346`, `device_panel_l1060.py:1152`) — a **blocking** Type-7
    request/response round trip (`mdp_controller/mdp_device.py:230-270` pattern via
    `bus.transfer(wait_response=True)`, `mdp_controller/bus.py:150-192`), holding
    `MDPBus._lock` (an `RLock`, `bus.py:72,157`) and blocking on
    `owner._transfer_event.wait(owner.com_timeout)` for up to `com_retry` (default 5,
    `mdp_device.py:61-82`) attempts at `com_timeout` (0.04s wired / 0.08s TCP,
    `bus.py:70`) each — worst case ~0.2-0.4s of GUI-thread stall per call, per linked
    panel, every 100ms.
  - `state_request_sender_timer` (`request_state()`, `device_panel.py:181-183`) and
    L1060's `target_poll_timer` (`request_targets()`, `device_panel_l1060.py:1093`) are
    nominally "fire-and-forget" (`bus.transfer(wait_response=False)`), but the send
    itself still calls `NRF24Adapter.nrf_send()`
    (`mdp_controller/nrf24_adapter.py:384-402`), which blocks the calling thread on
    `self._send_event.wait(timeout)` for the local radio's own TX-ack, with the same
    retry budget — same order-of-magnitude GUI-thread stall, just without waiting for
    the device's reply too.
  - `state_lcd_timer` (`update_state_lcd()`) is already safe: it only reads
    `self.store` under `store.sync_lock` (`device_panel.py:281-306`,
    `device_panel_l1060.py:1204-1219`) — no bus I/O. No change needed there.
- Two devices linked simultaneously (P906 + L1060, as in the failing run) double the
  timers contending for the same shared `MDPBus._lock`.
- The existing async realtime-value path
  (`register_realtime_value_callback`/`state_callback`,
  `mdp_controller/mdp_device.py:201-211`) is delivered on `NRF24Adapter._worker`'s
  background thread (`nrf24_adapter.py:219-220,270-303` → `_parse_data` → `_on_packet`
  → `_handle_packet` → the registered callback, e.g. `mdp_controller/mdp_p906.py:106-117`)
  with **no GUI-thread blocking involved**. It was investigated as a drop-in
  replacement for the status poll and rejected: it only ever carries `(voltage,
  current)` sample pairs (a Type-8 packet). Output state, lock state, set-points, input
  rail readings, temperature, model, and protection/error flags exist **only** in
  `get_status()`'s Type-7 response (`mdp_p906.py:126-176`, `mdp_l1060.py:142-185`) and
  are not derivable from the realtime stream. Dropping the poll entirely would silently
  freeze all of that UI.
- No existing behavior depends on the poll being synchronous. Both `update_state()`
  implementations already swallow failures silently
  (`except (TimeoutError, NRF24AdapterError): return`,
  `device_panel_p906.py:346-348`, `device_panel_l1060.py:1151-1154`), and there is no
  auto-unlink/disconnect-timeout logic anywhere tied to `get_status()` failing — link
  health is reported separately via `bus.speed_counter`
  (`gui_source/mdp_gui.py:355-368`). Moving the poll off the GUI thread costs nothing.
- `get_status()` has exactly two production call sites outside of `connect()`'s
  one-shot blocking call during `link()` — `update_state()` in each panel type — so this
  is a surgical change, not a rearchitecture.

## Decisions made

1. **Extend the existing async request/response pattern to the status packet, rather
   than adding new per-panel background threads or Qt cross-thread signals.** The
   codebase already has a working, proven idiom for this: a background thread
   (`NRF24Adapter._worker`) delivers parsed data into a lock-protected structure
   (`DeviceDataStore`, guarded by `store.sync_lock`), and a GUI-thread timer
   (`state_lcd_timer`) reads it back with no bus I/O. Status gets the same treatment —
   a small lock-protected "latest status" holder per panel, written by an async status
   callback on the worker thread, read by `update_state_timer` on the GUI thread. This
   was chosen over spinning up a dedicated `QThread`/`threading.Thread` per panel
   (more moving parts, no real parallelism gained since everything still serializes on
   `MDPBus._lock` anyway) and over introducing a new `pyqtSignal`-based marshaling path
   (unnecessary — the lock-protected-snapshot idiom already exists and is simpler to
   reason about).
2. **`connect()`'s one-shot `get_status()` call during `link()` stays synchronous and
   untouched.** It's a single, user-initiated blocking action (the user just clicked
   LINK), not a recurring timer, and out of scope for this problem.
3. **Fix the send-side blocking (`request_realtime_value`/`request_targets`/the new
   `request_status`) with a shared outbound queue, not one fix per call site.** All
   three go through the same `NRF24Adapter.nrf_send()` local-ack wait; queuing sends
   through one background-drained queue fixes all of them at once and matches the
   "one shared serialization point" reality of `MDPBus._lock`.
4. **No change to `state_lcd_timer` or the realtime V/I path.** Both are already
   GUI-thread-safe by construction; touching them would be scope creep.

## Phases

### Phase 0 — Async status request/response at the driver layer

- `mdp_controller/mdp_device.py`: add `request_status()` (fire-and-forget Type-7 send,
  mirrors `request_realtime_value()` at lines 179-199) and
  `register_status_callback()` (mirrors `register_realtime_value_callback()` at lines
  201-211).
- `mdp_controller/mdp_p906.py` / `mdp_l1060.py`: in `_handle_packet()`'s Type-7 branch,
  keep building the same status object `get_status()` already returns, but also invoke
  the registered async status callback with it (in addition to, not instead of, the
  synchronous return path `connect()` still uses).
- No GUI changes. Verify against `--sim` and the existing bench scripts
  (`test_l1060.py`, `test_main.py`) by registering a callback and confirming it fires
  with the same fields `get_status()` returns today.

### Phase 1 — Wire the panels to the async path

- `gui_source/device_panel.py`: `link()` registers a status callback per panel. The
  callback body (runs on `NRF24Adapter._worker`, off the GUI thread) does nothing but
  store the parsed status object into a small lock-protected holder
  (`self._latest_status`, own lock or reuse `store.sync_lock`) — no widget writes here.
- `update_state_timer`'s handler in both `device_panel_p906.py` and
  `device_panel_l1060.py` drops the direct `self.api.get_status()` call. It instead
  reads `self._latest_status` under the lock and, if a snapshot is present, runs the
  exact same widget-writing logic `update_state()` has today (model detection,
  output/lock state, set-point spinboxes, temperature, protection UI, etc.) —
  essentially unchanged, just fed from the stored snapshot instead of a live blocking
  call.
- `request_state()` (`device_panel.py:181-183`) also calls the new
  `api.request_status()` alongside its existing `request_realtime_value()` call, so the
  same timer cadence drives both requests.
- `connect()`'s blocking `get_status()` during `link()` is left exactly as-is (Decision 2).

### Phase 2 — Move the outbound send itself off the GUI thread

- `request_realtime_value()`, `request_targets()`, and the new `request_status()` all
  still block the calling thread inside `NRF24Adapter.nrf_send()`
  (`nrf24_adapter.py:384-402`) waiting for the local radio's TX-ack. Add a small
  outbound-send queue inside `NRF24Adapter` (or `MDPBus`), serviced by a dedicated
  background thread: `bus.transfer(wait_response=False, ...)` enqueues and returns
  immediately instead of calling `nrf_send()` inline; the queue-draining thread performs
  the actual send (and thus the local-ack wait) off the GUI thread.
- Confirm `MDPBus._lock` is still taken by the draining thread around each send, so
  ordering/serialization against `wait_response=True` transfers (which still block their
  own caller, e.g. `connect()`) is unchanged — only which thread does the waiting moves.
- This fixes `state_request_sender_timer` and `target_poll_timer` for free, since they
  already go through the same `transfer(wait_response=False)` path.

### Phase 3 — Verification

- Unit-level: extend `test_l1060.py`/`test_main.py` (or add a small bus-level test) to
  assert `request_status()`/`request_realtime_value()`/`request_targets()` return
  immediately (no blocking) and that queued sends still execute in order.
  Also assert the status callback delivers the same field set `get_status()` does today.
- `--sim`: confirm P906 and L1060 panels still show live output/lock/protection/
  temperature state correctly with the async path, both singly and linked together.
- Real hardware: repeat a long-duration P906 Battery Sim + L1060 Discharge run (an hour
  or more) with both panels linked, deliberately degrading the link partway through
  (e.g. added WiFi load per the existing coexistence bench notes, or briefly increasing
  distance/attenuation) to force retries, and confirm typing, scrolling, and Save CSV
  all stay responsive throughout — including while `labelErrRate`/CON-ERR shows nonzero
  errors.
- Confirm `bus.speed_counter`/CON-ERR reporting is unaffected — this plan only changes
  what blocks on the way to updating status widgets, not the error-rate accounting
  itself.
