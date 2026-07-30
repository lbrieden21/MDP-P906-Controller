# MDP Adapter firmware — bare-metal multiceiver rewrite

Replaces the shipped `nrf_adapter_source/` (STM32CubeMX + HAL +
`mokhwasomssi/stm32_hal_nrf24l01p`, single nRF24 RX pipe) with a bare-metal
firmware (direct CMSIS register access, no HAL/CubeMX) that adds real nRF24L01+
multiceiver support: each attached device (P906, L1060) gets its own hardware
RX pipe, so the host can attribute a response to a device by pipe number
instead of by packet content/timing.

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

`nrf_adapter_source/Modules/nrf24l01/nrf24l01p.c` (`mokhwasomssi/stm32_hal_nrf24l01p`)
only ever writes `RX_ADDR_P0` — `nrf24l01p_set_rx_address()` always targets
pipe 0 regardless of its `width` argument, and `nrf24l01p_reset()` leaves
`EN_RXADDR = 0x01` (pipe 0 only). There's no multi-pipe support to extend,
and pulling in CubeMX/HAL just to get a nRF24 register layer wasn't worth
it. Everything needed to reimplement it bare-metal was already recovered
from the shipped source, not reverse-engineered:

- **Pins** (`nrf_adapter_source/Core/Inc/main.h`): PA0 LED (active-low, open-drain),
  PA2 nRF IRQ (EXTI2, falling edge, pull-up), PA3 CSN, PA4 CE, PA5/6/7 SPI1
  SCK/MISO/MOSI (AF0), PA9/10 USART1 TX/RX (AF1).
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
host_link_test.py    Target-neutral host-protocol check (framing, dispatch,
                     settings persistence) shared by all four boards —
                     nothing in it depends on which is under test beyond the
                     port name.
Drivers/CMSIS        Copied verbatim from nrf_adapter_source/Drivers/CMSIS
                     (ST-provided register definitions only, no HAL driver
                     folder — this is the whole point of "no HAL"). STM32
                     targets only; STM32F1xx device headers added alongside
                     the existing STM32F0xx ones for the Blue Pill.
Drivers/teensy4      PJRC cores/teensy4, copied verbatim — same treatment
                     CMSIS gets. teensy4x/ only.
Drivers/teensy3      PJRC cores/teensy3, copied verbatim, same snapshot as
                     teensy4/ so its SPI library stays in sync. teensy3x/
                     only.
Drivers/teensy_libs  PJRC's SPI library, likewise verbatim. Shared by both
                     Teensy targets (renamed from teensy4_libs/ when the
                     Teensy 3.x port started reusing it).
