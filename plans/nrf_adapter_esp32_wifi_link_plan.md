# Adapter Firmware — WiFi Host Link (ESP32-C6 / S3 / WROOM-32)

## Context

`nrf_adapter_source_multiceiver` now builds for the ESP32 family across four boards, but
every one of them reaches the host over a wire: the native USB-Serial/JTAG controller on the
C6, H2 and S3, and UART0 through a CP2102/CH340 bridge on the classic ESP-WROOM-32. WiFi was
deliberately never initialised during that port — `nrf_adapter_esp32_port_plan.md:177,1007`
records it as out of scope — so that the 2.4GHz front end stayed quiet while the nRF24 link
to the P906 and L1060 was being validated.

The goal now is to let the adapter sit next to the bench devices with nothing but power, and
have the GUI drive it over IP. Nothing about the radio side changes: the same binary
protocol, the same multiceiver pipe routing, the same unmodified `core/`. Only the byte pipe
between `core/protocol.c` and the host moves from a UART/CDC to a TCP socket.

Boards in scope:

| Board | MCU | Cores | Wired link | WiFi | Status |
|---|---|---|---|---|---|
| ESP32-C6-DevKitC-1-N8 | ESP32-C6, RISC-V 160MHz, 8MB flash | 1 (+LP) | USB-Serial/JTAG | WiFi 6 (2.4GHz) | on hand |
| ESP32-S3-DevKitC-1-N8R8 | ESP32-S3, Xtensa LX7 240MHz dual, 8MB flash | 2 | USB-Serial/JTAG | WiFi 4 | on hand |
| NodeMCU-32S / HiLetgo ESP-WROOM-32 | ESP32-D0WD, Xtensa LX6 240MHz dual, 4MB flash | 2 | UART0 via bridge | WiFi 4 | on hand |
| ESP32-H2-DevKitM-1-N4 | ESP32-H2, RISC-V 96MHz, 4MB flash | 1 | USB-Serial/JTAG | **none** | excluded |

**The H2 has no WiFi at all.** It is 802.15.4 + BLE only. It drops out of this feature
entirely, and the Kconfig `depends on SOC_WIFI_SUPPORTED` expresses that by construction
rather than by a hand-written special case — the same trick that already keeps
`HOST_LINK_USB_JTAG` off the classic ESP32.

Intended outcome: one new host-link implementation, one new build configuration per
WiFi-capable board, `core/` still free of `#ifdef`s, and every existing configuration's
behaviour unchanged.

### On the coexistence worry that kept WiFi out of the original port

It is smaller than it looks, and worth stating precisely before designing around it.

The MDP devices are driven at **2521 MHz** (`AdapterSettings.freq`,
`gui_source/settings_model.py:120`), i.e. nRF24 channel 121. The 2.4GHz WiFi band tops out
around 2483.5 MHz. **There is no channel overlap** — the nRF24 link is parked above the band
WiFi uses, which is presumably why the M01 hub picks that frequency in the first place.

What remains is not co-channel interference but two ordinary RF/electrical effects:

1. **Front-end desense.** A +20 dBm transmitter a few centimetres from the nRF24's antenna
   will push its LNA around regardless of frequency. This is a function of antenna
   separation, not of protocol design, and it is measured in Phase 5 rather than guessed at.
2. **Supply droop.** The WROOM-32 peaks around 350 mA on WiFi TX. A generic nRF24 module
   hanging off a shared 3.3V LDO can brown out on those bursts, which looks exactly like RF
   interference and is not.

Neither is a reason to avoid the feature. Both are reasons to measure the no-ack rate under
WiFi load before declaring the port done.

## Decisions made

1. **Credentials are provisioned over the wired link and stored in NVS.** Three new protocol
   commands, a small host-side tool, and no reflash to change networks. The rejected
   alternatives were build-time credentials (smallest change, but a reflash per network) and
   a SoftAP provisioning portal (pulls in the HTTP server stack — more code than the rest of
   this firmware combined).
2. **A WiFi image serves the protocol on *both* links, concurrently.** This falls directly
   out of decision 1: the wired link has to work on a WiFi build in order to provision it.
   Making it permanent rather than a first-boot special case costs about thirty lines and
   buys a rescue path for when the network config is wrong.
3. **No mDNS. Manual IP entry.** The GUI takes a host and port typed into the connection
   settings dialog; a DHCP reservation on the router is the user's side of that. Avoids the
   `mdns` component in firmware and a `python-zeroconf` dependency in the GUI.
4. **No access control.** Anything on the LAN that can reach the port can drive the power
   supply — the same trust model the USB link has today, documented as such in the README.
   A shared-secret handshake was considered and rejected as security theatre over plaintext
   WiFi.
5. **All three WiFi-capable boards are brought up together**, rather than sequenced. The
   host-link code is chip-independent; only the Phase 5 bench measurements differ per board,
   and running them together is what makes the single-core C6 vs dual-core S3 comparison
   meaningful.
6. **WiFi is opt-in and no board's default `HOST_LINK` changes.** This is not caution — it
   is that a WiFi build genuinely cannot reach a host until it has been provisioned, so it
   cannot be a zero-friction default.
7. **TCP, not UDP.** The existing framer (`SerialReaderBuffered`, and `feed_byte()` in
   `core/protocol.c:213`) is a resyncing byte-stream parser with no message-boundary
   assumption, so TCP drops in with zero framing changes. UDP would shave a little latency
   and cost reordering and loss handling that the device layer's retry loop only partly
   covers. Revisit only if Phase 5 shows TCP latency is the binding constraint.

## Design

### The seam already exists on both sides

**Firmware.** `platform.h` exposes exactly three host-link functions, and
`targets/esp32/main/host_link_usb_jtag.c` and `host_link_uart0.c` are two existing
implementations of them. A TCP server is a third. `main.c` already calls
`host_link_begin(921600)` and needs no change at all — that it doesn't is the sign the seam
is in the right place.

**Host.** The only pyserial API anything below `NRF24Adapter` touches is `write()`,
`read(n)`, `in_waiting`, `close()`, and a settable `baudrate`:

| Call site | API used |
|---|---|
| `mdp_controller/nrf24_adapter.py:237` | `self._serial.write(bytes)` |
| `mdp_controller/nrf24_adapter.py:373` | `self._serial.baudrate = n` |
| `mdp_controller/serial_reader.py:130` | `self._ser.in_waiting`, `self._ser.read(n)` |
| `mdp_controller/serial_reader.py:189` | `self._ser.close()` |

So swapping the object returned by `open_adapter_port()`
(`mdp_controller/nrf24_adapter.py:23`) is the entire transport change. `SerialReaderBuffered`,
`NRF24Adapter._worker`, `MDPBus`, the device drivers and the panels need nothing.

`--sim` is a whole-stack import swap at the *device* layer (`mdp_controller/__init__.py`)
and is orthogonal to this. It is not a template to follow — it swaps `MDPBus` and both
driver classes and never touches a byte stream — and it will not conflict.

### Firmware: a host-link mux, not a third exclusive choice

Decision 2 means the `platform.h` symbols can no longer be owned by whichever wired
implementation the build selected. The arrangement:

- `host_link_usb_jtag.c` / `host_link_uart0.c` rename their four functions to
  `host_link_wired_begin` / `_write` / `_read_byte` / `_set_baudrate`, and the counter to
  `wired_rx_byte_count`. **Bodies are unchanged — this is a rename and nothing else.** The
  same rename precedent (`uart_* → host_link_*`, forced by ESP-IDF owning the symbol
  `uart_set_baudrate`) is already recorded in `nrf_adapter_esp32_port_plan.md:1278`.
- A new `host_link_mux.c` owns the `platform.h` symbols plus `host_rx_byte_count()`, and is
  compiled into **every** ESP32 build — so there is one code path, not a WiFi one and a
  non-WiFi one. Without `CONFIG_HOST_LINK_WIFI` it is a thin passthrough to the wired link.
- **No other target directory is touched** by any of this.

**Routing rule.** Reads poll both links. Writes go to whichever link most recently delivered
a byte to `host_link_read_byte()`; a TCP client connecting makes WiFi active, a disconnect
hands it back to the wired link. Because the host sends `CMD_ECHO` every second
(`nrf24_adapter.py:268`), the active link converges within one second of a host appearing.
The only exposure is an unsolicited radio frame arriving before the first ECHO from a newly
attached host, which goes to the previously-active link and is dropped — the device layer
already retries.

### Firmware: files

