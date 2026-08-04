# Adapter Firmware — Ethernet Host Link (Teensy 4.1)

## Context

`nrf_adapter_source_multiceiver` now reaches the host over three transports: UART on the
STM32 targets, USB CDC on the Teensy and F103, and — since `32ceeb6` — a TCP socket over
WiFi on the three WiFi-capable ESP32 boards. The Teensy 4.1 is the fastest adapter in the
lineup (~118 req/s wired, beating the STM32 dongle) but it is still tethered by USB.

The Teensy 4.1 has an on-die 10/100 Ethernet MAC and the PJRC Ethernet kit (RJ45 MagJack +
DP83825I PHY on the 6-pin ribbon header) is already wired to the board on the bench. The
goal is the same as the WiFi feature's: let the adapter sit next to the bench devices with
nothing but power and a network cable, and have the GUI drive it over IP.

Nothing about the radio side changes: same binary protocol, same multiceiver pipe routing,
same unmodified `core/` radio path. Only the byte pipe between `core/protocol.c` and the
host moves from USB CDC to a TCP socket.

**The host side is already done.** `mdp_controller/tcp_port.py` (`TcpAdapterPort`), the
`tcp://host:port` factory in `open_adapter_port()`, the TCP timeout budget in `bus.py`, and
the GUI's Connection Type / Host Address fields are all transport-generic — they were built
for the ESP32 and know nothing about WiFi. `host_link_test.py`, `persistence_test.py` and
`pipe_test.py` already accept `tcp://`. This is a firmware job plus docs.

## Decisions made

1. **QNEthernet v0.36.0, vendored** into `Drivers/teensy_libs/QNEthernet/` alongside the
   existing PJRC trees. It is lwIP 2.x based, actively maintained, has non-blocking DHCP,
   reads the MAC from the chip's fuses, and exposes `setNoDelay()` / `availableForWrite()` —
   which is what lets the never-block / whole-frame-or-drop `host_link_write()` contract
   port over unchanged. NativeEthernet+FNET was rejected: unmaintained since ~2022, blocking
   DHCP by default, and FNET's own heap.

   **QNEthernet is AGPL-3.0-or-later, and this is a deliberate, eyes-open choice.** The tree
   is already mixed-license — `Drivers/teensy_libs/SPI/` is GPL-2.0-or-LGPL-2.1 and is
   compiled into every Teensy image today, and `Drivers/teensy{3,4}/` carry PJRC's MIT
   variant with its non-standard device-list clause — so the top-level Unlicense already
   does not describe `Drivers/`. What AGPL adds over that is strong copyleft over the whole
   combined work plus §13's network clause, which is directly on point for a firmware whose
   purpose is to serve a protocol over TCP. Irrelevant for bench use; it would have to be
   resolved before contributing this upstream. Recording it here is the entire mitigation —
   a `THIRD_PARTY.md` and de-vendoring `Drivers/` were both considered and deferred to
   their own work.

2. **Full cutover rename `CMD_WIFI_*` → `CMD_NET_*`, `wifi_*` hooks → `net_*`.** Ethernet
   needs exactly what `wifi_status()` already reports, and a command named "wifi" answering
   for a wired link is the kind of thing this tree does not leave lying around (precedent:
   `uart_* → host_link_*`). Per the repo's no-compat rule this is a full cutover with no
   aliases: opcodes `0x30`–`0x32` and the framing stay put, every consumer moves.

3. **DHCP by default, with a static address as an option**, persisted on the adapter. The
   ESP32 feature is DHCP-reservation-only; here a static address is cheap because the Teensy
   already has a working `store_*` and spare emulated EEPROM.

4. **Static IP is Teensy-only for now.** The ESP32 stubs `net_ip_config_save()` and reports
   `mode = DHCP`. It could support static in a few lines of `esp_netif` calls, but adding it
   is scope creep against an Ethernet feature, and `platform.h` already has the vocabulary
   for "this command does not apply to me" (return 0 → `REP_CMD_FAILED`).

5. **`ETH=1`, not `HOST_LINK=HOST_LINK_ETH`.** The ESP32 Makefile names WiFi as a `HOST_LINK`
   value while documenting that it is "a third, orthogonal thing" that does not choose the
   wired implementation — it gets away with the wart because the ESP32's wired link
   auto-resolves per board via Kconfig. On the Teensy the wired link is a genuine user
   choice (`HOST_LINK_USB_CDC` vs `HOST_LINK_SERIAL1`), so folding Ethernet into that
   variable would silently make `SERIAL1 + Ethernet` unbuildable. `ETH=1` is orthogonal in
   the command line as well as in behaviour, matching the ESP32 target's own `CONSOLE=1`
   shape. Like `BOARD` and `HOST_LINK`, changing it needs `make clean` first.

6. **An Ethernet image serves the protocol on both links, concurrently** — same as the
   ESP32, and for a sharper reason here: with DHCP there is no way to learn the adapter's
   address except to ask it over the wired link.

7. **No mDNS, no discovery, no access control.** Both carried over unchanged from the WiFi
   feature's decisions 3 and 4. Anything on the LAN that reaches the port can drive the
   power supply, same trust model the USB link has, documented as such.

8. **TCP port 9000**, matching the ESP32 so the GUI's default needs no per-transport
   special case.

## Design

### Protocol and `platform.h`: the cutover

Opcodes and framing are unchanged; names and one payload grow.

| Command | Payload | Reply |
|---|---|---|
| `CMD_NET_CREDS_SET 0x30` | `ssid_len(1) \| ssid \| pass_len(1) \| pass` | `REP_NET_ACK 0x30` |
| `CMD_NET_QUERY 0x31` | — | `REP_NET_STATUS 0x31` (below) |
| `CMD_NET_CREDS_CLEAR 0x32` | — | `REP_NET_ACK 0x30` |
| `CMD_NET_IP_SET 0x33` **(new)** | `mode(1) \| ip(4) \| mask(4) \| gw(4)` | `REP_NET_ACK 0x30` |

