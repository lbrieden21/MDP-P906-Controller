# MDP Adapter firmware — bare-metal multiceiver rewrite

Replaces the shipped adapter firmware from `ElluIFX/MDP-P906-Controller`
(STM32CubeMX + HAL + `mokhwasomssi/stm32_hal_nrf24l01p`, single nRF24 RX pipe)
with a bare-metal firmware (direct CMSIS register access, no HAL/CubeMX) that
adds real nRF24L01+ multiceiver support: each attached device (P906, L1060)
gets its own hardware RX pipe, so the host can attribute a response to a
device by pipe number instead of by packet content/timing.

The STM32F030 target is built and bench-tested against a bare
STM32F030F4Px dev board plus an external USB-UART bridge for the host link —
not the original AliExpress USB-NRF24L01 dongle module the shipped firmware
and pin mapping were recovered from (see `../readme_EN.md`); that module
isn't part of this project's hardware anymore.

## Status

- **Parity layer**: reimplements every command the shipped firmware
  supports (`REBOOT`, `RESET`, `SET_BAUDRATE`, `NRF_TX`, `NRF_SET`,
  `NRF_SAVE`, `NRF_QUERY`, `ECHO`), byte-for-byte compatible with
  `mdp_controller/nrf24_adapter.py`'s existing `CMD`/`RESPONSE` enums and
  framing (`0xAA 0x55 <cmd> <len> <data>` host→adapter,
  `0xAA 0x66 <cmd> <len> <data>` adapter→host).
- **Pipe extension**: adds `CMD_NRF_OPEN_PIPE` (0x23, pipes 1–5 only) and
  `CMD_NRF_SET_TX_TARGET` (0x24, retargets `TX_ADDR`/`RX_ADDR_P0` only,
  without wiping pipes 1–5 the way a full `CMD_NRF_SET` reset would), and
  changes `REP_NRF_RECV_OK` (0x12) to lead with a pipe-number byte (from
  `STATUS.RX_P_NO`) before the raw 32-byte nRF24 payload
  (`protocol.c`: `#define PIPE_TAG_RECV_OK 1`). `nrf24_adapter.py` and
  `bus.py` both expect and strip this pipe-tag prefix on every
  `NRF_RECV_OK` frame; flip the define back to 0 only if testing against
  pre-pipe-tag driver code.
- Compiles clean (`arm-none-eabi-gcc 14.2`, `-Wall -Wextra -Wpedantic
  -Wshadow`, one unavoidable `-Wpedantic` note on the vector table's initial
  SP entry — standard for every Cortex-M startup file).

## Why bare-metal instead of extending the HAL library

`Modules/nrf24l01/nrf24l01p.c` in the shipped firmware
(`mokhwasomssi/stm32_hal_nrf24l01p`) only ever writes `RX_ADDR_P0` —
`nrf24l01p_set_rx_address()` always targets pipe 0 regardless of its `width`
argument, and `nrf24l01p_reset()` leaves
`EN_RXADDR = 0x01` (pipe 0 only). There's no multi-pipe support to extend,
and pulling in CubeMX/HAL just to get a nRF24 register layer wasn't worth
it. Everything needed to reimplement it bare-metal was already recovered
from the shipped firmware source (`ElluIFX/MDP-P906-Controller`), not
reverse-engineered:

- **Pins** (`Core/Inc/main.h`):

| Function | Pin | Notes |
|---|---|---|
| LED | PA0 | active-low, open-drain |
| nRF IRQ | PA2 | EXTI2, falling edge, pull-up |
| nRF CSN | PA3 | |
| nRF CE | PA4 | |
| SPI1 SCK | PA5 | AF0 |
| SPI1 MISO | PA6 | AF0 |
| SPI1 MOSI | PA7 | AF0 |
| USART1 TX | PA9 | AF1 |
| USART1 RX | PA10 | AF1 |
- **Clock tree** (`MDP_Adapter.ioc`, `SystemClock_Config()`): HSI(8MHz)/2 →
  PLL×12 → 48MHz SYSCLK/HCLK/PCLK, 1 flash wait state, SPI1 /4 → 12MHz,
  USART1 921600 baud default.
- **Host protocol** (`Core/Src/main.c` `handle_uart_command`/`parse_uart_data`,
  cross-checked against `mdp_controller/nrf24_adapter.py`'s `CMD`/`RESPONSE`
  enums): framing, every command's payload layout, and the settings
  persist/query format.
- **Part**: STM32F030F4Px (`MDP_Adapter.ioc`: `Mcu.Name=STM32F030F4Px`) — 16KB
  flash, 4KB SRAM.

## Layout

```
core/                Platform-neutral application code: nRF24L01+ driver
                     (nrf24l01p.c) and UART framing + command dispatch
                     (protocol.c). No #ifdefs, no MCU headers — reaches
                     hardware only through platform.h.
platform.h           The whole core/ ↔ silicon contract: SPI transfer, the
                     four nRF24 control lines, LED, millis/delay, host-link
                     read/write/baudrate, settings load/save, watchdog,
                     reboot.
targets/stm32f030/   Bare STM32F030F4Px dev board + external USB-UART
                     bridge — this project's actual dev/test hardware, not
                     the original shipped dongle module its pin mapping was
                     recovered from. Full implementation of platform.h,
                     plus startup/vector table, clock init and main(). Each
                     peripheral file supplies its own share of the contract
                     (gpio.c the control lines, spi.c the transfers, and so
                     on); gpio.h's port/mask helpers are target-private.
    linker/          STM32F030F4Px.ld: 15KB code region + reserved last 1KB
                     flash page for settings (see flash_store.c).
    Makefile         Builds this target; run make from inside this directory.
targets/stm32f103/   STM32 Blue Pill. Modelled file-for-file on stm32f030/ —
                     same peripheral split, same flash_store.c/watchdog.c
                     near-verbatim, gpio.c/spi.c/uart.c/system_clock.c
                     rewritten for the F1's register layout and 72MHz clock
                     tree. USART1 host link only; see "STM32 Blue Pill
                     target" below.
targets/teensy4x/    Teensy 4.0 / 4.1, selected by the Makefile's BOARD
                     variable. platform_teensy4.cpp is the entire C/C++
                     boundary — every platform.h entry point in one file,
                     wrapped in extern "C"; Arduino headers appear here and
                     in no shared header. pins.h holds the wiring, main.cpp
                     the setup()/loop() boot order.
targets/teensy3x/    Teensy 3.5 / 3.6, selected the same way. Modelled on
                     teensy4x/ — platform_teensy3.cpp is the whole boundary,
                     same file roles. Two real differences from teensy4x/:
                     a working status LED (Kinetis SPI0 can move its SCK off
                     pin 13) and a Kinetis WDOG watchdog instead of an i.MX
                     one. See "Teensy 3.x target" below.
targets/esp32/       ESP32-C6, ESP32-H2, ESP32-S3 and classic ESP32
                     (ESP-WROOM-32), selected by the Makefile's BOARD
                     variable. Built against ESP-IDF (CMake+Kconfig, not this
                     tree's usual Makefile-over-a-source-list) rather than
                     vendored — see "ESP32 target" below. platform_esp32.c is
                     the whole boundary except the host link, which splits
                     into host_link_usb_jtag.c / host_link_uart0.c; pins.h
                     holds the wiring, main.c the app_main()/loop boot order.
host_link_test.py    Target-neutral host-protocol check (framing, dispatch,
                     settings persistence) shared by every board —
                     nothing in it depends on which is under test beyond the
                     port name.
tx_burst_test.py     Target-neutral radio check: several NRF_TX frames in one
                     host write, then probes that the adapter returned to RX
                     and still receives replies. Needs a paired P906.
Drivers/             Third-party trees. **Not committed to git** — git-ignored
                     and fetched on demand by `tools/fetch_vendor.py` at the
                     pins recorded in `tools/vendor.json` (see "Building"
                     below for the fetch step). Full license inventory in
                     `THIRD_PARTY.md` at the repo root.
```