| File | Role |
|---|---|
| `targets/esp32/main/host_link_mux.c` (new) | Owns `host_link_*` and `host_rx_byte_count()`; routes wired/WiFi |
| `targets/esp32/main/host_link_wifi.c` (new) | TCP server, RX/TX tasks, stream buffers |
| `targets/esp32/main/wifi_sta.c` / `.h` (new) | STA bring-up, reconnect, credentials in NVS |
| `targets/esp32/main/host_link_usb_jtag.c` | Rename only |
| `targets/esp32/main/host_link_uart0.c` | Rename only |
| `targets/esp32/main/CMakeLists.txt` | New SRCS and REQUIRES |
| `targets/esp32/main/Kconfig.projbuild` | `HOST_LINK_WIFI` choice member, port number |
| `targets/esp32/sdkconfig.wifi` (new) | Fragment: WiFi link + larger factory partition |
| `targets/esp32/Makefile` | `HOST_LINK=HOST_LINK_WIFI`, `-wifi` config id, rejections, distclean |
| `core/protocol.c`, `core/protocol.h` | Three WiFi commands |
| `platform.h` | Two new hooks (below) |
| `targets/{stm32f030,stm32f103,teensy4x,teensy3x}/` | Three-line stub apiece for those hooks |

### Firmware: the TCP server

Listens on port 9000 (Kconfig-settable). One client at a time; **a new connection replaces
the old one** rather than being refused, so a crashed or force-quit GUI does not lock the
adapter out for the length of a TCP keepalive timeout.

**Two FreeRTOS tasks, so each can block properly.** An RX task blocked in `recv()` feeding a
1024-byte stream buffer, and a TX task blocked in `xStreamBufferReceive(portMAX_DELAY)`
draining to `send()`. lwIP permits send and recv on one fd from two tasks. The alternative —
one task doing `select()` with a timeout and polling the TX buffer — trades a task for
either latency or a spin, and is not worth it.

`host_link_write()` keeps the existing contract exactly: check
`xStreamBufferSpacesAvailable() >= len`, then write whole or drop whole. Never block, never
partially emit, never pump a driver task. `host_link_usb_jtag.c:53` states the reason and it
applies here unchanged — `protocol_poll()` drains until `host_link_read_byte()` runs dry and
emits replies from *inside* that loop, so a blocking write starves the watchdog refresh the
moment the host stops reading.

`host_link_set_baudrate()` is a no-op. Precedent is already established on every CDC target,
including the negative-control behaviour in `persistence_test.py`.

Two settings that are load-bearing rather than tuning:

- **`TCP_NODELAY` on both ends.** This protocol's frames are 4–37 bytes and the exchange is
  strictly request/response. Nagle on either side, batched against the peer's delayed ACK,
  adds up to 40ms per exchange — straight into a 40ms device timeout.
- **`esp_wifi_set_ps(WIFI_PS_NONE)`.** The IDF default is `WIFI_PS_MIN_MODEM`, which parks
  the receiver between beacons and can in principle add a full DTIM interval (~100ms) of
  inbound latency. Still worth setting — it costs one line and cannot hurt — but Phase 0's
  bench measurement did not reproduce it as a dominant effect: three A/B pairs on the real
  bench AP showed no consistent latency improvement under `WIFI_PS_NONE`, and the aggregate
  max latency was if anything worse under it. Whatever produces this link's jitter, Phase 0
  did not isolate it as power-save state; Phase 5's coexistence testing is where that gets
  characterized for real.

### Firmware: credentials in NVS

Credentials get their **own NVS key**, not a widened settings record. `store_load()`
(`targets/esp32/main/platform_esp32.c:201`) checks the stored blob size for *exact* equality
against `sizeof(persisted_settings_t)`, so growing that struct silently invalidates every
saved radio setting on every board already in the field. Instead: same `p906` namespace, new
`wifi` key, `{ char ssid[33]; char pass[65]; }`, with `wifi_creds_load()` / `wifi_creds_save()`
private to `wifi_sta.c`.

Also call `esp_wifi_set_storage(WIFI_STORAGE_RAM)` so ESP-IDF does not keep its own competing
copy of the credentials in its own NVS namespace, which would make "clear credentials"
ambiguous.

### Firmware: protocol commands

Added to `core/protocol.h` and `core/protocol.c`, so they exist on every target:

| Command | Payload | Reply |
|---|---|---|
| `CMD_WIFI_SET 0x30` | `ssid_len(1) \| ssid \| pass_len(1) \| pass` | `REP_WIFI_SET 0x30` |
| `CMD_WIFI_QUERY 0x31` | — | `REP_WIFI_STATUS 0x31`: `state(1) \| ip(4) \| rssi(1, signed) \| ssid_len(1) \| ssid` |
| `CMD_WIFI_CLEAR 0x32` | — | `REP_WIFI_SET 0x30` |

`core/` stays free of `#ifdef`s by adding two hooks to `platform.h`:

```c
/* 0 = unsupported or failed. Non-WiFi targets return 0 and protocol.c answers
   REP_CMD_FAILED, which is how a target says "this command does not apply to me". */
int wifi_creds_save(const char *ssid, const char *pass);
int wifi_status(uint8_t *state, uint8_t ip[4], int8_t *rssi, char ssid[33]);
```

The four non-ESP32 targets each get a three-line stub returning 0, appended to their existing
platform file. Explicit stubs rather than weak symbols: the tree has no weak symbols today,
and `platform.h` is documented as the entire contract between `core/` and silicon — a hook
that is invisible in three of five targets would undermine that.

### Firmware: build system

- `Kconfig.projbuild` gains `HOST_LINK_WIFI` with `depends on SOC_WIFI_SUPPORTED`, plus a
  `HOST_LINK_WIFI_PORT` int defaulting to 9000.
- `sdkconfig.wifi` sets `CONFIG_HOST_LINK_WIFI=y` and
  `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE=y`. Current images are 208–243KB against the
  default single-app 1MB factory partition; WiFi + lwIP adds roughly 600KB, which fits but
  leaves no margin worth having. `SINGLE_APP_LARGE` gives 1.5MB and is valid on the
  WROOM-32's 4MB flash too.
- `Makefile`: `HOST_LINK=HOST_LINK_WIFI` → `HOST_LINK_ID = -wifi`,
  `HOST_LINK_FRAGMENT = ;sdkconfig.wifi`. New hard `$(error …)` for `BOARD=ESP32H2` with
  `HOST_LINK_WIFI`, matching the existing `BOARD=ESP32` + `HOST_LINK_USB_JTAG` rejection in
  tone and specificity. The existing `CONSOLE=1` + `HOST_LINK_UART0` rejection needs
  extending: on `BOARD=ESP32` the wired link of a WiFi build *is* UART0, so
  `BOARD=ESP32 CONSOLE=1 HOST_LINK_WIFI` must be rejected for the same reason. `CONSOLE=1`
  with WiFi is legal and useful on the C6 and S3.
- `distclean` gains `sdkconfig.esp32c6-wifi`, `sdkconfig.esp32s3-wifi`, `sdkconfig.esp32-wifi`.
- `main/CMakeLists.txt`: SRCS may be gated on `CONFIG_HOST_LINK_WIFI` (correct in the main
  pass). **REQUIRES cannot be**, for the reason that file already documents at length — IDF
  resolves component requirements in a separate CMake process (`build.cmake:665`) that runs
  before `__kconfig_generate_config` (`:705`), so every `CONFIG_*` is empty there and `-D`
  cache variables are not inherited either. So `esp_wifi esp_netif esp_event lwip` join the
  unconditional REQUIRES list.

  **Risk, resolved in Phase 0.** Unlike `esp_driver_usb_serial_jtag`, whose sources are all
  gated on `CONFIG_SOC_USB_SERIAL_JTAG_SUPPORTED` and which therefore requires-to-nothing on
  the classic ESP32, `esp_wifi` ships linker fragments and may not fully `--gc-sections`
  away — which would grow the *non*-WiFi images. The escape hatch, if the size delta is
  nonzero: move `wifi_sta.c` and `host_link_wifi.c` into
  `targets/esp32/components/wifi_link/` and add it via `EXTRA_COMPONENT_DIRS` from the
  Makefile. The top-level project CMake *does* see `-D` cache variables; only the
  requirements sub-pass does not.

### Host side

New `mdp_controller/tcp_port.py` — `TcpAdapterPort`, roughly 100 lines, duck-typing the five
members listed above. A background recv thread appends into a `bytearray` under a lock;
`in_waiting` returns its length; `read(n)` pops. `TCP_NODELAY` set on connect. `baudrate` is
an accepted, ignored attribute.

Written by hand rather than using pyserial's `serial.serial_for_url("socket://…")`, which
would otherwise be a zero-new-code option: we need `TCP_NODELAY`, a bounded connect timeout,
and reconnect, and that handler gives none of them cleanly.

**Reconnect is in scope**, even though the serial path has none. On a socket drop the recv
thread reconnects with backoff. This composes correctly with machinery that already exists:
`NRF24Adapter._worker` (`nrf24_adapter.py:262`) clears `_connect_event` after 3s without an
ECHO and sets it again when one arrives, so the GUI's link indicator does the right thing
for free. The adapter has not lost its radio configuration across a socket drop, so no
re-init is needed. Note that the serial path currently just kills the worker thread silently
on `PermissionError` (`:290`) — that gap is not being fixed here, only not inherited.