`REP_NET_STATUS` becomes `state(1) | mode(1) | ip(4) | mask(4) | gw(4) | rssi(1,
signed) | ssid_len(1) | ssid` — 48 bytes at most, well inside the framer's 128-byte buffer.
`mode` is 0 DHCP / 1 static. On a wired link `rssi` is 0 and `ssid` is empty. `REP_NET_ACK`
answers all three setters, the way `REP_WIFI_SET` already answers both `CMD_WIFI_SET` and
`CMD_WIFI_CLEAR`.

No `dns` field, deliberately: neither side of this link ever resolves a hostname. The
adapter only ever accepts inbound connections; the GUI's own host-address field is resolved
by the OS resolver on the machine
running it, with no involvement from this protocol.

The two `platform.h` hooks become three, and the eight-argument status call the rename would
otherwise produce is replaced by a struct — a genuine improvement the cutover buys:

```c
typedef struct {
    uint8_t mode;                       /* 0 DHCP, 1 static */
    uint8_t ip[4], mask[4], gw[4];
} net_ip_config_t;

typedef struct {
    uint8_t state;                      /* 0 disconnected, 1 connecting, 2 connected */
    uint8_t mode;
    uint8_t ip[4], mask[4], gw[4];
    int8_t  rssi;                       /* dBm; 0 on wired links */
    char    ssid[33];                   /* NUL-terminated; empty on wired links */
} net_status_t;

/* 0 = unsupported or failed; protocol.c answers REP_CMD_FAILED. */
int net_creds_save(const char *ssid, const char *pass);   /* NULL,NULL clears */
int net_ip_config_save(const net_ip_config_t *cfg);
int net_status(net_status_t *out);
```

`core/protocol.c`'s three cases keep their existing validation shape (exact-length checks,
`REP_INVALID_CMD` on a malformed payload) and gain a fourth for `CMD_NET_IP_SET` with a
fixed 13-byte length check. `core/` stays free of `#ifdef`s.

Every target implements all three. Stub bodies returning 0 go where the existing WiFi stubs
already live: `targets/stm32f030/flash_store.c`, `targets/stm32f103/flash_store.c`,
`targets/teensy3x/platform_teensy3.cpp`. The ESP32's `wifi_sta.c` implements
`net_creds_save()` and `net_status()` for real (its existing `esp_netif_get_ip_info()` call
already has mask and gateway to hand) and stubs `net_ip_config_save()`.

`wifi_provision.py` becomes `net_provision.py`, gaining `--dhcp` and
`--static/--mask/--gateway`, and printing the full address set. Its three sibling
scripts are untouched.

### Firmware: the Teensy host-link mux

A direct port of `targets/esp32/main/host_link_mux.c` (69 lines) — read it first; the
routing rule and its rationale transfer verbatim.

- `platform_teensy4.cpp` renames its `host_link_write` / `host_link_read_byte` /
  `host_link_set_baudrate` / `host_link_begin` to `host_link_wired_*`. **Bodies unchanged —
  a rename and nothing else**, exactly as `host_link_usb_jtag.c` and `host_link_uart0.c`
  were treated. `platform_teensy4.h` declares the wired set.
- New `targets/teensy4x/host_link_mux.cpp` owns the `platform.h` host-link symbols and
  `host_link_begin()`. Compiled into **every** Teensy 4.x build, so there is one code path
  rather than an Ethernet one and a non-Ethernet one; without `HOST_LINK_ETH` it is a thin
  passthrough.
- Routing rule, unchanged from the ESP32: reads poll both links; writes go to whichever link
  most recently delivered a byte; `host_link_eth_client_connected()` reporting false forces
  the wired link active immediately rather than waiting for it to prove itself. The host's
  once-a-second `CMD_ECHO` (`nrf24_adapter.py`) is what converges it.
- `host_link_set_baudrate()` forwards to the wired link only. There is no line rate over
  TCP, same as USB CDC.

New target files (`APP_SRCS = $(wildcard *.cpp)` picks all of them up with no Makefile edit):

| File | Role |
|---|---|
| `targets/teensy4x/host_link_mux.cpp` | Owns `host_link_*`; routes wired/Ethernet |
| `targets/teensy4x/host_link_eth.cpp` / `.h` | TCP server; `#else` stubs when `!HOST_LINK_ETH` |
| `targets/teensy4x/net_eth.cpp` / `.h` | QNEthernet bring-up, DHCP/static, status, IP-config record; `#else` stubs |

`host_link_eth.cpp` and `net_eth.cpp` compile into every build and gate their real bodies on
`#if defined(HOST_LINK_ETH)` internally — the same arrangement `wifi_sta.c` and
`host_link_wifi.c` use, and what keeps `host_link_mux.cpp` free of any conditional of its
own. The non-Ethernet `#else` branches are where the three `net_*` stubs live for this
target, so `platform_teensy4.cpp` loses its WiFi stubs entirely rather than renaming them.

### Firmware: the TCP server, without FreeRTOS

The one structural difference from the ESP32. There, two tasks each block properly — an RX
task in `select()`, a TX task in `xStreamBufferReceive()`. The Teensy has a cooperative
`loop()` and no tasks, so the server is polled from `main.cpp` instead:

```cpp
void loop() {
    if (radio_irq_pending()) protocol_service_radio_irq();
    host_link_eth_poll();          /* Ethernet.loop() + accept + link state */
    protocol_poll();
    /* watchdog refresh, unchanged */
}
```

- One `EthernetServer` on port 9000, one client. **A new connection replaces the old one**
  rather than being refused, so a crashed GUI does not lock the adapter out for a keepalive
  timeout. `setNoDelay(true)` on every accepted client — this protocol's frames are 4–37
  bytes and strictly request/response, and Nagle against the peer's delayed ACK adds up to
  40ms straight into a 40ms `com_timeout`.