| Tree | Upstream | Pin | License | Used by |
|---|---|---|---|---|
| `Drivers/CMSIS/{Include,LICENSE.txt}` | `STMicroelectronics/cmsis-core` | tag `v5.4.0` | Apache-2.0 | every STM32 target |
| `Drivers/CMSIS/Device/ST/STM32F0xx` | `STMicroelectronics/cmsis-device-f0` | tag `v2.3.7` | Apache-2.0 | stm32f030 |
| `Drivers/CMSIS/Device/ST/STM32F1xx` | `STMicroelectronics/cmsis-device-f1` | tag `v4.3.5` | Apache-2.0 | stm32f103 |
| `Drivers/teensy3` | `PaulStoffregen/cores` | commit `7f107ee0` | MIT (PJRC variant, see below) | teensy3x |
| `Drivers/teensy4` | `PaulStoffregen/cores` | commit `7f107ee0` | MIT (PJRC variant, see below) | teensy4x |
| `Drivers/teensy_libs/SPI` | `PaulStoffregen/SPI` | commit `7c83d072` | GPL-2.0-only OR LGPL-2.1-only | teensy3x, teensy4x |
| `Drivers/teensy_libs/QNEthernet` | `ssilverman/QNEthernet` | tag `v0.36.0` | **AGPL-3.0-or-later** | teensy4x `ETH=1` |
| `Drivers/tinyusb` | `hathach/tinyusb` | tag `0.21.0` | MIT | stm32f103 USB CDC |

The PJRC cores are **not** "Teensyduino 1.59, copied verbatim" despite
`-DTEENSYDUINO=159` in both Teensy Makefiles — they byte-match
`PaulStoffregen/cores` master at `7f107ee0`, post-Teensyduino-1.62. The
define stays at 159 anyway: it is what upstream's own Makefile declares at
that exact commit, the only two gates anywhere in either core are
`#if TEENSYDUINO >= 159` in `IntervalTimer.h`, and a Teensy 4.x image builds
byte-identical at 159 vs. 162. See `plans/devendoring_plan.md` for the full
A/B.

TinyUSB is vendored as an explicit per-file list, not the whole `src/` tree —
only the device-side CDC class and the `stm32_fsdev` portable driver this
repo actually builds. QNEthernet is vendored whole (`src/` + 6 root files)
because it is a header-heavy lwIP-based stack, not a handful of files.

A new target adds a `targets/<name>/` directory implementing the same
`platform.h` and nothing else — `core/` never gains a conditional, so adding
a target cannot change the code any existing target compiles.

## Design notes / deviations from the shipped firmware

- **Radio servicing is deferred to the main loop on every target.** The EXTI
  or pin ISR only clears the pending bit and latches a flag; the main loop
  calls `protocol_service_radio_irq()` before `protocol_poll()`, since a
  received payload is sitting in the RX FIFO while host bytes are already
  buffered. Servicing is gated on the IRQ pin *level* as well as the latched
  edge: reading-then-clearing the flag is not atomic, and the nRF24 holds IRQ
  low until STATUS is cleared, so a lost or coalesced edge still leaves the
  pin low and the next call picks it up. One model covers every
  configuration, so nothing in `core/` or any target reasons about being
  called from interrupt context.
- **UART**: interrupt-driven RX and TX ring buffers instead of DMA +
  idle-line detection. Simpler, no DMA driver needed. TX is TXE-interrupt-
  drained rather than polled, so `host_link_write()` hands off a frame in
  bounded time instead of stalling the main loop for the whole UART frame time.
  USART1 sits at NVIC priority 1 against EXTI's 3. That relationship used to
  be load-bearing — it is what made a spin-wait inside the radio ISR safe —
  and is now merely harmless, kept rather than reset to the default.
- **Settings persistence**: a single reserved flash page (magic + payload +
  CRC16, erase+rewrite on `NRF_SAVE`) instead of the shipped firmware's
  MiniFlashDB wear-leveling KV store. Save is a low-frequency, host-triggered
  operation (settings dialog), so page-erase endurance was never a real
  constraint here.
- **Watchdog timeout: the reference is ~3.3s, and no target hits it exactly.**
  The figure every target aims at is the shipped firmware's IWDG — `/32`,
  reload 4095, ~3.28s nominal. Each family then misses it differently, for
  hardware reasons rather than inattention, so the spread is not a sign that
  some targets were treated more carefully than others:
  - **STM32 (F030 / F103)** — IWDG runs off the uncalibrated LSI, so the real
    timeout lands near 3.15–3.19s, implying an LSI around 41–42kHz against a
    40kHz nominal and a 30–60kHz spec. There is no knob: it varies per chip
    and moves with temperature and supply.
  - **Teensy 4.x** — WDOG1's timeout granularity is a flat 0.5s, so the only
    settings near the reference are 3.0s and 3.5s. `WT=6` picks 3.5s.
  - **Teensy 3.x** — Kinetis WDOG has ~5ms granularity and could hit any of
    these. It is calibrated to 3.5s to match the 4.x rather than to the 3.3s
    reference — a rounding artifact inherited from a different chip's
    granularity, kept because it is measured and verified rather than because
    3.5s means anything.

  What the timeout actually has to satisfy is: comfortably longer than the
  100ms refresh cadence plus the worst-case loop stall, and short enough to
  recover quickly. Every target sits between 3.15s and 3.51s — a window with
  an order of magnitude of headroom on both sides. Treat that spread as
  closed; it is not worth bench time.

  Separately, **reset-to-responsive is not the same quantity as the timeout**.
  On USB CDC targets the board re-enumerates before it can answer again, which
  adds roughly 0.6s on the Teensy 4.x. That latency is not part of the
  watchdog and must not be measured into it — see `platform_teensy3.cpp`'s
  comment for the measurement trap this creates.