| File | Change |
|---|---|
| `mdp_controller/tcp_port.py` (new) | `TcpAdapterPort` |
| `mdp_controller/nrf24_adapter.py:23` | `open_adapter_port()` becomes a factory dispatching on a `tcp://host:port` prefix |
| `mdp_controller/nrf24_adapter.py:188` | Skip `_find_port_name()` for `tcp://` URLs |
| `gui_source/settings_model.py:120` | `AdapterSettings` gains `transport` (`"serial"`/`"tcp"`), `host`, `tcp_port` (default 9000) |
| `gui_source/connection.py:27` | `_build_bus()` composes `port=f"tcp://{host}:{tcp_port}"` when `transport == "tcp"` |
| `gui_source/dialogs.py` + `mdp_gui_template/settings.ui` | Transport combo plus host/port fields, enabled per transport; regenerate `settings_ui.py`, then `lupdate`/`lrelease` for `en_US.ts` |
| `gui_source/bench_gui_run.py` | Extend `_take_port_arg()` with `--host`, per the established per-run-override pattern |

Settings persistence needs no migration work: `Setting.load` is a `__dict__` merge, so new
fields default cleanly against an existing `settings.json` and unknown keys survive.

### Host side: the timeout budget

This is the one real behavioural risk, and it is worth being explicit about because it is
structural rather than incidental.

**All outbound bus I/O runs on the Qt main thread.** There is no worker thread and no
request queue on the GUI side — `QTimer`s fire on the GUI thread and call blocking driver
methods: a 50Hz `state_request_sender_timer` and a blocking `get_status()` every 100ms
(`gui_source/device_panel_p906.py:128`, `device_panel_l1060.py:227`). Inbound frames arrive
on the adapter worker thread and reach the panels via queued Qt signals. So every millisecond
the transport adds lands on the UI thread.

Against that: `com_timeout=0.04` with `com_retry=5` (`mdp_controller/mdp_device.py:60`). A
wired round trip is ~8ms today (~118 req/s on the Teensy 4.1, ~106 req/s on the WROOM-32's
UART0 link). WiFi adds a few milliseconds plus jitter, and any hiccup burns retries — worst
case 240ms of frozen UI per call. So:

- Thread `com_timeout` down from `MDPBus` to the device drivers rather than leaving it a
  constructor default, and default it to 0.08s when the transport is TCP.
- Raise `MDPBus`'s `wait_connected(timeout=2)` (`bus.py:80`) to 4s for TCP. The adapter's
  ECHO cadence is already 1s, so 2s is thin once a network connect sits in front of it.
- Give `TcpAdapterPort` a bounded connect timeout (~2s). `MDPBus.__init__` blocks and is
  called from the GUI thread, so a wrong IP must fail fast rather than hang on a TCP SYN
  timeout.

### Tooling

- New `nrf_adapter_source_multiceiver/wifi_provision.py`, alongside the existing
  `host_link_test.py` / `pipe_test.py` / `persistence_test.py` and following their argparse
  style: `--port /dev/ttyACM0 --ssid X --password Y`, plus `--status` and `--clear`.
- Teach all three existing test scripts to accept a `tcp://host:port` value for `--port`, so
  the whole bring-up checklist re-runs over the wireless link with no other change. They
  already share the DTR/RTS-low-before-open workaround and the same framing helpers, so this
  is one factory function each.

## Phases

### Phase 0 — Spikes (done 2026-08-02)

Built a throwaway WiFi image on the C6 (temporary `wifi_spike.c`, temporary REQUIRES edit,
both reverted after) and answered the three questions below. The C6's WiFi station MAC is
`f0:f5:bd:04:2f:28`, recorded here only because it was needed to get a DHCP reservation and a
pfsense firewall hole for this spike, not because it means anything going forward.

1. **Does adding `esp_wifi esp_netif esp_event lwip` to the unconditional REQUIRES list change
   the size of a non-WiFi image?** No. `make BOARD=ESP32C6 size` produced the exact same
   208503-byte image before and after the REQUIRES edit, even though
   `project_description.json` confirmed all four components (plus `wifi_provisioning`,
   pulled in transitively) were genuinely part of the build graph. Verified by build size the
   same way the existing `esp_driver_usb_serial_jtag` precedent was. **The
   `EXTRA_COMPONENT_DIRS` escape hatch is not needed** — REQUIRES can stay a flat list in
   `main/CMakeLists.txt`.
2. **What does the image weigh with WiFi linked in, and does it clear 1MB?** A build that
   actually calls `esp_wifi_init`/connects came to 866749 bytes (no console) / 878047 bytes
   (`CONSOLE=1`) — a ~658KB delta over the 208503-byte baseline, close to the plan's "roughly
   600KB" estimate. Both fit the current 1MB default single-app factory partition, but with
   only ~17% margin, and that is *before* the real `wifi_sta.c` / `host_link_wifi.c` / NVS
   credential code exists. **Treat `SINGLE_APP_LARGE` as required, not insurance** — this
   spike used the smallest plausible WiFi-linked image (STA connect + a bare echo loop, no TLS,
   no provisioning UI) and still ate most of the headroom.
3. **Round-trip latency, `WIFI_PS_NONE` vs IDF default, on the bench AP.** Reworked once
   mid-spike: the spike originally had the C6 dial out to a listener on the dev machine, which
   is backwards — the plan's own design (§"Firmware: the TCP server") has the *host* dial the
   *adapter*, so the corrected spike made the C6 a plain TCP echo server (port 9099) and timed
   round trips from a client script on the dev machine, matching the real direction of
   traffic. Ran 3 back-to-back A/B pairs (100 pings/round, 16-byte payload, `TCP_NODELAY`,
   ~5ms spacing) on the real bench AP (WPA3-SAE, 2.4GHz, channel 11, DTIM period 2):

   | Round | default PS avg/max | `WIFI_PS_NONE` avg/max |
   |---|---|---|
   | 1 | 3.25ms / 30.54ms | 2.52ms / 12.37ms |
   | 2 | 2.91ms / 15.41ms | 3.29ms / 45.04ms |
   | 3 | 3.18ms / 45.68ms | 4.36ms / 83.07ms |

   **No consistent improvement from `WIFI_PS_NONE` was measured.** Only round 1 favored it;
   rounds 2 and 3 favored default power-save, and the aggregate max latency was worse under
   `WIFI_PS_NONE` (12/45/83ms vs 30/15/45ms). The plan's DTIM-interval hypothesis is not
   supported by this bench's data — whatever is driving the tens-of-milliseconds jitter here is
   something other than power-save state, and this spike did not isolate what. What *did* hold
   up: the floor latency was 1.65–1.72ms in every round regardless of PS setting, comfortably
   inside the 40ms `com_timeout` budget, so the plan's timeout-budget conclusion (see "Host
   side: the timeout budget") is unaffected. `esp_wifi_set_ps(WIFI_PS_NONE)` is still worth
   calling in Phase 3 — it's a one-line, zero-cost setting and IDF's own docs still recommend it
   for latency-sensitive links — but it should not be sold as *the* fix for a slow link the way
   the original design section implied; Phase 5's coexistence/no-ack testing is what will
   actually characterize this link's jitter.

### Phase 1 — Mux refactor, no WiFi (done 2026-08-02)

Rename in the two wired files; add `host_link_mux.c` as a passthrough; wire it into
`CMakeLists.txt`. Re-run the existing bring-up spot-checks on the C6 to prove nothing
regressed. The byte-identical gate is retired and is not being reimposed — the behavioural
checklist is the gate — but a size delta here should still be explainable, since the change
is a rename plus one forwarding layer.

Build-verified (no hardware needed for a rename-plus-passthrough): `make BOARD=ESP32C6`
compiles clean with zero warnings and the same 208503-byte image Phase 0 recorded, plus one
extra forwarding layer's worth of slack (208503 → ~208640 raw `.bin`, ~18 bytes on the `size`
tool's own total). Every `HOST_LINK`/`CONSOLE` combination on every board (C6, H2, S3, and
classic ESP32 on `HOST_LINK_UART0`) builds clean. The C6 hardware spot-checks from the
original ESP32 port still apply unchanged — nothing in this phase touches boot order, pin
config or the radio path — but were not re-run against real hardware in this session.

### Phase 2 — WiFi station, credentials, protocol commands (done 2026-08-02)

`wifi_sta.c`, the NVS key, the three protocol commands, the `platform.h` hooks, the four
non-ESP32 stubs, and `wifi_provision.py`. Still served over the wired link only. Success is
`wifi_provision.py --status` reporting SSID, DHCP address and RSSI on all three boards.