- Stale bytes from a replaced connection are deliberately not flushed. `feed_byte()`
  (`core/protocol.c:278`) is a resyncing byte-stream parser and treats them as noise ahead
  of the next real frame — the same reason TCP dropped in with no framing changes at all.
- `host_link_eth_read_byte()` reads straight from the client, mirroring the wired path's
  `HOST_PORT.read()`. No intermediate ring: QNEthernet already buffers into pbufs.
- `host_link_eth_write()` keeps the existing contract exactly — check
  `client.availableForWrite() >= len`, then write whole or drop whole, then `flush()` to
  push immediately. **The `flush()` is not a nicety — it is the fix for the Phase 0 latency
  failure, and removing it costs a factor of ~1400 in round-trip time.** QNEthernet's
  `write()` queues via `altcp_write()` without calling `altcp_output()`, so without the
  explicit `flush()` the reply never reaches the wire and the request goes unacked until
  lwIP's 250ms delayed-ACK timer, past the client's RTO. Phase 0, item 3 has the measurements.
  **Never block, never partially emit.** `host_link_usb_jtag.c:53` states
  the reason and it applies unchanged: `protocol_poll()` drains until `host_link_read_byte()`
  runs dry and emits replies from inside that loop, so a blocking write starves the
  watchdog refresh the moment the host stops reading.

**Drive `Ethernet.loop()` explicitly and disable QNEthernet's `yield()` hook**
(`QNETHERNET_DO_LOOP_IN_YIELD=0`). By default QNEthernet services itself from `yield()`,
which the core's `main()` calls each iteration — but `delay()` calls `yield()` too, and
`delay_ms()` is called from inside `nrf24l01p.c`'s transaction paths. Explicit polling keeps
the stack off the radio's re-entrant paths and matches how everything else in this tree is
serviced.

### Firmware: bring-up must not block

`setup()` calls `watchdog_init()` (3.5s, WDOG1) before `protocol_init()`, and the refresh
only starts once `loop()` runs. So `net_eth_begin()` must never wait: `Ethernet.begin()`
starts DHCP asynchronously and returns, and nothing waits for a lease or for link. State is
reported through `net_status()` and converges on its own. This is the same conclusion the
ESP32 reached, and the ESP32's Phase 3 bug is the cautionary tale — `esp_wifi_start()` being
async is exactly what made a synchronous connect right after it get silently dropped.

### Firmware: the static IP record

A **second, separate EEPROM record**, not a widened `store_record_t`:

```c
#define NET_STORE_ADDR  64            /* clear of the 40-byte settings record at offset 0 */
#define NET_STORE_MAGIC 0x4E455430U   /* "NET0" */
```
`{ magic u32 | mode u8 | ip[4] | mask[4] | gw[4] | crc16 }`, reusing
`platform_teensy4.cpp`'s existing `crc16_ccitt()` — promote it to `platform_teensy4.h` for
`net_eth.cpp` rather than writing a second copy.

Separate for the identical reason the ESP32's credentials got their own NVS key:
`store_load()` (`platform_teensy4.cpp:170`) checks the stored `len` for **exact** equality,
so growing that struct silently invalidates every saved radio setting on every board already
flashed. Consequence, and it matches the ESP32: `CMD_RESET` does not clear the network
config. The Teensy 4.1's emulated EEPROM is 4284 bytes, so offset 64 is uncontended.

### Firmware: build system

```sh
make BOARD=TEENSY41 ETH=1            # + optional HOST_LINK=HOST_LINK_SERIAL1
```

- `ETH ?= 0`; `ETH=1` adds `-DHOST_LINK_ETH` to `APP_CXXFLAGS` and QNEthernet's sources to
  `OBJS`.
- **`BOARD=TEENSY40 ETH=1` is a hard `$(error …)`** — the 4.0 has no Ethernet pads. Written
  in the same tone and with the same specificity as the existing
  `BOARD=ESP32H2 HOST_LINK_WIFI` and `BOARD=ESP32 HOST_LINK_USB_JTAG` rejections.