- **IWDG**: same prescaler/reload as shipped (`/32`, reload 4095, ~3.3s
  timeout on LSI), refreshed every 100ms from the main loop, same as before.
  `watchdog_init()` must issue the **start** key (`KR=0xCCCC`) before
  writing `PR`/`RLR` and waiting on `IWDG_SR` — that start command is what
  forces the LSI oscillator on (RM0360); waiting on `IWDG_SR` first hangs
  forever since it can never sync without LSI running. Got this backwards
  on the first pass, which parked the chip in that wait loop on every boot,
  before UART or anything else ever ran (found via a live GDB
  `target extended-remote` + `monitor halt` + backtrace over the OpenOCD/
  ST-LINK session, not guesswork — worth reaching for that immediately
  next time instead of LED-toggle bisection).
- **Multiceiver addressing**: `NRF_OPEN_PIPE` only accepts pipes 1–5, not 0.
  Pipe 0's `RX_ADDR_P0` must keep mirroring `TX_ADDR` — ShockBurst auto-ack
  replies land on pipe 0 using that address whenever the adapter itself is
  transmitting, so pipe 0 can't be reassigned to a device without breaking
  the adapter's own ACK reception. This matches the real MDP-M01 hub, which
  uses the same pipes-1–5 scheme (`base_addr + (0xE1+k)`, 5-device cap).
  Per nRF24 hardware, pipes 2–5 only have single-byte RX
  address registers — the host must keep those addresses' upper bytes equal
  to pipe 1's address (this is a chip limitation `nrf24l01p_open_rx_pipe()`
  doesn't need to enforce; it's inherent to how those registers work).
  Those shared upper bytes are written *only* by an `NRF_OPEN_PIPE` for pipe
  1 — `CMD_NRF_SET` doesn't touch `RX_ADDR_P1`, and neither does the reset it
  performs — so a host must open pipe 1 before (or instead of) relying on any
  of pipes 2–5, whether or not it has a device for pipe 1. Until it does, the
  register holds the chip's power-on default `C2:C2:C2:C2:C2` (or whatever a
  previous session left, for as long as the radio stays powered) and pipes
  2–5 receive nothing, while transmissions to those devices still ACK
  normally. `bus.py` opens pipe 1 when it constructs the bus.
- **Pipe number source**: `STATUS.RX_P_NO`, read for free off the SPI
  response byte that `R_RX_PAYLOAD` already returns — no extra SPI
  transaction, so this doesn't touch the timing-sensitive 50Hz Type-8
  polling path.

## STM32 Blue Pill target

Drop-in for the STM32F030 target in every way except the host link: same
peripheral selection (SPI1, USART1, same pins bar the LED), so an existing
nRF24 harness plugs straight in. Two host links, selected by `HOST_LINK`:

- **`HOST_LINK_USB_CDC` (default)** — native USB CDC on the Blue Pill's
  onboard USB port, via a vendored TinyUSB device stack (`Drivers/tinyusb`)
  and `usb_cdc.c`/`usb_descriptors.c`. No external bridge needed. Enumerates
  as `cafe:4001`, with the 96-bit factory UID as the USB serial number so
  boards are distinguishable in `/dev/serial/by-id`. The host must be given
  an explicit `--port`: `nrf24_adapter.py` autodetects only the CP210x
  bridge, so this build is driven the same way the Teensy targets are.
- **`HOST_LINK_USART1`** — USART1 at 921600 through an external USB-UART
  bridge, on PA9/PA10. The original path, and the only one on this chip with
  a real line rate, so it stays the reference for anything baudrate-related.

**Switching `HOST_LINK` requires `make clean` first** — it changes both the
source list and a `-D` that reaches already-built objects.

Two CDC behaviours that are by design, not faults:

- `host_link_set_baudrate()` is a no-op — USB CDC has no line rate of its own.
  `CMD_SET_BAUDRATE` still ACKs and the value is still persisted, so the
  settings record is identical to the USART1 build's; only the physical rate
  stops responding.
- `host_link_write()` drops a whole frame rather than blocking when the CDC TX
  FIFO is full, which only happens if the host has stopped reading. It must
  not wait, and in particular must not pump `tud_task()` while waiting:
  `protocol_poll()` drains until `host_link_read_byte()` runs dry, and
  `tud_task()` is also what refills the RX FIFO, so pumping from inside
  `host_link_write()` feeds the loop that is calling it and starves the
  watchdog refresh.

- **Wiring** (`targets/stm32f103/gpio.h`): identical to the F030 target
  except the LED. No AFIO remap on either peripheral.

| Function | Pin | Notes |
|---|---|---|
| LED | PC13 | active-low, open-drain, 2MHz |
| nRF IRQ | PA2 | EXTI2 |
| nRF CSN | PA3 | |
| nRF CE | PA4 | |
| SPI1 SCK | PA5 | |
| SPI1 MISO | PA6 | |
| SPI1 MOSI | PA7 | |
| USART1 TX | PA9 | |
| USART1 RX | PA10 | |
- **SPI runs at 9MHz** (`BR_1`, /8 off the 72MHz PCLK2), under the
  nRF24L01+'s 10MHz ceiling. The F030's 12MHz is above spec and was not
  carried over — the same call already made for the Teensy targets.
- **No bootloader.** These boards flash only over SWD (see Flashing below),
  unlike the original dongle module's firmware, which ships a USART/DFU
  bootloader — this target has no equivalent fallback.
- **Settings and watchdog**: `flash_store.c` and `watchdog.c` are
  near-verbatim copies of the F030 target's — same record format, same CRC16,
  same IWDG prescaler/reload (`/32`, 4095, ~3.3s on LSI) — since the F1's
  flash controller and IWDG are the same IP for these purposes. A stalled
  main loop resets the board in ~3.2s, and the settings record survives a
  power cycle.

## Teensy 4.x target (4.0 / 4.1)

A drop-in replacement for the STM32 target: same framing, same command set,
same pipe-tagged `REP_NRF_RECV_OK`, so the existing host harnesses work
unmodified apart from the port name. One target directory, `targets/teensy4x/`,
builds either board — `make` alone gives the 4.1, `make BOARD=TEENSY40` the
4.0. Nothing else differs: `pins.h` and every `platform_teensy4.*` source file
are identical on both, since pins 2/9/10/11/12/13 are valid on both boards and
the 4.0's smaller emulated EEPROM (1080 bytes vs. the 4.1's, `E2END 0x437`)
still comfortably fits the 40-byte settings record. **Switching `BOARD`
requires `make clean` first** — the define only reaches the framework objects
under `build/fw`, and `make` has no way to know they're stale otherwise.

