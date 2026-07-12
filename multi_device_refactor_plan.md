# Multi-Device Refactor — MDP-P906-Controller GUI

## Context

The L1060 electronic-load protocol is solved (in mdp_commander) and the current GUI is verified working against the P906. Before adding L1060 support, the single-device GUI must be restructured to host multiple devices. This refactor implements **nothing L1060-specific** — it ends with the app at exact feature parity with today's single-P906 behavior, but restructured so a second device type is a new driver + panel class + registry entry.

**Decisions made with the user:**
1. **Layout:** side-by-side device panels (left) + one shared chart area (right) whose two plots can chart any connected device's channels (device-qualified data-source combos).
2. **Connection model:** shared NRF24 adapter owned by the GUI/connection layer; device drivers attach to it. Bench-validated today: P906 + L1060 share one adapter, same RF address/channel, interleaved transactions. **Adapter firmware is being rewritten** (see "Adapter firmware" below) so each attached device gets its own nRF24 RX pipe — the bus then routes incoming packets by hardware pipe number instead of guessing from packet content/timing. Connect flow becomes: open adapter → link each configured device → adapter opens that device's pipe.
3. **Aux features** (Presets, Sweep, Wave Gen, Keep Power, Battery Sim, Sequence) stay P906-only, moved into the P906 panel functionally untouched.

**Standing constraints:** no compat shims/deprecated aliases (full cutover); no wholesale porting from mdp_commander; `--sim` mode must keep working; user runs all git commits themselves.

## Current architecture (verified)

- `gui_source/mdp_gui.py` (3045 lines) is a monolith: `RealtimeData` (112), `RecordData` (130), `Setting` json settings (222), `MDPMainwindow` god-class (364–2242) with single `self.api: Optional[MDP_P906]` (383), `MDPSettings` (2248), `MDPGraphics` (2431), `TransparentFloatingWindow` (2617), `ResultGraphWindow` (2775), module-global `MainWindow` used by dialogs (e.g. 2322).
- **Bug-in-waiting:** `data`, `_v_set`, `_i_set`, `_output_state`, etc. are *class* attributes (369–380) — must become instance attributes.
- `mdp_controller/mdp_p906.py`: `MDP_P906` constructs its own `NRF24Adapter` (61); `_callback` (117) dispatches on packet type byte `data[0]`; `_transfer` (195) = wait-for-header + event + retry. `auto_match` (438) temporarily rewrites adapter RF settings (broadcast addr, freq 2478) — an adapter-level operation.
- `mdp_controller/nrf24_adapter.py`: single `_recv_callback` slot (343), own reader thread.
- **Protocol fact that shaped the transport redesign:** response types 4/6/7/8/10 carry no device identity in the payload — only types 5 and 9 contain idcode (confirmed against both `L1060_PROTOCOL.md` and `P906_PROTOCOL.md`; Type 10 is L1060-only traffic and easy to miss since the P906 never sends it). A content/timing-based demux ("route a response to whichever device last sent a request of that type") was evaluated and rejected: this driver's performance edge over mdp_commander comes from fire-and-forget sends (`_transfer(..., wait_response=False)`, used by `request_realtime_value()` to drive the live chart), which return control to Qt after the send-ack but *before* the actual response is parsed — so a second device's timer can fire and send its own request before the first device's response has been routed, misattributing idcode-less Type 7/8/10 data between devices. Fixing this in software would mean giving up the non-blocking realtime-poll path. Instead: **rewrite the adapter firmware** to expose real nRF24 multiceiver (per-device RX pipes + pipe-tagged receive frames) — the same mechanism the MDP-M01 itself uses (its dispatch scheme, `base_addr + (0xe1+k)`, is literally the standard multiceiver pipe-address pattern, and its 5-device cap is pipes 1–5). Device identity then comes from the hardware pipe number, not packet content, and stays safe even with multiple outstanding non-blocking requests in flight. See "Adapter firmware" below.
- Chart pipeline keys on **localized combo text** (`tr("电压")` etc.) in `get_data` (1068), `draw_graph` (1138), `func_sweep` (1558–1571) — must become stable channel keys.
- UI workflow: Qt Designer `.ui` → manual `pyuic5` → committed `mdp_gui_template/*_ui.py`. i18n: `mdp.pro` → `pylupdate5` → `en_US.ts` → `lrelease` → `en_US.qm`; translation contexts are class names, so moved strings need a one-time `.ts` context port.
- Sim: `mdp_controller/__init__.py` swaps `MDP_P906` for `__sim_mdp_p906.MDP_P906` on `--sim`/`MDP_SIM_MODE`.

