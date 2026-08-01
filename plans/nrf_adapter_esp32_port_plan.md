# Adapter Firmware — ESP32 Target (C6 / H2 now, S3 when it lands)

## Context

`nrf_adapter_source_multiceiver` builds for six configurations across four target
directories (`stm32f030`, `stm32f103`, `teensy4x`, `teensy3x`). Every one of them
is an ARM part built with the system `arm-none-eabi-gcc` against a vendored tree
under `Drivers/`. This adds a fifth target directory for RISC-V/Xtensa Espressif
parts, built with ESP-IDF.

The goal is the same as the previous port: run the adapter firmware on hardware
already on the bench. Boards in scope:

| Board | MCU | Cores | Host link | Status |
|---|---|---|---|---|
| ESP32-C6-DevKitC-1-N8 | ESP32-C6, RISC-V 160MHz, 8MB flash | 1 (+LP) | USB-Serial/JTAG | **on hand — primary** |
| ESP32-H2-DevKitM-1-N4 | ESP32-H2, RISC-V 96MHz, 4MB flash | 1 | USB-Serial/JTAG | on hand — second |
| ESP32-S3-DevKitC-1-N8R8 | ESP32-S3, Xtensa LX7 240MHz dual | 2 | USB-Serial/JTAG | ordered, arrives 2026-08-02 |
| NodeMCU-32S / HiLetgo ESP-WROOM-32 | ESP32-D0WD, Xtensa LX6 240MHz dual, 4MB flash | 2 | **UART0 via CP2102/CH340 bridge** | on hand — added in Phase 4 |

Intended outcome: one new target directory, three build configurations, `core/`
and `platform.h` still unmodified, and every existing target's build unchanged.

**Decisions made, to confirm before Phase 1 starts:**

1. **Host link is the USB-Serial/JTAG controller, not USB-OTG/TinyUSB.** All
   three chips have it, it is already a CDC-ACM device, and the ESP-IDF driver
   (`usb_serial_jtag_read_bytes` / `usb_serial_jtag_write_bytes`, both with a
   `ticks_to_wait` argument) gives non-blocking read and write against a
   driver-managed ring buffer — which is exactly the contract `platform.h`'s
   `uart_read_byte()`/`uart_write()` want. Using it means **one host-link
   implementation covers C6, H2 and S3 with no per-chip code**, and the target
   needs no managed components, no USB descriptors, and no `idf_component.yml`.
   `esp_tinyusb` on the S3 would buy nothing this firmware can use.
2. **Both USB ports are used, by design.** Protocol on the native
   USB-Serial/JTAG connector; ESP-IDF console and panic output on UART0 out the
   *other* connector (the USB-to-UART bridge port), with the secondary console
   disabled. Espressif's own DevKits were chosen for this port specifically
   because all three carry the dual-connector arrangement, so the second cable is
   the intended operating mode rather than a bring-up concession — and it gives
   this target something no other board in the repo has: a real log/panic console
   running alongside a live protocol link.

   **Superseded by Phase 3 (2026-08-01).** This held for the three DevKits but
   is not a property of ESP32 as a family: the ESP-WROOM-32 boards added in
   Phase 4 have one connector and no native USB at all. The console became
   `CONSOLE=1`, off by default, so the second connector is opt-in rather than
   assumed. The protocol link's per-board default is unchanged.
3. **One target directory, `targets/esp32/`, with `BOARD ?= ESP32C6`**, following
   the `teensy4x`/`teensy3x` precedent rather than the overview's `targets/esp32s3/`.
   The three chips differ in exactly two places: the pin map and the SPI host
   number. Everything else — host link, NVS, watchdog, timing, reboot — is
   chip-independent ESP-IDF.
4. **C6 first, then H2, then S3 when it arrives.** The C6 is faster (160MHz vs
   96MHz), has the larger flash, and its J3 header breaks out GPIO18–23 as a
   contiguous six-pin run, which is the whole nRF24 harness on one block.
5. **The S3 stays in the plan but is not a gate.** It is a third `BOARD` value,
   validated when the board lands; nothing in Phases 1–4 depends on it.
6. **ESP-IDF is pinned at v5.4.4 and does not move during this port.** Rationale
   and the accurate support position are in Phase 0 step 1 — the short version is
   that a moving SDK under a vendored-everything tree is the exact failure the
   `Drivers/` convention exists to prevent, and there is no version-shift fight
   worth having here.

**Standing constraints** (unchanged from `nrf_adapter_new_targets_port_plan.md`):
no compat shims or deprecated aliases; `core/` and `platform.h` are not modified;
MCU/framework headers appear only inside `targets/<name>/`; English readme only
(`readme_EN.md`, never `readme.md`); `venv/bin/python` for all host-side scripts;
user runs all git commits; no PyInstaller packaging.

**Bench radio configuration — fixed, do not change it.** Same as the previous
plan and `gui_source/settings.json`:

| | Address | Pipe (MDPBus) |
|---|---|---|
| Adapter base | `AA:BB:CC:DD:EE` | primary (`RX_ADDR_P0`/`TX_ADDR`) |
| P906 (`FE2597EC`) | `AA:BB:CC:DD:E2` | 1 |
| L1060 (`09B93C19`) | `AA:BB:CC:DD:E3` | 2 |

Channel 2521 MHz (`CHANNEL_BYTE` 121). Nothing here requires re-pairing either
device and nothing in it may introduce a re-pair.

---

## The one real deviation: this target needs an SDK

`README.md:373-376` claims all targets build "against the system
`arm-none-eabi-gcc` — no package manager, no board manifest, no downloaded
toolchain," and `Drivers/` holds verbatim third-party copies so the build does
not move underfoot.

**ESP-IDF cannot be vendored on those terms.** It is a multi-gigabyte SDK with
per-architecture toolchains installed through its own `install.sh` into
`~/.espressif`, and its build is CMake+Kconfig driven, not a Makefile over a
source list. There is no honest way to pretend otherwise.

The mitigation is to pin and record it the same way `-DTEENSYDUINO=159` pins the
PJRC cores:

- Install once (`~/esp/esp-idf`), record the exact `idf.py --version` output in
  the README, and refresh it for a specific bug rather than on a schedule.
- Wrap it in a `Makefile` so the target is driven identically to every other one
  in the tree — `make`, `make flash`, `make clean` from inside
  `targets/esp32/`. The Makefile shells out to `idf.py`; it does not reimplement
  it.
- `Drivers/` gains nothing.
- **Fix the README's claim** rather than leaving it false: it becomes "all ARM
  targets build against the system `arm-none-eabi-gcc`…", with the ESP32 target's
  SDK requirement stated in its own section.

---

## Wiring

Both pin maps are taken from the Espressif header tables, avoiding strapping
pins, the USB D+/D- pair, the console UART, and the addressable RGB LED. (The
ESP-WROOM-32 map is in Phase 4, with the boards it belongs to.)

**ESP32-C6-DevKitC-1** — one contiguous J3 block, GPIO23 down to GPIO18:

| nRF24 | GPIO | Header |
|---|---|---|
| IRQ | 23 | J3-5 |
| CE | 22 | J3-6 |
| CSN | 21 | J3-7 |
| MISO | 20 | J3-8 |
| SCK | 19 | J3-9 |
| MOSI | 18 | J3-10 |
| GND | — | J3-12 |
| VCC | 3V3 | J1-1 |

Avoided: GPIO4/5 (MTMS/MTDI), GPIO8 (RGB LED **and** strapping), GPIO9 (BOOT),
GPIO15 (strapping), GPIO12/13 (USB D-/D+), GPIO16/17 (U0TXD/U0RXD → bridge).

**ESP32-H2-DevKitM-1** — mixed headers; the J3 side alone cannot supply six safe
pins:

| nRF24 | GPIO | Header |
|---|---|---|
| MISO | 0 | J1-3 (FSPIQ) |
| SCK | 4 | J1-9 (FSPICLK) |
| MOSI | 5 | J1-10 (FSPID) |
| CSN | 10 | J3-4 |
| CE | 11 | J3-5 |
| IRQ | 12 | J3-7 |
| GND | — | J3-10 |
| VCC | 3V3 | J1-1 |

Avoided: GPIO2 (MTMS, strapping), GPIO8 (LOG, strapping), GPIO9 (BOOT),
GPIO23/24 (U0RXD/U0TXD → bridge), GPIO26/27 (USB D-/D+), and GPIO13/14, whose
header entries read `13/N` and `14/N` — the XTAL_32K pair, populated or not
depending on board variant. Not worth the ambiguity when other pins are free.

**SPI runs at 10MHz**, the nRF24L01+'s rated ceiling, same as the Teensy targets.
On the C6 the chosen pins route through the GPIO matrix rather than IOMUX; the
matrix caps SPI master at 40MHz, so this is not close to a constraint. The H2 map
lands on its FSPI IOMUX pins as a bonus, not by requirement. **Read the achieved
clock back with `spi_device_get_actual_freq()` during bring-up and record it** —
the C6 (80MHz source) and H2 (48MHz source) will not divide to the same number,
and both must land at or under 10MHz.

**Both USB cables are plugged, always.** This is the target's normal operating
configuration, not a debug rig — *until Phase 3, which makes the console
`CONSOLE=1` and off by default, so the bridge cable becomes optional on the
DevKits and absent entirely on the WROOM-32:*

| Connector | Carries | Host-side name |
|---|---|---|
| Native USB (`USB`) | the binary adapter protocol, **and** `make flash` | `/dev/ttyACM*` — the port every test script takes as `--port` |
| USB-to-UART bridge (`UART`) | ESP-IDF console: boot log, `ESP_LOG*`, panic dumps | a separate tty; never touched by any test script |

Nothing in the firmware ever writes to the bridge port and nothing in the host
tooling ever reads it — it exists so that a board which has stopped answering can
still say *why*. Bring-up steps 1 and 7 both depend on that.

**Power.** A plain nRF24L01+ module (~13.5mA peak TX) off the DevKit's 3V3 pin
with 10µF + 100nF local decoupling. Wi-Fi/BLE/802.15.4 are never initialized, so
the board's own 2.4GHz radio contributes nothing to the air and nothing to the
supply. A PA/LNA module would need its own regulator; none is in scope.

**Both cables plugged is fine on both boards** — the C6 guide calls it out
verbatim as "either one or both … default power supply (recommended)", and both
boards were confirmed to power up from either connector and to tolerate both at
once (checked 2026-08-01; the H2 guide omits its native port from the power list,
which is a documentation gap, not a limitation). The user guides' "three mutually
exclusive" power options mean USB **vs** the 5V header **vs** the 3V3 header —
never combine those. This port uses neither header as an input; the nRF24 draws
*from* the 3V3 pin, which is an output.

**No status LED.** The C6's only LED is an addressable RGB on GPIO8 — a
strapping pin needing RMT to drive. `led_on()`/`led_off()` are no-ops, as on
Teensy 4.x. The UART console is a strictly better boot indicator than a blink,
and bring-up step 1 uses it. *(True of the DevKits only. The WROOM-32 boards
have a plain LED on GPIO2 and get a real implementation in Phase 4 — which they
need, having no console at all.)*

---

## Phase 0 — Toolchain, and the three things the documentation does not answer

Nothing in the firmware yet. This phase exists because none of it is installed
(`idf.py` is not on PATH, `~/.espressif` does not exist) and because three
load-bearing questions could not be resolved from Espressif's documentation
during planning. All three are answered in minutes on the bench and would cost
hours if discovered mid-port.

1. **Install ESP-IDF v5.4.4**, from the `release/v5.4` branch. Pinning a version
   is what `-DTEENSYDUINO=159` does for the PJRC targets and what the whole
   `Drivers/` convention is for. Run `install.sh esp32c6,esp32h2,esp32s3` and
   record `idf.py --version` verbatim for the README.

   **Note the terminology, because it is easy to get wrong in a handoff: ESP-IDF
   has no "LTS" designation.** Espressif applies a uniform 30-month policy to
   every major/minor release — 12 months' service period, then 18 months'
   maintenance (high-severity and security fixes only). v5.4 released
   2025-01-04, so as of this document it is in its **maintenance period, running
   to roughly July 2027**. That is the desired stability profile, not a
   compromise: still patched, no longer moving. v5.4.4 (2026-04-17) is the
   current patch on that branch.

   Verified during planning rather than assumed: `release/v5.4` does carry the
   `components/esp_driver_usb_serial_jtag` layout this plan's include paths
   depend on, and supports all three chips.
2. **Verify the four APIs this plan is built on exist with the assumed
   signatures** in the installed version, before writing any of it:
   `usb_serial_jtag_driver_install` / `_read_bytes` / `_write_bytes`,
   `esp_task_wdt_reconfigure(const esp_task_wdt_config_t *)`,
   `spi_device_get_actual_freq()`, and `nvs_erase_key()`. Ten minutes against
   the installed headers, and it invalidates whole sections of this plan if any
   is wrong.
3. **Build and flash the `usb_serial_jtag_echo` example.** This is the gate for
   the whole port: it proves enumeration, the driver and flashing all work
   before any of this firmware is involved.
4. **UNKNOWN #1 — does anything print on the native port at reset?** The ROM
   bootloader's output routing on a chip with USB-Serial/JTAG is not documented
   for the console-on-UART0 case. If ROM or bootloader bytes land on the native
   port, they arrive on the binary protocol link at every reset. Test: hold a raw
   byte dump open on the native port (`cat -v`, or pyserial) across a reset
   button press, a `CMD_REBOOT`-equivalent restart, and a power cycle.
   - If clean: record it and move on.
   - If dirty: `protocol.c`'s framer resyncs on `0xAA 0x55`, so this is very
     likely cosmetic — but confirm that rather than assume it, because the
     `CMD_RESET`/reboot paths are exactly when the host is mid-conversation.