- **Wiring** (`targets/teensy4x/pins.h`): LPSPI4 on fixed pins, SPI at 10MHz (nRF24L01+'s rated ceiling).

| Function | Pin | Notes |
|---|---|---|
| nRF IRQ | 2 | |
| nRF CE | 9 | |
| nRF CSN | 10 | |
| SPI MOSI | 11 | LPSPI4 fixed |
| SPI MISO | 12 | LPSPI4 fixed |
| SPI SCK | 13 | LPSPI4 fixed |
- **No status LED.** Pin 13 is the onboard LED *and* LPSPI4's SCK, so it is
  unavailable, and `led_on()`/`led_off()` are no-ops — they only ever drove
  cosmetic activity indication. There is no boot blink on this target, and so
  no delay between `protocol_init()` and the first poll; nothing depends on
  one.
- **Host link**: USB CDC (`Serial`) by default; `-DHOST_LINK_SERIAL1` swaps in
  `Serial1` at 921600 for exact parity with the shipped adapter. Both sit
  behind the same three `platform.h` functions, and nothing in `core/` is
  conditional on the choice. On CDC `host_link_set_baudrate()` is a no-op, but
  `CMD_SET_BAUDRATE` still ACKs and still persists the value, so the command's
  observable protocol behaviour is unchanged — only the physical link speed
  stops responding to it. `nrf24_adapter.py` needs no changes: pyserial
  ignores the baudrate for a CDC device.
- **Settings**: PJRC's flash-emulated EEPROM (~4KB), with `flash_store.c`'s
  record format unchanged — `[magic u32][len u16][payload 32][crc16]`, same
  CRC16-CCITT — so `persisted_settings_t` round-trips identically on both
  targets.
- **Watchdog**: WDOG1, driven directly rather than through Teensyduino's
  `WDT_T4` — that is a separate library needing vendoring, while `imxrt.h` (in
  the vendored core already) declares every register, and the core itself never
  touches WDOG1. Same approach as the STM32 target's `watchdog.c`. `WT=6` gives
  (6+1) × 0.5s = **3.5s**, the nearest step to the ~3.3s reference (see the
  watchdog-timeout note under Design notes for why no target hits it exactly),
  refreshed on the same 100ms cadence from `loop()`. Note `SRS` and `WDA` are
  active-low "do not assert now" controls and must be written 1, or enabling
  the watchdog resets the part immediately.

### Ethernet host link (`ETH=1`)

A third host link, 4.1-only: the protocol runs over a TCP socket instead of USB CDC or
Serial1, so the adapter can sit on the bench with nothing but power and a network cable and
the GUI drives it over IP. Nothing about the radio side changes — same binary protocol, same
multiceiver pipe routing, same unmodified `core/`. **`BOARD=TEENSY40 ETH=1` is a hard build
error** — the 4.0 has no Ethernet pads.

`ETH=1` vendors `Drivers/teensy_libs/QNEthernet` — fetch it first if it is not
already present (every other target needs nothing extra):

```sh
python3 ../../../tools/fetch_vendor.py --target teensy4x-eth
make BOARD=TEENSY41 ETH=1                         # + optional HOST_LINK=HOST_LINK_SERIAL1
teensy_loader_cli --mcu=TEENSY41 -s -w -v build/MDP_Adapter_Multiceiver.hex
```

QNEthernet is AGPL-3.0-or-later — a `ETH=1` image is a combined work under
its terms. See `THIRD_PARTY.md`.

**Wiring**: the PJRC Ethernet kit (RJ45 MagJack + DP83825I PHY) on the 6-pin ribbon header
that ships for it — no `pins.h` entries, it is not GPIO.

**The wired link keeps working on an Ethernet build — both links are served concurrently.**
Same reasoning as the ESP32's WiFi link: with DHCP there is no other way to learn the
adapter's address than asking it over the wired link first. Whichever link most recently
delivered a byte from the host becomes the active one. One TCP client at a time on port 9000
— a new connection replaces the old one rather than being refused, so a crashed or
force-quit GUI doesn't lock the adapter out.

**Addressing** is DHCP by default; a static address can be persisted instead, independent of
the radio settings record (`CMD_RESET` does not touch it), with `net_provision.py` (same
script and same `CMD_NET_*` commands the ESP32's WiFi link above uses for status/IP query):

```sh
venv/bin/python net_provision.py --port /dev/ttyACM0 --status
venv/bin/python net_provision.py --port /dev/ttyACM0 --static 192.168.1.50 --mask 255.255.255.0 --gateway 192.168.1.1
venv/bin/python net_provision.py --port /dev/ttyACM0 --dhcp
```

Once connected, the GUI reaches the adapter at `tcp://<ip>:9000` — on the host side this is
the connection dialog's transport setting, the same one the ESP32's WiFi link uses (see
`../readme_EN.md`).

**No access control**, same trust model as the wired link and the ESP32's WiFi link: anything
on the LAN that can reach the port can drive the power supply.

### Why this target uses PJRC's core when the STM32 one shed HAL/CubeMX

The i.MX RT1062 has **no internal flash** — code runs from external QSPI. That
makes two required services expensive from scratch: settings storage needs a
FlexSPI self-programming driver executing from ITCM, and USB CDC needs a full
device stack (endpoint queue heads, transfer descriptors, enumeration,
CDC-ACM). Plus a boot header / FlexSPI config block and a considerably hairier
clock/PLL bring-up than the F030's HSI→PLL→48MHz. Going fully bare-metal here
is not the same trade it was on the F030, where CMSIS register access got us
everything. The same reasoning carries to the Teensy 3.x target below — same
PJRC core, same trade.

## Teensy 3.x target (3.5 / 3.6)

Modelled directly on `targets/teensy4x/`: one target directory, `BOARD ?=
TEENSY35` in the Makefile selects the chip (`make BOARD=TEENSY36` for the
other), and `platform_teensy3.cpp` carries over the 4.x target's CSN/
transaction bracketing, byte-at-a-time SPI, and deferred-to-`loop()` IRQ
handling unchanged. **Switching `BOARD` requires `make clean` first**, same
reason as the 4.x target.

Three genuine differences from `teensy4x/`:

- **`host_link_write()` must call `HOST_PORT.send_now()`** -- load-bearing.
  `usb_serial_write()` only hands a packet to the USB hardware once it fills
  `CDC_TX_SIZE` (64B); a partial packet instead arms a 5ms flush timer
  (`Drivers/teensy3/usb_serial.c:229-234`). Every reply this firmware sends is
  well under 64B, and the host will not issue the next request until the reply
  lands, so without `send_now()` the entire link runs at that timer's cadence —
  measured, it more than halves throughput. The 4.x core has no equivalent
  per-write gate and needs nothing; the F103's TinyUSB path calls
  `tud_cdc_write_flush()` for the same reason.
- **The status LED works here.** Kinetis SPI0 can move its SCK off pin 13 —
  `SPI.setSCK(14)` before `SPI.begin()` — freeing pin 13 for the onboard LED,
  so `led_on()`/`led_off()` are real `digitalWriteFast()` calls and the
  four-blink boot indicator is back (`pins.h`: `NRF_SCK_PIN 14`).
- **The watchdog is Kinetis WDOG, not i.MX WDOG1**, driven directly rather
  than through a vendored library — `tonton81/WDT_T4` is i.MX-only, and
  PJRC's `cores/teensy3` ships no watchdog API beyond leaving the peripheral
  disabled-but-reconfigurable at boot. Configuring it from `watchdog_init()`
  means re-unlocking it (PJRC's `ResetHandler` already unlocked it once,
  before `setup()` ever runs), and that unlock only holds the register window
  open for **256 bus cycles** — interrupts are disabled across
  unlock→configure in `platform_teensy3.cpp` so nothing can miss it. The
  timeout is 3.5s — matching the 4.x target's WDOG1 timeout rather than the
  ~3.3s reference it rounds off (see the watchdog-timeout note under Design
  notes), same 100ms refresh cadence from `loop()` — but neither `CLKSRC` nor
  `TOVALL` follows
  the reference manual's clock model on this silicon: `CLKSRC` is left clear
  and `TOVALL` is calibrated per board by stall test (698 on the 3.5, 688 on
  the 3.6, both landing within ±10ms of 3.5s). See that file's comment for the
  method, including the measurement trap that makes the obvious approach
  over-read by nearly a second.

- **Wiring** (`targets/teensy3x/pins.h`): SPI0 on fixed pins with SCK moved to 14 (freeing pin 13 for LED). SPI at 10MHz (same as every other target).

| Function | Pin | Notes |
|---|---|---|
| nRF IRQ | 2 | |
| nRF CE | 9 | |
| nRF CSN | 10 | |
| SPI MOSI | 11 | SPI0 fixed |
| SPI MISO | 12 | SPI0 fixed |
| SPI SCK | 14 | moved from pin 13 for LED |
- **Settings**: PJRC's flash-emulated EEPROM, FlexNVM-backed — 4096 bytes on
  the 3.5/3.6 (`E2END 0xFFF`) — with the same record format and CRC16 as
  every other target.
- **One linker quirk not present on the 4.x target**: the vendored
  `mk20dx128.c` references `__rtc_localtime`, an external symbol the Arduino
  build environment normally supplies as the compile-time Unix timestamp to
  seed the RTC on a fresh power-up. Nothing in the vendored tree defines it,
  so the Makefile supplies it via `-Wl,--defsym=__rtc_localtime=$(shell date
  +%s)`. This is the one target whose `.bin` is not reproducible build to
  build — harmless, since only the STM32F030 and Teensy 4.x `.bin`s carry a
  byte-identical regression gate.

## ESP32 target (C5 / C6 / H2 / S3 / classic ESP32)

Five boards, one target directory, selected by `BOARD` in the Makefile:

| `BOARD` | Chip | Host link default | Status LED | Notes |
|---|---|---|---|---|
| `ESP32C5` | ESP32-C5-DevKitC-1(-N8R8) | native USB-Serial/JTAG | no-op | RGB LED on GPIO27 (strapping) |
| `ESP32C6` (default) | ESP32-C6-DevKitC-1 | native USB-Serial/JTAG | no-op | RGB LED on a strapping pin |
| `ESP32H2` | ESP32-H2-DevKitM-1 | native USB-Serial/JTAG | no-op | same reason |
| `ESP32S3` | ESP32-S3-DevKitC-1(-N8R8) | native USB-Serial/JTAG | no-op | RGB LED on GPIO38 (v1.1) / GPIO48 (v1.0) |
| `ESP32` | NodeMCU-32S / HiLetgo ESP-WROOM-32 | UART0 (its only option) | **real**, GPIO2 | no native USB at all |

Unlike the ARM targets above, **this one needs an SDK, not a vendored tree** —
ESP-IDF is CMake+Kconfig driven and multi-gigabyte, so it is pinned and
recorded rather than copied into `Drivers/`:

```sh
git clone -b v5.5.5 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf
cd ~/esp/esp-idf && ./install.sh esp32,esp32c5,esp32c6,esp32h2,esp32s3
python $IDF_PATH/tools/idf_tools.py install cmake ninja   # not optional on Linux --
                                                            # install.sh marks these
                                                            # on_request, and without
                                                            # them idf.py fails with the
                                                            # misleading "cmake" must be
                                                            # available on the PATH
```

Pinned at **v5.5.5** (`idf.py --version` → `ESP-IDF v5.5.5`, compiler
`riscv32-esp-elf-gcc (crosstool-NG esp-14.2.0_20260121) 14.2.0` for the
RISC-V chips, a matching Xtensa toolchain for the S3/classic ESP32) — same
reasoning as `-DTEENSYDUINO=159` above: a moving SDK under a
vendored-everything tree is exactly what `Drivers/` exists to prevent. The
bump from v5.4.4 was forced by the ESP32-C5: production silicon needs
ESP-IDF ≥5.5, since 5.4.x only carried preview support for beta3 chips.

### Build / flash

```sh
cd targets/esp32
source ~/esp/esp-idf/export.sh   # or: export IDF_PATH=~/esp/esp-idf
make                                          # ESP32C6, native USB-Serial/JTAG, console off
make BOARD=ESP32C5                            # no `make clean` needed -- BOARD gets its own
                                               # build dir and sdkconfig
make BOARD=ESP32H2
make BOARD=ESP32S3
make BOARD=ESP32                              # classic ESP32, UART0 host link (forced)
make CONSOLE=1                                # add the ESP-IDF console -- see below
make flash PORT=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00
make BOARD=ESP32 flash PORT=/dev/serial/by-path/<...>-port0   # bridge port; see host link below
```

`/dev/ttyACM*`/`/dev/ttyUSB*` are not stable handles with more than one board
on a bench — the native USB-Serial/JTAG port renumbers across a hardware
reset, and a plain CP2102 module often has no unique serial string at all.
Prefer `/dev/serial/by-id/` (MAC-derived on the native ports) or
`/dev/serial/by-path/` (physical-port-derived, when `by-id` collapses two
boards to the same name).

### Host link: `HOST_LINK_USB_JTAG` (default) vs `HOST_LINK_UART0`

The C5/C6/H2/S3 default to their native USB-Serial/JTAG controller — already a
CDC-ACM device, non-blocking read/write against a driver-managed ring buffer,
needs no managed components. Classic ESP32 has neither USB-Serial/JTAG nor
USB-OTG, so `HOST_LINK_UART0` (through its CP2102/CH340 bridge) is both its
only option and its forced default; `BOARD=ESP32 HOST_LINK=HOST_LINK_USB_JTAG`
is a hard build error naming the missing peripheral.

`HOST_LINK_UART0` is also buildable on the C5/C6/H2/S3
(`make HOST_LINK=HOST_LINK_UART0`) — the cheapest way to exercise the second
host link, since those boards keep their native port free as an out-of-band
channel while the protocol runs on the bridge.

Both host links share the same non-negotiable contract: `host_link_write()`
must never block. A full TX ring drops the frame rather than waiting, because
`protocol_poll()` emits replies from inside its own drain loop and a blocking
write there would starve the 100ms watchdog refresh.

**Baudrate is real on exactly one configuration.** `host_link_set_baudrate()`
is a no-op on USB-Serial/JTAG (no physical line rate to retune, same as every
CDC target elsewhere in this tree) but a genuine `uart_set_baudrate()` call on
`HOST_LINK_UART0` — the one ESP32 configuration, and the only configuration in
this whole tree besides USART1/Serial1, where `persistence_test.py`'s
negative-control step (silence expected at the wrong baudrate) is actually
expected to *pass* rather than "legitimately fail for want of a physical line
rate."

### Console: `CONSOLE=0` (default) vs `CONSOLE=1`

The ESP-IDF console — boot log, `ESP_LOG*`, panic dumps — comes out UART0 to
the DevKits' second USB connector (the bridge), a cable nothing in normal
operation reads. It is **off by default**
(`CONFIG_ESP_CONSOLE_NONE` + `CONFIG_BOOTLOADER_LOG_LEVEL_NONE`), and is a
bring-up tool rather than a running-configuration fixture: turn it on
(`make CONSOLE=1`) when there's a boot or a watchdog stall to look at, then
reflash the default build before putting the board back on the bench.
`CONFIG_ESP_CONSOLE_SECONDARY_NONE` stays on in **both** builds regardless —
without it, log output mirrors onto the native port by default on any chip
with USB-Serial/JTAG, contaminating the binary protocol link even when the
primary console is off.

`CONSOLE=1` conflicts with `HOST_LINK_UART0` (UART0 can't be both the
protocol link and the console) and is a hard build error on that combination.
Classic ESP32 (`BOARD=ESP32`) can never take a console at all, for the same
reason — its GPIO2 LED is the only boot/liveness indicator it has.

### WiFi host link (`HOST_LINK_WIFI`)

A third host link, alongside the two wired ones above: the protocol runs over a TCP socket
instead of USB-Serial/JTAG or UART0, so the adapter can sit on the bench with nothing but
power and the GUI drives it over IP. Nothing about the radio side changes — same binary
protocol, same multiceiver pipe routing, same unmodified `core/`.

| `BOARD` | WiFi | Notes |
|---|---|---|
| `ESP32C5` | WiFi 6 (2.4 / 5GHz) | dual-band part, `esp_wifi_set_band_mode()` never called so the IDF default (auto, both bands) stands; both bands exercised. `--status` reports the associated channel, which is how to tell which band it landed on |
| `ESP32C6` | WiFi 6 (2.4GHz) | |
| `ESP32S3` | WiFi 4 | |
| `ESP32` (WROOM-32) | WiFi 4 | wired link is UART0, same as `HOST_LINK_UART0` above |
| `ESP32H2` | **none** | 802.15.4 + BLE only — excluded by `depends on SOC_WIFI_SUPPORTED`, not a hand-written special case |

```sh
make BOARD=ESP32C6 HOST_LINK=HOST_LINK_WIFI      # or ESP32C5 / ESP32S3 / ESP32
make flash PORT=...                              # over the wired link, same as any other build
```

**The wired link keeps working on a WiFi build — both links are served concurrently.** This
is what makes provisioning possible in the first place: the wired link has to work in order to
set credentials before WiFi exists at all. Whichever link most recently delivered a byte from
the host becomes the active one; a TCP client connecting makes WiFi active, a disconnect hands
it back to the wired link immediately. One TCP client at a time — a new connection replaces
the old one rather than being refused, so a crashed or force-quit GUI doesn't lock the adapter
out.

**Provisioning** happens over the wired link, with `net_provision.py` (same directory as
`host_link_test.py` -- shared with the Teensy Ethernet host link, which uses the same
CMD_NET_* commands for its own status/IP query):

```sh
venv/bin/python net_provision.py --port /dev/ttyACM0 --ssid MyNetwork --password hunter2
venv/bin/python net_provision.py --port /dev/ttyACM0 --status   # SSID, DHCP address, RSSI, channel/band
venv/bin/python net_provision.py --port /dev/ttyACM0 --clear    # erase stored credentials
```

Credentials live in their own NVS key, independent of the radio settings record — `CMD_RESET`
(which invalidates the radio settings) does not touch them, and they survive a reboot or power
cycle on their own. Once connected, the GUI reaches the adapter at `tcp://<ip>:9000` (port is
Kconfig-settable, `HOST_LINK_WIFI_PORT`); on the host side this is the connection dialog's
transport setting — see `../readme_EN.md`.

**No access control.** Anything on the LAN that can reach the port can drive the power supply
— the same trust model the USB link already has, just extended over IP instead of requiring
physical access to the cable. Treat the network the adapter is provisioned onto accordingly.

`CONSOLE=1` combines freely with `HOST_LINK_WIFI` on the C5/C6/S3 (their native port is free
either way); on classic ESP32 it's a hard build error, for the same reason `CONSOLE=1` already
conflicts with `HOST_LINK_UART0` above — UART0 can't be the console and a host link (wired or
the wired half of a WiFi build) at once.

### Wiring

Every map avoids strapping pins, the USB D+/D- pair, the console UART, and
(where applicable) the addressable RGB LED. `NRF_SPI_HZ` is 10MHz — the
nRF24L01+'s rated ceiling — on every board; the achieved clock is read back
with `spi_device_get_actual_freq()` and differs by board only because the SPI
source clock does (80MHz on the C6/S3/classic ESP32, 48MHz on the H2, 160MHz
on the C5):

**ESP32-C5-DevKitC-1(-N8R8)** — `SPI2_HOST`, 10000 kHz achieved (160MHz /
16 divides exactly):

| nRF24 | GPIO | Header |
|---|---|---|
| SCK | 6 | J1-7 (FSPICLK) |
| MISO | 8 | J1-9 |
| MOSI | 9 | J1-10 |
| CSN | 10 | J1-11 (FSPICS0) |
| CE | 24 | J3-4 |
| IRQ | 23 | J3-5 |

The trio routes through the GPIO matrix rather than IOMUX (SPI2's IOMUX
MOSI/MISO are GPIO7/GPIO2, both strapping pins), capping SPI master at
40MHz — nowhere near a constraint at 10MHz. **GPIO15 (SPICS1 on any
PSRAM-fitted module) is unavailable on the -N8R8 and marked NC on that
variant's J3-6** — the non-obvious trap on this board, since every other
GPIO in the strapping/USB/flash/JTAG exclusion list is easy to spot from the
datasheet alone.

**ESP32-C6-DevKitC-1** — one contiguous J3 block, `SPI2_HOST`, 10000 kHz achieved:

| nRF24 | GPIO | Header |
|---|---|---|
| IRQ | 23 | J3-5 |
| CE | 22 | J3-6 |
| CSN | 21 | J3-7 |
| MISO | 20 | J3-8 |
| SCK | 19 | J3-9 |
| MOSI | 18 | J3-10 |

**ESP32-H2-DevKitM-1** — mixed headers, `SPI2_HOST`, 9600 kHz achieved (the
only board that doesn't land at 10000 — 48MHz doesn't divide there):

| nRF24 | GPIO | Header |
|---|---|---|
| MISO | 0 | J1-3 (FSPIQ) |
| SCK | 4 | J1-9 (FSPICLK) |
| MOSI | 5 | J1-10 (FSPID) |
| CSN | 10 | J3-4 |
| CE | 11 | J3-5 |
| IRQ | 12 | J3-7 |

**ESP32-S3-DevKitC-1(-N8R8)** — one contiguous J1 block (the chip's default
FSPI IOMUX group), `SPI2_HOST`, 10000 kHz achieved:

| nRF24 | GPIO | Header |
|---|---|---|
| IRQ | 9 | J1-15 (FSPIHD) |
| CE | 14 | J1-20 (FSPIWP) |
| CSN | 10 | J1-16 (FSPICS0) |
| MISO | 13 | J1-19 (FSPIQ) |
| SCK | 12 | J1-18 (FSPICLK) |
| MOSI | 11 | J1-17 (FSPID) |

On the -N8R8 variant, octal PSRAM claims GPIO35-37 — unused by this map, and
left disabled in `sdkconfig.defaults.esp32s3` regardless, since the largest
buffer this firmware needs is a 32-byte radio payload. ESP32-S3 strapping
pins are GPIO0, **GPIO3**, GPIO45 and GPIO46 — all four avoided, though GPIO3
is easy to miss (some documentation lists only the other three).

**NodeMCU-32S / HiLetgo ESP-WROOM-32** (classic ESP32-D0WD) — one header
edge on both the 30- and 38-pin variants, `SPI3_HOST` (VSPI — `SPI2_HOST`
here is HSPI, whose IOMUX includes GPIO12, the flash-voltage strapping pin),
10000 kHz achieved:

| nRF24 | GPIO | Note |
|---|---|---|
| MOSI | 23 | VSPI IOMUX |
| CE | 22 | |
| CSN | 21 | |
| MISO | 19 | VSPI IOMUX |
| SCK | 18 | VSPI IOMUX |
| IRQ | 17 | |
| LED | 2 | onboard, real implementation (see below) |

**Do not carry this map to an ESP32-WROVER board** — GPIO16/17 are consumed
by PSRAM there, and IRQ would need to move.

### Status LED

Every board except classic ESP32 has `led_on()`/`led_off()` as no-ops. The
DevKits' only LED is an addressable RGB one sitting on a strapping pin that
needs RMT to drive — not worth it when the console is a strictly better boot
indicator and available on the same boards. The WROOM-32 boards have neither
an RGB LED nor a console (UART0 is their protocol link), so GPIO2 gets the
real implementation `nrf24l01p.c` already pulses around every radio
operation on the STM32 targets — their only sign of life.

### NVS instead of a CRC'd flash record

Every other target in this tree persists settings as a hand-rolled
`[magic][len][payload][crc16]` record in a reserved flash page. This target
uses ESP-IDF's NVS key-value store instead (namespace `p906`, key
`settings`, one blob) — NVS already provides its own integrity checking and
wear-levelling, so the hand-rolled record would be a CRC inside a CRC. The
observable behaviour `persistence_test.py` actually checks is unchanged:
`store_load()` returns 0 for absent-or-corrupt, and a `persisted_settings_t`
round-trips byte-for-byte. `store_save(NULL, 0)` (the `CMD_RESET` invalidate
path) erases the NVS key rather than writing an empty blob.