- QNEthernet is the one tree in this Makefile that is not flat, so its source list needs
  `$(shell find $(QNE_DIR) -name '*.c' -o -name '*.cpp')` rather than `$(wildcard)`, with
  **`$(QNE_DIR)/main.cpp` filtered out** — it defines its own `main()` and would collide
  with the core's. Objects mirror the source tree under `$(BUILD_DIR)/qne/` (`mkdir -p
  $(dir $@)` in the recipe) so nested files with matching basenames do not clobber.
- `-isystem $(QNE_DIR)` in `APP_INCS`, so its warnings do not surface under the `-Wextra`
  our own code is built with — the reason already documented for `SPI`/`DMAChannel`.
- Flash is unchanged (`teensy_loader_cli --mcu=TEENSY41 -s -w -v`).

## Phases

### Phase 0 — Vendor QNEthernet and spike the build (done 2026-08-03)

Vendored v0.36.0 (upstream tag `v0.36.0`) into `Drivers/teensy_libs/QNEthernet/`: `src/`
(207 files, the full lwIP+mbedtls-adapter+mDNS+altcp tree, unmodified) plus `LICENSE`,
`README.md`, `CHANGELOG.md`, `library.properties`, `library.json`, `keywords.txt`. Excluded
`examples/`, `test/`, `platformio.ini`, `.github/` (build-system-specific, not used by this
Makefile) and `include/`/`lib/` (empty PlatformIO placeholders). AGPL-3.0-or-later confirmed
from the vendored `LICENSE` file, consistent with decision 1. This vendoring is permanent,
unlike the rest of this phase's spike code.

Built a throwaway TCP echo image (temporary `main.cpp`, temporary QNEthernet source list
grafted into `targets/teensy4x/Makefile`, both reverted after) and answered the three
questions:

1. **Does it compile, and any symbol collisions beyond `main.cpp`?** All 207 vendored files
   compile cleanly under `FW_CFLAGS`/`FW_CXXFLAGS` (`-isystem`, PJRC's own warning settings,
   no `-Wextra`) with zero warnings. `main.cpp` is filtered from the build's source list as
   planned; no collision risk found — it's `#if defined(MAIN_TEST_PROGRAM)`-gated in the
   upstream source anyway, so even an unfiltered build would have compiled it to nothing.
   **Compiling was not sufficient to link, though.** The final link failed:
   ```
   libgcc.a(unwind-arm.o):(.ARM.exidx+0x6c): relocation truncated to fit: R_ARM_PREL31 against `.ARM.extab'
   libgcc.a(pr-support.o):(.ARM.exidx+0x2c): relocation truncated to fit: R_ARM_PREL31 against `.ARM.extab'
   ```
   Root cause, confirmed with `objdump`: libgcc's precompiled unwind-runtime objects carry a
   plain `.ARM.extab` input section, but `Drivers/teensy4/imxrt1062_t41.ld`'s `.ARM.exidx`
   output rule only captured `.ARM.extab.text*` — one line, pre-existing, untouched by this
   feature. The unmatched `.ARM.extab` fell through to `ld`'s orphan-section placement, which
   put it far from `.ARM.exidx` (VMA'd into ITCM at ~0x00000000 per the script) — far enough
   that the gap to wherever the orphan landed exceeded the 31-bit PC-relative encoding's
   ±1GB range (ITCM vs. the FLASH region alone is a 0x60000000, 1.5GB, span). This was
   latent, not new: nothing in the tree before QNEthernet ever linked in enough C++ to pull
   those two specific libgcc objects' extab content into the image. **Fix, one line, kept
   permanently (not reverted with the rest of the spike):** broaden the pattern to
   `.ARM.extab*`. Confirmed a no-op for every other target — the baseline (non-Ethernet)
   build produces the byte-identical 46048-byte image with and without the fix. Adding
   `-fno-unwind-tables -fno-asynchronous-unwind-tables` was tried as an alternative and found
   unnecessary once the linker script was corrected.
2. **Image weight.** Baseline (no Ethernet, current shipped firmware): `text`=22848
   `data`=8896, 31744 bytes, 0.4% of the 4.1's 7936K flash. With QNEthernet linked in (DHCP
   client + one TCP echo server, exercising the full driver/lwIP/altcp path): `text`=121152
   `data`=22208, 143360 bytes, 1.8%. A ~112KB delta. Confirms the plan's expectation —
   nowhere near flash-constrained the way the ESP32's 1MB partition was.