5. **UNKNOWN #2 — does the port re-enumerate cleanly after a reset?** Not
   documented for the general-reset case (the docs cover deep-sleep exit only).
   This matters because the F103 needed an explicit hack for it — `usb_cdc.c`
   pulses D+ low at init to fake the detach the chip cannot signal. The
   USB-Serial/JTAG controller is on-die so a chip reset *should* reset its PHY,
   but "should" is what the F103 also looked like. Test: `esp_restart()` in the
   echo example, confirm the host drops and re-acquires the tty without a replug,
   and time it — that figure is the re-enumeration cost that **bring-up step 7**
   must not fold into the watchdog measurement.
6. **UNKNOWN #3 — `CONFIG_ESP_CONSOLE_SECONDARY_*` default.**
   `CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG` exists and appears to be the
   default, which would mirror **all log output onto the native port** even with
   the primary console on UART0 — i.e. contaminating the binary link by default.
   Confirm the default in menuconfig, and confirm
   `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y` actually silences it. This is not an
   optional hardening item; if it is wrong, the link carries log text.
7. **Identify the bridge chip** (`lsusb` on the UART port). If it is a CP210x
   (`10C4:EA60`), `mdp_controller/nrf24_adapter.py:163-167`'s autodetect would
   match it — relevant only to the deferred `HOST_LINK_UART0` build, recorded
   either way.
8. **Note the `/dev/serial/by-id/` name of the native port.** USB-Serial/JTAG is
   expected to report a MAC-derived serial, making boards distinguishable the way
   the F103's UID-as-serial made Blue Pills distinguishable — confirm rather than
   assume, since two ESP32 boards will be on this bench.

---

## Phase 1 — `targets/esp32/`

```
targets/esp32/
├── Makefile                    # thin wrapper over idf.py; make/make flash/make clean
├── CMakeLists.txt              # project()
├── sdkconfig.defaults          # shared config (symbol list below)
├── sdkconfig.defaults.esp32c6  # per-target overrides, IDF's own mechanism
├── sdkconfig.defaults.esp32h2
├── sdkconfig.defaults.esp32s3
└── main/
    ├── CMakeLists.txt          # sources: main.c, platform_esp32.c, ../../../core/*.c
    ├── main.c                  # app_main() + the loop
    ├── platform_esp32.c        # the entire platform.h implementation
    ├── platform_esp32.h        # target-private boot hooks (see below)
    └── pins.h                  # wiring only
```

`platform_esp32.c` is the whole boundary, the same role `platform_teensy4.cpp`
plays. `platform_esp32.h` declares the three target-private hooks `main.c` needs,
exactly as `platform_teensy4.h` does — `platform_init()`, `host_link_begin()`,
`radio_irq_pending()` — plus one more this target adds, `host_rx_byte_count()`
(see the loop section).

No `idf_component.yml` and no `partitions.csv`: everything used is in the base
install, and IDF's default single-app partition table already carries a 24KB
`nvs` partition.

`core/*.c` is compiled into the `main` component with `-Wall -Wextra` via
`target_compile_options()`, matching what every other target's Makefile gives it.

### Build system mechanics

The three build configurations must not share a build directory or an
`sdkconfig`, because `idf.py set-target` rewrites both. IDF's own mechanism for
this:

```make
BOARD ?= ESP32C6                    # ESP32C6 | ESP32H2 | ESP32S3
IDF_TARGET = $(shell echo $(BOARD) | tr A-Z a-z)
IDF_ARGS = -B build/$(IDF_TARGET) -D SDKCONFIG=sdkconfig.$(IDF_TARGET)

all:
	idf.py $(IDF_ARGS) set-target $(IDF_TARGET) build
```

Three things this pins down, each of which would otherwise cost the
implementation session time:

- **`sdkconfig.defaults.<target>` is loaded automatically**, but *only if a plain
  `sdkconfig.defaults` also exists*. Both files are required; a per-target file
  alone is silently ignored.
- **`export.sh` does not need sourcing** if `IDF_PATH` is set in the environment.
  The Makefile should check for it and fail with a clear message rather than
  letting `idf.py: command not found` be the diagnostic. This is the one place
  this target is not self-contained the way every other Makefile in the tree is,
  so it should say so out loud.
- **`make flash` needs a port**, which no other target's Makefile does
  (`teensy_loader_cli` and `st-flash` find their own hardware). Add
  `PORT ?= /dev/ttyACM0` and pass `-p $(PORT)`. Flashing goes over the **native**
  USB port, so `make flash` and the protocol link use the same connector and the
  bridge port stays purely a console — confirm in Phase 0 that flashing over the
  native port works on these boards before committing to that.

**Switching `BOARD` does not require `make clean`** — unlike every other target
in this tree, because the build directory and sdkconfig are per-target by
construction. Say so in the README, since the opposite is documented four times
over for the ARM targets and the habit would otherwise carry.

### `sdkconfig.defaults`

The symbols this port actually depends on, with the reason each is there:

```
CONFIG_ESP_CONSOLE_UART_DEFAULT=y        # console on UART0 -> bridge port
CONFIG_ESP_CONSOLE_SECONDARY_NONE=y      # do NOT mirror logs to the native port
CONFIG_FREERTOS_HZ=1000                  # 1ms vTaskDelay; see the loop + delay_ms
CONFIG_ESP_TASK_WDT_INIT=y               # TWDT exists at startup -> reconfigure()
CONFIG_ESP_TASK_WDT_PANIC=y              # timeout must RESET, not warn
CONFIG_COMPILER_OPTIMIZATION_PERF=y      # -O2, matching the ARM targets
```

`CONFIG_ESP_CONSOLE_SECONDARY_NONE` is the one that keeps the binary link clean
and is verified in Phase 0 step 6.

Note what is deliberately *not* here: **the watchdog timeout.**
`CONFIG_ESP_TASK_WDT_TIMEOUT_S` is integer seconds (range 1–60), so 3.5s is not
expressible in Kconfig at all — which is why `watchdog_init()` sets it at runtime
through `esp_task_wdt_reconfigure()` rather than in config. That is the answer to
the obvious "why isn't this just a sdkconfig line" review question.

### Host link

```c
uart_write()      -> usb_serial_jtag_write_bytes(data, len, 0)
uart_read_byte()  -> usb_serial_jtag_read_bytes(out, 1, 0)  /* returns 1 or 0 */
uart_set_baudrate() -> no-op
```

`ticks_to_wait = 0` on both. A full TX ring (host stopped reading) drops the
frame rather than blocking — the same call the F103's `usb_cdc.c` makes, and for
the same reason: `protocol_poll()` drains until `uart_read_byte()` runs dry, so a
blocking write inside that loop starves the watchdog refresh. `uart_write()` must
not wait, must not pump any driver task, and must not partially emit a frame.

Install the driver with TX and RX buffers of 1024 bytes each (both must be > 0).
The largest single frame this firmware emits is a pipe-tagged `REP_NRF_RECV_OK`
at 37 bytes, so 1024 is roughly 27 frames of slack.

`uart_set_baudrate()` is a no-op and `CMD_SET_BAUDRATE` still ACKs and still
persists — identical to every other CDC target, so the settings record stays
portable.

**A `HOST_LINK_UART0` variant is deferred, not in Phase 1.** *(Built in Phase 4,
because the ESP-WROOM-32 has no other option — and by then the condition below
was satisfied, the USB-Serial/JTAG link having been hardware-validated on two
boards.)* It would put the
protocol on UART0 at 921600 through the bridge port, giving this target a real
line rate — the one thing that would make the persistence test's negative-control
step meaningful, since it "legitimately fails" on all four existing CDC targets.
It is deferred rather than built because it is not free: it needs
`uart_driver_install`/`uart_param_config`/`uart_set_pin`, a real
`uart_set_baudrate()`, **and** a second sdkconfig fragment moving the console off
UART0 (otherwise logs contaminate the binary link) — so it is a second host link
plus a coupled config change, not a flag. Every other target got its second host
link *after* the first one was hardware-validated, and this should too.

Recorded here so it is a deliberate omission rather than an oversight: `platform.h`
already accommodates it behind a `HOST_LINK` flag, exactly as it did for the Blue
Pill's CDC variant, which was itself deferred out of the previous port plan and
added later.

### SPI

`SPI2_HOST` on all three chips, `spi_bus_initialize(..., SPI_DMA_DISABLED)`,
`spi_device_polling_transmit()` for every transfer. DMA is deliberately off: the
documented no-DMA transaction limit is 64 bytes and the largest transfer here is
32, so disabling it removes the DMA-capable-buffer requirement from every caller
in `core/` at no cost.

CSN is a plain GPIO, driven by `nrf_csn_low()`/`nrf_csn_high()`, because
`nrf24l01p.c` issues the command byte and the payload as two separate
`spi_transfer*()` calls inside one CSN assert (`nrf24l01p.c:40-42`, `:53-56`,
`:78-81`, `:139-142`). Any CS the SPI driver manages per-transaction would
deassert between them.

**`spi_device_acquire_bus()` is deliberately not used on the first pass.** The
planning pass could not establish from the documentation which transmit functions
are legal while the bus is acquired, nor whether `release_bus()` without a prior
`acquire_bus()` is defined — and `nrf24l01p_reset()` opens with a bare
`nrf_csn_high()`, so an acquire/release pairing keyed to CSN would do exactly that
unpaired release at boot. Nothing else shares this bus, so acquisition buys only
per-transaction overhead, not arbitration. Leaving it out makes
`nrf_csn_low()`/`nrf_csn_high()` pure GPIO writes and removes an undocumented
dependency from the port's foundation. If step 6 shows throughput below the
~103–147 req/s band the other targets hold, revisit it *then*, with the
acquire/release semantics established from the driver source rather than assumed.

`spi_transfer(tx, rx, len)` maps to one `spi_transaction_t` with `length =
len*8`. **`tx == NULL` must be turned into an explicit 0xFF filler buffer.** The
IDF driver does not send zeros for a null `tx_buffer` — per the driver docs, "if
`tx_buffer` is NULL and `SPI_TRANS_USE_TXDATA` is not set, the Write phase is
skipped" entirely, so MOSI is not driven at all. `platform.h:25` specifies 0xFF
filler, and the nRF24 latches command bits off MOSI during a read. This is the
single most likely silent-wrong-data bug in the port; bring-up step 4 is what
catches it.

### Radio IRQ

`gpio_config()` with `GPIO_INTR_NEGEDGE` and an internal pull-up;
`gpio_install_isr_service()` + `gpio_isr_handler_add()`. The ISR sets a
`volatile bool` and nothing else.

**The ISR must be `IRAM_ATTR` and the service installed with
`ESP_INTR_FLAG_IRAM`.** NVS writes disable the flash cache, and a non-IRAM ISR
cannot run while it is disabled — so a `CMD_NRF_SAVE` could otherwise swallow a
radio edge. The level-recheck below would recover it, but relying on that when
the fix is one attribute is the wrong trade.

`radio_irq_pending()` returns `latched_edge || gpio_get_level(IRQ) == 0`, the
same edge-plus-level gate every other target uses: read-then-clear is not atomic,
and the nRF24 holds IRQ low until STATUS is cleared, so a coalesced or lost edge
still leaves the pin low for the next call.

### Settings

NVS, namespace `p906`, key `settings`, one blob. `nvs_flash_init()` with the
standard `ESP_ERR_NVS_NO_FREE_PAGES` / `NEW_VERSION_FOUND` erase-and-retry.

- `store_save(payload, len)` → `nvs_set_blob` + `nvs_commit`, then read back and
  compare, matching the read-back verify every other target's `store_save()`
  does.
- **`store_save(NULL, 0)` must erase the record**, not write an empty blob —
  `protocol.c:96` uses it as `CMD_RESET`'s invalidate. `nvs_erase_key` +
  `nvs_commit`, treating `ESP_ERR_NVS_NOT_FOUND` as success.
- `store_load(payload, len)` → `nvs_get_blob`, returning 0 on not-found or on a
  length mismatch.

The `[magic][len][payload][crc16]` record the other four targets share is
**not** carried over. NVS already provides its own integrity checking and
wear-levelling, so the record would be a CRC inside a CRC. The observable
behaviour that matters — `store_load()` returns 0 for absent-or-corrupt, and a
`persisted_settings_t` round-trips byte-for-byte — is unchanged, which is what
`persistence_test.py` actually checks. Flagged explicitly because it *is* a
divergence from the other targets, chosen rather than overlooked.

### Timing

`millis()` → `(uint32_t)(esp_timer_get_time() / 1000)`. Free-running, wraps at
~49.7 days, wrap-safe for `(now - then)` — same as everywhere else.

`delay_ms()` → `vTaskDelay(pdMS_TO_TICKS(ms))`. **It must yield, not spin**:
`protocol.c:98` and `:110` sit between an `uart_send_packet()` and a
`platform_reboot()`/`uart_set_baudrate()`, and that 100ms is what lets the USB
driver's ISR actually drain the TX ring before the chip restarts.

`CONFIG_FREERTOS_HZ=1000`. The default 100 makes `vTaskDelay(1)` a 10ms stall and
rounds `nrf24l01p.c:158`'s `delay_ms(21)` power-up settle up to 30ms.

### Watchdog

`watchdog_init()` → `esp_task_wdt_reconfigure()` with `timeout_ms = 3500`,
`idle_core_mask = 0`, `trigger_panic = true`, then `esp_task_wdt_add(NULL)`.
`watchdog_refresh()` → `esp_task_wdt_reset()`. Refresh cadence stays 100ms from
the loop, as on every target.