### Watchdog: ESP-IDF's Task Watchdog, not a hardware IWDG

`esp_task_wdt_reconfigure()` (not `_init()` — the TWDT is already running at
boot, watching idle tasks at the Kconfig default) with `timeout_ms = 3500`,
`idle_core_mask = 0` and, load-bearing, **`trigger_panic = true`** — without
it the TWDT prints a warning and keeps running, which is worse than no
watchdog at all, since nothing else in this codebase would notice. The
3500ms figure lives in `platform_esp32.c`, not `sdkconfig.defaults`: Kconfig's
own `CONFIG_ESP_TASK_WDT_TIMEOUT_S` is integer seconds only (1-60), so 3.5s
isn't expressible there.

Two things about the TWDT that are not true of a hardware IWDG: it is
interrupt-driven and task-scoped, so it covers a stalled protocol loop but
not a stall with interrupts disabled (ESP-IDF's separate Interrupt Watchdog,
on by default, covers that case instead — two mechanisms, not one); and on
the DevKits, a TWDT panic prints a full backtrace to the console (when
`CONSOLE=1`) before `esp_restart()`, which is real time inside the
reset-to-reset interval, not measurement noise:

| Board | reset-to-reset | Notes |
|---|---|---|
| ESP32-C5 | 4.177s | `CONSOLE=1`, includes console panic-dump time |
| ESP32-C6 | 4.199s | `CONSOLE=1`, includes console panic-dump time |
| ESP32-H2 | 4.207s | `CONSOLE=1`, same |
| ESP32-S3 | 3.837s (`CONSOLE=1`) / 3.601s (`CONSOLE=0`) | both bracket 3500ms cleanly |
| classic ESP32 | 3.657s | no console build exists for this board at all |