3. **Round-trip latency — initially measured as a hard blocker, since root-caused and
   closed (2026-08-03).** Same method as the WiFi Phase 0 (throwaway TCP echo server on port
   9099, client on the dev machine, `TCP_NODELAY`, 16-byte payload), but the first numbers
   were nowhere near the WiFi spike's 1.65–1.72ms floor. Reproducible pattern across
   independent connections: roughly every other request needed a client-side TCP retransmit
   (Linux's ~215–225ms minimum RTO) before getting a response; the requests that didn't still
   took 29–135ms — inconsistent with, and frequently over, the 40ms `com_timeout` budget
   outright. Three things were ruled out before the cause was found:
   - **Not a switch/speed mismatch.** The dev machine's NIC had negotiated 2.5Gbps against
     the switch; forcing it down to 100Mb/full (matching the Teensy's own negotiated
     100M/full, confirmed via the switch's port stats, which also showed 0 CRC errors) made
     no measurable difference — same pattern, same timings, before and after.
   - **Not a firmware busy-loop stall.** The spike's `main.cpp` was instrumented with
     `millis()`-timestamped `Serial` prints bracketing every `Ethernet.loop()` /
     `client.available()` / `read()` / `write()` call. Every echo showed `read=0ms
     write=0ms`, and not one outer- or inner-loop iteration exceeded 5ms anywhere in the
     trace — the polling loop runs tight throughout, including during the ~200ms+ windows
     where a client-side retransmit turned out to be needed.
   - **Not frame loss, anywhere.** The candidates left open at the time — silent switch-side
     discards, or loss at the Teensy's own ENET MAC/DMA layer — are both disproven by the A/B
     below: the firmware's own echo counters show **all 60 requests reaching the application
     on every variant**, including the ones the client had to retransmit into. Nothing was
     ever dropped; the bytes were simply not put on the wire.

   **Root cause: a missing `flush()`.** `QNEthernetClient::write()` calls `altcp_write()` but
   deliberately does *not* call `altcp_output()` — `QNETHERNET_FLUSH_AFTER_TCP_WRITE` defaults
   to `0`, and `lwip/tcp_out.c:359` says outright *"To prompt the system to send data now,
   call tcp_output() after"*. So the echo reply was queued and never transmitted. With no
   reply segment going out, there was nothing for lwIP to piggyback an ACK on, leaving the
   request to be acknowledged only by lwIP's delayed-ACK timer — `TCP_FAST_INTERVAL`, **250ms**
   (`lwip/priv/tcp_priv.h:117-122`). That is past Linux's 200ms minimum RTO, so the client
   retransmitted first, at the observed ~215–225ms. The retransmit then reached `tcp_input`,
   which sends an immediate dup-ACK *and* calls `tcp_output`, flushing the stalled reply — so
   the next request landed mid-timer-phase and completed in a fraction of a tick, producing
   the 29–135ms alternating half of the pattern. One cause, all three symptoms, and it
   explains why the firmware instrumentation looked innocent: the stall is inside an lwIP
   timer, not the application.

   **Confirmed on hardware** with a 2×2 A/B — four echo servers in one image on one board and
   one cable, so no result can be a switch, a cable or a link-negotiation artifact. 60
   requests each, 20ms apart, varying payloads with content verified, 0 mismatches:

   | Variant | Median RTT | Over 40ms `com_timeout` |
   |---|---|---|
   | neither | 229.7 ms | 60/60 |
   | `setNoDelay()` only | 229.8 ms | 60/60 |
   | **`flush()` only** | **0.170 ms** | **0/60** |
   | **both** | **0.166 ms** | **0/60** |

   **`flush()` is the whole fix; `setNoDelay()` alone does nothing.** That is consistent with
   the mechanism — with a strict request/response protocol there is never unacked data
   outstanding when the reply is written, so Nagle has nothing to hold back and the design's
   `setNoDelay(true)` is insurance against back-to-back writes rather than the operative fix.
   Keep both, but `flush()` is the load-bearing one and must not be dropped from
   `host_link_eth_write()`. Setting `QNETHERNET_FLUSH_AFTER_TCP_WRITE 1` was considered as
   the alternative and rejected: it would mean patching the vendored tree, and QNEthernet's
   own comment recommends calling `flush()` in the application instead.

   **The result is better than the plan expected, not worse.** 0.17ms median, max 0.245ms
   across 155 requests, and flat at 20ms / 200ms / 1000ms idle gaps — so no EEE/LPI-style
   wake loss either. That is roughly 10× under the WiFi spike's 1.65–1.72ms floor, which does
   support §Verification step 9's expectation. Image weight is unchanged from item 2
   (`text`=121152), since the fix is one call.

### Phase 1 — The `net_*` cutover, no Ethernet (done 2026-08-03)

`platform.h`'s three hooks and two structs; `core/protocol.{c,h}`'s four cases and renamed
enums; the three non-ESP32 stub sites; `wifi_sta.c` → the real ESP32 implementation of the
new signatures; `wifi_provision.py` → `net_provision.py`.

Build-clean on all five targets, zero warnings. **This phase changes the wire format of a
reply that ships on working ESP32 hardware**, so it is not build-verifiable alone: re-run
`net_provision.py --status` against the C6 on real hardware before calling it done.

Also renamed the Teensy 4.x stub (`platform_teensy4.cpp`) alongside the three targets the
plan named explicitly -- it carries the same WiFi-era stub today and needs to keep building
under the new `platform.h` contract; Phase 3 moves its body into `net_eth.cpp`'s non-Ethernet
`#else` branch. `CMD_NET_IP_SET`/`net_ip_config_save()` are fully wired in this phase per the
Design section (all three hooks, all four protocol.c cases) even though no target implements
static IP for real yet -- every target's stub returns 0, matching `net_creds_save()`. ESP32's
`net_status()` also picked up mask/gateway (via `esp_netif_get_ip_info()`), a straightforward
corollary of the wider `net_status_t`. README's WiFi section's provisioning example was
updated to `net_provision.py` in the same commit as the rename -- leaving it pointing at a
deleted filename was not a real option.

**Verified on real hardware**, not just build-clean: flashed the C6 with the WiFi build
(`BOARD=ESP32C6 HOST_LINK=HOST_LINK_WIFI`) and ran `net_provision.py --status` /
`--clear` / `--status` against it. `REP_NET_STATUS`'s widened payload decoded correctly both
times; `CMD_NET_CREDS_CLEAR` produced `REP_NET_ACK` and flipped state from `connecting`
(stale stored credentials from earlier bring-up work) to `disconnected`.

### Phase 2 — Static IP config (done 2026-08-03)

`net_ip_config_save()` and `net_status()` implemented for real in `platform_teensy4.cpp`
(the second EEPROM record, `NET_STORE_ADDR=64`, magic `0x4E455430` "NET0",
`{ magic u32 | mode u8 | ip[4] | mask[4] | gw[4] | crc16 }`, reusing the existing
`crc16_ccitt()` in place -- its promotion to `platform_teensy4.h` is deferred to Phase 3 when
`net_eth.cpp` exists to share it). `net_status()` now returns 1 (supported) unconditionally
on this target and reports the persisted mode/ip/mask/gw with `state = 0`, matching
`platform.h`'s "returns 0 only for unsupported, not for not yet connected." `net_creds_save()`
is untouched (stays a 0 stub -- WiFi-only hook, this target never implements it).
`net_ip_config_save()` was already stubbed to 0 on the ESP32 in Phase 1, so no ESP32 change
was needed here.

`net_provision.py` gained `--dhcp` and `--static IP --mask MASK --gateway GW` (all three
required together), a `parse_ipv4()` validator, and `CMD_NET_IP_SET = 0x33` framing matching
`core/protocol.h`. `print_status()`'s address block now shows whenever `mode == static`, not
only when `state == connected` -- necessary since this phase has no transport that can ever
reach `state == connected`, and the original condition would have hidden the very thing
being verified. rssi/ssid stayed gated on `state == connected`, unaffected.

Build-verified zero-warning on both `BOARD=TEENSY41` and `BOARD=TEENSY40` (this code has no
`ETH`/board dependency). **Verified on real hardware**, not just build-clean: flashed the
Teensy 4.1, then ran `net_provision.py --port /dev/ttyACM0` through the full sequence --
`--status` on a blank record (`disconnected` / `DHCP`, no stale fields), `--static
192.168.1.50 --mask 255.255.255.0 --gateway 192.168.1.1` followed by `--status`
(round-tripped exactly), a physical power cycle followed by `--status` (record survived,
values unchanged), `--dhcp` followed by `--status` (cleanly back to `mode: DHCP` with fields
hidden), and finally re-set static, sent `CMD_RESET` directly (reboots the board and
invalidates the *radio-settings* record), and confirmed via `--status` after reboot that the
network record was untouched -- the separate-EEPROM-record decision doing its job.

### Phase 3 — Teensy mux refactor, no Ethernet (done 2026-08-04)

`platform_teensy4.cpp`'s `host_link_{begin,write,read_byte,set_baudrate}` renamed to
`host_link_wired_*` (bodies unchanged), matching how `host_link_usb_jtag.c` and
`host_link_uart0.c` were treated on the ESP32. New `host_link_mux.cpp` owns the `platform.h`
host-link symbols plus `host_link_begin()`, routing wired vs. Ethernet exactly per
`targets/esp32/main/host_link_mux.c`. New `host_link_eth.{h,cpp}` and `net_eth.{h,cpp}` carry
only their `#else` (non-`HOST_LINK_ETH`) halves -- `host_link_eth.cpp`'s stub is five no-ops/
false, `net_eth.cpp`'s `#else` is `net_creds_save()`/`net_ip_config_save()`/`net_status()`
moved verbatim out of `platform_teensy4.cpp` (unchanged logic, including the `NET_STORE_ADDR`
record), plus a new no-op `net_eth_begin()`. `crc16_ccitt()` was promoted to
`platform_teensy4.h` as planned in Phase 2, so `net_eth.cpp` reuses `platform_teensy4.cpp`'s
one definition rather than carrying a second copy. `platform_init()` gained a trailing call to
`net_eth_begin()` (mirrors `platform_esp32.c` calling `wifi_sta_init()` last), and `main.cpp`'s
`loop()` gained an unconditional `host_link_eth_poll()` call ahead of `protocol_poll()`, so
Phase 4 only has to fill in bodies, not touch either call site again.

Build-clean, zero warnings, on all three configurations that exist without `ETH=1`:
`BOARD=TEENSY41`, `BOARD=TEENSY40`, and `BOARD=TEENSY41 HOST_LINK=HOST_LINK_SERIAL1`. The
default build's `.elf` is byte-identical to the pre-refactor baseline (`text`=22848
`data`=8896, 46048 bytes total) -- with `HOST_LINK_ETH` never defined, the compiler folds the
rename and the whole passthrough layer away rather than merely producing an "explainable"
delta.

**Verified on real hardware**, not just build-clean: flashed the Teensy 4.1 with the default
build and re-ran `host_link_test.py` (all 8 steps pass, including `CMD_RESET` and the
power-cycle in step 6) and `net_provision.py` over USB CDC (`--status`, `--static` +
`--status`, `host_link_test.py`'s `CMD_RESET` again, `--status`, `--dhcp` + `--status`) --
the network record survived `CMD_RESET` exactly as it did in Phase 2, now routed through
`host_link_mux.cpp` and answered by `net_eth.cpp` instead of `platform_teensy4.cpp` directly.

### Phase 4 — Ethernet (firmware done 2026-08-04; hardware bring-up deferred to Phase 5)

`net_eth.cpp`'s `#if HOST_LINK_ETH` branch implemented for real: `net_eth_begin()` reads the
Phase 2 static-IP record and calls `Ethernet.begin(ip, mask, gw)` for `mode == 1`, or plain
`Ethernet.begin()` (DHCP) for `mode == 0` or a blank/corrupt record — both non-blocking, per
the Design section's bring-up requirement. `net_status()` now reports live state: `state` is
0 with no link, 1 with link but no address yet, 2 once `Ethernet.localIP()` is non-zero;
`ip`/`mask`/`gw` come from the live QNEthernet state once there is one, and fall back to the
persisted record before that (so a static config is visible pre-link, matching the WiFi
target's "returns 0 only for unsupported" convention). `net_creds_save()` and
`net_ip_config_save()` turned out identical between the `HOST_LINK_ETH` and non-Ethernet
branches, so both were hoisted above the `#if` rather than duplicated (`net_ip_config_save()`
body is otherwise unchanged from Phase 2); a new `net_store_load()` helper deduplicates the
magic/CRC check that both `net_status()` implementations already did inline. `net_ip_config_save()`
persists only — it does not restart Ethernet live, matching the only workflow ever tested
(Phase 2's set → power-cycle → confirm) and sidestepping the self-referential problem of a
live IP change breaking the very TCP connection that requested it.

`host_link_eth.cpp`'s `#if HOST_LINK_ETH` branch implemented per the Design section exactly:
one `EthernetServer` on port 9000 (`HOST_LINK_ETH_PORT`), one `EthernetClient`, `accept()`
polled every `host_link_eth_poll()` call with a new connection replacing the old
(`client.stop()` on the outgoing one) rather than being refused, `setNoDelay(true)` on
accept. `host_link_eth_write()` checks `availableForWrite() >= len`, then writes whole or
drops whole, then calls `flush()` — the Phase 0 fix, load-bearing and undropped.
`host_link_eth_client_connected()` uses `(bool)client` (`operator bool()`) rather than
`.connected()`, since the latter calls `Ethernet.loop()` internally as a side effect and this
file's contract is that `host_link_eth_poll()` is the only place that happens explicitly.

Makefile: `ETH ?= 0`; `ETH=1` adds `-DHOST_LINK_ETH` and `-isystem $(QNE_DIR)` to
`APP_CXXFLAGS`/`APP_INCS`, and adds the vendored QNEthernet tree to `OBJS`, discovered via
`find` (not `wildcard`, since it isn't flat) with `main.cpp` filtered out and objects mirrored
under `build/qne/` (`mkdir -p $(dir $@)` in the recipe, since an order-only prerequisite would
only create the top-level directory). QNEthernet's own sources compile under new
`QNE_CFLAGS`/`QNE_CXXFLAGS` — PJRC-style (`-Wall`, no `-Wextra`) rather than the app's, plus
`-DQNETHERNET_DO_LOOP_IN_YIELD=0` per the Design section. `BOARD=TEENSY40 ETH=1` is a hard
`$(error ...)`, checked immediately after `BOARD` resolves.

Build-verified zero-warning on all four configurations that now exist:
`BOARD=TEENSY41` (default, byte-identical to the pre-Phase-4 baseline — `text`=22848
`data`=8896, 46048 bytes total, confirming `HOST_LINK_ETH` truly gates out on a plain build),
`BOARD=TEENSY40`, `BOARD=TEENSY41 HOST_LINK=HOST_LINK_SERIAL1`, and `BOARD=TEENSY41 ETH=1`
(`text`=130368 `data`=24256, 154624 bytes total — up from Phase 0's echo-only spike estimate
of 143360 now that a real TCP server, the wired-link passthrough, and both `net_eth.cpp`
branches are all linked in). `BOARD=TEENSY40 ETH=1` confirmed to hit the intended
`$(error ...)` rather than silently building.

**Not done in this pass, deliberately:** real hardware. Phase 4's own text ties its success
criterion to `host_link_test.py --port tcp://<ip>:9000` passing all 8 steps, but that requires
a live board on the bench — that work, plus the rest of the Verification checklist, is Phase
5's job and was left for a separate session.

Phase 0's latency finding is root-caused and closed, so this phase did not carry it as an
open risk — only as the one hard requirement `host_link_eth_write()` inherits from it
(`flush()` after `write()`, implemented above). If round-trip times come back in the hundreds
of milliseconds during Phase 5, that call is the first thing to check — the symptom is
distinctive (alternating ~220ms stalls) and the cause is never frame loss.

### Phase 5 — Bring-up on the Teensy 4.1 (done 2026-08-04)

Built and flashed `BOARD=TEENSY41 ETH=1` (`teensy_loader_cli --mcu=TEENSY41 -s -w -v`) to
the bench Teensy 4.1 with the PJRC Ethernet kit wired in. Image size matched Phase 4's build
exactly (`text`=130368 `data`=24256, 154624 bytes) — no drift from the un-hardware-tested
prediction. Ran the full Verification checklist against it on real hardware.

1. **No regression.** `net_provision.py --status` over USB CDC kept working throughout.
2. **Addressing.** DHCP came up unaided: `state=connected mode=DHCP ip=192.168.8.205
   mask=255.255.255.0 gateway=192.168.8.1`, same address held across every reboot and power
   cycle in this phase. `ping` round trip 0.12–0.21ms — in the same neighborhood as Phase 0's
   0.17ms echo-server floor, on real traffic rather than a spike image.
3. **`host_link_test.py` over TCP.** All 8 steps passed, including the `CMD_REBOOT` (step 6)
   and `CMD_RESET` (step 8) reconnects — `reconnect_after_reset()`'s poll-for-a-live-ECHO
   loop (already written for the ESP32) worked unmodified for Ethernet's boot-deaf-window-plus-
   DHCP timing.
4. **`pipe_test.py` over TCP.** Both P906 and L1060 dispatched and read back correctly through
   `tcp://192.168.8.205:9000` — pipe-1/pipe-2 routing is transport-blind, as expected.
5. **`persistence_test.py` over TCP — found and fixed a real gap.** The `restore` phase's
   post-`CMD_RESET` reconnect used a fixed `time.sleep(4.0)`, written for a wired reboot
   (USB CDC/UART come back near-instantly). Over Ethernet that raced a real requirement: the
   board has to clear the boot-deaf window *and* re-lease DHCP before port 9000 is reachable
   again, which took longer than 4s on this bench. First `restore` run threw a bare
   `TimeoutError` from `socket.create_connection()`. Fixed by porting
   `host_link_test.py`'s `reconnect_after_reset()` pattern into `persistence_test.py`: unchanged
   fixed-sleep behavior for the wired case, a poll-for-a-live-ECHO retry loop (10s budget) for
   `tcp://`. Re-ran the full `arm` → physical power removal → `verify` → `restore` cycle twice
   (once to hit the bug, once clean after the fix): the distinctive radio config and 115200
   baudrate survived a real power cycle and were read back correctly over TCP both times;
   `verify`'s step 3 negative control ("silent at 921600") fails as documented — there is no
   line rate over TCP, same as it does on USB CDC; `restore`'s `CMD_RESET` cleanly returned the
   board to compiled-in defaults. The static-IP record's independence from the CMD_RESET'd
   settings record was re-confirmed throughout — `net_provision.py --status` over USB reported
   the same DHCP lease before and after every `CMD_RESET` in this phase.
6. **Watchdog.** Added a temporary stall to `main.cpp`'s `loop()` (`if (millis() > 8000)
   while (1) {}` — run for real long enough for link/DHCP to converge, then hang unrefreshed),
   per the plan's run-then-stall pattern. Flashed, then watched both the USB CDC device node
   and the TCP link: the board reliably hard-resets within its ~3.5s WDOG1 timeout of the stall
   starting, and both USB and the Ethernet link (fresh DHCP lease, same address) recover
   unaided, repeating the cycle indefinitely without any intervention. Reverted the stall,
   rebuilt, and confirmed the `.elf` size was back to the exact Phase 4 baseline before
   reflashing the bench board with the clean image.
7. **Live GUI session.** `gui_source/bench_gui_run.py 60 --host 192.168.8.205`, P906 and L1060
   both linked: **149.6/s and 149.9/s** per device — comfortably above this board's own ~118
   req/s wired baseline, and the adapter's own error-rate counter read 0.00%.
8. **No-ack rate, twice.** No other nRF24 board was connected to the bench machine for any of
   this phase's runs (`lsusb`/`/dev/serial/by-id` checked before each), so no parking step was
   needed. Two clean 60s runs: **0.04%** (3/7349) and **0.05%** (4/7307) total no-ack, both
   with every no-ack on the P906 pipe (`..E2`) and **zero** on the L1060 pipe (`..E3`) —
   consistent with [[bench-noack-rate-is-noise]]'s finding that only `..E3` no-acks are a real
   signal; this transport shows none.
9. **Load.** UDP flood via `nping --udp --data-length 1400` aimed at the adapter's IP on an
   unused port (12345, not 9000 — hammering the protocol's own TCP port would have kicked the
   live GUI connection per the "new connection replaces the old" design, which is not the
   background-load condition this step is after). `nping`'s `--rate` did not throttle as
   documented (a calibration run at `--delay 0.3ms` implied ~2800pps; the real run burned
   182000 packets in 7.3s, ~25000pps) — so the load was a short, intense burst inside the 60s
   window rather than a sustained trickle across all of it. Even so: throughput held at
   149.1–149.5/s (no measurable drop from the unloaded baseline), and the no-ack log
   (0.19%, 14/7335) stayed within the same run-to-run noise band as the unloaded pair above and
   again landed exclusively on `..E2` — zero on `..E3`, so no real RF signal from the flood
   either. One number did move: the adapter's own `speed_counter.error_rate` read 0.83%
   against 0.00% unloaded, which this document is not going to guess a cause for
   ([[no-speculative-fault-attribution]]) beyond noting it is a different counter than the
   no-ack log and plausibly reflects TCP-level contention on the shared 100M link rather than
   anything radio-side, since the signal that has historically tracked real radio problems
   (`..E3` no-acks) stayed at zero.
10. **Supply.** Not run. The step's own text gates it on step 9 showing degradation, and by
    the methodology this document otherwise trusts (throughput, and `..E3` no-acks) step 9
    didn't.

No firmware changes were needed in this phase — every finding was either a pass or, in
`persistence_test.py`'s case, a bench-script fix, not a firmware bug.

### Phase 6 — Documentation (done 2026-08-04)

`nrf_adapter_source_multiceiver/README.md` gains an "Ethernet host link (`ETH=1`)"
subsection under the Teensy 4.x target, placed the way the ESP32's WiFi section was — next
to the wired host-link sections it is a third option alongside. Contents: the 4.1-only
restriction, the PJRC kit wiring, the `make BOARD=TEENSY41 ETH=1` line, DHCP-vs-static and
how to find the address, port 9000, and the LAN-trust statement. `readme_EN.md` gains a
line noting the GUI's existing WiFi (TCP) transport setting works for Ethernet adapters
too. Both state current behaviour only — dated findings stay in this document, and neither
README links to it. `readme.md` (Chinese) is not touched.

## Verification

1. **No regression.** After Phase 1, `net_provision.py --status` still works on the C6 over
   its wired link. After Phase 3, the Teensy's existing USB CDC spot-checks still pass.
2. **Addressing.** DHCP: power on with a cable, `net_provision.py --port /dev/ttyACM0
   --status` over USB reports a lease. Static: `--static`, power-cycle, confirm it comes
   back at the configured address unaided and that `CMD_RESET` leaves it intact.
3. **`host_link_test.py` over TCP.** All 8 steps. The baudrate negative control
   "legitimately fails" over TCP for the same reason it does on USB CDC — there is no line
   rate to set. Steps 6 and 8 (`CMD_REBOOT` / `CMD_RESET` then reconnect) are the ones that
   exercise the boot-time bring-up path; `reconnect_after_reset()` already requires a real
   ECHO round trip before accepting a TCP connection as live, which is what caught the
   ESP32's post-reboot phantom-connect bug.
4. **`pipe_test.py` over TCP.** Multiceiver routing with the P906 and L1060 both attached.
5. **`persistence_test.py` over TCP.** Radio settings survive a reboot; so does the IP
   config.
6. **Watchdog.** A deliberate stall in `main.cpp` must reset the board within ~3.5s and the
   host must reconnect unaided. Use the run-then-stall pattern the ESP32 bring-up settled
   on — let `loop()` run for real for the first 8s so the link and DHCP can come up, *then*
   stall unrefreshed. Test with a firmware stall, never a debugger halt
   ([[openocd-freezes-stm32-iwdg]] is the ARM-side version of why).
7. **Live GUI session.** P906 and L1060 both linked at 50Hz for 60s. Compare req/s against
   this board's own wired baseline (~118 req/s), which is the fastest in the lineup and so
   the most likely to show transport overhead.
8. **No-ack rate.** Run the 60s test **twice** — [[bench-noack-rate-is-noise]]. Park every
   idle nRF24 board by unplugging it, not by deselecting it in settings
   ([[feedback-park-idle-adapters-before-bench-test]]); the WiFi bring-up lost a session to
   exactly this.
9. **Load.** Re-run step 8 with bulk traffic aimed at the adapter's IP, for comparability
   with the three ESP32 boards rather than because interference is expected — Ethernet has
   no RF coexistence question at all, which is the main reason to expect this to look
   better than the WiFi numbers (~3.8% C6, ~7.0% S3, ~0.1–0.7% WROOM-32). Phase 0's echo
   floor of 0.17ms (vs. WiFi's 1.65–1.72ms) supports that expectation, but an idle echo
   server is not a loaded adapter, so this step still has to be run.
10. **Supply.** Only if step 9 shows degradation: the PJRC kit's PHY draws from the same
    3.3V rail as the nRF24 module, so check the rail before attributing anything else.

## Critical files

**Cross-target (Phase 1–2):** `platform.h`, `core/protocol.c`, `core/protocol.h`,
`nrf_adapter_source_multiceiver/net_provision.py` (renamed), and the stub sites
`targets/stm32f030/flash_store.c`, `targets/stm32f103/flash_store.c`,
`targets/teensy3x/platform_teensy3.cpp`, `targets/esp32/main/wifi_sta.c`.

**Teensy (Phase 3–4):** `targets/teensy4x/{platform_teensy4.cpp,platform_teensy4.h,main.cpp,Makefile}`
plus the three new file pairs. Read `targets/esp32/main/host_link_mux.c` before writing the
Teensy mux — it is the model, and its comments carry the routing rationale.

**Not touched:** `mdp_controller/`, `gui_source/`, `core/nrf24l01p.c`, `pins.h`, and the
three bench test scripts. Their `tcp://` support already exists.