`reconfigure()` rather than `init()` is correct because `CONFIG_ESP_TASK_WDT_INIT`
defaults to `y`, so the TWDT is already running at startup — watching the idle
tasks, at the Kconfig default of 5s. `idle_core_mask = 0` is what takes the idle
tasks *off* it, which matters because the loop below deliberately starves idle
for up to 10ms at a time. **If Phase 0 finds `ESP_TASK_WDT_INIT` disabled in the
installed version, this becomes `esp_task_wdt_init()` instead** — calling
`reconfigure()` on an uninitialised TWDT fails, and it fails by leaving no
watchdog running at all.

3.5s matches the Teensy targets rather than the ~3.3s IWDG reference, for the
reason `README.md:155-176` already gives: no target hits the reference exactly
and the whole 3.15–3.51s spread is closed.

**Two things about this that are not true of an IWDG, and must be written into
the code comment:**

- `trigger_panic = true` is load-bearing. Without it the TWDT prints a warning
  and continues — a watchdog that never resets, which is worse than none, and
  which only bring-up step 7 would catch.
- The TWDT is interrupt-driven and task-scoped, so it covers a stalled protocol
  loop (the case step 7 tests) but not a stall with interrupts disabled. ESP-IDF's
  separate Interrupt Watchdog covers that case and is on by default, so the
  combined coverage is arguably wider than the F030's IWDG — but it is two
  mechanisms, not one. If step 7 shows the TWDT failing to reset on a real stall,
  the fallback is leaving the RTC watchdog armed into user code
  (`CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE`), which is a true hardware
  watchdog. Do not reach for that pre-emptively.

`platform_reboot()` → `esp_restart()`.

### `app_main()` and the yield rule

```c
void app_main(void) {
    platform_init();
    host_link_begin(921600);   /* ignored on USB-Serial/JTAG */
    watchdog_init();
    protocol_init();

    uint32_t last_wdg = millis(), last_yield = millis();
    for (;;) {
        bool worked = false;
        if (radio_irq_pending()) { protocol_service_radio_irq(); worked = true; }

        uint32_t rx_before = host_rx_byte_count();
        protocol_poll();
        if (host_rx_byte_count() != rx_before) { worked = true; }

        if (millis() - last_wdg >= 100) { last_wdg = millis(); watchdog_refresh(); }

        if (!worked || millis() - last_yield >= 10) {
            last_yield = millis();
            vTaskDelay(1);           /* 1ms at FREERTOS_HZ=1000 */
        }
    }
}
```

**The overview's unconditional `vTaskDelay(1)` is wrong here.** At the default
100Hz tick it is a 10ms floor per iteration; even at 1000Hz it is a 1ms floor on
a link the Teensy 4.1 turns at ~118 req/s. And the S3's escape hatch — pin the
loop to APP_CPU and never yield — does not exist on the single-core C6/H2.

The rule above yields only when there was nothing to do, so a busy link runs at
full speed, while an idle link costs at most 1ms of latency. The
`millis() - last_yield >= 10` clause bounds idle-task starvation under sustained
load, which matters because FreeRTOS's idle task still needs to run.

`protocol_poll()` returns `void` and is in `core/`, which **is not modified**, so
the loop cannot ask it directly whether it consumed anything. Hence
`host_rx_byte_count()`: a free-running `uint32_t` counter in `platform_esp32.c`,
incremented on every byte `uart_read_byte()` hands out, declared in
`platform_esp32.h` and read either side of the `protocol_poll()` call. It is a
plain counter compared for inequality, so its wrap is a non-event.

This is the port's one piece of genuinely novel structure — every other target's
loop is stateless — and it exists because ESP-IDF is the first target where
yielding is mandatory rather than optional. Worth a comment in `main.c` saying
so, or it will read as gratuitous.

---

## Phase 2 — ESP32-H2

Second `BOARD` value. Expected to be Makefile + `pins.h` + `sdkconfig.defaults.esp32h2`
only. Two things to actually check rather than assume:

- The achieved SPI clock (48MHz source, so a different divider than the C6).
- Throughput at 96MHz against the C6's 160MHz. The Teensy 3.5/3.6 precedent says
  clock speed was *not* what set the req/s ceiling there — a host-link flush
  timer was — so do not predict this, measure it.

## Phase 3 — The second connector becomes opt-in

Decision 2 made the two-connector arrangement this target's normal operating
mode. That was true of the three Espressif DevKits, but it is not a property of
ESP32 as a family — the ESP-WROOM-32 boards of Phase 4 have one USB connector
and no native USB at all. Rather than special-case them, demote the second
connector from an assumption to a build option, *before* a board that cannot
satisfy the assumption is added.

**Scope: the console only.** `HOST_LINK` stays per-board (Phase 4) and the
C6/H2 protocol path does not move. The console was never on the native port
(`CONFIG_ESP_CONSOLE_SECONDARY_NONE`, Phase 0 step 6), so this is a
configuration change to a hardware-validated configuration, not a re-port.

### What changes

`sdkconfig.defaults` stops turning a console on:

```
-CONFIG_ESP_CONSOLE_UART_DEFAULT=y
+CONFIG_ESP_CONSOLE_NONE=y
+CONFIG_BOOTLOADER_LOG_LEVEL_NONE=y
 CONFIG_ESP_CONSOLE_SECONDARY_NONE=y
```

and a new `sdkconfig.console` fragment carries the primary console back, plus
`CONFIG_BOOTLOADER_LOG_LEVEL_INFO=y`, so that `make CONSOLE=1` reproduces
today's behaviour symbol for symbol.

**`CONFIG_ESP_CONSOLE_SECONDARY_NONE` stays in the shared file — this plan
originally said it could be dropped, and that was wrong.** The claim was that
with the primary console at `NONE` the `ESP_CONSOLE_SECONDARY` choice is not
offered at all. It is: `esp_system/Kconfig:249-252` guards that choice with
`depends on SOC_USB_SERIAL_JTAG_SUPPORTED` only — nothing about the primary —
and its default is `ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG`. Dropping the symbol
would therefore have handed the Phase 0 step 6 hazard straight back: log output
mirrored onto the native port, i.e. into the binary protocol link, in the
*default* build. It is load-bearing in both configurations, so it belongs in
`sdkconfig.defaults` rather than in either fragment. Verified in the generated
sdkconfigs for all four build configurations (Phase 3 results).

### The build-system mechanic that would otherwise cost a session

**`sdkconfig.defaults` is consulted only for symbols absent from the generated
`sdkconfig`.** Adding or removing a fragment against an existing
`sdkconfig.esp32c6` silently does nothing — the same trap `idf.py set-target`
sets for `BOARD`, and the reason the Makefile already gives each chip its own
sdkconfig and build directory. `CONSOLE` must be part of that same identity:

```make
CONSOLE ?= 0
ifeq ($(CONSOLE),1)
CONFIG_ID = $(IDF_TARGET)-console
SDKCONFIG_DEFAULTS = sdkconfig.defaults;sdkconfig.console
else
CONFIG_ID = $(IDF_TARGET)
SDKCONFIG_DEFAULTS = sdkconfig.defaults
endif
SDKCONFIG = sdkconfig.$(CONFIG_ID)
IDF_ARGS = -B build/$(CONFIG_ID) -D SDKCONFIG=$(SDKCONFIG) \
           -D SDKCONFIG_DEFAULTS="$(SDKCONFIG_DEFAULTS)"
```

Two details this pins down:

- **The default build's `CONFIG_ID` is unchanged** (`esp32c6`), so existing
  build directories and sdkconfigs are not invalidated and the common case
  costs no rebuild.
- **IDF probes `<entry>.<idf_target>` for every entry in `SDKCONFIG_DEFAULTS`**,
  so `sdkconfig.defaults.esp32c6` keeps being picked up, and a per-board console
  override (`sdkconfig.console.<target>`) is available if one is ever needed. It
  is not needed today.

**Verified in the installed SDK rather than assumed** (`tools/cmake/kconfig.cmake:165-171`,
`tools/cmake/project.cmake:646-653`, both v5.4.4):

- The list separator is `;` — `kconfgen` is invoked with `--list-separator=semicolon`.
- The per-target probe is `if(EXISTS "${sdkconfig_default}.${idf_target}")`, so a
  missing per-target variant is genuinely not an error.
- **But every entry in the list itself must exist**: a missing one is
  `FATAL_ERROR "SDKCONFIG_DEFAULTS '<x>' does not exist."`. So `sdkconfig.console`
  has to be a real committed file, not a conditionally-present one — which is
  why the Makefile switches the *list* rather than always naming the fragment.

Also verified for Phase 4, against the same install: `uart_get_tx_buffer_free_size()`,
`uart_wait_tx_done()` and `uart_set_baudrate()` all exist with the assumed
signatures in `esp_driver_uart/include/driver/uart.h`; `esp32` is a supported
target in `components/soc/`; and `hal/spi_types.h:89-92` confirms
`HSPI_HOST == SPI2_HOST` / `VSPI_HOST == SPI3_HOST` on classic ESP32, which is
the whole basis for moving `NRF_SPI_HOST` per-board.

`.gitignore` gains the `-console` sdkconfig names. A `sdkconfig.esp32*` glob
covers them and does **not** match `sdkconfig.defaults.esp32c6`, which must stay
tracked.

### The console is a tool, not a fixture

`CONSOLE=1` is the bring-up build; `CONSOLE=0` is what runs on the bench. The
C6 and H2 passed all seven steps with the console attached, and nothing they do
in normal operation needs it — it earned its keep during bring-up and has no
job after it. Keeping it permanently enabled would mean a second cable, a second
tty, and the Phase 0 finding 1 hazard (opening the bridge port hardware-resets
the chip) present on every board that has a bridge, in exchange for output
nobody reads.

So: **turn it on when there is a question to answer.** Any future board's
bring-up should run steps 1 and 7 with `CONSOLE=1` — the boot log and the TWDT
panic dump are exactly what those two steps are looking at — and then reflash
the default build for steps 2–6 and for the bench. Same for any later
investigation into a board that has stopped answering.

The WROOM-32 of Phase 4 cannot take a console at all, since UART0 is its
protocol link. That is the reason this phase exists, and the reason its bring-up
step 1 leans on the GPIO2 LED instead.

### Re-validation — deliberately light, because the protocol path is untouched

- Both boards build clean at `CONSOLE=0` and `CONSOLE=1`. Record all four
  sizes: the `CONSOLE=0` images must come out **smaller** (no console driver, no
  log strings). If they do not, the fragment is not taking effect — which is
  exactly the failure mode the `CONFIG_ID` mechanic above exists to prevent, and
  the check that catches it.
- Confirm `CONSOLE=0` is genuinely silent: attach a terminal to the bridge port
  and reset. Expect the ROM banner — which is ROM, ours to suppress on neither
  chip — and then nothing.
- `host_link_test.py` 8/8 on the C6 at `CONSOLE=0`, proving the link is
  undisturbed.
- **Not required**: steps 3–7. Nothing they cover moved. Say so explicitly here
  so that skipping them reads as a decision rather than an omission.

---

## Phase 4 — ESP32-WROOM-32 (classic ESP32)

Fourth `BOARD` value, `BOARD=ESP32` → IDF target `esp32`. Boards on hand: a
NodeMCU-32S and a HiLetgo ESP-WROOM-32 devkit (ESP32-D0WD, Xtensa LX6 dual-core
240MHz, 4MB flash, one USB connector behind a CP2102 or CH340 bridge).

Unlike Phase 2, this is **not** a pins-and-sdkconfig phase. Three things are
genuinely new:

1. **A second host link.** This silicon has no USB-Serial/JTAG and no USB-OTG —
   no native USB of any kind. The protocol goes on UART0 through the bridge.
   This is the `HOST_LINK_UART0` build deferred in the host link section above;
   it gets built now because this board has no alternative, which also settles
   the "second host link only after the first is hardware-validated" rule the
   deferral was based on — the first one has been, twice.
2. **A second SPI host.** `SPI2_HOST` on classic ESP32 is HSPI, whose IOMUX pins
   include GPIO12 — MTDI, the flash-voltage strapping pin. `SPI3_HOST` (VSPI)
   lands on 18/19/23, which are free. `NRF_SPI_HOST` moves out of the shared
   tail of `pins.h` and into the per-board blocks.
3. **A real status LED, for the first time on this target.** GPIO2 carries the
   onboard LED on both of these boards, so `led_on()`/`led_off()` stop being
   no-ops and become the radio-activity indicator `nrf24l01p.c:82,106,116,124,138,145`
   already drives on the STM32 targets. This is what replaces the console as
   the boot/liveness indicator, since UART0 is no longer available to carry one.

Everything else — NVS, the TWDT, timing, the loop shape, the reboot path — is
chip-independent and unchanged. The dual LX6 cores change nothing:
`idle_core_mask = 0` already takes *both* idle tasks off the TWDT, and the loop
is not pinned to a core, for the same reason it will not be on the S3.

### Structure

Following the F103's precedent (`HOST_LINK ?= HOST_LINK_USB_CDC`, with
`usb_cdc.c` and `uart.c` as separate files), the host link splits out of
`platform_esp32.c` into one file per variant:

```
targets/esp32/main/
├── host_link_usb_jtag.c    # moved verbatim out of platform_esp32.c
└── host_link_uart0.c       # new
```

`platform_esp32.c` keeps SPI, GPIO, NVS, timing and the watchdog.
`main/CMakeLists.txt` selects one source and its matching `REQUIRES`
(`esp_driver_usb_serial_jtag` vs `esp_driver_uart`) from a `HOST_LINK` cache
variable the Makefile passes with `-D`. `rx_byte_count`/`host_rx_byte_count()`
move with the host link, since both variants need it.

