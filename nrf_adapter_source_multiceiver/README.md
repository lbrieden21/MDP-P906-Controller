# MDP Adapter firmware — bare-metal multiceiver rewrite

Replaces the shipped `nrf_adapter_source/` (STM32CubeMX + HAL +
`mokhwasomssi/stm32_hal_nrf24l01p`, single nRF24 RX pipe) with a bare-metal
firmware (direct CMSIS register access, no HAL/CubeMX) that adds real nRF24L01+
multiceiver support: each attached device (P906, L1060) gets its own hardware
RX pipe, so the host can attribute a response to a device by pipe number
instead of by packet content/timing.

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
targets/stm32f030/   This target's full implementation of platform.h, plus
                     startup/vector table, clock init and main(). Each
                     peripheral file supplies its own share of the contract
                     (gpio.c the control lines, spi.c the transfers, and so
                     on); gpio.h's port/mask helpers are target-private.
    linker/          STM32F030F4Px.ld: 15KB code region + reserved last 1KB
                     flash page for settings (see flash_store.c).
    Makefile         Builds this target; run make from inside this directory.
targets/teensy41/    Teensy 4.1 target. platform_teensy41.cpp is the entire
                     C/C++ boundary — every platform.h entry point in one
                     file, wrapped in extern "C"; Arduino headers appear
                     here and in no shared header. pins.h holds the wiring,
                     main.cpp the setup()/loop() boot order,
                     host_link_test.py the host-protocol acceptance check.
Drivers/CMSIS        Copied verbatim from nrf_adapter_source/Drivers/CMSIS
                     (ST-provided register definitions only, no HAL driver
                     folder — this is the whole point of "no HAL").
                     STM32 target only.
Drivers/teensy4      PJRC cores/teensy4, copied verbatim — same treatment
                     CMSIS gets. Teensy target only.
Drivers/teensy4_libs PJRC's SPI library, likewise verbatim.
```

A second target adds a `targets/<name>/` directory implementing the same
`platform.h` and nothing else — `core/` never gains a conditional, so a new
target cannot regress an already-validated one. (Adding the Teensy target
left the STM32 `.bin` byte-identical.)

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

## Teensy 4.1 target

A drop-in replacement for the STM32 dongle: same framing, same command set,
same pipe-tagged `REP_NRF_RECV_OK`, so the existing host harnesses work
unmodified apart from the port name.

Hardware-validated against the P906 + L1060 bench, and faster than the STM32
dongle it replaces: on a 60s two-device run through the GUI it delivered ~17%
more samples at less than half the no-ack rate (1.91% vs 4.61%), with zero
failed requests on either. Details and method in
`../nrf_adapter_teensy41_port_plan.md`.

- **Wiring** (`targets/teensy41/pins.h`): SPI is LPSPI4 on its fixed pins —
  SCK 13, MOSI 11, MISO 12 — plus CSN 10, CE 9, IRQ 2. SPI runs at 10MHz, the
  nRF24L01+'s rated ceiling; the STM32 target's 12MHz (48/4) is above spec and
  there was no reason to carry that over.
- **No status LED.** Pin 13 is the onboard LED *and* LPSPI4's SCK, so it is
  unavailable, and `led_on()`/`led_off()` are no-ops. They only ever drove
  cosmetic activity indication. The STM32's four-blink boot indicator is gone
  with it, along with the ~800ms it consumed between `protocol_init()` and the
  first poll — nothing depends on that delay.
- **Radio IRQ runs in thread context, not the ISR.** This is the one real
  design change. The STM32 does blocking SPI *and* `uart_write` inside its
  EXTI handler, which is only safe because USART1 sits at NVIC priority 1
  against EXTI2_3's 3, so the UART ISR preempts and drains the TX ring. That
  priority relationship does not survive the port. Here the ISR only latches
  the edge and `loop()` calls `protocol_service_radio_irq()`; at 600MHz
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
everything.

## Building

Both targets build with plain `make` against the system `arm-none-eabi-gcc`
(14.2) — no package manager, no board manifest, no downloaded toolchain.
Teensyduino ships its own older gcc, but 14.2 builds the PJRC core unmodified.

```sh
sudo apt-get install gcc-arm-none-eabi   # arm-none-eabi-gcc 14.2, if not already installed

cd targets/stm32f030 && make   # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}
cd targets/teensy41  && make   # -> build/MDP_Adapter_Multiceiver.{elf,hex}
make clean                     # either target
```

To build the Teensy target against `Serial1` instead of USB CDC:

```sh
cd targets/teensy41
make clean && make HOST_LINK=HOST_LINK_SERIAL1
```

The `make clean` is required — the flag only reaches two objects, so make will
not rebuild them on its own when it changes.

## Flashing

### Teensy 4.1

```sh
sudo apt-get install teensy-loader-cli
cd targets/teensy41
make flash    # teensy_loader_cli --mcu=TEENSY41 -s -w -v build/*.hex
```

`-s` soft-reboots into the bootloader over USB, so no button press is needed
as long as the running firmware still enumerates as USB serial; `-w` waits for
the board. Drop `-s` and press the button if the board is wedged or running a
non-`USB_SERIAL` build.

Then run the host-protocol check (no radio needed — it covers framing,
dispatch, and EEPROM persistence across a reboot):

```sh
../../../venv/bin/python host_link_test.py /dev/ttyACM0
```

When comparing the two adapters against the same devices, park the idle one on
an unused address and channel first. Any normal run leaves an adapter in RX on
the bench address, where it will auto-ACK packets meant for the device under
test — this measurably corrupts throughput and error-rate measurements.

### STM32F030 dongle

Via ST-LINK V2 over SWD (no BOOT0/3V3 short needed — that's only for the
UART bootloader method in `readme_EN.md`, which doesn't apply here). Wire
SWCLK/SWDIO/GND/3V3 from the ST-LINK to the target; SWD uses PA13/PA14,
which this firmware never touches (see pin mapping above), so there's no
conflict with the app pins.

```sh
cd targets/stm32f030

# stlink-tools (st-flash)
sudo apt-get install stlink-tools
st-flash write build/MDP_Adapter_Multiceiver.bin 0x08000000

# or OpenOCD
sudo apt-get install openocd
openocd -f interface/stlink.cfg -f target/stm32f0x.cfg \
  -c "program build/MDP_Adapter_Multiceiver.elf verify reset exit"
```

If the target's read/write protection is set, `st-flash` will refuse to
write — run `st-flash erase` first (mass-erases and drops RDP back to
level 0), or `openocd ... -c "stm32f0x unlock 0; reset halt"` with OpenOCD.