```

A new target adds a `targets/<name>/` directory implementing the same
`platform.h` and nothing else — `core/` never gains a conditional, so adding
a target cannot change the code any existing target compiles.

## Design notes / deviations from the shipped firmware

- **UART**: interrupt-driven RX and TX ring buffers instead of DMA +
  idle-line detection. Simpler, no DMA driver needed. TX had to stay
  non-blocking (TXE-interrupt-drained, not polled): `uart_send_packet()` can
  run from inside the nRF24 EXTI ISR (`nrf_rx_done`/`nrf_tx_done` are called
  from `nrf24l01p_irq()`), and a blocking TX there would add UART-frame-time
  latency to every nRF24 IRQ — directly on the timing-sensitive 50Hz Type-8
  polling path. USART1 is NVIC priority 1,
  higher than EXTI2_3's priority 3, so USART1's ISR can always preempt and
  drain the TX ring even while EXTI2_3's handler is still executing.
- **Settings persistence**: a single reserved flash page (magic + payload +
  CRC16, erase+rewrite on `NRF_SAVE`) instead of the shipped firmware's
  MiniFlashDB wear-leveling KV store. Save is a low-frequency, host-triggered
  operation (settings dialog), so page-erase endurance was never a real
  constraint here.
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
- **Pipe number source**: `STATUS.RX_P_NO`, read for free off the SPI
  response byte that `R_RX_PAYLOAD` already returns — no extra SPI
  transaction, so this doesn't touch the timing-sensitive 50Hz Type-8
  polling path.

## STM32 Blue Pill target

Drop-in for the STM32F030 target in every way except the host link: same
peripheral selection (SPI1, USART1, same pins bar the LED), so an existing
nRF24 harness plugs straight in. USART1 at 921600 through an external
USB-UART bridge is the **only** host link — the Blue Pill's onboard USB port
is wired to the F103's USB-FS device peripheral, which would need a vendored
USB device stack plus a CDC glue layer for no real benefit over reusing the
validated USART1 path. `platform.h` already accommodates a CDC variant behind
a `HOST_LINK` flag if this is ever wanted as a follow-on.

- **Wiring** (`targets/stm32f103/gpio.h`): identical to the F030 target
  except the LED, which moves to the onboard **PC13** (active-low,
  open-drain, 2MHz). nRF IRQ PA2 (EXTI2), CSN PA3, CE PA4, SPI1 SCK/MISO/MOSI
  PA5/6/7, USART1 TX/RX PA9/10 — no AFIO remap on either peripheral.
- **SPI runs at 9MHz** (`BR_1`, /8 off the 72MHz PCLK2), under the
  nRF24L01+'s 10MHz ceiling. The F030's 12MHz is above spec and was not
  carried over — the same call already made for the Teensy targets.
- **The radio is serviced in the ISR.** USART1 sits at NVIC priority 1
  against EXTI2's priority 3, so the UART ISR can always preempt and drain
  the TX ring from inside the radio handler; that is what lets this target
  service the radio straight out of `EXTI2_IRQn` rather than deferring it to
  the main loop.
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

- **Wiring** (`targets/teensy4x/pins.h`): SPI is LPSPI4 on its fixed pins —
  SCK 13, MOSI 11, MISO 12 — plus CSN 10, CE 9, IRQ 2. SPI runs at 10MHz, the
  nRF24L01+'s rated ceiling.
- **No status LED.** Pin 13 is the onboard LED *and* LPSPI4's SCK, so it is
  unavailable, and `led_on()`/`led_off()` are no-ops — they only ever drove
  cosmetic activity indication. There is no boot blink on this target, and so
  no delay between `protocol_init()` and the first poll; nothing depends on
  one.
- **Radio IRQ runs in thread context, not the ISR.** Servicing the radio from
  the ISR would mean blocking SPI *and* `uart_write` inside it, which is only
  safe where the UART interrupt can preempt and drain the TX ring — there is
  no such NVIC priority relationship here. Instead the ISR only latches the
  edge and `loop()` calls `protocol_service_radio_irq()`; at 600MHz
  against a 20ms Type-8 poll period the deferral costs microseconds, and
  `uart_write` is never called from interrupt context at all.
  Servicing is gated on the IRQ pin *level* as well as the latched edge — the
  nRF24 holds IRQ low until STATUS is cleared, so a second event arriving
  before servicing produces no new falling edge.
- **Host link**: USB CDC (`Serial`) by default; `-DHOST_LINK_SERIAL1` swaps in
  `Serial1` at 921600 for exact parity with the shipped adapter. Both sit
  behind the same three `platform.h` functions, and nothing in `core/` is
  conditional on the choice. On CDC `uart_set_baudrate()` is a no-op, but
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
  (6+1) × 0.5s = **3.5s**, the nearest step to the STM32's ~3.3s IWDG timeout,
  refreshed on the same 100ms cadence from `loop()`. Note `SRS` and `WDA` are
  active-low "do not assert now" controls and must be written 1, or enabling
  the watchdog resets the part immediately.

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

Two genuine differences from `teensy4x/`:

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
  unlock→configure in `platform_teensy3.cpp` so nothing can miss it. Timeout
  is 3500 against the 1kHz LPO clock source, 3.5s — matching the 4.x target's
  WDOG1 timeout, same 100ms refresh cadence from `loop()`.

- **Wiring** (`targets/teensy3x/pins.h`): SPI0 on its fixed pins — MOSI 11,
  MISO 12 — with SCK moved to 14 as above — plus CSN 10, CE 9, IRQ 2. Same
  10MHz SPI ceiling as every other target.
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

## Building

All four targets build with plain `make` against the system
`arm-none-eabi-gcc` (14.2) — no package manager, no board manifest, no
downloaded toolchain. Teensyduino ships its own older gcc, but 14.2 builds
the PJRC cores unmodified.

```sh
sudo apt-get install gcc-arm-none-eabi   # arm-none-eabi-gcc 14.2, if not already installed

cd targets/stm32f030 && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}
cd targets/stm32f103 && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}
cd targets/teensy4x  && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex}, TEENSY41
cd targets/teensy4x  && make BOARD=TEENSY40      # same dir, TEENSY40
cd targets/teensy3x  && make                    # -> build/MDP_Adapter_Multiceiver.{elf,hex}, TEENSY35
cd targets/teensy3x  && make BOARD=TEENSY36      # same dir, TEENSY36
make clean                                       # any target
```

**Switching `BOARD` on `teensy4x`/`teensy3x` requires `make clean` first** —
the define only reaches the framework objects under `build/fw`, and `make`
has no way to know they're stale otherwise. The same applies to switching
`HOST_LINK` on either Teensy target.

To build a Teensy target against `Serial1` instead of USB CDC:

```sh
cd targets/teensy4x   # or targets/teensy3x
make clean && make HOST_LINK=HOST_LINK_SERIAL1
```

## Flashing

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