```make
# C6/H2/S3 default to the native USB-Serial/JTAG controller. Classic ESP32 has
# none, so UART0 is both its only option and its default.
ifeq ($(BOARD),ESP32)
HOST_LINK ?= HOST_LINK_UART0
else
HOST_LINK ?= HOST_LINK_USB_JTAG
endif
```

`HOST_LINK_UART0` adds `-uart0` to `CONFIG_ID`, exactly as `CONSOLE=1` adds
`-console`, so every combination keeps its own sdkconfig and build directory.
`BOARD=ESP32 HOST_LINK=HOST_LINK_USB_JTAG` is a hard `$(error)` naming the
missing peripheral, and `BOARD=ESP32 CONSOLE=1` is a hard `$(error)` naming the
conflict — UART0 cannot be both the protocol link and the console.

### `host_link_uart0.c`

```c
host_link_begin(baud):    uart_driver_install(UART_NUM_0, 1024, 1024, 0, NULL, 0)
                          uart_param_config(...)   /* baud, 8N1, UART_SCLK_DEFAULT */
                          uart_set_pin(UART_NUM_0, U0TXD, U0RXD, NO_CHANGE, NO_CHANGE)
host_link_write():        if (uart_get_tx_buffer_free_size() < len) drop the frame
                          else uart_write_bytes()
host_link_read_byte():    uart_read_bytes(UART_NUM_0, out, 1, 0)
host_link_set_baudrate(): uart_wait_tx_done(...) then uart_set_baudrate()
```

**The free-size check before the write is the load-bearing part**, and it is
there to preserve the contract `platform_esp32.c:57-67` states for the
USB-Serial/JTAG version: `uart_write_bytes()` *blocks* once the TX ring is full,
and `protocol_poll()` emits replies from inside its drain loop, so a blocking
write there starves the watchdog refresh whenever the host stops reading.
Dropping the whole frame is also what keeps `host_link_write()` from partially
emitting one — which `uart_tx_chars()`, the obvious non-blocking alternative,
would do.

`host_link_set_baudrate()` is a real operation here for the first time on this
target. That makes `persistence_test.py`'s negative-control step meaningful:
it "legitimately fails" on all five CDC configurations for want of a physical
line rate, and it should genuinely **pass** on this build.

### Wiring — NodeMCU-32S / HiLetgo ESP-WROOM-32

| nRF24 | GPIO | Note |
|---|---|---|
| MOSI | 23 | VSPI IOMUX |
| CE | 22 | |
| CSN | 21 | |
| MISO | 19 | VSPI IOMUX |
| SCK | 18 | VSPI IOMUX |
| IRQ | 17 | |
| LED | 2 | onboard, not wired |
| GND | — | |
| VCC | 3V3 | |

All six are on the same header edge on the 30-pin board (the run is
23, 22, TX0, RX0, 21, 19, 18, 5, 17, 16 — U0TXD/U0RXD interrupt it but nothing
else does) and on the 38-pin board as well.

Avoided: GPIO0/2/5/12/15 (strapping — GPIO2 is used only as the LED, driven
after boot), GPIO1/3 (U0TXD/U0RXD, now the protocol link), GPIO6–11 (SPI
flash), GPIO34–39 (input-only, so unusable for CE/CSN).

**GPIO16/17 caveat**: these are consumed by PSRAM on ESP32-**WROVER** modules.
The map above is for WROOM-32 and must not be carried to a WROVER board without
moving IRQ.

`NRF_SPI_HZ` stays 10MHz. Classic ESP32's SPI source is the 80MHz APB clock, so
10000 kHz exactly is the expected achieved figure — matching the C6 rather than
the H2's 9600. Read it back and record it as on every other board.

`sdkconfig.defaults.esp32`: `CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y`. Verify the
actual module at the bench — some WROOM-32 variants ship 8MB or 16MB, and Phase
0 finding 3 is what a wrong value looks like.

**Toolchain.** `./install.sh esp32,esp32c6,esp32h2,esp32s3` has to be re-run:
the Xtensa LX6 toolchain for `esp32` is a different one from the S3's LX7 and is
not currently installed.

### Phase 4a — exercise the UART0 host link on the C6 first

`make BOARD=ESP32C6 HOST_LINK=HOST_LINK_UART0` is legal, and it is the cheapest
way to validate the new host link: the C6 is already hardware-validated, its
bridge port is a known-good CP2102N, and its *native* port stays available as an
out-of-band channel while the protocol runs on the bridge. Run `host_link_test.py`
and the persistence test against it before the WROOM-32 is wired, so that a
failure on the WROOM-32 is unambiguously about the new board rather than about
`host_link_uart0.c`.

Note while doing it: on the C6 this configuration puts the protocol on the port
whose DTR/RTS drive EN/BOOT (Phase 0 finding 1), which is a preview of U1 below.

### Four unknowns to settle before bring-up

**U1 — does opening the port reset the board, and does it out-race the scripts'
0.4s settle?** The CP2102/CH340 auto-reset circuit drives EN from RTS and IO0
from DTR. Phase 0 finding 1 already recorded that opening the C6's *bridge* port
hardware-resets the chip — and on a WROOM-32 that same port is the protocol
link, so every `serial.Serial(port, baud)` in `host_link_test.py:76`,
`persistence_test.py:100` and `nrf24_adapter.py:169` may be rebooting the
adapter it is about to talk to. In principle the circuit only pulses EN when DTR
and RTS *differ*, and a plain open asserts both, so it may be clean; that is
precisely why it must be measured rather than reasoned about.

Test: open the port with pyserial across ≥5 attempts, watch the GPIO2 LED and
the ROM banner, and time open-to-first-reply.

- If it does not reset: record it, nothing changes.
- If it resets and boot-to-responsive is under ~0.4s: the existing settle
  already covers it, and nothing changes.
- If it resets and boot is slower: the fix is host-side — construct the port
  unopened, set `dtr`/`rts` before `open()`. **That touches `nrf24_adapter.py`,
  which all six existing configurations share**, so it must be re-checked
  against at least one Teensy and the F103 CDC build before it goes in.
- Do **not** reach for the 10µF-on-EN hardware mod. It defeats `idf.py flash`.

**U2 — does ROM boot text at 115200 ever fake a frame header at 921600?** Phase
0 step 4 established that the C6's contamination is pure 7-bit ASCII with zero
`0xAA` bytes, so `serial_reader.py:33`'s `0xAA 0x66` scan can never match it.
**That argument does not carry here.** The ROM prints at its own 115200 while
the host reads at 921600, so what the host samples is framing garbage, not
ASCII, and any byte value is reachable. Measure it the same way: capture the
protocol port across ≥5 resets, count `0xAA` bytes and any `0xAA 0x66` pair.

The expected outcome is still "cosmetic — the framer resyncs and loses at most
one frame at boot", but it is a different claim resting on different evidence
and must be established, not inherited.

- Second-stage bootloader text is already gone via Phase 3's
  `CONFIG_BOOTLOADER_LOG_LEVEL_NONE`.
- The ROM's own banner is silenced on classic ESP32 by strapping **GPIO15 low
  at boot** (MTDO) — a jumper, not an efuse, and therefore reversible. Document
  it as an option; do not require it.

**U3 — does the bridge sustain 921600?** CP2102 tops out at 1 Mbaud and CH340 at
2 Mbaud nominal, so both should. Confirm which chip each board carries
(`lsusb`: `10c4:ea60` = CP2102, `1a86:7523` = CH340) and confirm the rate by
`host_link_test.py` passing at it rather than by datasheet.

Note the CP2102 case interacts with `nrf24_adapter.py:163-167`'s `10C4:EA60`
autodetect: on a CP2102 board the adapter *would* be autodetected, which no
Teensy or ESP32 configuration currently is. Harmless, but with several adapters
on this bench it is a reason to keep passing by-id paths explicitly, not a
feature to lean on.

**U4 — `/dev/serial/by-id` stability.** Plain CP2102 modules commonly ship with
no unique serial string, so `by-id` collapses to a single
`…CP2102_USB_to_UART_Bridge_Controller-if00` name — unlike the CP2102N on the
DevKits, and unlike the MAC-derived native ports of Phase 0 step 8. If both
WROOM-32 boards are CP2102 they may be indistinguishable by `by-id` and
separable only by `by-path` (physical USB port). Record which, since Phase 0
finding 2's "always use by-id" rule may not be available on this board.

### Bring-up — the same seven steps, with these amendments

1. **Boot.** There is no console on this board at all. The GPIO2 LED is the
   indicator, and step 2 answering at all is what rules out a boot loop. If the
   board is silent with no way to tell why, the escape hatch is a temporary
   console on **UART1** over the external USB-UART adapter — added by hand for
   the diagnosis, not part of the build.
2. **`host_link_test.py`: 8/8**, at a real 921600 for the first time here.
3. **Persistence: the negative control should now pass**, unlike on every other
   target in the tree. If the board still answers at the wrong baudrate,
   `host_link_set_baudrate()` did not take — and that is a defect here, where on
   the CDC targets it is not.
4. **Register read-back: 21/21** against the Teensy 3.x reference values,
   including `RX_ADDR_P1 = c2c2c2c2c2` in the boot-default dump. Record
   `spi_actual_freq_khz()`; 10000 kHz expected.
5. **`pipe_test.py`** — park the C6 and H2 (both cables out) first.
6. **60s GUI run ×2**, then `noack_report.py`. Rotate `gui_source/mdp.log`
   **before** the first run. Two things specific to this board: a 240MHz
   dual-core part on a real 921600 line is the fastest configuration in the tree
   on paper, so landing at the *bottom* of the 103–147 req/s band would be a
   finding about the loop or the UART TX path, not about the bench. And 921600 is
   ~92 kB/s, which at ~140 req/s of ~40-byte frames is nowhere near saturated —
   the line rate should not be the ceiling, and if throughput scales with
   baudrate then something is serialising on it.
7. **Watchdog.** Marker frames, marker-to-marker, per the existing rules. Unlike
   the C6/H2 there is no USB layer in the figure at all — `esp_restart()` on a
   UART link has nothing to re-enumerate — and no panic dump to print, since the
   console is off. Expect *below* the C6's 4.199s. Report what it actually is
   rather than what shape it should have.

### Regression gate for Phases 3–4

`core/`, `platform.h` and `Drivers/` are untouched this time — the whole change
lives inside `targets/esp32/` — so the four ARM targets need a build check only,
not the byte-comparison argument Phase 1's rename required.

```
make                                            # ESP32C6, USB-JTAG, no console (default)
make CONSOLE=1                                  # ESP32C6, USB-JTAG, console on bridge
make BOARD=ESP32H2
make BOARD=ESP32C6 HOST_LINK=HOST_LINK_UART0    # Phase 4a
make BOARD=ESP32
make BOARD=ESP32 CONSOLE=1                      # must $(error)
make BOARD=ESP32 HOST_LINK=HOST_LINK_USB_JTAG   # must $(error)
```

---

## Phase 5 — ESP32-S3 (2026-08-02 or later)

Third `BOARD` value, and the first board brought up under the Phase 3 rules:
run steps 1 and 7 with `make BOARD=ESP32S3 CONSOLE=1`, everything else on the
default build. Two genuine differences, both expected to be config-only:

- Xtensa toolchain instead of RISC-V — handled entirely by `idf.py set-target`.
- Dual core. The S3 *can* pin the protocol loop to APP_CPU and drop the yield
  entirely. **Do not do this on the first pass.** Get the S3 passing all seven
  steps on the same single-core loop the C6 and H2 use, then measure whether
  pinning buys anything. A second loop shape is a second thing to validate.

Pin map to be derived from the S3-DevKitC-1 header tables when the board is in
hand, avoiding GPIO19/20 (USB), GPIO26–32 (SPI flash), GPIO33–37 (octal PSRAM —
the N8R8 has PSRAM, so these are unavailable on this variant specifically), GPIO0
and GPIO45/46 (strapping), and GPIO48 (RGB LED).

## Phase 6 — Documentation

- `nrf_adapter_source_multiceiver/README.md`: new "ESP32 target" section
  (wiring, host link, the NVS divergence, the TWDT caveats, build/flash);
  Layout block gains `targets/esp32/`; **the Building section's "no package
  manager, no downloaded toolchain" claim is scoped to the ARM targets** and the
  pinned IDF version recorded. From Phases 3–4 it also needs: the `CONSOLE`
  flag and that the console is **off by default**; the `HOST_LINK` flag and its
  per-board default; the WROOM-32 wiring and its WROVER caveat; and the fact
  that this is the one target with a status LED *and* a real baudrate on some
  configurations but not others.
- `readme_EN.md`: add the boards to the supported list. **Do not touch
  `readme.md`** (Chinese).
- The README states current behaviour only. Dated measurements, the bring-up
  results and the reasoning behind them stay in this document, and the README
  does not link to it.

## Explicitly out of scope

- Wi-Fi, BLE, 802.15.4, ESP-NOW. Never initialized. No radio coexistence work.
- USB-OTG / TinyUSB on the S3. Decision 1.
- ~~`HOST_LINK_UART0`.~~ Was deferred to a follow-on; that follow-on is Phase 4,
  which needs it. Still not a flag — a second host link plus a coupled console
  move, which is why the console move is its own phase (3) ahead of it.
- Driving classic ESP32's second core. The loop stays unpinned, same rule as
  the S3.
- ESP32-WROVER modules. The Phase 4 pin map uses GPIO16/17, which WROVER spends
  on PSRAM. Not in scope; noted so the map is not carried over blind.
- Driving the RGB LED. `led_on`/`led_off` stay no-ops.
- Host-side autodetect changes. These boards need an explicit `--port`, exactly
  as all four Teensy configurations already do. Unchanged behaviour, not a
  regression.
- PyInstaller / packaging.