All five are measured reset-to-reset (marker-frame to marker-frame), never
marker-to-tty-disconnect — the disconnect method over-reads by USB teardown
time, which is what put the Teensy 3.x figures elsewhere in this file wrong
by the better part of a second.

## Building

All four ARM targets build with plain `make` against the system
`arm-none-eabi-gcc` (14.2) — no package manager, no board manifest, no
downloaded toolchain baked into the repo. Teensyduino ships its own older
gcc, but 14.2 builds the PJRC cores unmodified. The ESP32 target is the one
exception in this tree — see "ESP32 target" above for its SDK requirement.

The one thing every ARM target *does* need first is its vendored `Drivers/`
trees, which are git-ignored rather than committed (see the pin table under
"Layout" above and `THIRD_PARTY.md` for what and why). `make` on its own
names the exact fetch to run if they're missing, but the zero-friction
default is to fetch everything once, up front:

```sh
sudo apt-get install gcc-arm-none-eabi   # arm-none-eabi-gcc 14.2, if not already installed
python3 ../tools/fetch_vendor.py          # fetches every vendored tree; see --target to fetch less

cd targets/stm32f030 && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}
cd targets/stm32f103 && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}, USB CDC
cd targets/stm32f103 && make HOST_LINK=HOST_LINK_USART1   # same dir, USART1 + external bridge
cd targets/teensy4x  && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex}, TEENSY41
cd targets/teensy4x  && make BOARD=TEENSY40      # same dir, TEENSY40
cd targets/teensy3x  && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex}, TEENSY35
cd targets/teensy3x  && make BOARD=TEENSY36      # same dir, TEENSY36
make clean                                       # any target
```