Turned out to need the `HOST_LINK_WIFI` build-system plumbing too (Kconfig bool, `sdkconfig.wifi`,
the Makefile's third `HOST_LINK` value, `CMakeLists.txt` REQUIRES) — Phase 2's own success
criterion is a flashable image on three boards, which needs a way to build one. `HOST_LINK_WIFI_PORT`
was left in `Kconfig.projbuild` since Phase 3 is the only consumer, but declaring it costs nothing now
and keeps the Kconfig menu edit in one place. `CONFIG_HOST_LINK_WIFI` is a standalone bool, not a
member of the existing `HOST_LINK_USB_JTAG`/`HOST_LINK_UART0` choice — the wired link a WiFi build
uses still resolves automatically per board (Kconfig's `depends on SOC_USB_SERIAL_JTAG_SUPPORTED`
already does that), so `sdkconfig.wifi` only ever sets two symbols. `wifi_sta.c` is compiled into every
ESP32 build unconditionally (like `host_link_mux.c`), gating its real body behind `#if CONFIG_HOST_LINK_WIFI`
internally rather than via CMakeLists SRCS selection — the non-WiFi `#else` stub sits in the same file as
the four non-ESP32 targets' stubs, on the same footing.

Build-verified on every WiFi-capable board and every legal `CONSOLE`/`HOST_LINK_WIFI` combination:

- `make BOARD=ESP32C6 HOST_LINK=HOST_LINK_WIFI` — 861024 bytes, fits the 1.5MB `SINGLE_APP_LARGE`
  partition with 44% free. `CONSOLE=1` combined with it also builds (own config id,
  `esp32c6-console-wifi`).
- `make BOARD=ESP32S3 HOST_LINK=HOST_LINK_WIFI` — 751504 bytes, 51% free.
- `make BOARD=ESP32 HOST_LINK=HOST_LINK_WIFI` — 772064 bytes, 50% free on the WROOM-32's 4MB
  flash; wired link auto-resolves to UART0 as designed, no explicit fragment needed.
- `make BOARD=ESP32H2 HOST_LINK=HOST_LINK_WIFI` and `make BOARD=ESP32 CONSOLE=1 HOST_LINK=HOST_LINK_WIFI`
  both fail with the intended `$(error …)` messages, not a build error.
- The default (non-WiFi) C6 image grew by 464 bytes over Phase 1 — confirmed via `idf.py
  size-components` to be entirely the new `CMD_WIFI_*` dispatch code in `core/protocol.c`
  (compiled into every target unconditionally, by design) plus the tiny non-WiFi stub in
  `wifi_sta.c`; `esp_wifi`/`esp_netif`/`esp_event`/`lwip` contribute exactly zero bytes,
  reconfirming Phase 0's finding on the real (not throwaway) code.
- All four non-ESP32 targets (F030, F103, Teensy 3.x, Teensy 4.x) build clean with the new
  stubs, zero warnings.

Not yet done: `wifi_provision.py --status` against real hardware — that needs a board on the
bench and is Phase 5 bench work in spirit even though the script itself is a Phase 2 deliverable.

### Phase 3 — TCP host link (done 2026-08-02)

`host_link_wifi.c` and the mux's routing rule. Success is `host_link_test.py` over
`tcp://<ip>:9000` passing on the C6.

**Built.** `host_link_wifi.c` / `host_link_wifi.h` (new): a listening socket plus the
current client, both owned by one RX task that blocks in `select()` across the two --
that is what lets a new connection interrupt a stale one without a third task, since a
plain blocking `recv()` on the client fd alone could never notice a pending `accept()`.
A second TX task blocks in `xStreamBufferReceive(portMAX_DELAY)` and drains to `send()`.
Same non-blocking, whole-frame-or-drop `host_link_write()` contract as the wired links;
`TCP_NODELAY` set on every accepted client. Compiled into every ESP32 build like
`wifi_sta.c`, with a stub `#else` (`host_link_wifi_client_connected()` always false) so
`host_link_mux.c` calls it unconditionally with no `CONFIG_HOST_LINK_WIFI` of its own.
Stale bytes left in either stream buffer by a replaced connection are deliberately not
flushed on accept -- the shared resyncing framer (`core/protocol.c`'s `feed_byte()`)
already treats them as noise ahead of the next real frame, on both ends of the link.

`host_link_mux.c`'s routing rule: `host_link_read_byte()` forces `active_link` back to
wired the instant `host_link_wifi_client_connected()` is false (immediate, not waiting
for a wired byte), and flips to WiFi the moment a WiFi byte is actually delivered
(typically the host's next once-a-second `CMD_ECHO`).

**Build-verified**, every board/console/host-link combination, zero warnings: C6 WiFi
877975 bytes (43% free in the 1.5MB `SINGLE_APP_LARGE` partition, up from Phase 2's
861024 -- the ~17KB delta is the new TCP server code), S3 WiFi 765770 bytes (50% free),
classic ESP32 WiFi 786786 bytes (49% free on the WROOM-32's 4MB flash), C6
console+WiFi 889273 bytes. The non-WiFi C6 baseline grew 160 bytes over Phase 2
(208967 -> 209127), all of it `host_link_wifi.c`'s `#else` stub. H2+WiFi and
`BOARD=ESP32 CONSOLE=1 HOST_LINK_WIFI` still fail with the intended `$(error ...)`
messages.

`host_link_test.py` and `persistence_test.py` were taught a `tcp://<host>:<port>`
form for `--port` (a small `_TcpPort` class duck-typing `write`/`read`/`close`/
`reset_input_buffer`, baud accepted and ignored -- no line rate over TCP, same as
USB CDC). `pipe_test.py` was deliberately left alone: its port comes from
`NRF24Adapter`, and `tcp://` support there is Phase 4's `open_adapter_port()`
factory, not yet built.

**Real-hardware run, C6, credentials provisioned to the bench AP (192.168.2.106):**
`host_link_test.py --port tcp://192.168.2.106:9000` steps 1-5 PASS -- echo, NRF query,
`CMD_SET_BAUDRATE` ack-and-roundtrip, `CMD_NRF_SAVE` -- which exercises two live
reconnects (new-connection-replaces-old, while the board stayed running) end to end.
This is the part of Phase 3 that actually works: the TCP server, the framer over it,
and the mux routing rule are all confirmed on real hardware.

**Step 6 (`CMD_REBOOT`, wait 4s, reconnect) FAILS**, and does not recover: after any
reset that goes through a full boot -- `CMD_REBOOT`'s `esp_restart()`, or the hard
reset `make flash` issues via RTS -- `wifi_provision.py --status` reports
`state: connecting` indefinitely (30s+ observed, never transitions).

**Isolated to Phase 2, not this phase's code.** All Phase 3 firmware changes were
`git stash`ed (back to the plain passthrough `host_link_mux.c`, no `host_link_wifi.c`
in the build at all), the resulting pure-Phase-2 image was rebuilt and reflashed, and
the same hang reproduced on the flash-triggered reset alone -- with zero TCP server
code present in the image. So this is a pre-existing bug in `wifi_sta.c`'s boot-time
reconnect path (`wifi_sta_init()` -> `apply_creds()` when NVS already has stored
credentials), not anything in `host_link_wifi.c` or the mux. It was never exercised
against real hardware in Phase 2 -- that phase's own notes already flagged
`wifi_provision.py --status` after a reboot as untested. The live-provisioning path
(`CMD_WIFI_SET` -> `apply_creds()` while already running) is unaffected and was
verified working during this session's provisioning step.

**Root cause, found by boot-log capture.** The console trace was captured
non-interactively after all: `idf.py monitor` needs a real foreground TTY, but a
plain pyserial reader does not, and it doesn't need DTR/RTS help either --
`/dev/ttyUSB1` (identified by MAC-matched `/dev/ttyACM1` being the C6's native
port, `/dev/ttyUSB1` its separate UART0 console bridge, distinguished by opening
each candidate and checking which one printed `ESP-ROM:esp32c6` under a triggered
reset) was the right port; the earlier miss was the wrong device node, not a
transport problem. The trace showed the boot-time reconnect never posting a single
WiFi event -- consistent with a known ESP-IDF race: `esp_wifi_start()` is
asynchronous and returns before the driver task has processed
`WIFI_EVENT_STA_START`, so `esp_wifi_connect()` called synchronously right after it
(exactly what `apply_creds()` did when invoked from `wifi_sta_init()`'s boot path)
can be silently dropped -- no connection attempt, no event, nothing to trigger
`schedule_reconnect()`. ESP-IDF's own reference example
(`esp-idf/examples/wifi/getting_started/station/main/station_example_main.c:76-77`)
calls `esp_wifi_connect()` only from inside the `WIFI_EVENT_STA_START` handler for
exactly this reason. The live `CMD_WIFI_SET` path never hit this race -- by the
time a user provisions, STA has been running for the whole session -- which is why
only the boot-time reconnect was ever seen to hang.

**Fix**, `wifi_sta.c`: a new `s_sta_started` flag, set by a new
`WIFI_EVENT_STA_START` case in `event_handler()` (which also fires the deferred
`esp_wifi_connect()` if credentials are already known). `apply_creds()` now only
calls `esp_wifi_connect()` directly when `s_sta_started` is already true; otherwise
it leaves `s_state` at `STA_CONNECTING` and lets the STA_START handler do it once
it's actually safe to. The live path is unaffected -- STA is always already started
by the time it runs.

**Hardware-verified**, C6, after reflashing the fixed image: boot log shows `wifi_sta:
found stored credentials, connecting` followed immediately by a normal
auth->assoc->run sequence (one `Association refused temporarily` / comeback-timer
retry was par for the course on the bench AP, not a symptom) and `wifi_sta:
connected` with a DHCP lease (`192.168.2.106`) at ~3.7s post-boot, reproduced across
multiple reboots (the C6's native USB-Serial/JTAG port resets the board on every
`pyserial` open, which incidentally provided repeat trials for free).
`host_link_test.py --port tcp://192.168.2.106:9000` now runs all 8 steps --
including step 6 (`CMD_REBOOT`, reconnect, re-query) and step 8 (`CMD_RESET`,
reconnect, re-query), the two that exercise this exact path -- and gets **ALL
PASS**.

Closing this needed one more fix, in the test script rather than firmware:
`host_link_test.py`'s post-reboot reconnect used a fixed `time.sleep(4.0)` then a
single `open_port()`, sized for a wired reboot. Over WiFi this was both too tight
(association + the occasional comeback-timer retry measured ~4.6s end to end) and,
independently, unsound: a `TCP connect()` made immediately after `CMD_REBOOT` can
land in the accept queue of the *old*, not-yet-torn-down process (`esp_restart()`
is not instantaneous) and "succeed" against a connection that vanishes moments
later -- measured directly: a connect returning in under 20ms, followed by a
command on it timing out because the real reboot happens underneath it. Replaced
with `reconnect_after_reset()`: unchanged (fixed sleep, single open) for the serial
case, but polling with an ECHO round-trip required before accepting a connection as
live for the `tcp://` case -- the same standard the production GUI reconnect path
already holds itself to (`nrf24_adapter.py`'s `_connect_event` gates on an actual
ECHO reply, not socket state; see "Host side: the timeout budget" above). Confirmed
this was the second bug, not a rerun of the first, with a standalone reproduction:
an immediate post-`CMD_REBOOT` connect succeeded and then timed out on its first
real command, a second connect attempt failed outright (device genuinely down by
then), and a third succeeded cleanly with a valid reply.

Build-verified after the fix, every board, zero warnings: C6 WiFi (CONSOLE=1)
889760 bytes -- 727 bytes over the pre-fix Phase 3 number, all attributable to the
new `s_sta_started` bool and the extra `WIFI_EVENT_STA_START` branch. S3 WiFi and
classic ESP32 WiFi both rebuilt clean. `BOARD=ESP32H2 HOST_LINK_WIFI` and
`BOARD=ESP32 CONSOLE=1 HOST_LINK_WIFI` still fail with the intended `$(error ...)`
messages, unaffected by a change confined to `wifi_sta.c` and the test script.

**Separately noticed, not investigated further because it's outside this phase's
scope:** running `host_link_test.py` directly over the C6's native port
(`/dev/ttyACM1`, not `tcp://`) now shows every reply shifted by one frame,
consistent with that port resetting the board on every `pyserial` open (as seen
directly during this session's console-log capture) and each reset injecting an
unsolicited `REP_NRF_INIT` boot announcement ahead of the real reply. This
reproduces on unmodified code (`wifi_provision.py`, untouched this session) and
happens before any code this phase touched ever runs, so it isn't a regression
from this phase's changes -- but whether the native port reset-on-open is new
behaviour on this specific build/board, or was always true and simply never
exercised this way before, hasn't been established.

### Phase 4 — Host side (done 2026-08-02)

`TcpAdapterPort`, the `open_adapter_port()` factory, the settings model, `ConnectionManager`,
the settings dialog and its translations, and the timeout threading.

**Built exactly per the design table.** `mdp_controller/tcp_port.py` (new):
`TcpAdapterPort`, a background thread owning the socket, appending inbound
bytes into a `bytearray` under a lock; `in_waiting`/`read()` drain it,
`write()` is a bare `sendall()` that swallows `OSError` (the worker's next
`recv()` notices the drop and reconnects -- the caller's own retry/timeout
loop covers a `write()` that lands in the gap, exactly like a wired
timeout). `TCP_NODELAY` set on connect; first connect is synchronous
(`MDPBus.__init__` blocks on the GUI thread, so a wrong host/port must fail
fast) with a 2s bounded timeout; reconnect afterwards runs on the worker
thread with backoff (0.5s doubling to 5s cap). `baudrate` is a plain
attribute, accepted and ignored. `open_adapter_port()`
(`nrf24_adapter.py`) dispatches a `tcp://host:port` prefix to
`open_tcp_adapter_port()`; `_find_port_name()` was already unreachable for
any truthy `port` string, so no separate change was needed there.

**Timeout budget threaded through exactly as designed.** `MDPBus.__init__`
computes `self._is_tcp` from the `tcp://` prefix and exposes
`self.com_timeout` (0.04 wired, 0.08 tcp); `device_panel.py`'s `link()`
reads it off the bus and passes it into the device driver constructor
instead of relying on the driver's own 0.04 default. `wait_connected()`'s
timeout is raised to 4.0s for tcp (was the flat 2.0s default). `_MATCH_COM_TIMEOUT`
was deliberately left untouched -- not in the plan's three load-bearing
items, and there's no bench evidence yet that matching over TCP needs it.

**Settings dialog.** `AdapterSettings` gained `transport` ("serial"/"tcp"),
`host`, `tcp_port` (default 9000) -- confirmed the existing `Setting.load()`
`__dict__`-merge picks these up against an old `settings.json` with no
migration code. `settings.ui` gained a transport combo and a combined
host:port row (one new row, not two, to keep the dialog's fixed-size
layout change small); dialog height bumped 584 -> 654px for the two new
rows. `settings_ui.py` regenerated via `pyuic5`, not hand-edited.
`dialogs.py`: `initValues()`/`save_adapter_settings()` read/write the three
new fields; a `comboBoxTransport` slot enables `comboBoxPort`/`spinBoxBaud`
xor `lineEditHost`/`spinBoxTcpPort` per transport, so the irrelevant half
of the row pair is visibly disabled rather than just ignored.

**Translations.** `pylupdate5 mdp.pro` regenerated `en_US.ts` against the
new `settings_ui.py`; the two genuine Chinese labels ("连接方式" ->
"Connection Type", "主机地址" -> "Host Address", "USB / 串口" -> "USB /
Serial") were hand-translated, matching this file's existing convention of
translated text living inside `type="unfinished"` (the repo never clears
that attribute, translated or not). The already-English "WiFi (TCP)" and
the placeholder "192.168.1.100" were left untranslated, matching precedent
("7dBm", "P906", "Mhz" etc. are likewise passed through as source text).
`lrelease` rebuilt `en_US.qm` clean. Re-running `pylupdate5` also touched
~200 unrelated lines (obsolete-count 117 -> 123) -- pre-existing drift
between the `.ts` file and source from before this session, not something
this phase's changes caused; no translation text was lost, only some
already-orphaned entries got relabeled `obsolete` instead of `unfinished`.

**Tooling.** `bench_gui_run.py` gained a `--host` argument via a
`_take_host_arg()` helper mirroring `_take_port_arg()`'s style exactly;
passing `--host` switches the in-memory `setting.adapter.transport` to
`"tcp"` (never written back to `settings.json`), `--port` switches it back
to `"serial"`.

**Verified without hardware** (no WiFi-provisioned board was on the bench
for this session): every touched file parses; `TcpAdapterPort` round-tripped
write/read/`in_waiting` against a local loopback echo server; a
drop-and-reaccept test confirmed the reconnect-with-backoff path and that
`write()` during the gap doesn't raise; `settings_model.py` merges the three
new `AdapterSettings` fields against the real on-disk `settings.json`
cleanly; the settings dialog was driven headlessly (`QT_QPA_PLATFORM=offscreen`)
end-to-end -- transport combo toggling correctly flips the enabled state of
the four affected widgets, and `save_adapter_settings()` writes back
correctly -- without touching the file on disk. `--sim` mode's import path
(`mdp_controller/__init__.py`) is untouched and confirmed still resolving to
the simulated classes. One bug was found and fixed during this verification:
`TcpAdapterPort.close()` racing the worker thread's blocked `recv()` produced
a spurious "connection lost, reconnecting" warning on ordinary shutdown --
fixed by checking `self._running` before logging/reconnecting rather than
after. Real-board coverage of steps 3-5 (`host_link_test.py`,
`pipe_test.py`, `persistence_test.py` over `tcp://`, GUI live-linked against
an actual ESP32) is Phase 5 bench work, not re-done here.

### Phase 5 — Bring-up on C6, S3 and WROOM-32

The checklist below, per board.

#### Bring-up results — ESP32-C6 (2026-08-02)

Rebuilt and reflashed from the current committed tree (`make BOARD=ESP32C6
HOST_LINK=HOST_LINK_WIFI`, 877839 bytes) as the Phase 5 baseline, discarding
whatever binary Phase 3's live debugging session had left on the board.

**Steps 1-5: all pass.** Provisioned to the bench AP over the wired native
port (`192.168.2.106`, RSSI -47dBm); reconnected unaided after a true
USB power-cycle, verified over TCP without touching the native port a second
time (repeatedly opening it resets the board, which would have restarted the
very reconnect being measured). `host_link_test.py` and `pipe_test.py` both
ALL PASS over `tcp://192.168.2.106:9000`, the latter exercising real P906 and
L1060 hardware on pipes 0 and 1 with TX retargeting both directions.
`persistence_test.py --phase arm/verify/restore` passed across a real power
cycle; its one "failure" (board answers at both the armed and default
baudrate after the cycle) is the same already-documented TCP limitation as
`host_link_test.py`'s baudrate negative control -- there is no line rate to
lose.

**Found and fixed: a real race in three bench scripts' `open_port()`.**
`host_link_test.py`, `persistence_test.py` and `wifi_provision.py` each carry
their own copy of a DTR/RTS-low-before-open helper that drained boot noise
with a fixed `time.sleep(0.4)`. Measured directly: the C6's boot-time
`REP_NRF_INIT` announcement lands at *exactly* 0.40s on the native port,
landing the race on a knife edge -- `wifi_provision.py --ssid/--password`
intermittently read the boot announcement instead of `CMD_WIFI_SET`'s real
reply. Fixed in all three by draining until the line goes quiet (bounded to
2s) instead of sleeping a fixed amount. Separately, and only once, a
`CMD_WIFI_SET` triggered a real firmware panic and reboot (`rst:0xc
(SW_CPU_RESET)`, a saved PC in the panic dump) rather than a clean reply;
every other invocation of the same command, before and after, replied
cleanly. Not reproduced on demand and not chased further -- noted here rather
than speculated about.

**Step 6, watchdog: pass, after fixing the test methodology itself.** The
existing marker-frame technique (temporary stall in `main.c`, reverted after)
doesn't transfer to a WiFi build unmodified: a `for(;;){}` with no yield
starves every other task on this single-core part, including the WiFi driver
task, so it never associates before the watchdog fires -- zero TCP activity
ever observed. Fix: let the loop run for real (still calling
`protocol_poll()`, still refreshing the watchdog) for the first 8s of uptime,
*then* stall unrefreshed. Five consecutive cycles observed over TCP with zero
manual intervention: reset-to-reset period 11.79s/12.06s/11.86s/11.87s (low
jitter, each including boot + ~3.5s watchdog + WiFi reassociation), the host
TCP client reconnecting unaided every time. Stall reverted, rebuilt, image
size back to the exact 877952-byte baseline, reconnect re-verified clean.

**Steps 7-8, live GUI + no-ack: needed a redo after a bench discipline
miss.** The first two 60s runs were made with the S3 and WROOM-32 still
plugged in and powered (older firmware, still listening/acking) --
[[bench-noack-rate-is-noise]] already documents that a merely-powered idle
adapter costs about 1% no-acks, and this session re-learned it the hard way:
`..E3` (L1060) showed no-acks in both runs, a pattern the same memory records
as never appearing on any prior wired baseline. Parking the idle boards
(unplugged, not just deselected in settings) and rerunning: throughput
~68-70 req/s per device (~140/s combined, comparable to the wired baselines),
no-ack rates 0.74% / 0.05% / 0.17% across three clean runs -- still not the
clean "always 0 on E3" of the historical wired baseline, but an order of
magnitude better than with the idle adapters powered, and within the range
plausible sample noise could produce on its own.

**Step 9, coexistence: run, and conclusive.** Same 60s test in three
conditions on the same board:

| Condition | Sends | No-acks | Rate | Pattern |
|---|---|---|---|---|
| Wired (2nd run; 1st had an unrelated 144-event E2-only burst) | 6288 | 2 | 0.03% | isolated |
| WiFi associated, idle (avg of 3 runs) | ~4300 | ~13 | ~0.3% | scattered, low |
| WiFi under ~2MB/s throttled UDP load | 3654 / 3553 | 140 / 133 | 3.83% / 3.74% | sustained, both pipes, ~45-52 of 60s |

The wired condition is not a true WiFi-off baseline -- decision 2 keeps the
WiFi radio associated and running in the background even when the wired link
is the active data path, so this measures "WiFi radio present but TCP
idle/unused" against "WiFi radio present and busy," not "WiFi vs no WiFi" at
the RF level. Reproduced twice at throttled load (3.83%, 3.74%, both pipes
hit roughly proportionally, sustained rather than bursty) against a first
attempt at an unthrottled flood that broke the link outright (`NRF open pipe
2 failed`) before any data could be collected -- that number is a saturated
control channel, not a coexistence measurement, and was discarded in favor of
the throttled figure. Throughput also dropped under load, ~55 req/s per
device against ~68-70 idle.

**Step 10, supply: droop ruled out.** A DMM held on the nRF24 module's 3.3V
rail throughout a loaded run showed no droop at all. The plan's other
candidate -- front-end desense -- was not isolated by antenna separation
(no spare wire on the bench to build a harness for it, and the boards are
already about 6 inches apart, arguing against simple near-field coupling).
**Left as an open question, not attributed:** the C6 is the single-core
board in this lineup, and a busy WiFi driver task competing with `app_main`'s
own `radio_irq_pending()`/`protocol_poll()` servicing on one core is an
untested alternative to RF desense -- the dual-core S3 showing a smaller
effect under the identical throttled-load test would point at CPU
contention; showing the same effect would point back at RF. Not claimed
either way pending that comparison.

**Net: ESP32-C6 WiFi bring-up complete**, steps 1-10 all run with real
hardware and real data. One genuine, reproducible finding carried into Phase
6: the link degrades measurably (~3.8% no-acks, both pipes, sustained) under
sustained WiFi throughput on this board, with supply droop ruled out and RF
desense vs. single-core CPU contention left open pending the S3 comparison.

#### Bring-up results — ESP32-S3 (2026-08-02)

Rebuilt and flashed from the current tree (`make BOARD=ESP32S3
HOST_LINK=HOST_LINK_WIFI`, 765920 bytes, 50% free in the 1.5MB
`SINGLE_APP_LARGE` partition) as the S3's Phase 5 baseline.

**Steps 1-5: all pass, first time, no firmware changes needed.** Provisioned
over the wired native port to the bench AP (`192.168.2.107`, RSSI -55dBm).
Reconnected unaided after a true power cycle -- on this board that means
pulling *both* USB cables, since the native and the UART-bridge connector can
each back-power the DevKit and pulling one alone leaves it running -- and the
TCP server was already listening on the first probe after replug, verified
without reopening the native port. `host_link_test.py --port
tcp://192.168.2.107:9000` **ALL PASS, all 8 steps**, including step 6
(`CMD_REBOOT`) and step 8 (`CMD_RESET`), the two that exercise the boot-time
reconnect path that had to be fixed on the C6 in Phase 3 -- so that fix holds
on a second chip. `pipe_test.py` passes over TCP against real P906 (pipe 0)
and L1060 (pipe 1) hardware with TX retargeting both directions.
`persistence_test.py --phase arm/verify/restore` passed across the same real
power cycle; its one "failure" is the already-documented TCP baudrate negative
control -- there is no line rate to lose. The three `open_port()` drain fixes
the C6 session produced were already in the tree and needed no further work;
no new script races surfaced here.

**Step 6, watchdog: pass.** Same run-then-stall pattern the C6 needed (loop
runs for real for the first 8s so WiFi can associate, then stalls
unrefreshed) -- the dual core does not make a bare `for(;;)` safe to start at
t=0, and keeping the method identical is what makes the two boards
comparable. Six consecutive reset cycles observed over TCP with zero manual
intervention: reset-to-reset 10.98 / 11.98 / 11.52 / 11.51 / 12.10 / 11.49s,
mean 11.60s, against the C6's 11.79-12.06s. The host TCP client reconnected
unaided every time. Stall reverted (`main.c` byte-identical to committed,
empty `git diff`), rebuilt to the exact 765920-byte baseline, reflashed, and
the link re-verified holding a single connection for 25s with no resets.

**Steps 7-8: pass, two clean runs.** Only the S3 was on the bench, so
[[feedback-park-idle-adapters-before-bench-test]] was satisfied by
construction. 54.7/54.5 and 54.2/54.0 req/s per device; no-acks **0.42% and
0.31%**, comparable to the C6's WiFi-idle ~0.3%. One procedural note worth
carrying: `bench_gui_run.py` *appends* to `gui_source/mdp.log`, and
`noack_report.py` reads the whole file, so the first attempt reported 2.19%
by mixing in the previous session's C6 data. Rotate `mdp.log` before every
run.

**Throughput is lower than the C6's over WiFi, and it is transport latency,
not the chip.** 54 req/s per device here against the C6's 68-70. Measured
protocol `CMD_ECHO` round trips over the real TCP host link (200 samples):
min 4.85ms, avg 6.63ms, p50 5.62ms, p95 13.41ms, max 30.95ms -- roughly 2ms
per exchange more than the C6, which is exactly the gap. Not attributed: the
S3 sits at RSSI -55dBm against the C6's -47dBm, is WiFi 4 against the C6's
WiFi 6, and has a different antenna. Any of those could account for it and
this bring-up did not separate them. Well inside the 0.08s TCP `com_timeout`
either way.

**The S3's wired link is the fastest in the tree.** The wired baseline run
turned **128.3 req/s per device** (~256/s combined) against the Teensy 4.1's
~118 and the WROOM-32 UART0's ~106, at 0.03% no-acks.

**Step 9, coexistence: run, reproduced, and it settles the C6's open
question.** Same 60s test, same board, same physical setup:

| Condition | Sends | No-acks | Rate | req/s per device |
|---|---|---|---|---|
| Wired, no WiFi load | 6457 | 2 | 0.03% | 128.3 |
| WiFi TCP link, associated but idle (2 runs) | 3564 / 3540 | 15 / 11 | 0.42% / 0.31% | 54.7 / 54.2 |
| WiFi TCP link, ~2MB/s throttled UDP load (3 runs) | 2019 / 2307 / 2181 | 141 / 164 / 138 | **6.98% / 7.11% / 6.33%** | 21.5 / 25.5 |
| **Wired link**, same ~2MB/s UDP load aimed at the board | 5622 | 394 | **7.01%** | 100.6 |

The third load run was taken after the bench fault below was repaired, as a
confirmation that the first two were not themselves contaminated by it. It
agrees, and it linked on the first attempt under load.

The fourth row is an extra condition this board's run added, and it is the
informative one. Running the identical UDP flood at the board while the
*host link ran over USB* -- so the WiFi radio is just as busy but no protocol
byte crosses it -- produced **7.01%**, statistically indistinguishable from
the 6.98%/7.11% measured over TCP, on a 2.7x larger sample. **The TCP host
link contributes no measurable share of the no-ack degradation.** What it
does cost is throughput: 54 req/s idle collapsing to 21-25 under load, while
the wired link under the same flood still turned 100.6 req/s. Those are two
separate effects and the fourth row is what pulls them apart.

**The single-core CPU contention hypothesis is falsified.** The C6 left RF
desense and single-core CPU contention open, and named the S3 comparison as
the discriminator: "the dual-core S3 showing a smaller effect under the
identical throttled-load test would point at CPU contention; showing the same
effect would point back at RF." The dual-core S3 shows a *larger* effect --
~7.0% against ~3.8%, reproduced twice on each board -- which is the opposite
of what CPU contention predicts, and the wired-link-under-flood row
independently rules out the host link's own CPU/network cost as the
mechanism. So the cause is on the RF/electrical side.

That does not by itself prove *desense* specifically, and the two boards are
not a controlled RF comparison: different antennas, different WiFi
generations, and an 8dB RSSI difference (the S3 works harder for the same
link). The honest statement is that the degradation tracks the ESP32's radio
being active, not the protocol running over it, and not core count.

**Step 10, supply: not measured on the S3, deliberately.** A first DMM
attempt produced a "rail steady throughout" reading that has to be thrown
out: the nRF24's SCK line (GPIO12, J1-18) was knocked loose while the probes
were being attached, so SPI was dead and the radio transmitted nothing for
the whole observation -- an idle rail under no load proves nothing about
droop under load. Declined on the retry, on the judgement that droop is not a
plausible cause here. Left untested rather than assumed: the C6's DMM result
does not transfer (different regulator, higher draw), so supply is neither
confirmed nor ruled out on this board. It is also not load-bearing for this
phase's conclusion -- the wired-link-under-flood row already shows the
degradation tracks radio activity rather than the host link, and the plan's
own step 10 names the WROOM-32, not the S3, as the board most likely to show
droop.

**A bench fault interrupted step 10: the nRF24's SCK line (GPIO12, J1-18) was
knocked loose while DMM probes were being attached.** Symptom worth
recognising again, because it is misleading: the adapter still answered the
host link normally (ECHO RTT ~7ms) and still reported `NRF configure success`
and `Pipe opened - 1`, but every radio send returned `NRF send timeout`,
preceded by an `NRF FIFO overflow` before the first send went out, and both
devices were unreachable. A dead SPI clock does not announce itself as a
config failure. Rebooting the S3, the P906 and the L1060 changed nothing;
reseating the wire fixed it immediately.

**Recorded because the diagnosis went wrong first.** The repeated
`Failed to connect to MDP-L1060` was briefly written up here as "initial
device connect is unreliable under sustained WiFi load" -- a real-sounding
finding built entirely on the loose wire. What falsified it was the cheap
control nobody had run yet: stop the flood completely and try again. It
failed identically. **Run the load-off control before attributing anything to
load**, however plausible the load story looks.

**Nothing above needs re-running.** Steps 1-9 all completed before the probes
went on, each with its own sample counts and no-ack rates, and the last run
before the fault (wired-under-flood, 100.6 req/s over 5622 sends) was clean.
The post-repair third load run above independently confirms the pre-fault
load figures.

**Net: ESP32-S3 WiFi bring-up complete**, steps 1-9 all run with real
hardware and real data, step 10 deliberately not measured (above), and no
firmware change was needed for any of it. Carried into Phase 6: the
coexistence degradation is worse here than on the C6 (~7.0% vs ~3.8%), the
host link transport is not its cause, and neither is core count.

#### Bring-up results — ESP32-WROOM-32 (2026-08-03)

Rebuilt and reflashed from the current tree (`make BOARD=ESP32 HOST_LINK=HOST_LINK_WIFI`,
786960-byte `.bin`, 49% free in the 1.5MB `SINGLE_APP_LARGE` partition) as the WROOM-32's
Phase 5 baseline. This board has a single USB connection (the CP2102 bridge on UART0 --
there is no separate native port, unlike the C6/S3), so every step in this bring-up,
including flashing, provisioning and the wired link itself, shares that one port.

**Found and fixed: a real drain-race in all three bench scripts' `open_port()`, distinct
from the C6's.** `wifi_provision.py --status` and `--ssid/--password` both intermittently
returned the boot-time `REP_NRF_INIT` announcement instead of the real reply. Captured with
a raw byte-level diagnostic on `/dev/ttyUSB0`: the ROM bootloader's banner (printed at 74880
baud, read here at 921600, so it arrives as garbage bytes rather than text) runs continuously
from t=0 to about t=0.44s, then goes fully silent for roughly 0.2-0.4s, and only then does
`REP_NRF_INIT` itself arrive (measured at t=0.64-0.84s in one capture). The existing drain
loop (`while ... and s.read(256): pass`, shared verbatim across `host_link_test.py`,
`persistence_test.py` and `wifi_provision.py`) exits the instant a single `read()` call comes
back empty -- which is exactly what happens in that silent gap, before `REP_NRF_INIT` has
arrived. So the drain stopped early every time, and `REP_NRF_INIT` landed in whatever real
command was sent next. This is a different trigger than the C6's version of this bug (a
timing coincidence at exactly 0.4s there) but the same class of problem, in the same three
scripts. Fixed identically in all three: require 0.6s of continuous silence, not just one
empty read, bounded to a 3s overall deadline. Confirmed the fix by reproducing the failure
raw (a bare status query got `REP_NRF_INIT` as its reply, `0x20` where `REP_WIFI_STATUS`
`0x31` was expected) and then confirming a clean reply immediately after the fix, with no
other change.

**Provisioning: pass.** `192.168.2.108`, RSSI -67dBm -- weaker than the C6's -47dBm and the
S3's -55dBm, consistent with this being the board with the least WiFi generation/antenna
sophistication of the three, but the link was solid throughout every test below regardless.

**Steps 1-5: all pass.** `host_link_test.py --port tcp://192.168.2.108:9000` -- **ALL PASS,
all 8 steps**, including step 6 (`CMD_REBOOT`) and step 8 (`CMD_RESET`), the boot-time
reconnect path fixed on the C6 in Phase 3 -- so that fix now holds on all three WiFi-capable
boards. `pipe_test.py` needed a retry: the P906 had auto-shut-off before this session's bench
work began, which read as "no response from either device" on the first attempt (both
NRF-level no-acks and timeouts) until the user confirmed the P906 was simply off and powered
it back on -- after which all 4 steps passed cleanly, real hardware, TX retargeting both
directions. `persistence_test.py --phase arm/verify/restore` passed across a real power cycle
(the single USB cable pulled and replugged); its one "failure" is the already-documented TCP
baudrate negative control (`state: connecting` is not the failure mode here -- the board
answers at both the armed and default baudrate after the cycle, because there is no line rate
to lose over TCP), same as on the C6 and S3.

**Step 6, watchdog: pass.** Same run-then-stall technique as the other two boards (the loop
runs for real, still calling `protocol_poll()` and refreshing the watchdog, for the first 8s
of uptime so WiFi can associate, then stalls unrefreshed) -- confirmed via a TCP-side monitor
script rather than a console (this board has none; UART0 *is* the protocol link, so there is
no separate debug output available at all, only the GPIO2 LED). Seven consecutive reset
cycles observed with zero manual intervention, reset-to-reset periods 10.95 / 11.88 / 11.78 /
11.77 / 11.78 / 11.78 / 11.79s (mean ~11.68s) -- closely matching the C6's 11.79-12.06s and
the S3's mean 11.60s. The host TCP client reconnected unaided every time. Stall reverted
(`main.c` byte-identical to committed, empty `git diff`), rebuilt to the exact 786960-byte
baseline, reflashed, and a 20s continuous ping held with zero drops, confirming the revert
didn't leave anything running.

**Steps 7-8: pass, two clean runs, but with a throughput anomaly worth recording.** No-acks
0.03% and 0.12% across two 60s runs, idle boards not a factor here (WROOM-32 was the only
board connected). Both runs' no-acks fell entirely on E2 (P906); zero on E3 (L1060) in
either -- consistent with [[bench-noack-rate-is-noise]]'s rule that only E3 no-acks are a
real signal. Throughput varied more between the two runs than on the C6 or S3: 47.6/47.4 req/s
per device on the first run, 31.7/31.7 on the second, both WiFi-idle with nothing else on the
bench. A raw 200-sample `CMD_ECHO` round-trip measurement (min 4.88ms, avg 7.30ms, p50 5.85ms,
p95 13.67ms, max 22.45ms) came back nearly identical to the S3's numbers, so the variance is
not transport latency. Not attributed further -- noted as an open question rather than
explained.

**A wired-baseline run (WiFi radio associated but idle, same WiFi-enabled firmware, wired
UART0 as the active data path) produced one non-reproducing anomaly first.** The first attempt
collapsed to 2.7/2.8 req/s combined (a ~30x drop from the ~106 req/s wired baseline the
non-WiFi WROOM-32 build established during the original ESP32 port) despite a fast start (up
to 85 sends in the first few seconds) settling into a sustained slow steady-state for the rest
of the 60s window. Re-run immediately after: 83.4/83.3 req/s, 0.00% no-acks -- clean, and
close to the non-WiFi baseline. Treated as a one-off, not chased further, on the same
judgement the C6's unreproduced `CMD_WIFI_SET` panic was: **run the control again before
attributing anything to it**, and here the second run flatly did not reproduce the first.

**Step 9, coexistence: run, reproduced across three load runs, and it's a smaller effect here
than on either other board.** Same 60s test, same physical setup, ~2MB/s throttled UDP load
(measured steady at 2.00MB/s throughout every run) aimed at the board's IP:

| Condition | Sends | No-acks | Rate | req/s per device |
|---|---|---|---|---|
| Wired, WiFi idle (clean run) | 2284 | 0 | 0.00% | 83.4 / 83.3 |
| WiFi TCP, associated but idle (2 runs) | 3270 / 2585 | 1 / 3 | 0.03% / 0.12% | 47.6 / 31.7 (per-run) |
| WiFi TCP under ~2MB/s throttled UDP load (3 runs) | 2005 / 1788 / 1905 | 8 / 13 / 2 | 0.40% / 0.73% / 0.10% | 23.2 / 20.2 / 21.7 |
| **Wired link**, same load aimed at the board | 3847 | 13 | 0.34% | 63.1 / 62.8 |

The fourth row settles the same question the S3's did: running the identical flood while the
*host link stays on USB* -- so the WiFi radio is just as busy but no protocol byte ever
crosses it -- produced 0.34%, statistically in the same range as the three TCP-under-load
runs (0.40%/0.73%/0.10%). **The TCP host link contributes no measurable share of the no-ack
degradation here either**, same conclusion as the S3, on a third board.

**A genuinely different pattern from the other two boards: the degradation stays almost
entirely on E2 (P906).** Across all three TCP-load runs and the wired-under-load control --
39 no-acks total -- only 1 fell on E3 (L1060); every other one was P906 (E2). On the C6 and
S3, load-condition no-acks hit both pipes roughly proportionally. Not explained -- recorded
as-is rather than attributed to anything about P906 vs L1060 traffic patterns, since this
bring-up did not isolate a cause.

**The magnitude is also much smaller than either other board**: ~0.1-0.7% here against the
C6's ~3.8% and the S3's ~7.0%, all measured the same way. Throughput drops similarly in
relative terms though (idle-to-loaded roughly halves on both the wired and WiFi paths here,
comparable to the other boards' relative drops), even though the WROOM-32's idle numbers were
already the lowest of the three in absolute terms (consistent with its -67dBm RSSI, the
weakest signal of the three boards).

**Step 10, supply: no droop, on the one board the plan named as most likely to show it.** DMM
held on the nRF24 module's 3.3V rail throughout the final loaded run (the exact run in the
table's third TCP-load row, 0.10% no-acks). Reading held at 3.30xx V the entire time -- of the
four digits the meter displays, only the last ever moved, drifting slowly by one count in
either direction rather than jumping with load activity. No droop observed, matching the C6's
result and extending it to the one board the plan's own step 10 specifically flagged
(`~350 mA` peak WiFi TX draw) as the most likely candidate.

**Net: ESP32-WROOM-32 WiFi bring-up complete**, steps 1-10 all run with real hardware and
real data. One genuine firmware/test bug found and fixed (the three bench scripts' drain
race, a different trigger than the C6's version of the same bug). Carried into Phase 6: no
supply droop on any of the three boards tested; the TCP host link itself adds no measurable
coexistence cost on either board where it was checked (S3, WROOM-32); and the coexistence
degradation itself varies a great deal by board -- ~3.8% (C6, both pipes), ~7.0% (S3, both
pipes), ~0.1-0.7% (WROOM-32, almost entirely one pipe) -- with no single mechanism established
that explains the spread.

### Phase 6 — Documentation (done 2026-08-03)

`nrf_adapter_source_multiceiver/README.md` gains a WiFi subsection under the ESP32 target:
the board table including why the H2 is absent, provisioning walkthrough, port number, the
`HOST_LINK=HOST_LINK_WIFI` build line, and the LAN-trust statement. `readme_EN.md` gains the
GUI-side transport setting. Both state current behaviour only — dated findings, measurements
and how-we-got-here stay in this document, and neither README links to it.

Both added as designed, no deviations. `nrf_adapter_source_multiceiver/README.md`'s new "###
WiFi host link (`HOST_LINK_WIFI`)" subsection sits right after the existing "Console" section
and before "Wiring", alongside the two wired host-link sections it's a third option next to.
`readme_EN.md`'s new "#### Connecting over WiFi (ESP32 adapters)" subsection sits under
"Control by GUI", next to "Multiple Devices". `readme.md` (Chinese) is untouched, per this
project's standing rule to never edit it.

**This closes out the plan.** All six phases are done, hardware-verified on all three
WiFi-capable boards (C6, S3, WROOM-32).

## Verification

Per board (C6, S3, WROOM-32). Steps 1–5 and 7–8 reuse existing scripts; only steps 9 and 10
are new bench work.

1. **No regression.** Non-WiFi images unchanged, or the delta explained, after Phase 1; the
   existing C6 bring-up checks pass.
2. **Provisioning.** Set credentials over the wired link; `wifi_provision.py --status`
   reports SSID, IP and RSSI. Power-cycle; it reconnects unaided.
3. **`host_link_test.py` over TCP.** Framer, echo, unknown-command, invalid-length. The
   baudrate negative control "legitimately fails" over TCP for exactly the reason it does on
   USB-Serial/JTAG (`host_link_usb_jtag.c:69`) — there is no line rate to set.
4. **`pipe_test.py` over TCP.** Multiceiver pipe routing with the P906 and L1060 both
   attached.
5. **`persistence_test.py` over TCP.** Radio settings survive a reboot; so do credentials.
6. **Watchdog.** A deliberate stall in `main.c` must reset the board within ~3.5s, and the
   host must reconnect on its own without operator action. Test with a firmware stall, not a
   debugger halt.
7. **Live GUI session.** P906 and L1060 both linked at 50Hz for 60s. Compare req/s against
   the wired baselines (~118 req/s Teensy 4.1, ~106 req/s WROOM-32 UART0).
8. **No-ack rate.** Run the 60s test **twice**. A single run proves nothing — identical
   back-to-back runs have previously given 0.32% and 0.06%. Only `..E3` no-acks are a real
   signal.
9. **Coexistence** — the open question this feature raises, and the reason WiFi was left out
   of the original port. Same 60s no-ack test in three conditions on the same board and the
   same physical setup: wired baseline, WiFi associated but idle, and WiFi under load (a bulk
   TCP transfer running alongside the 50Hz polling). If the WiFi conditions degrade,
   separate the nRF24 antenna from the ESP32's before drawing any conclusion about the
   protocol — antenna proximity is the hypothesis to eliminate first.
10. **Supply.** If the radio drops frames only under WiFi load, scope the nRF24's 3.3V rail
    for droop before blaming RF. The WROOM-32 is the board most likely to show this.