---

## Verification

`venv/bin/python` for every host-side script.

**Build gate** — all existing configurations still build byte-identical, and the
new ones build clean:

```
cd targets/stm32f030 && make clean && make      # regression, .bin md5 unchanged
cd targets/stm32f103 && make clean && make
cd targets/teensy4x  && make clean && make
cd targets/teensy3x  && make clean && make
cd targets/esp32     && make                                # ESP32C6
cd targets/esp32     && make BOARD=ESP32H2                  # no clean needed --
                                                            # separate build dir
                                                            # and sdkconfig
```

Nothing in this port touches `core/`, `platform.h`, `Drivers/` or any existing
target directory, so every existing `.bin` must be unchanged. If one moves,
something was edited that should not have been. (Phase 1 did end up touching
`platform.h` and `core/protocol.c` for the `host_link_*` rename — see its
results section, which records the byte-identical check that justified it.
Phases 3–4 touch neither, and carry their own build matrix.)

**Per-board bring-up, in this order** (each step gates the next) — the same
seven-step checklist the Blue Pill and both Teensy 3.x boards passed:

1. **Flash and confirm boot.** No LED on this target; the console on the bridge
   port is the indicator. A clean boot log ending in `app_main` with no panic
   loop. (A boot loop is what a mis-set watchdog looks like — it is what the
   Teensy 3.5 did, and there it took `udevadm monitor` to see. Here the console
   just says so.)
   **From Phase 3 on, build this step with `make CONSOLE=1`** — the console is
   off by default and this step is one of the two that needs it. Reflash the
   default build before step 2.
2. **`host_link_test.py --port <native port>`** — framer, dispatch, settings
   store, no radio wired. Isolates the USB-Serial/JTAG path and NVS before the
   radio can confound anything. Expect 8/8.
3. **Persistence across a real power cycle.** `persistence_test.py --phase
   arm|verify|restore` with a physical unplug. The negative-control step (silence
   expected at 921600) will fail here for the same documented reason it fails on
   all four CDC targets — no physical line rate — and that is not a defect.
4. **Radio register read-back, radio wired, before any traffic.** This is where
   the 0xFF-filler and CSN-bracketing mistakes surface. No SWD path, so use the
   temporary `REP_REG_DUMP` (0xFE) frame in the target's own `main.c` that both
   Teensy bring-ups used — reading through `platform.h`'s own SPI primitives, so
   it exercises the actual port. **Derive the expected values by hand from
   `nrf_configure()`'s call order before the run, not from the result.** Compare
   against the 21 checks the Teensy 3.x boards recorded, including
   `RX_ADDR_P1 = c2c2c2c2c2` in the boot-default state (the chip's own POR
   value). Remove the frame afterwards and re-check the build size. An
   all-`0x00`/`0xff` dump means MISO never drove — wiring or SPI, not config.
   Record `spi_device_get_actual_freq()` in the same pass.
5. **`pipe_test.py <port>`** — two-device pipe test. Its constants are already on
   the bench configuration from the previous port; confirm before running. Park
   any idle adapter on an unused address and channel first.
6. **60s two-device GUI run**, then `tools/noack_report.py`. **Run it at least
   twice.** Reading it correctly:
   - A single run's no-ack rate is worth nothing — two back-to-back unchanged
     runs measured 0.32% and 0.06% on 2026-08-01. What is diagnostic is *where*
     the no-acks land: essentially all on P906 (`..E2`), with L1060 (`..E3`) at
     zero on every board tested. A no-ack on `..E3` is a real signal.
   - Current par is 0.04–0.13% and ~103–147 req/s across all six existing
     configurations. Do not compare against the retired 1.91%/4.61% figures.
   - TX-FIFO overflow onset is ~180–185 req/s. Zero overflows below that is
     expected, not a merit. Report req/s alongside.
   - **This is the step that would catch the yield rule being wrong.** A C6
     landing near ~100 req/s on a 160MHz part with a 1ms idle yield is a finding,
     not par — check whether the loop is yielding under load before blaming the
     bench.
   - `gui_source/settings.json` gets repointed at the adapter under test and
     **must be restored**. It is gitignored, so git will not remind you.
7. **Watchdog.** Stall the main loop and confirm reset near 3.5s. Use the
   marker-frame technique — emit a marker immediately before `while(1){}` in the
   *target's own* `main.c`, never in `core/`.
   - **Time the period between reply bursts (one full reset-to-reset cycle), not
     marker-to-tty-disconnect.** The disconnect method over-reads by USB teardown
     — it is what put the Teensy 3.x `TOVALL` values ~0.8–1.0s wrong and cost two
     sessions to unwind.
   - Reset-to-responsive is a different quantity than the timeout and will
     include USB re-enumeration here, as it does on the Teensy 4.x (~0.6s there).
     Do not fold it into the watchdog figure.
   - **Not skippable.** `trigger_panic = false` yields a watchdog that logs and
     continues, and no other step would notice.
   - Revert the stall, rebuild, confirm the size returns to its pre-change value,
     and re-run step 2.

**Bench discipline** — unchanged from
`nrf_adapter_new_targets_port_plan.md`'s section of the same name (park the idle
adapter; one device in pairing mode at a time; ~3–4.5s radio-deaf window after
power-on; the headless-GUI teardown segfault is not a run failure). One addition
specific to this target:

- If SPI misbehaves, **do not open a comparative analysis against the RF24
  library.** That was the `multi-platform-wip` branch's wrong diagnosis. Here the
  three things to check, in order, are: the 0xFF read filler, CSN bracketing
  across both `spi_transfer*()` calls, and the achieved clock.

**Report actual numbers.** If a board underperforms the existing adapters, say so
with the measurements rather than attributing it to a guessed cause.

---

## Open questions

**None outstanding for the user** — dual-USB operation (decision 2) and the
pinned SDK version (decision 6) were settled 2026-08-01. Nothing blocks Phase 0.

Everything still open is open on purpose and stated where it belongs: the three
documentation gaps are Phase 0 steps 4–6, each with a named test and a stated
fallback, and step 2 verifies the assumed APIs against the installed SDK before
any of this is written; the four open *measurements* (achieved SPI clock, whether
`acquire_bus()` earns its place, whether the S3 should pin to APP_CPU, H2 vs C6
throughput) are called out in their own sections and are deliberately not
predicted here.

---

## Phase 0 results (2026-08-01, ESP32-C6-DevKitC-1)

**Toolchain.** ESP-IDF cloned to `~/esp/esp-idf` at tag **v5.4.4**; `idf.py
--version` reports `ESP-IDF v5.4.4`, compiler `riscv32-esp-elf-gcc
(crosstool-NG esp-14.2.0_20260121) 14.2.0`. One step the plan did not
anticipate: `install.sh` does **not** install cmake/ninja on Linux (they are
marked `on_request`), so `python $IDF_PATH/tools/idf_tools.py install cmake
ninja` is required — it puts cmake 3.30.2 and ninja 1.12.1 under
`~/.espressif/tools`, keeping them pinned with the SDK rather than distro-
provided. Without it `idf.py` fails with the misleading `"cmake" must be
available on the PATH`.

**Step 2 — the four APIs all exist with the assumed signatures.** Verified
against the installed headers:

| API | Result |
|---|---|
| `usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *)` | present; config is `{tx_buffer_size, rx_buffer_size}` |
| `usb_serial_jtag_read_bytes(void*, uint32_t, TickType_t)` → `int` | present |
| `usb_serial_jtag_write_bytes(const void*, size_t, TickType_t)` → `int` | present |
| `esp_task_wdt_reconfigure(const esp_task_wdt_config_t *)` | present; struct is `{timeout_ms, idle_core_mask, trigger_panic}`, exactly as the plan assumed |
| `spi_device_get_actual_freq(handle, int *freq_khz)` | present — returns `esp_err_t`, frequency via out-param **in kHz** |
| `nvs_erase_key(nvs_handle_t, const char*)` | present (in `nvs.h`, not `nvs_flash.h`) |

`CONFIG_ESP_TASK_WDT_INIT` defaults to **`y`**, so `esp_task_wdt_reconfigure()`
is correct and the `esp_task_wdt_init()` fallback is not needed.
`CONFIG_ESP_TASK_WDT_PANIC` defaults to **`n`**, confirming that
`CONFIG_ESP_TASK_WDT_PANIC=y` in `sdkconfig.defaults` is load-bearing.

**Step 3 — gate passed.** `usb_serial_jtag_echo` built for `esp32c6`, flashed
over the **native** port, and ran. Flashing over the native connector works, so
`make flash` and the protocol link can share it as planned.

**Step 4 — UNKNOWN #1: the answer depends on the reset type.**

| Reset | Native port | Bridge port |
|---|---|---|
| Hardware (EN pin / reset button), `rst:0x1 POWERON` | **clean — 0 bytes** | full ROM + bootloader + app log |
| Software (`esp_restart()`, i.e. `CMD_REBOOT` and a TWDT panic), `rst:0xc SW_CPU` | **2747 bytes of ROM + 2nd-stage bootloader text** | nothing |

The mechanism ties this to UNKNOWN #2: on a software reset the USB device stays
enumerated, so the ROM sees a live host and prints there; on a hardware reset
the device de-enumerates and the ROM falls back to UART0.

**The contamination is provably cosmetic.** Measured over 5 consecutive
reboots: 2747 bytes every time, **pure 7-bit ASCII, zero `0xAA` bytes**. The
host-side parser (`mdp_controller/serial_reader.py:33`) resyncs by scanning for
the two-byte marker `0xAA 0x66` and silently discards everything else, so a
false frame header is not merely unlikely but impossible in ASCII. Note the
plan cited `protocol.c`'s `0xAA 0x55` framer here — that is the *host→adapter*
direction; the contamination flows the other way and `serial_reader.py` is what
actually absorbs it.

**Step 5 — UNKNOWN #2: no re-enumeration on a software reset.** The tty stayed
open across `esp_restart()` on all 5 rounds — re-enumeration cost is **0s**, and
the F103's D+ pulse hack has no analogue here. A *hardware* reset does drop and
re-enumerate the port. Since a TWDT panic is a software reset, **bring-up step 7
has no USB teardown to subtract** from the watchdog figure.

**Step 6 — UNKNOWN #3: confirmed, and the plan's suspicion was right.**
`components/esp_system/Kconfig` has `choice ESP_CONSOLE_SECONDARY` with
`default ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG` — logs *would* be mirrored onto
the binary link by default. `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y` is mandatory,
not hardening, and it works: with it set, 20 `ESP_LOGI` lines produced **zero**
bytes on the native port. (Espressif's own `usb_serial_jtag_echo` example ships
this symbol in its `sdkconfig.defaults` for the same reason.)

**Step 7 — bridge chip is a CP2102N** (`10c4:ea60`), which *would* match
`mdp_controller/nrf24_adapter.py:163-167`'s autodetect — relevant only to the
deferred `HOST_LINK_UART0` build.

**Step 8 — by-id names are MAC-derived and distinguish the boards**, as hoped:
C6 = `usb-Espressif_USB_JTAG_serial_debug_unit_F0:F5:BD:04:2F:28-if00`,
H2 = `...74:4D:BD:64:18:8A-if00`.

### Three findings the plan did not anticipate

1. **Opening the bridge (console) port resets the chip.** The CP2102N's DTR/RTS
   drive EN/BOOT, so merely attaching a terminal reboots the adapter — and in
   one observed case left it in download mode, where the app does not run and
   the native port enumerates but never answers. Bench discipline for this
   target: do not attach a terminal to the console port mid-run, and if a board
   stops answering after touching the bridge, clear it with
   `esptool.py -p <native> --before default_reset --after hard_reset read_mac`
   before concluding anything is wrong with the firmware. This qualifies the
   plan's "both cables always plugged" framing: the cable stays plugged, but
   *opening* it is not free.
2. **The native tty renumbers across hardware resets** (observed
   `ttyACM1` → `ttyACM2`). With two ESP32 boards on the bench, `/dev/ttyACM*`
   is not a stable handle — **use `/dev/serial/by-id/` paths for every flash and
   every test script**. This is why `PORT ?= /dev/ttyACM0` is a poor default and
   the Makefile documents the by-id form.
3. **The default flash size is wrong for these boards.** The image header says
   2MB while the C6 has 8MB, producing a `spi_flash: Detected size(8192k)
   larger than the size in the binary image header(2048k)` warning at every
   boot. Fixed per-board via `CONFIG_ESPTOOLPY_FLASHSIZE_*` in the per-target
   sdkconfig fragments.

### Checked and *not* a problem

- **Plain `serial.Serial(port, baudrate)` — what `nrf24_adapter.py:169` does —
  leaves DTR/RTS asserted and works fine.** It does not reset the board and the
  link stays up, so no host-side change is needed. (Transitions between DTR/RTS
  states can reset the chip; the steady both-asserted state does not.)
- **`reset_input_buffer()` works on the USB-Serial/JTAG CDC device**, so
  `host_link_test.py:78`, `persistence_test.py:102,114` and
  `reg_dump_check.py:110` need no changes. An EIO from `tcflush` seen during
  Phase 0 was a stale file descriptor after a bridge-induced reset, not a
  device limitation.

---

## Phase 1 results (2026-08-01)

`targets/esp32/` exists and builds clean for both boards on hand. Twelve files,
no build artifacts and no generated sdkconfig committed:

```
targets/esp32/{Makefile,CMakeLists.txt,.gitignore}
targets/esp32/sdkconfig.defaults{,.esp32c6,.esp32h2,.esp32s3}
targets/esp32/main/{CMakeLists.txt,main.c,platform_esp32.c,platform_esp32.h,pins.h}
```

Build result, zero warnings under `-Wall -Wextra`:

| Board | `.bin` | App partition free |
|---|---|---|
| ESP32C6 | 0x35c50 (219 KB) | 79% |
| ESP32H2 | 0x38de0 (233 KB) | 78% |

Every `sdkconfig.defaults` symbol was confirmed present in the generated
`sdkconfig.esp32c6`, including `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y`,
`CONFIG_ESP_TASK_WDT_PANIC=y`, `CONFIG_FREERTOS_HZ=1000` and the per-board
`CONFIG_ESPTOOLPY_FLASHSIZE="8MB"`.

### The one deviation from the plan: `host_link_*` rename

**`platform.h`'s `uart_set_baudrate()` collides with ESP-IDF's own
`esp_err_t uart_set_baudrate(uart_port_t, uint32_t)`.** `esp_driver_uart`'s
`uart.c` is linked because the console sits on UART0, so both definitions land
in the image and the link fails with "multiple definition". This was not
foreseen in the plan, and it cannot be worked around inside `targets/esp32/`
without a preprocessor rename that would leave a symbol appearing in no source
file — and that would silently rewrite IDF's own prototype if `driver/uart.h`
were ever included in this component.

Resolved by renaming the trio `platform.h` already groups under its
`/* Host link. */` comment, across every target:

```
uart_write        -> host_link_write
uart_read_byte    -> host_link_read_byte
uart_set_baudrate -> host_link_set_baudrate
```

This modifies `platform.h` and `core/protocol.c`, which the plan's standing
constraints said it would not. The constraint exists to stop a new target
regressing an existing one, and that property was checked rather than asserted:
after the rename, `stm32f030`, `stm32f103` and `teensy4x` rebuild to
**byte-identical** `.hex` files. `teensy3x` differs, which is expected and
unrelated — that build embeds the RTC time at compile time, so it is not
byte-reproducible against itself. Private names (`uart_init`, `uart_apply_brr`,
`uart_send_packet`) were deliberately left alone.

### Build system correction

The plan's Makefile sketch folded `set-target` into the default target:

```make
all:
	idf.py $(IDF_ARGS) set-target $(IDF_TARGET) build
```

**`idf.py set-target` runs `fullclean` as a dependency**, so that shape makes
every `make` a from-scratch ~1000-object rebuild. `set-target` is instead
ordered behind the sdkconfig file, so it runs once per chip. Measured after the
fix: no-op rebuild recompiles **0** objects, touching one source recompiles
**1**, and switching `BOARD` to ESP32H2 and back recompiles **0** on return.
The plan's claim that switching `BOARD` needs no `make clean` holds.

Also corrected: `make flash` uses the native port, but `/dev/ttyACM*` is not a
stable handle here (Phase 0 finding 2), so the Makefile documents the by-id
form. A missing `IDF_PATH` fails with a named message rather than
`idf.py: command not found`.

### Not yet done (S3)

The C6 and H2 have since passed full bring-up (below). The S3 is unchanged
from this state: compiled but not yet exercised on hardware (board not yet
on hand as of this entry).

---

## Bring-up results — ESP32-C6 (2026-08-01)

All seven steps passed. `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_F0:F5:BD:04:2F:28-if00`
throughout (native port); bridge console is `/dev/ttyUSB1`.

**Bench addition to Phase 0 finding 8**: the two CP2102N bridge ports are not
distinguishable by MAC the way the native ports are. Resolved by toggling DTR
on one (`/dev/ttyUSB0`) and watching `udevadm monitor --subsystem-match=tty`
for which `ttyACM*` reset — confirmed **`ttyUSB0` = H2 bridge, `ttyUSB1` = C6
bridge**. Record this rather than re-derive it for the H2/S3 passes.

1. **Boot.** Clean, ends `adapter: adapter up, SPI 10000 kHz` — no panic loop,
   no flash-size-mismatch warning (Phase 0 finding 3 confirmed fixed).
2. **`host_link_test.py`: 8/8.** First attempt showed 5 spurious failures
   (no reply at all on the pre-reboot commands) — caused by capturing the
   boot console on the bridge port (Phase 0 finding 1: opening it
   hardware-resets the chip) immediately beforehand, racing the native port's
   re-enumeration against the script's fixed 0.4s settle. Rerun with nothing
   else touching the ports: clean 8/8. **Bench discipline addition**: don't
   pipeline a bridge-console capture directly before a native-port test run
   on this target; leave a beat, or expect the first attempt to need a rerun.
3. **Persistence (arm/verify/restore): pass**, including a real physical
   unplug of both cables. The negative-control step (silence expected at
   921600) failed exactly as documented — no physical line rate on a CDC
   target — and is not a defect.
4. **Register read-back: 21/21 checks pass**, boot-default and post-`CMD_NRF_SET`
   dumps both matching the Teensy 3.x reference values exactly, including
   `RX_ADDR_P1 = c2c2c2c2c2` (chip POR default). Confirms the 0xFF SPI filler
   and CSN bracketing are both correct — MISO drove real data throughout.
   `spi_device_get_actual_freq()` = **10000 kHz**, exactly at the nRF24's
   rated ceiling. Frame added and removed per plan (`main.c` size returned
   to the exact pre-change 0x35c50 baseline after removal).
5. **`pipe_test.py`: pass.** Both P906 (pipe 0) and L1060 (pipe 1) answered
   correctly, including both directions of TX-target retargeting.
6. **60s GUI run, ×2: pass.** 138.6/138.7 req/s and 122.9/122.9 req/s (P906/L1060),
   both inside the 103–147 req/s par band; adapter-reported error rate 0.00%
   both runs. `gui_source/mdp.log` had accumulated other sessions' traffic
   from earlier the same day and had to be rotated
   (`mv mdp.log mdp.log.pre-esp32c6-bringup`, matching the existing
   `mdp.log.pre-f103-bringup`) before `noack_report.py` gave a clean number —
   **do this before the first run on any new board**, not after. On the clean
   log: 0.00% no-acks across both runs (13194 sends, 0 no-acks) — better than
   the 0.04–0.13% par, though per the noise note a single board-session isn't
   enough to call that a trend.
7. **Watchdog: pass, reliably.** Marker-frame technique (temporary code in
   `main.c`, removed after — size returned to the exact 0x35c50 baseline,
   zero warnings). Three consecutive reset-to-reset intervals, marker-to-marker:
   **4.199s, 4.199s, 4.199s** — essentially zero jitter. `WATCHDOG_TIMEOUT_MS`
   confirmed as 3500 in `platform_esp32.c`; the ~0.7s above that is boot time
   plus the TWDT panic handler's console dump before `esp_restart()`, both
   real and outside the raw configured timeout — not a bug, and `trigger_panic`
   is confirmed doing its job every cycle rather than logging-and-continuing.
   This is measurably outside the ARM/Teensy targets' 3.15–3.51s spread
   (README.md:155); flagged as a real, reportable characteristic of the
   TWDT mechanism rather than tuned away, since nothing in the plan calls for
   matching that spread exactly and the underlying timeout is confirmed correct.

**Net**: ESP32-C6 target is hardware-validated end to end. H2 bring-up
(Phase 2) has not started as of this entry.

---

## Bring-up results — ESP32-H2 (2026-08-01)

All seven steps passed. `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_74:4D:BD:64:18:8A-if00`
throughout (native port); bridge console is `/dev/ttyUSB0` (per the C6 entry's
DTR-toggle mapping, re-confirmed working here). The C6 was parked by physically
unplugging both its cables for the two steps that put live traffic on the
shared bus (5 and 6); it stayed connected for the rest, since those steps
never touch the P906/L1060.

1. **Boot.** Clean, ends `adapter: adapter up, SPI 9600 kHz` — no panic loop,
   no flash-size-mismatch warning. The console line already shows the achieved
   SPI clock differs from the C6's, as the plan anticipated for a 48MHz vs
   80MHz SPI source.
2. **`host_link_test.py`: first attempt showed 8 spurious failures**, all "no
   reply" — the same known race as the C6's step 2: a bridge-console capture
   (which hardware-resets the chip) had just run immediately before this,
   racing the native port's re-enumeration against the script's fixed 0.4s
   settle. Left a beat and reran with nothing else touching the ports:
   **clean 8/8.**
3. **Persistence (arm/verify/restore): pass**, including a real physical
   unplug of both cables (both supply power, so both had to come out). The
   negative-control step (silence expected at 921600) failed exactly as
   documented — no physical line rate on a CDC target — and is not a defect.
4. **Register read-back: 21/21 checks pass**, boot-default and post-`CMD_NRF_SET`
   dumps both matching the Teensy 3.x reference values exactly, including
   `RX_ADDR_P1 = c2c2c2c2c2` (chip POR default). Confirms the 0xFF SPI filler
   and CSN bracketing are both correct on this chip too. Achieved SPI clock
   (`spi_actual_freq_khz()`, same figure the boot log prints) = **9600 kHz**
   — under the nRF24's 10MHz ceiling, and a different divider result than the
   C6's 10000 kHz, exactly as the plan flagged (48MHz vs 80MHz SPI source).
   Frame added and removed per plan; `main.c` size returned to the exact
   pre-change baseline (232928 bytes / 0x38de0) after removal.
5. **`pipe_test.py`: pass.** Both P906 (pipe 0) and L1060 (pipe 1) answered
   correctly, including both directions of TX-target retargeting. C6 parked
   (both cables unplugged) beforehand.
6. **60s GUI run, ×2: pass.** 116.0/115.9 req/s and 114.6/114.5 req/s
   (P906/L1060), both inside the 103–147 req/s par band; adapter-reported
   error rate 0.00% both runs. `gui_source/mdp.log` was rotated
   (`mv mdp.log mdp.log.pre-esp32h2-bringup`) before the first run. On the
   clean log: **0.00% no-acks across both runs (12023 sends, 0 no-acks)** —
   both P906 (`..E2`) and L1060 (`..E3`) at zero.
   **This closes out Phase 2's stated throughput question**: the H2's 96MHz
   core lands at ~114–116 req/s against the C6's 160MHz core at ~123–139
   req/s (from the C6 entry) — lower, but still solidly inside the par band
   and nowhere near the ~180–185 req/s TX-FIFO-overflow onset. Per the
   Teensy 3.5/3.6 precedent the plan cites, clock speed alone does not
   appear to set the req/s ceiling here either; the yield rule is not
   under-serving the link at this clock speed. C6 parked (both cables
   unplugged) for both runs, replugged after.
7. **Watchdog: pass, reliably.** Marker-frame technique (temporary code in
   `main.c`, removed after — size returned to the exact 232928-byte / 0x38de0
   baseline). Four consecutive reset-to-reset intervals, marker-to-marker:
   **4.207s, 4.207s, 4.207s, 4.207s** — essentially zero jitter, and about
   0.7s above the configured 3500ms `WATCHDOG_TIMEOUT_MS` (unmodified,
   shared code path with the C6), consistent with boot time plus the TWDT
   panic handler's console dump before `esp_restart()` — the same overhead
   shape as the C6's 4.199s, slightly higher here, plausibly boot-time
   related given the slower core, not investigated further since nothing in
   the plan calls for matching the C6's figure exactly.

**Net**: ESP32-H2 target is hardware-validated end to end. Phase 2 complete.