## Adapter firmware — multiceiver rewrite (prerequisite for the bus)

The current dongle (STM32F030F4P6 + CP210x USB-UART bridge, per `readme_EN.md`) runs closed-source firmware built by the P906-Controller author against `mokhwasomssi/stm32_hal_nrf24l01p`. That library exposes no RX-address configuration at all (single hard-coded default address, no `RX_ADDR_Pn` writes anywhere in its source), so it can't be the base for multiceiver and it doesn't remove the HAL/CubeMX dependency either — the current author necessarily added address-configuration code beyond it already. Given no interest in the CubeIDE/CubeMX toolchain, the replacement is a **bare-metal rewrite** (direct CMSIS register access, no HAL) rather than an extension of that library.

- **Pin mapping already recovered** from the shipped firmware (gpio.c/spi.c/main.h): PA0 status LED, PA2 NRF IRQ (EXTI, falling edge), PA3 CSN (software-toggled GPIO), PA4 CE, PA5/PA6/PA7 SPI1 SCK/MISO/MOSI. Standard 5-wire nRF24 interface, all GPIOA — no reverse-engineering left to do.
- **Host protocol is fully known** from `nrf24_adapter.py` (§`CMD`/`RESPONSE` enums) — the replacement firmware reimplements the same `0xAA 0x55 <cmd> <len> <data>` framing and existing commands (`REBOOT`, `RESET`, `SET_BAUDRATE`, `NRF_TX`, `NRF_SET`, `NRF_SAVE`, `NRF_QUERY`, `ECHO`) directly against nRF24L01+ registers, no reverse-engineering needed there either.
- **New protocol additions:**
  - A pipe-open command (e.g. `NRF_OPEN_PIPE(pipe_num 0-5, address[5])`) writing `RX_ADDR_Pn`/`EN_RXADDR`/`RX_PW_Pn` directly.
  - `NRF_RECV_OK` frames gain a leading pipe-number byte, read from the nRF24 `STATUS` register's `RX_P_NO` field in the existing IRQ-driven receive path (preserve the current interrupt-on-PA2 design rather than moving to polling — that's part of why this dongle already outperforms mdp_commander's Arduino adapter).
- **Flashing procedure already documented** in `readme_EN.md` (BOOT0/3V3 short + STM32CubeProgrammer) — reusable as-is.
- **Firmware source lives in this repo**, new top-level directory `nrf_adapter_source_multiceiver/`.
- Until this firmware exists and is validated on the bench (send/recv parity with the current closed firmware, then pipe-tagged multi-device receive), `bus.py`'s pipe-based routing has no hardware to run against — treat this as a blocking prerequisite for Phase 1, not parallel work that can slip.

## Target architecture

### Transport: `mdp_controller/bus.py` — new `MDPBus` (Qt-free)