**Switching `BOARD` on `teensy4x`/`teensy3x` requires `make clean` first** —
the define only reaches the framework objects under `build/fw`, and `make`
has no way to know they're stale otherwise. The same applies to switching
`HOST_LINK` on either Teensy target, and on `stm32f103`, where it also
changes which source files are compiled.

To build a Teensy target against `Serial1` instead of USB CDC:

```sh
cd targets/teensy4x   # or targets/teensy3x
make clean && make HOST_LINK=HOST_LINK_SERIAL1
```

## Flashing

### ESP32 (C5 / C6 / H2 / S3 / classic ESP32)

`make flash` (see "ESP32 target" above for `PORT`, `BOARD`, `HOST_LINK` and
`CONSOLE`) — over the native USB-Serial/JTAG connector on the C5/C6/H2/S3, or
the bridge connector (the only one there is) on classic ESP32. No separate
flashing tool: the Makefile shells out to `idf.py flash`, which drives
`esptool.py` itself.

### Teensy 4.0 / 4.1 / 3.5 / 3.6

```sh
sudo apt-get install teensy-loader-cli
cd targets/teensy4x   # or targets/teensy3x
make flash    # teensy_loader_cli --mcu=<board> -s -w -v build/*.hex
```

`-s` soft-reboots into the bootloader over USB, so no button press is needed
as long as the running firmware still enumerates as USB serial; `-w` waits for
the board. Drop `-s` and press the button if the board is wedged or running a
non-`USB_SERIAL` build.