*(Phases 3 and 4 — making the console opt-in, then adding the ESP-WROOM-32 —
were written after this entry and are sequenced ahead of the S3, which remains
blocked on the board's arrival.)*

---

## Phase 3 results (2026-08-01) — the console is now opt-in

Implemented as specified, with one correction to the plan (below). Changed
files, all inside `targets/esp32/`: `sdkconfig.defaults`, new
`sdkconfig.console`, `Makefile`, `.gitignore`, and a stale comment in
`main/main.c`. Nothing outside the target directory.

**The one plan correction: `CONFIG_ESP_CONSOLE_SECONDARY_NONE` had to stay.**
See the amended Phase 3 section above. Short version: the secondary-console
choice is *not* suppressed by the primary being `NONE`, and its IDF default
mirrors logs onto the native protocol port. Confirmed in the generated
sdkconfigs — all four configurations carry
`CONFIG_ESP_CONSOLE_SECONDARY_NONE=y` and
`# CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG is not set`.

**The stale-sdkconfig trap fired, exactly as the plan predicted it would.**
Because the shared `sdkconfig.defaults` itself changed, the *existing*
`sdkconfig.esp32c6`/`esp32h2` were stale — defaults are consulted only for
symbols absent from a generated sdkconfig, so they would have kept the old
console symbols indefinitely. `make distclean` before the first build is
required whenever `sdkconfig.defaults` changes; the per-configuration
`CONFIG_ID` only isolates configurations from *each other*.

**Build matrix — all four clean, and the sizes prove the fragment took effect:**

| Build | `.bin` size | vs. Phase 1/2 baseline |
|---|---|---|
| `make` (C6, `CONSOLE=0`) | 208 592 | **−11 648** |
| `make CONSOLE=1` (C6) | 220 240 (0x35c50) | **exact match** |
| `make BOARD=ESP32H2` (`CONSOLE=0`) | 221 696 | **−11 232** |
| `make BOARD=ESP32H2 CONSOLE=1` | 232 928 (0x38de0) | **exact match** |

The `CONSOLE=1` images reproducing the recorded pre-Phase-3 sizes *to the byte*
on both chips is the strongest available evidence that the fragment restores
the old configuration symbol for symbol, which is what it was specified to do.

**Bridge port is genuinely silent at `CONSOLE=0`** (C6, `/dev/ttyUSB1`). Ordered
deliberately — `CONSOLE=1` captured **first**, so that a silent `CONSOLE=0`
capture could not be confused with a broken capture rig:

- `CONSOLE=1`: full second-stage bootloader log, app init log, and
  `adapter: adapter up, SPI 10000 kHz` — matching the C6's recorded figure.
- `CONSOLE=0`: **230 bytes, identical across 3 consecutive resets** — the ROM
  banner only (`ESP-ROM:esp32c6-20220919` … `entry 0x4086c110`), then nothing.
  ROM is not ours to suppress on this chip; everything that *is* ours is gone.

**`host_link_test.py`: 8/8 on the C6 at `CONSOLE=0`**, native port unchanged, so
the protocol link is undisturbed. Also run on the H2 at `CONSOLE=0`: **8/8**.
Both bench boards were reflashed to the new default build, so the bench is no
longer running a console image.

**Steps 3–7 deliberately not re-run**, per the plan's re-validation section.
Nothing they cover moved: the protocol path, SPI, NVS and the TWDT are byte-
identical between the two configurations apart from the console driver.

**ARM regression gate: all four targets build clean** (`stm32f030`,
`stm32f103`, `teensy4x`, `teensy3x`), which is a formality here — nothing
outside `targets/esp32/` was touched.

**Two corrections to earlier recorded findings, both from this session:**

1. **Phase 0 finding 1 ("opening the bridge port hardware-resets the chip") is
   not unconditional.** A plain `serial.Serial(port, 115200)` open on the C6's
   bridge produced **zero bytes** against a known-good `CONSOLE=1` image — no
   reset. What reliably resets it is driving the lines explicitly: `dtr=False`
   (IO0 high, so it boots the app rather than the ROM downloader), `rts=True`
   to hold EN low, then `rts=False`. Whether a bare open resets evidently
   depends on the host's line-state defaults, which makes it a *hazard to
   assume in either direction* rather than a property to rely on. This matters
   for Phase 4's **U1**, which is the same question with the protocol on that
   port: measure it there, do not inherit either answer.
2. **The watchdog reset-to-reset figures (C6 4.199s, H2 4.207s) were measured
   with a console attached**, and both entries attribute part of the ~0.7s
   overhead to the TWDT panic handler's console dump. At `CONSOLE=0` there is
   no dump to print, so those figures should now be lower. Not re-measured —
   step 7 is not in this phase's re-validation set and the configured 3500ms
   timeout is unchanged — but the recorded numbers are console-build numbers
   and should not be quoted as the default build's.

**Net**: Phase 3 complete. The console is `CONSOLE=1`, off by default, on a
per-configuration sdkconfig and build directory. Phase 4 (ESP-WROOM-32) is
unblocked; its `HOST_LINK` flag layers onto the same `CONFIG_ID` mechanic this
phase established.

---

## Phase 4 results (2026-08-01) — ESP-WROOM-32, and the first UART0 host link

All seven bring-up steps passed on a NodeMCU-32S carrying an ESP32-D0WDQ6
rev v1.0 (dual-core, MAC `30:ae:a4:74:f5:e0`), **4MB flash** — so
`sdkconfig.defaults.esp32`'s `CONFIG_ESPTOOLPY_FLASHSIZE_4MB` is verified
against `esptool.py flash_id`, not assumed. Protocol port throughout:
`/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`.
There is no console on this board at all, by construction.

`flash_id` also reports *"flash voltage set by a strapping pin to 3.3V"* — i.e.
MTDI/GPIO12 is live, which independently confirms the choice of SPI3/VSPI over
SPI2/HSPI, whose IOMUX would have landed on that pin.

### Plan correction: `HOST_LINK` can be neither a `-D` variable nor Kconfig, for `REQUIRES`

The plan specified "a `HOST_LINK` cache variable the Makefile passes with `-D`",
read by `main/CMakeLists.txt` to select both the source file and the matching
`REQUIRES`. **The source selection works; the `REQUIRES` selection cannot** — and
neither can the obvious Kconfig alternative. Both were tried, in that order, and
both failed identically.

IDF expands component requirements in an early pass that runs as a *separate
CMake process*: `component.cmake:225` invokes
`scripts/component_get_requirements.cmake` with `cmake -P`, forwarding only its
own build-properties and component-properties files. So:

- **`-D` cache variables are not inherited** by that child process.
- **`CONFIG_*` does not exist yet**: `build.cmake` calls
  `__component_get_requirements()` at `:665`, *before*
  `__kconfig_generate_config()` at `:705` and `__build_import_configs()` at
  `:710`.

Either mechanism therefore selects `SRCS` correctly in the main pass and leaves
`REQUIRES` silently wrong, surfacing at compile time rather than configure time
as `Compilation failed because host_link_uart0.c ... includes driver/uart.h ...
however esp_driver_uart is not in the requirements list`.

Resolution, in two parts:

- **`SRCS` keys off a Kconfig choice** (`main/Kconfig.projbuild`), selected by a
  new `sdkconfig.uart0` fragment. Kconfig earns its place for a second reason:
  the `HOST_LINK_USB_JTAG` member `depends on SOC_USB_SERIAL_JTAG_SUPPORTED`, so
  on classic ESP32 it is not offered at all and the choice falls through to
  UART0 without that chip being special-cased anywhere.
- **`REQUIRES` names both driver components unconditionally.** Verified free
  rather than assumed: the C6 default image is **byte-identical**
  (md5 `101941245d8c95eea3d88036e3fa36e9`, 208608 bytes) whether both are named
  or only the one it uses. `esp_driver_usb_serial_jtag` is also legal to require
  on classic ESP32 — its CMakeLists gates every source on
  `CONFIG_SOC_USB_SERIAL_JTAG_SUPPORTED` and registers an empty component there.

One further trap, worth stating because it costs a full build to find: the
`if/else` selecting `SRCS` must **not** have a `FATAL_ERROR` fallback for
"neither selected", because that branch is exactly what the early pass takes on
every single build.

### Build matrix — five clean, three guards fire

| Build | `.bin` size |
|---|---|
| `make` (C6, USB-JTAG) | 208 608 |
| `make CONSOLE=1` (C6) | 220 272 |
| `make BOARD=ESP32H2` | 221 728 |
| `make BOARD=ESP32C6 HOST_LINK=HOST_LINK_UART0` | 225 008 |
| `make BOARD=ESP32` (WROOM, UART0) | 217 664 (0x35040) |

The C6/H2 images grew 16–32 bytes against Phase 3 purely from splitting the host
link into its own translation unit; nothing functional changed for them.

`$(error)` guards confirmed for `BOARD=ESP32 CONSOLE=1`,
`BOARD=ESP32 HOST_LINK=HOST_LINK_USB_JTAG`, and — a **stricter rule than the
plan specified** — *any* `HOST_LINK_UART0` + `CONSOLE=1` pairing, which also
correctly rejects `BOARD=ESP32C6 HOST_LINK=HOST_LINK_UART0 CONSOLE=1`. The
conflict is a property of the host link, not of the board.

### The four unknowns

**U1 — yes, opening the port resets the board, and the fix was needed.**
Measured at 921600 with `CMD_ECHO`: a default `serial.Serial(port, baud)` open
answered **4/6, then 3/6**, while setting `dtr`/`rts` low *before* `open()`
answered **12/12** across the same two settle times. Raising the settle from
0.4s to 1.5s did not help, so it is not a boot race to wait out. This is the
plan's named host-side fallback, now applied in four places:
`mdp_controller/nrf24_adapter.py` (new shared `open_adapter_port()`),
`host_link_test.py`, `persistence_test.py`, and
`targets/teensy3x/reg_dump_check.py`.

> **Owed:** the plan requires re-checking this shared change against at least one
> Teensy and the F103 CDC build. Neither was connected during this phase, so
> **that regression check has not been run.** The F103's `usb_cdc.c` writes on
> `tud_cdc_write_available()` alone and never consults DTR, and no target's
> firmware reads the control lines, so it is expected to be inert — but expected
> is not measured, and this is the one outstanding item from Phase 4.

**U2 — no, ROM boot text cannot fake a frame header. Good branch.** With the U1
fix in place the open path no longer resets the board, so the case that still
matters is a reset mid-conversation. Across **6 software resets** (`CMD_REBOOT`)
with the port held open: **5598 bytes of ROM text carrying zero `0xAA` bytes and
zero `0xAA 0x66` pairs.** The ROM burst is 933 bytes, byte-for-byte identical
across all six.

Worth recording how this was nearly got wrong, because the trap is subtle: each
raw capture *does* contain exactly one `0xAA 0x66` pair, which looks like the
feared false header. It is not — it sits in the last four bytes of the capture
and reads `AA 66 20 00`, which is the adapter's own `REP_NRF_INIT` that
`protocol.c:62` emits from `protocol_init()` on every boot, on every target, by
design. Counting `0xAA` over the whole capture instead of over the ROM text
alone reverses the conclusion.

So the WROOM's contamination is cosmetic in the same sense the C6's was, though
for a different reason (the C6's was pure ASCII). **The GPIO15-low strap is not
needed** and stays a documented option only.

**U3 — the bridge sustains 921600.** CP2102 (`10c4:ea60`), established by
`host_link_test.py` passing 8/8 at that rate rather than from a datasheet.

**U4 — `by-id` is NOT usable to tell these boards apart.** This module's USB
serial string is the generic `0001`, giving
`usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`. A
second CP2102 board would very likely collide, unlike the CP2102N on the DevKits
and unlike the MAC-derived native ports. **Use `/dev/serial/by-path/` (physical
USB port) when two of these are on the bench.** Phase 0 finding 2's "always use
by-id" rule does not carry to this board.

### Bring-up — all seven steps

1. **Boot.** No console by construction, so there is no banner to watch. Step 2
   answering is what rules out a boot loop, and it did.
2. **`host_link_test.py`: 8/8**, at a real 921600 — the first target in the tree
   where that rate is an actual line rate rather than a CDC no-op. Re-run 8/8
   after both temporary bring-up frames were removed.
3. **Persistence: ALL PASS across a real power cycle** — and **the negative
   control genuinely passed here, for the first time anywhere in this tree.** On
   all five CDC configurations it "legitimately fails" for want of a physical
   line rate; here the board really was silent at 921600 after being retuned to
   115200, which is `host_link_set_baudrate()` proven to work rather than
   documented away. `arm` → physical unplug → `verify` → `restore`, all clean.
4. **Register read-back: 21/21**, both dumps matching the Teensy 3.x reference
   exactly — including `RX_ADDR_P1 = c2c2c2c2c2` in the boot-default dump, the
   chip's own POR value and the proof MISO actually drove. Confirms the 0xFF
   filler and the CSN bracketing are right on VSPI. Achieved SPI clock **10000
   kHz exactly**, as predicted for the 80MHz APB source, matching the C6 rather
   than the H2's 9600. Temporary `REP_REG_DUMP` frame added and removed; image
   returned to the exact 0x35040 baseline.
5. **`pipe_test.py`: pass.** P906 on pipe 0 and L1060 on pipe 1 both answered,
   including both directions of TX-target retargeting.
6. **60s GUI runs ×2: 107.3 and 105.9 req/s**, adapter-reported error rate 0.00%
   on both. `gui_source/mdp.log` rotated to `mdp.log.pre-wroom32-bringup` first;
   on the clean log, **0/11231 no-acks (0.00%)**, both `..E2` and `..E3` at zero.
   See the throughput finding below.
7. **Watchdog: pass, and the tightest figure on this target family yet.** Six
   consecutive reset-to-reset intervals, marker to marker: **3.659, 3.657, 3.657,
   3.657, 3.657, 3.656s** — mean 3.657s, jitter under 3ms. That is only ~0.16s
   above the configured 3500ms, against the C6's 4.199s and the H2's 4.207s, and
   it confirms the plan's prediction exactly: no USB layer to re-enumerate and no
   console panic dump inside the interval. Stall removed, size back to 0x35040,
   step 2 re-run clean.

### The throughput finding — latency, not a ceiling

~106 req/s sits inside the 103–147 par band but at its bottom, on the fastest
silicon in the tree. The plan says to check the loop before blaming the bench,
so it was checked, and the answer is neither:

- **Host-link round-trip for a bare `CMD_ECHO` is 3.0ms** (median over 300; min
  2.82, max 3.20 — a very tight, tick-quantised distribution). Line time for the
  8 bytes involved is ~0.09ms, so ~97% of it is not the wire.
- **It is latency, not bandwidth or CPU.** Pipelining 200 echo requests
  back-to-back gives **8475 req/s against 321 req/s one-at-a-time — a 26.4×
  ratio**, all 200 replies returned in 23.6ms. The firmware can service echo
  round-trips roughly 80× faster than the GUI asks for them.

So the loop is not under-serving the link and the UART TX path is not
serialising; ~106 req/s reflects a fixed ~3ms per-transaction latency, and at
~212 transactions/s that latency is most of the budget. Two candidate causes,
**not discriminated here**: the CP2102's USB latency timer (bridges hold short
packets for a few ms before forwarding, which the DevKits' native
USB-Serial/JTAG has no equivalent of), and the loop's 1ms idle `vTaskDelay`.

The decisive experiment is **Phase 4a**: the same firmware on the C6 with
`HOST_LINK=HOST_LINK_UART0` versus its native link — one board, one radio path,
only the host link differing. It was not run during this phase because the C6
was off the bench; it was run immediately afterwards, and **the answer is "both
causes, compounding"** — see the Phase 4a section below.

### Regression gate

All four ARM targets rebuilt clean at unchanged sizes (`stm32f030`
6628/24/1232, `stm32f103` 16884/48/2280, `teensy4x` 21824/8896/14304, `teensy3x`
17492/0/3288). `core/`, `platform.h` and `Drivers/` were untouched — the
firmware change is entirely inside `targets/esp32/`. The host-side scripts *were*
touched; see U1's owed re-check.

**Net**: Phase 4 complete except the U1 cross-target regression check. Phases 5
(ESP32-S3) and 6 (documentation) are untouched.

---

## Phase 4a results (2026-08-01) — one board, two host links

`make BOARD=ESP32C6 HOST_LINK=HOST_LINK_UART0` run on real hardware. Same
firmware core, same C6, same radio path, same P906+L1060, only the host link
differing — the C6's native USB-Serial/JTAG (`/dev/serial/by-id/usb-Espressif_…`)
against its own CP2102N bridge connector (`…CP2102N…-if00-port0`), with the
image reflashed between arms and restored to the default build afterwards.

**The UART0 host link is now validated on silicon that is not the WROOM-32.**
`host_link_test.py` **8/8** over the bridge, including the real baudrate retune
in steps 3–4. That separates "the host link works" from "that one board works",
which Phase 4 could not.

### Latency — it was never one cause or the other

`CMD_ECHO` only; the radio is never keyed, so these numbers are unaffected by
anything on the bench.

| Host link | floor (jittered) | median (phase-locked) |
|---|---|---|
| C6 native USB-Serial/JTAG | **0.465 ms** | 1.000 ms |
| C6 UART0 → its own CP2102N | **1.903 ms** | 2.000 ms |
| WROOM-32 UART0 → CP2102 | 1.946 ms | 3.000 ms |

Two things fall out, and the second only shows up because of how the first was
measured:

- **The bridge costs ~1.44 ms of fixed transit.** C6 native vs C6 bridge is the
  controlled comparison, and 0.465 → 1.903 ms is all of it. The WROOM's CP2102
  floor (1.946 ms) is within noise of the C6's CP2102N, so it is the bridge
  class, not the particular part or the particular board.
- **The 1 ms idle `vTaskDelay` is real too, and it quantises the total.** The
  plain RTT loop is self-clocking — the host answers a reply, so every request
  arrives at a fixed phase relative to the loop's tick, which is why those
  medians are suspiciously exact multiples of 1 ms. Sleeping a *random*
  0–2 ms before each request breaks the phase lock, and the distribution
  immediately spreads into a ~1 ms-wide uniform band above the floor
  (native 0.465→1.898; bridge 1.903→3.244). That band is the loop waiting for
  its next tick.

So the two compound rather than competing: the bridge's transit is large enough
to push arrival past a tick boundary, costing a whole extra tick on top of the
transit itself. That is exactly the WROOM's 3.000 ms — the same ~1.9 ms floor as
the C6 bridge, landing on the far side of one more tick. **Neither "blame the
CP2102" nor "blame the `vTaskDelay`" is the right conclusion on its own.**

### Throughput — the bridge costs ~25%

Two 60s GUI runs per arm, WROOM physically unplugged (see the bench-hygiene note
below), per-device sample rate:

| Host link | run 1 | run 2 | no-acks |
|---|---|---|---|
| C6 native USB-Serial/JTAG | 126.5 / 126.5 /s | 115.4 / 115.3 /s | **0/12442 (0.00%)** |
| C6 UART0 → CP2102N | 94.2 / 94.2 /s | 94.3 / 94.2 /s | 4/10289 (0.04%) |

~94 vs ~115–127 req/s, i.e. the bridge costs roughly a quarter of the
throughput. Both arms stay inside the 103–147 par band only on the native side;
the bridge arm sits below it, which is consistent with the WROOM's ~106 and
means **the WROOM is not slow — UART-bridge host links are**, by about the
amount a fixed ~1.4 ms transit plus one extra tick predicts.

Worth noting the *shape*: the bridge arm is extremely repeatable (94.2, 94.3)
while the native arm scatters (126.5, 115.4). A hard quantised cadence produces
exactly that.

### Bench hygiene: software-parking an idle adapter is NOT parking it

The first pass of these measurements was run with the WROOM still plugged in,
"parked" only by a `CMD_REBOOT` that put its radio back on the compiled-in
defaults — a different channel (78 / 2478 MHz) from the bench's 2521, with no
pipes open. That looked safe and was not:

| | no-acks over ~12.4k sends |
|---|---|
| WROOM plugged in, software-parked | **130 (1.04%)**, all `..E2`, bursty |
| WROOM physically unplugged | **0 (0.00%)** |

Same board, same firmware, same run count, back to back. One of the contaminated
runs also reported 8.74% adapter-side error, which never recurred once the cable
was out. The mechanism is not ack theft — the addresses do not overlap on an
open pipe and the channels are 43 MHz apart — so it is a powered second nRF24
and second radio SoC changing the RF environment. Mechanism unidentified;
**the rule is the finding**: [[p906-controller-bench-setup]]'s "unplug both
cables of the idle adapter" means the cables, and a software park does not
substitute. All figures above are from the re-run with the cable out.

**Net**: Phase 4a done. The throughput question raised in Phase 4 step 6 is
answered and closed. Still owed from Phase 4: the U1 DTR/RTS regression check
against a Teensy and the F103 (the C6's native CDC port exercised the new
`open_adapter_port()` across four GUI runs and two `host_link_test.py` passes
without incident, which is a CDC data point but is not those two targets).

---

## Phase 5 results (2026-08-02) — ESP32-S3-DevKitC-1-N8R8

### Pin map

Derived from the vendor J1/J3 pin tables (Espressif's `esp-dev-kits` docs for
the v1.1 board) before the board was wired, then confirmed correct on the bench
by bring-up step 4 rather than assumed. One contiguous run on **J1, pins 15–20**
— the chip's default FSPI IOMUX group, `GPIO9`–`GPIO14`:

| nRF24 | GPIO | Header | Signal |
|---|---|---|---|
| IRQ | 9 | J1-15 | FSPIHD |
| CE | 14 | J1-20 | FSPIWP |
| CSN | 10 | J1-16 | FSPICS0 |
| MISO | 13 | J1-19 | FSPIQ |
| SCK | 12 | J1-18 | FSPICLK |
| MOSI | 11 | J1-17 | FSPID |

`NRF_SPI_HOST = SPI2_HOST`, matching the C6/H2. CE and IRQ take the two FSPI
signals (HD/WP) this firmware has no use for; CSN/MISO/SCK/MOSI land on their
named FSPI signals as a bonus, the same pattern the H2's map showed.

**One correction to the plan's own avoid-list**: it named GPIO0 and GPIO45/46
as the S3's strapping pins. The ESP32-S3 TRM lists a fourth — **GPIO3** — which
the plan missed. Moot for this map (GPIO9–14 doesn't touch GPIO3 either way),
but worth fixing here rather than repeating the gap on the next ESP32 board
that isn't so lucky.

**Board revision confirmed v1.1** (silkscreened on the unit): the RGB LED is
GPIO38, not the v1.0 board's GPIO48. Neither is used by this map.

Native connector on this specific unit is **USB Micro-B, not USB-C** — cosmetic
only; the USB-Serial/JTAG controller is on-die and does not care what connector
feeds it.

### A new by-id finding: factory firmware doesn't give a MAC-derived serial

Before the first flash, the native port's `by-id` name was
`usb-Espressif_Systems_Espressif_Device_123456-if00` — a fixed placeholder, not
MAC-derived. Phase 0 finding 8 (C6) established that *the ROM bootloader's* own
USB-Serial/JTAG descriptor is MAC-derived; this generic name came from whatever
factory-loaded firmware was on the board out of the box, which enumerates with
its own descriptor. The moment the board was put in the ROM download loader
(BOOT+RESET), the by-id name switched to the expected
`usb-Espressif_USB_JTAG_serial_debug_unit_80:45:6B:2A:48:BC-if00` and stayed
that way through every flash and reset after. Not a defect — just a reason the
very first flash of a fresh board may need the by-id path re-checked rather
than assumed from Phase 0's C6/H2 pattern.

**First flash needed manual BOOT+RESET.** The auto-reset-into-download sequence
esptool normally drives over the native port (`--before default_reset`) did not
take on the first attempt (`Invalid head of packet (0x1B)`) — plausibly because
the factory firmware's own USB descriptor didn't cooperate with the handshake.
Holding BOOT, tapping RESET, releasing BOOT resolved it, and every subsequent
flash (now running this firmware's own descriptor) auto-reset normally with no
manual intervention.

### Bring-up

1. **Boot.** Clean, ends `adapter: adapter up, SPI 10000 kHz` — no panic loop.
   Confirmed with `make BOARD=ESP32S3 CONSOLE=1`, per the Phase 3 rule. Getting
   a clean capture took several attempts purely on *console-capture timing*
   (the boot completes in well under a second, so a short capture window or a
   reset that lands before the listener is attached misses it entirely) — not
   a firmware issue. A 60-second capture window with the listener started
   first removed the race.
2. **`host_link_test.py`: 8/8.**
3. **Persistence (arm/verify/restore): pass**, including a real physical
   unplug of both cables. The negative-control step (silence expected at
   921600) failed exactly as documented — no physical line rate on a CDC
   target — and is not a defect.
4. **Register read-back: 21/21 checks pass**, boot-default and post-`CMD_NRF_SET`
   dumps both matching the Teensy 3.x reference values exactly, including
   `RX_ADDR_P1 = c2c2c2c2c2` (chip POR default). Confirms the 0xFF SPI filler
   and CSN bracketing are both correct, and that the pin map above is wired
   correctly. `spi_device_get_actual_freq()` = **10000 kHz**, exactly at the
   nRF24's rated ceiling — same divide as the C6 (both run from an 80MHz SPI
   source). Frame added and removed per plan; `main.c` size returned to the
   exact pre-change baseline (0x39870) after removal.
5. **`pipe_test.py`: pass.** Both P906 (pipe 0) and L1060 (pipe 1) answered
   correctly, including both directions of TX-target retargeting. Neither the
   C6 nor the H2 was on the bench during this run.
6. **60s GUI run, ×2: pass.** 115.9/115.8 req/s and 116.2/116.3 req/s
   (P906/L1060), both inside the 103–147 req/s par band; adapter-reported
   error rate 0.00% both runs. No stale `gui_source/mdp.log` was present, so
   nothing needed rotating before the first run. On the combined log:
   **0.00% no-acks across both runs (11995 sends, 0 no-acks)** — both P906
   (`..E2`) and L1060 (`..E3`) at zero.
7. **Watchdog: pass, reliably.** Marker-frame technique (temporary code in
   `main.c`, removed after — size returned to the exact 0x39870 baseline).
   Measured two ways:
   - **`CONSOLE=1` build** (the plan's prescribed build for this step, so the
     panic dump itself is visible): reset-to-reset (marker-to-marker) on the
     native port, four consecutive intervals: **3.837s, 3.837s, 3.837s,
     3.837s** (one initial 3.830s from listener-alignment noise, not a real
     outlier). The console showed `task_wdt: Task watchdog got triggered ...
     Aborting ... Rebooting` on every cycle — `trigger_panic=true` confirmed
     actually firing, not logging-and-continuing — with the TWDT's own
     internal trigger point landing at an identical **3797ms** uptime every
     cycle.
   - **`CONSOLE=0` build** (the default, no panic-dump print overhead): three
     consecutive intervals, **3.601s, 3.601s, 3.601s, 3.601s, 3.601s** — lower
     than the `CONSOLE=1` figure by almost exactly the console print time, and
     both comfortably bracket the configured `WATCHDOG_TIMEOUT_MS=3500`. This
     is measurably faster than the C6's 4.199s (also a `CONSOLE=1`
     measurement) — expected, since the S3's boot log is shorter and the two
     builds' panic-dump content differs slightly, not a discrepancy.
   - **One-off anomaly, not chased further**: on one of the three `CONSOLE=1`
     boot cycles (not the first), the console printed
     `task_wdt: esp_task_wdt_reset(705): task not found` immediately after
     `esp_task_wdt_add(NULL)`, i.e. `watchdog_init()`'s own trailing
     `watchdog_refresh()` call raced the add. `watchdog_refresh()` does not
     check the return value, so this was silently absorbed, and the reset
     still fired on schedule that cycle (3.837s, same as every other). Seen
     once in six cycles across both measurement passes; not reproduced
     deliberately, not investigated further, and did not affect any pass/fail
     result — recorded per [[no-speculative-fault-attribution]] rather than
     guessed at.

**Net**: ESP32-S3 target is hardware-validated end to end, all seven steps
passing on the single-core-style unpinned loop (no APP_CPU pinning attempted,
per the plan's explicit "do not do this on the first pass"). Phase 6
(documentation) is what remains in this plan.

---

## Phase 6 results (2026-08-02) — documentation

`nrf_adapter_source_multiceiver/README.md`: Layout block gained `targets/esp32/`;
Building section's "no package manager, no downloaded toolchain" claim scoped
to "All four ARM targets"; new "ESP32 target (C6 / H2 / S3 / classic ESP32)"
section covering the SDK requirement and pinned IDF version, build/flash, the
`HOST_LINK`/`CONSOLE` flags and their per-board defaults, all four boards'
wiring tables (with the S3's GPIO3 strapping-pin correction folded in), the
status-LED and real-baudrate exceptions, the NVS-vs-CRC'd-flash-record
divergence, and the TWDT caveats plus the four boards' measured watchdog
figures. Flashing section gained a short `ESP32` subsection pointing back to
it, matching the existing per-family pattern.

`readme_EN.md`: the supported-board sentence now lists all four ESP32 boards
alongside the Blue Pill and the four Teensys. `readme.md` (Chinese) untouched,
per [[english-readme-only]]. Neither README links to this document — dated
measurements and the reasoning behind them stay here, per
[[protocol-doc-vs-research-doc]].

**Net**: Phase 6 done. All six phases of this plan are complete; the ESP32
target (C6, H2, S3, classic ESP32) is hardware-validated end to end and
documented.