- Constructs/owns the `NRF24Adapter` from `(port, baudrate, address, freq, tx_output_power)`, pushes `NRF24AdapterSetting`, registers itself as the single recv callback.
- `attach(device, pipe)/detach(device)`; devices expose `_on_packet(bytes)`. `attach()` sends the new pipe-open command for that device's idcode-derived address.
- `transfer(owner, packet, wait_response=True)`: hoisted from `MDP_P906._transfer` (195–219), still serialized with a `threading.Lock` around the send (one physical UART/radio, so sends are naturally one-at-a-time regardless of routing scheme) — but the lock is no longer what makes response attribution safe. The `wait_response=False` fire-and-forget path (e.g. `request_realtime_value`) keeps its existing non-blocking behavior unchanged; it's no longer a correctness risk because response identity comes from the pipe byte, not from send ordering.
- `_on_recv(pipe, data)` (new adapter callback signature, carrying the firmware's pipe byte) routes to `_pipe_owners[pipe]._on_packet(data)`; logs frames on unclaimed pipes. Types 5/9 may still additionally cross-check idcode as a sanity assertion, but it's no longer load-bearing for routing.
- `auto_match(dispatch_freq)` moves here from `MDP_P906.auto_match` (438) — holds the bus lock for its full duration, restores RF settings after, and allocates + opens the next free pipe for the newly discovered device as part of dispatch.
- `speed_counter` forwarded from adapter.

### Driver: `MDP_P906` becomes pure driver

- New ctor: `MDP_P906(bus, idcode, m01_channel=0, led_color=…, com_timeout=0.04, com_retry=5, blink=True, debug=False)`. Deletes adapter construction (61) and RF setting push (99–110); `_callback` → `_on_packet`; `_transfer` → `bus.transfer(self, …)`; drops `auto_match`; `close()` → `bus.detach(self)`.
- `connect()` (413) keeps name/logic — it is the per-device "link" step.

### Sim seam

- `__init__.py` exports **both** `MDP_P906` and `MDPBus`, real or sim per `--sim`/`MDP_SIM_MODE`.
- `__sim_mdp_p906.py` gains a sim `MDPBus` stub (same ctor signature; `auto_match` returns fake idcode; attach/detach/transfer no-ops) and its `MDP_P906` gets the new `(bus, idcode, …)` signature, behavior unchanged.

### GUI device abstraction (lives in `gui_source/` — UI-coupled, tr()'d labels)

- **`gui_source/device_core.py`**: `ChannelSpec(key, label, unit, hide_above=None)` with stable keys `"voltage"/"current"/"power"/"resistance"`; `DeviceDataStore` = generalization of `RealtimeData` with `times` + `series: dict[key, np.ndarray]`, sync_lock, energy/avg accumulators, `append()` (ring-roll from `state_callback` 915–931), `clear()`, `get_series(key, display_pts, …)` (from `get_data` 1068). `RecordData` moves here too.
- **`gui_source/device_panel.py`**: `DevicePanelBase(QtWidgets.QWidget)` — owns `device_id`, `display_name`, `channels`, `store`, `linked`; signals (`values_signal`, `display_data_signal`, `highlight_point_signal`, `link_state_changed`); `link(bus)` / `unlink()` / `set_data_fps()` / `apply_theme()`. Registry `DEVICE_PANEL_TYPES = {"P906": P906DevicePanel}` — the future L1060 plug point.
- **`gui_source/device_panel_p906.py`**: `P906DevicePanel` — receives ~1500 lines of device logic verbatim from `MDPMainwindow` (v_set/i_set/output_state properties 597–650, update_state 652, open/close_state_ui 755–804, request_state 862, state_callback 866, update_state_lcd 934, presets 1395–1447, sweep 1449–1599, wave gen 1601–1690, keep power 1692–1759, battery sim 1761–1900, sequence 1902–2242, per-device timers from initTimer 480–503). Widget object names preserved so `connectSlotsByName` auto-wiring keeps working.
- **`gui_source/connection.py`**: `ConnectionManager(QObject)` — `open()` builds `MDPBus` from settings then `panel.link(bus)` for each panel; `close()`; `match()` for the settings dialog (replaces `on_btnMatch_clicked`'s ad-hoc `MDP_P906` construction and its `MainWindow.api` global read at 2322). Replaces body of `on_btnConnect_clicked` (807–860).

### GUI decomposition (keep Qt Designer + pyuic5 workflow)

- **New `mdp_gui_template/device_panel_p906.ui`**: XML-transplant `frameLcd` + `frameOutputSetting` (incl. aux tabWidget) + `frameSystemState` from `mainwindow.ui` under a `QWidget` root; compile to `device_panel_p906_ui.py`, export `Ui_DevicePanelP906`.
- **`mainwindow.ui` slims to shell**: connection bar (frameSystemSetting + adapter-level ComSpeed/ErrRate labels, fed by `bus.speed_counter`), `widgetDeviceArea` with `QHBoxLayout layoutDevices` (panels inserted programmatically, side by side), `frameGraph` unchanged. Static items removed from `comboGraph1Data/2Data` (populated programmatically with `itemData=(device_id, channel_key)` and label `f"{display_name}: {ch.label}"`).
- **`mdp_gui.py` splits** into flat modules: `app_context.py` (paths/flags/logger/FPSCounter/helpers), `settings_model.py`, `device_core.py`, `device_panel.py`, `device_panel_p906.py`, `connection.py`, `dialogs.py` (MDPSettings/MDPGraphics with constructor injection — no module-global `MainWindow`), `aux_windows.py` (FloatingWindow, ResultGraphWindow verbatim). `mdp_gui.py` remains entry module: QApplication/font/translator bootstrap (order matters, lines 84–109), `MDPMainwindow` shell (chart area, slider, record, connect button, dialog wiring, shell part of `set_theme`), `show_app()`.
- Floating window binds to the panel's `values_signal`; ResultGraphWindow stays a shared singleton connected to each panel's signals.

### Settings schema

```json
{ "adapter": { "comport", "baudrate", "address", "freq", "txpower" },
  "devices": [ { "type": "P906", "id": "p906-1", "name": "P906",
                 "idcode", "m01ch", "color", "blink",
                 "output_warning", "lock_when_output", "ignore_hw_lock",
                 "presets", "v_threshold", "i_threshold", "avgmode",
                 "cali": { "use", "v_k", "v_b", "i_k", "i_b", "vset_k", "vset_b", "iset_k", "iset_b" } } ],
  "ui": { "theme", "color_palette", "data_pts", "display_pts", "graph_max_fps",
          "state_fps", "interp", "opengl", "antialias", "bitadjust" } }
```

- `settings.ui`: regroup into "Adapter" and "Device: P906" groups (same widgets). `graphics.ui`: calibration/thresholds/avgmode group relabeled P906-specific, backed by `setting.devices[0]`.

## File change map

| File | Change |
|---|---|
| `nrf_adapter_source_multiceiver/` | **NEW** bare-metal firmware rewrite: existing protocol + pipe-open command + pipe-tagged `NRF_RECV_OK` |
| `mdp_controller/bus.py` | **NEW** `MDPBus`, routes by pipe number |
| `mdp_controller/mdp_p906.py` | Driver-only rework (see above) |
| `mdp_controller/__sim_mdp_p906.py` | Sim `MDPBus` + new ctor signature |
| `mdp_controller/__init__.py` | Export `MDP_P906` + `MDPBus`, real/sim |
| `mdp_controller/nrf24_adapter.py` | Add pipe-open command send + pipe-tagged recv-frame parsing (`_recv_callback` signature gains pipe byte) |
| `mdp_protocal.py` | Unchanged |
| `test_main.py` | bus + driver construction |
| `gui_source/app_context.py` | **NEW** extraction from mdp_gui.py 1–110, 155–219, 353–361 |
| `gui_source/settings_model.py` | **NEW** `Setting` (nested adapter/devices/ui schema) |
| `gui_source/device_core.py` | **NEW** ChannelSpec, DeviceDataStore, RecordData |
| `gui_source/device_panel.py` | **NEW** DevicePanelBase + registry |
| `gui_source/device_panel_p906.py` | **NEW** P906DevicePanel (~1500 lines moved logic) |
| `gui_source/connection.py` | **NEW** ConnectionManager |
| `gui_source/dialogs.py` | **NEW** MDPSettings, MDPGraphics (injected deps) |
| `gui_source/aux_windows.py` | **NEW** FloatingWindow, ResultGraphWindow verbatim |
| `gui_source/mdp_gui.py` | Shrinks to bootstrap + shell + show_app |
| `mdp_gui_template/device_panel_p906.ui`/`_ui.py` | **NEW** extracted frames, pyuic5 |
| `mdp_gui_template/mainwindow.ui`/`_ui.py` | Shell-only; recompile |
| `mdp_gui_template/settings.ui`, `graphics.ui` + compiled | Regroup/relabel; recompile |
| `gui_source/mdp.pro`, `en_US.ts`, `en_US.qm` | Add sources; pylupdate5; port contexts; lrelease |
| `gui_source/settings.json` | Rewritten to nested schema on first run |
| readme(s) | Note new schema + layout |

## Implementation phases (each ends runnable; verify with `--sim`)

- **Phase 0 — Baseline:** run `--sim`, record manual checklist results as parity reference.
- **Phase 0.5 — Adapter firmware (blocking prerequisite for Phase 1's real-hardware work, can start immediately):** bare-metal rewrite reaching parity with the current closed firmware first (send/recv round trip against a real P906, matching today's CON-ERR baseline), *then* add the pipe-open command and pipe-tagged receive frames, validated with two devices dispatched to separate pipes on one adapter. `--sim` phases below don't depend on this and can proceed in parallel; anything requiring real hardware does.
- **Phase 1 — Transport/driver split:** `bus.py`; rework `mdp_p906.py`; sim `MDPBus`; exports; minimally adapt `on_btnConnect_clicked`/`on_btnMatch_clicked` in place (still flat settings). Best phase for an early **real-hardware smoke test** against the new firmware (watch CON-ERR rate vs. the Phase 0.5 baseline, and confirm pipe-routed attribution with both devices live).
- **Phase 2 — Settings schema:** `settings_model.py`; update all ~120 `setting.*` consumers (grep-driven); storage regrouping in dialogs (defer `.ui` cosmetics to Phase 4). Verify with a fresh settings.json; confirm idempotent re-save.
- **Phase 3 — Channel-keyed data model:** `device_core.py`; replace RealtimeData; rewrite state_callback/get_data/draw_graph/func_sweep/dump/record onto channel keys; programmatic combos with itemData. Still monolithic shell.
- **Phase 4 — GUI decomposition (4 reviewable chunks):**
  - 4a: `.ui` split + recompiles + panel skeleton in `layoutDevices`; shell drives widgets via `self.panel.ui.*` (mechanical rename).
  - 4b: move device logic + per-device timers into `P906DevicePanel`; class attrs → instance attrs; introduce base class.
  - 4c: `connection.py`, `dialogs.py`, `aux_windows.py`, `app_context.py`; delete module-global `MainWindow` refs (constructor injection); rewire signal hookups into `show_app`.
  - 4d: `mdp.pro` + pylupdate5 + `.ts` context port + lrelease; verify with `--english`.
- **Phase 5 — Multi-device plumbing:** shell builds panels from `setting.devices` via registry (list of 1); device-qualified combo labels; per-panel link status; per-device failure reporting in `ConnectionManager.open()`; floating window bound to panel. Sanity: a fake second P906 settings entry renders two panels (then removed — not shipped).
- **Phase 6 — Packaging + docs + hardware pass:** README notes, final real-hardware pass (Match, LED color, blink, HW lock, calibration round-trip, real settings config sanity check).

## Risks (ranked)

1. **Adapter firmware bring-up** — bare-metal STM32F030F4P6, no prior team experience with this toolchain (deliberately skipping CubeIDE/CubeMX/HAL). Mitigation: pin mapping and host protocol are both already fully known (no reverse-engineering left), flashing procedure is already documented, and the nRF24 SPI command set is small; de-risk by reaching closed-firmware parity before touching the pipe extension, per Phase 0.5.
2. **Phase 4b logic migration** — `connectSlotsByName` fails *silently* on name mismatch. Mitigation: preserve every objectName in the extracted `.ui`; grep-diff `on_*` slots vs `.ui` object names; full checklist pass.
3. **Bus routing on real hardware** — new firmware/host-protocol boundary in the timing-sensitive path (50 Hz type-8 + blocking type-7), plus the new pipe-open/dispatch sequencing. Mitigation: reach firmware protocol parity before adding the pipe extension (Phase 0.5); Phase-1 hardware smoke test comparing error rate and confirming pipe attribution with both devices live.
4. **Settings consumer sweep** — missed rename = runtime AttributeError on obscure paths (e.g. `avgmode` only when `len(rtvalues)==9`). Mitigation: per-key grep checklist; sim runs with cali on, both avgmodes, thresholds > 0.
5. **Translation context loss** (291 messages). Mitigation: one-time `.ts` context port + `--english` visual pass.
6. **Class-attribute state → instance attributes** — explicit step in 4b.
7. **Import-time side effects** — all widget construction stays in `show_app()`-time code; new modules must be import-clean.

## Verification

Per-phase `--sim` checklist (Phase 0 = baseline): launch (`--sim`, `--sim --english`); Settings dialog incl. Match; Connect/Disconnect/reconnect; output on/off incl. warning path; V/I spinboxes + quickset + digit-step; LCDs/progress bars; both plots × all channels incl. resistance and "无"; slider scroll-back; keep/autoscale; clear; dump CSV; record CSV + 30 s autosave; energy/avg-power/RecordClear; presets apply/edit/save-persist; sweep V+I with response record + result window fit; wave gen ×5; keep-power PID; battery sim full cycle; sequence editor (all 4 action types, context menu, save/load, single/loop/stop); floating window (toggle/drag/collapse/opacity); graphics dialog live-apply incl. theme dark/light; app close. Final pass on real bench P906 (user hardware) incl. auto-match, LED color, blink, HW-lock, COM-speed/error labels, real settings round-trip.

## Out of scope

- Anything L1060-specific (driver, panel, channels, modes) — comes after this refactor.
- Multiple physical adapters (design permits later: ConnectionManager holds one bus; N buses = list + per-device assignment). Superseded as the *primary* multi-device mechanism by firmware-level multiceiver, but stays available as a bench debugging fallback (see `mdp-commander-bench-setup` memory) for any cross-device ambiguity the pipe firmware can't resolve.
- Porting code files from mdp_commander.
- Git commits (user handles).