Then run the host-protocol check (no radio needed — it covers framing,
dispatch, and settings persistence across a reboot). It's target-neutral and
lives at the firmware root, one level above every `targets/<name>/` dir:

```sh
../../../venv/bin/python ../../host_link_test.py --port /dev/ttyACM0
```

When comparing two adapters against the same devices, park the idle one on
an unused address and channel first. Any normal run leaves an adapter in RX on
the bench address, where it will auto-ACK packets meant for the device under
test — this measurably corrupts throughput and error-rate measurements.

### STM32F030 / STM32F103 Blue Pill

Both flash the same way — via ST-LINK V2 over SWD, no bootloader on either
board. Wire SWCLK/SWDIO/GND/3V3 from the ST-LINK to the target; SWD uses
PA13/PA14, which this firmware never touches on either board (see pin
mapping above), so there's no conflict with the app pins. (The original
dongle module also has a UART bootloader, documented in `readme_EN.md`'s
BOOT0/3V3-short method — irrelevant here, since SWD needs neither.)

```sh
cd targets/stm32f030   # or targets/stm32f103

# stlink-tools (st-flash)
sudo apt-get install stlink-tools
st-flash write build/MDP_Adapter_Multiceiver.bin 0x08000000

# or OpenOCD -- swap target/stm32f0x.cfg for target/stm32f1x.cfg on the Blue Pill
sudo apt-get install openocd
openocd -f interface/stlink.cfg -f target/stm32f0x.cfg \
  -c "program build/MDP_Adapter_Multiceiver.elf verify reset exit"
```

If the target's read/write protection is set, `st-flash` will refuse to
write — run `st-flash erase` first (mass-erases and drops RDP back to
level 0), or `openocd ... -c "stm32f0x unlock 0; reset halt"` (`stm32f1x` on
the Blue Pill) with OpenOCD.

On the F103's CDC build, `st-flash reset`, `CMD_REBOOT` and a watchdog reset
all re-enumerate the port on their own — `usb_cdc.c` pulses D+ low at init to
fake the detach the F103 cannot signal itself. No replug needed. A debugger
halt (`nrf_regdump.gdb`) does drop the tty for the duration; a reset
afterwards brings it back.

## Refreshing a vendored tree

`Drivers/` holds pinned third-party copies, fetched by `tools/fetch_vendor.py`
at the pins recorded in `tools/vendor.json` (see "Building" above). Refresh
for a specific core-level bug, not on a schedule — the whole point of pinning
is that the build does not move under you.

**Mechanics, any tree**: bump that tree's `pin` (tag or commit) in
`tools/vendor.json`, then

```sh
python3 ../tools/fetch_vendor.py --tree <name> --force   # re-fetch just that tree
python3 ../tools/fetch_vendor.py --update-hashes          # re-record tree_sha256
```

then rebuild every configuration that tree feeds and re-run the per-board
checklist. The one exception is `cores-teensy4`'s `imxrt1062_t41.ld` patch
(see "The one local patch" in `plans/devendoring_plan.md`): the script
asserts its `find` string occurs exactly once before applying it, so a pin
bump that moves that line fails the fetch loudly instead of silently dropping
the patch — do not carry the patch forward by hand without finding out why it
stopped matching.

**PJRC cores (`Drivers/teensy3`, `teensy4`, `teensy_libs/SPI`)** are pinned to
`PaulStoffregen/cores` commit `7f107ee0` and `PaulStoffregen/SPI` commit
`7c83d072`, not a Teensyduino release — see the pin table under "Layout".
`-DTEENSYDUINO=159` in both target Makefiles is independent of this pin and
does not need to move when it does; see the note there for why it stays.
`teensy_loader_cli` is the only part of the PJRC distribution this repo still
needs directly, and it builds standalone from PJRC's GitHub.

**Watch the Kinetis watchdog constants on any Teensy 3.x refresh.**
`WDOG_TOVALL` in `targets/teensy3x/platform_teensy3.cpp` is `893` on the 3.5
and `529` on the 3.6. Neither came from the reference manual — both were
calibrated by multi-point stall testing on the specific chip, and the file's
own comment warns to re-measure if its timing-sensitive surroundings change
materially. A core refresh or a compiler change is exactly that. Checklist
step 7 is what catches it, and a silently mis-timed watchdog is the one
failure nothing else in the checklist would surface.

**TinyUSB (`Drivers/tinyusb`)** — currently **0.21.0**, vendored as an
explicit per-file list in `tools/vendor.json` (the device-side CDC class and
the `stm32_fsdev` portable driver only) rather than the whole `src/` tree, so
a version bump may need that file list edited if upstream adds, removes or
renames a file this build touches. It is built with `-std=gnu11` rather than
the target's `-std=c11`, because `fsdev_common.c` uses a bare `asm("NOP")`
that `__STRICT_ANSI__` rejects — if a refresh appears to need a source edit
to compile, check the standard first. Upstream ships an `stm32f103_bluepill`
board, so an F103 regression there is worth reporting rather than patching
around.

**QNEthernet (`Drivers/teensy_libs/QNEthernet`)** — currently **v0.36.0**,
`teensy4x` `ETH=1` only. AGPL-3.0-or-later; see `THIRD_PARTY.md`.
