# Teensy 4.1 Port — `nrf_adapter_source_multiceiver`

## Context

`nrf_adapter_source_multiceiver/` is the bare-metal (CMSIS, no HAL/CubeMX) rewrite of the
MDP adapter firmware that adds real nRF24L01+ multiceiver support. It is hardware-validated
on the STM32F030F4Px dongle and is what `bus.py` / `pipe_test.py` currently talk to.

The goal here is to run the same firmware on a Teensy 4.1 without giving up the validated
STM32 build. Going bare-metal already did most of the work: dropping the HAL forced every
hardware service behind a hand-rolled, narrow interface, and those interfaces *are* the
platform abstraction layer. There are ~12 functions between the application and the silicon.

**Scope:** Teensy 4.1 only. No other targets are in scope for this document.

**Standing constraints:** no compat shims (full cutover); the STM32 build must stay
byte-identical through the refactor phase; user runs all git commits.

## Current state (verified by reading the tree)

Of ~1,100 lines of application code, ~645 are already platform-neutral:

| File | Lines | Platform coupling |
|---|---|---|
| `Core/Src/nrf24l01p.c` | 361 | **None.** Reaches hardware only through six one-line inlines at `nrf24l01p.c:40-57` (`cs_low`/`cs_high`/`ce_high`/`ce_low`/`led_on`/`led_off`) plus `spi1_transfer*`. |
| `Core/Src/protocol.c` | 284 | **Three touchpoints:** `#include "stm32f0xx.h"` (`protocol.c:4`), two `NVIC_SystemReset()` calls (`protocol.c:95`, `protocol.c:102`), and the `EXTI2_3_IRQHandler` trampoline (`protocol.c:257-262`). |

Everything else is a thin shim over something Teensyduino provides at a higher level:

| File | Lines | Becomes on Teensy |
|---|---|---|
| `Core/Src/startup.c` | 110 | deleted — framework provides vector table / `Reset_Handler` |
| `Core/Src/flash_store.c` | 111 | ~20 lines (`EEPROM.get/put`) |
| `Core/Src/uart.c` | 96 | ~25 lines (`Serial1` or USB `Serial`) |
| `Core/Src/gpio.c` | 62 | ~15 lines (`pinMode`) |
| `Core/Src/system_clock.c` | 53 | ~5 lines (`millis`/`delay` exist) |
| `Core/Src/main.c` | 40 | ~20 lines (`setup`/`loop`) |
| `Core/Src/spi.c` | 37 | ~15 lines |
| `Core/Src/watchdog.c` | 22 | ~5 lines (`WDT_T4`) or dropped |
| `linker/STM32F030F4Px.ld`, `Makefile` | — | not used by the Teensy target |

Net-new Teensy implementation is realistically 100–150 lines.

Resource headroom is a non-issue: 1 MB RAM vs 4 KB, 600 MHz vs 48 MHz. 3.3 V logic matches
the radio.

## Target structure

```
nrf_adapter_source_multiceiver/
  core/                 nrf24l01p.c/.h, protocol.c/.h   — no #ifdefs, no MCU headers
  platform.h            the ~12-function contract (below)
  targets/stm32f030/    today's gpio.c spi.c uart.c system_clock.c watchdog.c
                        flash_store.c startup.c main.c + linker/ + Makefile,
                        moved verbatim
  targets/teensy41/     platform_teensy41.cpp + main.cpp + pins.h + Makefile
  Drivers/CMSIS/        unchanged, STM32 target only
  Drivers/teensy4/      PJRC cores/teensy4, copied verbatim, Teensy target only
```

As built, `targets/teensy41/` also carries `platform_teensy41.h` (the three target-private
boot hooks: `platform_init`, `host_link_begin`, `radio_irq_pending`) and
`host_link_test.py`; PJRC's SPI library needed vendoring too, at
`Drivers/teensy4_libs/SPI/`, since `cores/teensy4` does not include it.

**No `#ifdef PLATFORM_X` in `core/`.** Each target supplies a full implementation of
`platform.h`. This is deliberately *not* the shared-source-with-conditionals structure of the
`multi-platform-wip` branch: the point is that the STM32 target — the only hardware-validated
one — cannot be regressed by work on a new target.

`platform.h` contract (already implied by today's headers):

```
spi_transfer_byte(u8) -> u8          nrf_csn_low/high()      led_on/off()
spi_transfer(tx, rx, len)            nrf_ce_low/high()       platform_reboot()
millis() -> u32                      uart_write(buf, len)    store_load(buf, len) -> int
delay_ms(u32)                        uart_read_byte(*u8)     store_save(buf, len) -> int
watchdog_init/refresh()              uart_set_baudrate(u32)
```

Note this replaces `gpio.h`'s `gpio_set(GPIO_TypeDef*, mask)` with the six named operations
the driver actually uses. The port/mask concept is STM32-specific and does not survive.

## Build system — plain `make`, no PlatformIO

Keep the existing STM32 `Makefile` exactly as it is (zero dependencies beyond a compiler,
already validated). The Teensy target gets a **second Makefile of the same shape**, so both
targets build with `make` and neither needs a package manager, a board manifest, or a
downloaded toolchain.

- **Compiler:** the system `arm-none-eabi-gcc` (14.2.1, already installed and used for the
  STM32 build).
- **Framework:** PJRC's `cores/teensy4`, copied verbatim into `Drivers/teensy4/` — same
  treatment `Drivers/CMSIS` already gets, and PJRC ship that directory with a command-line
  Makefile intended for exactly this. Link against their `imxrt1062_t41.ld`.
- **Flashing:** `teensy_loader_cli` (a small standalone C program) against the `.hex`.

PlatformIO and `arduino-cli` were both considered and rejected: each brings its own package
manager and toolchain download for a single target, and `arduino-cli` additionally wants a
sketch- or library-shaped source tree, which fights the `core/` + `targets/` split.

**Deferred to bring-up:** the exact flag set (`-mcpu=cortex-m7 -mfpu=fpv5-d16
-mfloat-abi=hard`, `-D__IMXRT1062__ -DARDUINO_TEENSY41 -DF_CPU=600000000`, and whatever the
core turns out to need) is settled against real hardware in Phase 1, not guessed here. We are
**assuming GCC 14.2 builds the PJRC core** — Teensyduino ships its own, older gcc, so this is
the one dependency-level unknown. It surfaces the moment the core first compiles, and if 14.2
does fight it, installing PJRC's toolchain alongside is a contained fallback that changes
nothing else in this plan.

### Why the Teensy target uses PJRC's core at all

The STM32 target deliberately shed HAL/CubeMX, so leaning on an Arduino core here is worth
justifying rather than drifting into. The i.MX RT1062 has **no internal flash** — code runs
from external QSPI (XIP, or copied to ITCM at boot). That makes two required services
expensive to do from scratch:

- **Settings storage** needs a FlexSPI self-programming driver executing from ITCM. PJRC's
  EEPROM emulation already solves this.
- **USB CDC** needs a full device stack (endpoint queue heads, transfer descriptors,
  enumeration, CDC-ACM) — well over a thousand lines.

Plus a boot header / FlexSPI config block and a considerably hairier clock/PLL bring-up than
the F030's HSI→PLL→48 MHz. Going fully bare-metal here is not the same trade as it was on the
F030, where CMSIS register access got us everything.

**This couples to the host-link decision:** USB CDC is what makes PJRC's core non-optional.
The `HOST_LINK_SERIAL1` fallback (below) would remove the USB stack from the equation — the
flash-storage problem would remain, so it still wouldn't justify going bare-metal, but it is
a second reason to keep that fallback compiling rather than deleting it.

### C/C++ boundary

`core/*.c` compiles as C; `targets/teensy41/*.cpp` compiles as C++ and wraps its
`platform.h` implementations in `extern "C"`. That is the entire boundary — Arduino headers
appear only inside `targets/teensy41/`, never in a shared header. The
`multi-platform-wip` branch's guard tangle came from doing the opposite.

## Phase 0 — make `core/` portable (STM32 only, no behavior change)

This phase must leave the STM32 firmware functionally identical. It is the only phase that
touches validated code.

1. **Introduce `platform.h`** and move the six GPIO inlines out of `nrf24l01p.c:40-57` into
   the contract. `Core/Inc/gpio.h`'s `gpio_set`/`gpio_clear`/`GPIO_TypeDef` become
   STM32-target-private.
2. **`protocol.c`:** `NVIC_SystemReset()` → `platform_reboot()`; drop
   `#include "stm32f0xx.h"`; move `EXTI2_3_IRQHandler` (`protocol.c:257-262`) into the STM32
   target — it is already just a 5-line trampoline into `nrf24l01p_irq()`. Replace it in
   `core/` with an exported `protocol_service_radio_irq()` the target calls.
3. **`nrf24l01p.c:171` — replace the cycle-count busy-wait.**
   `for (volatile int i = 0; i < 0xffff; i++) {}` in `nrf24l01p_reset()` is a
   CPU-speed-dependent delay (order of 10 ms on a 48 MHz Cortex-M0; a small fraction of that
   on a 600 MHz Cortex-M7). It must become `delay_ms(N)` with an explicit N measured on the
   STM32, so that build keeps its current timing and the Teensy inherits a wall-clock value
   rather than a cycle count. Measurement procedure below.
4. **Move files** into `core/` + `targets/stm32f030/`; update `Makefile` paths and
   `README.md`'s "Layout" section. Add `build/` to `.gitignore` if it is still tracked.

### Measuring the busy-wait (step 3)

The STM32F030 is a Cortex-M0 and has **no DWT cycle counter**, so GDB can't time the loop
directly. Measure it in firmware against SysTick and read the result out over the existing
ST-LINK/GDB session — no protocol changes, no scope needed.

1. Add temporary instrumentation to `Core/Src/main.c`, immediately after `systick_init()`
   (it must run after SysTick is live, and before `protocol_init()`):

   ```c
   /* TEMPORARY — remove once N is known */
   volatile uint32_t g_busywait_us;
   ...
   systick_init();
   {
       uint32_t t0 = millis();
       for (int n = 0; n < 100; n++) {
           for (volatile int i = 0; i < 0xffff; i++) {}
       }
       g_busywait_us = (millis() - t0) * 10;  /* total_ms / 100 * 1000 */
   }
   ```

   100 repetitions turn a ~10 ms delay into ~1 s, so millisecond-resolution `millis()` gives
   a sub-1% result. Keep `-O2` — the loop is `volatile` so it won't be optimised away, but
   changing flags would change the answer.

2. `make`, flash, then read the value:

   ```sh
   openocd -f interface/stlink.cfg -f target/stm32f0x.cfg &
   arm-none-eabi-gdb build/MDP_Adapter_Multiceiver.elf \
     -ex 'target extended-remote :3333' -ex 'break protocol_init' \
     -ex 'continue' -ex 'print g_busywait_us'
   ```

   Breaking on `protocol_init` guarantees the measurement block has completed.

3. Round the microsecond figure **up** to a whole millisecond and use that as N in
   `delay_ms(N)`. Rounding up only ever gives the radio more settle time than it has today.

4. Delete the instrumentation block and the `g_busywait_us` global, rebuild, reflash,
   re-run `pipe_test.py`.

Record the measured value in this document when you have it, so the number has a provenance
rather than looking arbitrary later:

> **Measured (2026-07-28):** busy-wait = **20490 µs** at 48 MHz → `delay_ms(21)`.
> SysTick-timed over 100 repetitions, read out over SWD; reproduced exactly on two runs.
> `arm-none-eabi-gdb` is not installed on this machine — `gdb-multiarch` 16.3 works
> unmodified against the ELF. Before flashing, the instrumented loop was confirmed to
> compile to an instruction-identical body to the real one in `nrf24l01p_reset` (same
> registers, same stack slot).
>
> Note this is **2× the plan's "order of 10 ms" estimate**, and 37% above a pure cycle
> count (11 cycles × 65535 at 48 MHz = 15.0 ms) — the balance is flash wait-state fetch
> stall. A guessed 10 ms would have halved the radio's validated settle time.

**Gate:** rebuild the STM32 firmware and diff the resulting `.bin` against the pre-refactor
build. Step 3 will change the binary; steps 1, 2 and 4 should not. Do 1/2/4 first, confirm a
byte-identical `.bin`, then do 3 as its own commit. Reflash and re-run `pipe_test.py` against
the P906 + L1060 bench setup before moving on.

> **Gate outcome (2026-07-28): passed, with the byte-identical check substituted.**
>
> Byte-identity is not achievable for steps 1/2/4 as specified, and not for a worrying
> reason: converting the six `static inline` GPIO ops into the cross-TU functions that
> *are* the platform boundary necessarily turns ~20 inlined `BSRR` stores into calls
> without LTO, and changing flags to preserve inlining would itself move the binary.
> Substituted a stricter check — an address-independent, rename-aware per-function
> disassembly diff against the pre-refactor build:
>
> - **33 functions instruction-identical, 0 removed, 8 added** (exactly the new
>   `platform.h` entry points).
> - The 19 changed functions are fully accounted for: 17 `nrf24l01p_*` where inline
>   `BSRR` stores became `bl nrf_csn_low` / `nrf_ce_high` / … in identical positions;
>   `EXTI2_3_IRQHandler` gaining one call level; and `handle_command`, where the delta is
>   exactly 2× inlined `NVIC_SystemReset` bodies out and 1× `bl platform_reboot` in (GCC
>   tail-merged the two cases — verified via the switch jump table that case `0x00` now
>   points at the `bl` and case `0x03` at the `CMD_RESET` block).
> - `text` 6624 → 6596 (steps 1/2/4), → 6572 after step 3 drops the loop.
>
> The dongle's pre-existing flash was byte-identical to the pre-refactor baseline build,
> so the before/after comparison is clean. A full 16 KB backup was taken first.
>
> Hardware verification: radio registers read back correct over SWD after boot
> (`RF_CH=0x4e`, `RF_SETUP=0x0f`, `EN_RXADDR=0x01`, `CONFIG=0x0f`, `FIFO_STATUS=0x11`),
> then `pipe_test.py` passed — including Step 6, where TX retargeted to the L1060 returns
> a reply tagged **pipe=1** while the P906 stays on pipe 0. 5 of 6 runs fully clean; one
> dropped Type 7 response out of 24 gets (link-layer ACK succeeded, application response
> did not arrive within the 1 s window). Not attributable to the refactor without an A/B
> against the baseline binary, which was not run.
>
> Two bench notes that cost time and are worth knowing next time:
> - Both devices had drifted off the addresses `pipe_test.py` hard-codes and answered
>   nothing, including the broadcast sweep. Recovered by putting each in discover mode
>   **one at a time** (two at once collide on the 2478 broadcast) and re-dispatching
>   P906 → `AABBCCDDEE` and L1060 → `153614FAE1`, both @ 2521.
> - `pipe_test.py` had been silently broken since commit `7facf79 add multi device
>   support`, which split the pipe byte into a separate callback argument; its
>   one-argument `nrf_register_recv_callback` lambda raised `TypeError` inside the worker
>   thread, so every step reported `NO RESPONSE` regardless of firmware. Fixed.

## Phase 1 — Teensy 4.1 target

### Step 0: get an empty firmware building and flashing

Before writing any adapter code, prove the toolchain end to end — this is where the GCC 14.2
assumption gets tested, and it is much easier to debug against a blinking LED than against a
silent radio.

1. Copy PJRC `cores/teensy4` into `Drivers/teensy4/`.
2. Write `targets/teensy41/Makefile` (mirroring the STM32 one's structure) and get a
   do-nothing `setup()`/`loop()` to link against the core with `imxrt1062_t41.ld`.
3. Flash with `teensy_loader_cli` and confirm a blink plus USB CDC enumeration
   (`/dev/ttyACM*`) and echo.

Settle the real flag set here. If GCC 14.2 fights the core, install PJRC's toolchain and
point the Makefile's `CC` at it — nothing else in this plan changes.

> **Step 0 outcome (2026-07-29): passed on the first build.**
>
> **GCC 14.2 builds the PJRC core unmodified** — the plan's one dependency-level unknown
> is closed. No patches to the vendored tree, no PJRC toolchain needed, zero warnings from
> the core at `-Wall`. `text 13632 / data 8896 / bss 13984` for the blink. Flashed with
> `teensy_loader_cli --mcu=TEENSY41 -s -w -v`, enumerated as `/dev/ttyACM0`
> (`16c0:0483`), CDC echo verified byte-exact.
>
> Settled flag set (`targets/teensy41/Makefile`):
> - `-mcpu=cortex-m7 -mthumb -mfloat-abi=hard -mfpu=fpv5-d16`
> - `-D__IMXRT1062__ -DARDUINO_TEENSY41 -DF_CPU=600000000 -DUSB_SERIAL -DLAYOUT_US_ENGLISH
>   -DARDUINO=10813 -DTEENSYDUINO=159`
> - `-O2 -g -ffunction-sections -fdata-sections`; C++ adds
>   `-std=gnu++17 -felide-constructors -fno-exceptions -fpermissive -fno-rtti`
> - link: `-Os -Wl,--gc-sections,--relax -T$(FW_DIR)/imxrt1062_t41.ld`, `-lm -lstdc++`
>
> Three deviations from PJRC's own Makefile, each deliberate:
> - **`-larm_cortexM7lfsp_math` dropped.** That is a prebuilt CMSIS-DSP library shipped
>   only with Teensyduino, and nothing this firmware links needs it. Confirmed by
>   `arm-none-eabi-nm -u` on the ELF: **zero undefined symbols.**
> - **`USING_MAKEFILE` not defined**, so the core's `main.cpp` calls `setup()`/`loop()`
>   rather than compiling in its own blink loop. It is the only file that reads the macro.
> - **Framework objects are linked directly, not archived.** An archive would drop
>   `bootdata.c`'s `.flashconfig`/`.ivt` and the vector table — placed by the linker script
>   and referenced by no symbol — producing a firmware that builds and does not boot.
>
> Two smaller build-system notes: objects go to `build/{fw,core,app}/` because the core has
> its own `main.cpp` that would otherwise collide with the target's, and the vendored trees
> are included with `-isystem` in the app's flag set so `SPI.h`→`DMAChannel.h` does not trip
> `-Wdeprecated-copy` under the `-Wextra` our own code is built with.

### Pin map (decide first, it constrains the LED)

Teensy 4.1's onboard LED is **pin 13, which is also LPSPI4 SCK**. The status LED therefore
either moves to a spare GPIO or `led_on`/`led_off` become no-ops on this target. The LED is
purely cosmetic activity indication (driven from the TX/RX paths in `nrf24l01p.c`), so either
is acceptable — but pick one deliberately rather than wiring SCK to the LED.

Proposed: SCK 13, MOSI 11, MISO 12 (LPSPI4); CSN 10, CE 9, IRQ 8; LED on a free pin or
dropped. All Teensy 4.x digital pins support `attachInterrupt`, so IRQ placement is free.

> **Decided (2026-07-29):** SCK 13, MOSI 11, MISO 12 (LPSPI4); CSN 10, CE 9, **IRQ 2**;
> **no LED** — `led_on`/`led_off` are no-ops. Recorded in `targets/teensy41/pins.h`.
>
> A third option was on the table and rejected: LPSPI3 (`SPI1`) on MOSI 26 / SCK 27 /
> MISO 39 would have freed pin 13 for the onboard LED at no hardware cost — the SPI
> library's T4.1 tables confirm those pins. Not taken; the LED is not wanted, and LPSPI4
> is the better-trodden path.

### SPI

`SPI.beginTransaction(SPISettings(10000000, MSBFIRST, SPI_MODE0))` around each complete
CSN-asserted transaction — i.e. begin/end must bracket `cs_low() … cs_high()`, not individual
bytes. CSN and CE stay plain GPIO; do **not** use the SPI library's hardware CS.

**10 MHz, not 12.** The STM32 build runs SPI1 at 48/4 = 12 MHz (`spi.c:13`), already above
the nRF24L01+'s 10 Mbps rating. It works there; there is no reason to carry the over-spec
divisor onto a new target.

### IRQ handling — the one real design change

Today `nrf24l01p_irq()` runs *inside* the EXTI handler, does blocking SPI, then calls
`uart_send_packet` → `uart1_write`, which spin-waits for TX ring space. That is only safe
because `uart.c:6-21` deliberately sets USART1 to NVIC priority 1 against EXTI2_3's priority
3, so the UART ISR can preempt and drain the ring. **That priority relationship does not
survive the port.**

On Teensy, stop doing radio work in the ISR: the ISR sets a `volatile bool`, and `loop()`
calls `protocol_service_radio_irq()`. At 600 MHz against a 20 ms Type-8 poll period the added
latency is microseconds, and the entire priority-inversion argument disappears — `uart_write`
is no longer called from interrupt context at all.

Also gate servicing on the IRQ pin *level*, not only the stored edge flag: the nRF24 holds
IRQ low until the STATUS flags are cleared, so a second event arriving before servicing
produces no new falling edge. `nrf24l01p_rx_irq()` already drains until the FIFO is empty, so
this is belt-and-braces rather than a fix for an observed failure — but it costs one
`digitalReadFast` per loop iteration and removes the question entirely.

### Host link — **decided: USB CDC, with `Serial1` as a compile-time fallback**

Teensy 4.1 has native USB, so the primary path is USB CDC (`Serial`): ~12 Mbit/s, no
external USB-serial converter, no second cable. Selected by `-DHOST_LINK_USB_CDC` in
`targets/teensy41/Makefile`; `-DHOST_LINK_SERIAL1` switches to `Serial1` at 921600 for exact
behavioural parity with the shipped adapter if CDC misbehaves on the bench.

Both are ~25 lines behind the same three `platform.h` functions, so the fallback is a real
escape hatch rather than a rewrite. The switch lives entirely in
`targets/teensy41/platform_teensy41.cpp` — nothing in `core/` is conditional on it.

**This requires no changes to `core/protocol.c`.** On CDC, `uart_set_baudrate()` becomes a
no-op; `CMD_SET_BAUDRATE` (`protocol.c:105-119`) still replies `REP_BAUDRATE_SET` and still
persists the value through `flash_store`, so the command's observable protocol behaviour and
its interaction with `CMD_NRF_QUERY` / settings persistence are unchanged. Only the physical
link speed stops responding to it. The `delay_ms(100)` before the switch is pointless on CDC
but harmless, and leaving it keeps the two link modes on identical code paths.

Host side needs no changes either: `nrf24_adapter.py` opens the port with a baudrate,
`pyserial` ignores it for a CDC device, and the Teensy enumerates as `/dev/ttyACM*`. Only
the configured port name differs.

Either way, `uart_write` maps onto the framework's own buffered write; the 256-byte rings in
`uart.c` are not reimplemented.

### Settings storage

`EEPROM.get`/`EEPROM.put` against Teensy 4.1's flash-emulated EEPROM (≈4 KB). Keep
`flash_store.c`'s existing magic + payload + CRC16 record format so the same
`persisted_settings_t` round-trips; only the read/write primitives change. The payload is
comfortably under `STORE_MAX_PAYLOAD` (32).

### Watchdog

`WDT_T4<WDT1>` from Teensyduino, refreshed from the same 100 ms cadence in `main.c:33-39`.
Low value on a bring-up target — acceptable to stub `watchdog_init`/`watchdog_refresh` for
Phase 1 and add it in Phase 2.

> Stubbed, as permitted. `WDT_T4` is a separate Teensyduino library that would need
> vendoring alongside SPI; deferred with the rest of the watchdog work to Phase 2.

### Phase 1 outcome (2026-07-29): complete, target implemented and flashed

Net-new Teensy code came in at **353 lines** across `platform_teensy41.cpp` (249),
`main.cpp` (53), `platform_teensy41.h` (26) and `pins.h` (25) — against the plan's
100–150 estimate, the overrun being comment density rather than logic.

**`core/` was not touched.** The STM32 `.bin` rebuilt **byte-identical** to the Phase 0
build, which is the check Phase 0 could not perform on itself.

Implementation notes worth carrying forward:

- **SPI transactions bracket CSN, as specified.** `nrf_csn_low()` does
  `SPI.beginTransaction(settings)` then drops CSN; `nrf_csn_high()` raises CSN then
  `endTransaction()`. `nrf24l01p_reset()` opens with a bare `nrf_csn_high()` and so calls
  `endTransaction()` unpaired — checked and benign: T4's `endTransaction` only restores
  NVIC masks when `usingInterrupt()` was called, which this firmware never does.
- **`spi_transfer()` is a byte loop over `SPI.transfer()`**, not the library's block
  transfer. Same shape as the STM32 target's `spi.c`, so the 0xFF read filler matches
  without depending on `_transferWriteFill`, and there is no second code path to validate.
  32 bytes at 10 MHz is ~26 µs of clock; per-byte overhead on a 600 MHz M7 is noise.
- **`millis()` needed no implementation** — the framework already exports it as `extern "C"`
  with exactly `platform.h`'s signature, so it links straight through. It is the one
  contract function not defined in `platform_teensy41.cpp`.
- **`radio_irq_pending()` consumes the latched edge before servicing**, not after, so an
  edge arriving mid-service cannot be lost. The worst case is one redundant
  `nrf24l01p_irq()` that finds no STATUS flags set.
- **`platform_reboot()`** is `SCB_AIRCR = 0x05FA0004`, not Teensyduino's
  `_reboot_Teensyduino_()` — the latter drops to the bootloader rather than restarting the
  application. On CDC this drops and re-enumerates the port, which is what a real reset
  does; hosts must reopen.

**Hardware verification (no radio attached yet).** `targets/teensy41/host_link_test.py`,
9/9 pass over CDC — `CMD_ECHO`; `CMD_NRF_QUERY` returning the exact compiled-in defaults
(`4e01070220030105aabbccddee`); `CMD_SET_BAUDRATE` ACKing with the link still alive after;
`CMD_NRF_SAVE`; `CMD_REBOOT` + reconnect + re-query proving **EEPROM persistence across a
power cycle**; an unknown opcode rejected without desyncing the framer; and `CMD_RESET`
invalidating the record so a re-query returns to defaults. That is Phase 2 step 2 already
satisfied for the CDC build. `-DHOST_LINK_SERIAL1` was build-verified only, as planned.

**Radio path smoke test.** The module was wired during this phase, so one further check ran:
`CMD_NRF_SET` (2521 MHz, `AABBCCDDEE`) returned `REP_NRF_INIT`, and three `CMD_NRF_TX`
frames each returned `REP_NRF_SEND_FAIL`. `SEND_FAIL` is the *informative* result here — it
means the radio transmitted, exhausted its retries, asserted IRQ, and the reply came back
through `radio_irq_pending()` → `protocol_service_radio_irq()`. Reaching it requires
`STATUS` bit 0x10 set **and** 0x20 clear, so MISO is returning discriminating data, not a
stuck level: a stuck-low MISO gives no reply at all, stuck-high gives `SEND_OK`. SPI both
directions, the IRQ wire, and the deferred-service design are therefore all working. The
failure itself is expected — nothing was listening on that address at the time.

Sizes: CDC `text 21824 / data 8896 / bss 14304`; `SERIAL1` `text 23872 / data 9920 /
bss 14432`. Against 8 MB flash and 1 MB RAM, irrelevant — noted only as a baseline.

Still outstanding, all needing live devices: Phase 2 steps 1 (explicit register read-back)
and 3–5 (single-device, multiceiver, full GUI).

## Phase 2 — bring-up and validation

The firmware is a drop-in replacement: same framing, same command set, same pipe-tagged
`REP_NRF_RECV_OK`. So the existing harnesses are the acceptance tests, unmodified except for
the port name.

1. **Radio present:** read back `RF_CH`/`RF_SETUP`/`EN_RXADDR` after `nrf_configure()` and
   confirm they hold written values. This is where an SPI or reset-timing mistake shows up
   first, before any protocol work.
2. **Host protocol parity:** `CMD_ECHO`, `CMD_NRF_QUERY`, `CMD_NRF_SET`, `CMD_NRF_SAVE` +
   power-cycle + `CMD_NRF_QUERY` (proves EEPROM persistence). `CMD_SET_BAUDRATE` must still
   ACK and still persist under CDC even though the link speed does not change.
   Build-verify `-DHOST_LINK_SERIAL1` as well, but only functionally validate the CDC build
   — the fallback exists for the case where CDC gives trouble, and validating it on the
   bench is only worth the time if that happens.
3. **Single-device traffic:** connect the P906 alone via `mdp_controller`, confirm realtime
   polling and set operations. P906 is the reference device for any divergence.
4. **Multiceiver:** run `nrf_adapter_source_multiceiver/pipe_test.py` against the P906 +
   L1060 bench setup. It exercises `CMD_NRF_OPEN_PIPE` (0x23), `CMD_NRF_SET_TX_TARGET`
   (0x24), and pipe-tagged receive — the whole reason this firmware exists.
5. **Full GUI:** both devices connected through one adapter, sustained run. Compare
   `tools/noack_report.py` output against an STM32-adapter baseline captured on the same
   bench.

Steps 3–5 need the real bench; there is nothing useful to simulate here.

### Phase 2 outcome (2026-07-29): complete, all five steps passed

The Teensy target is validated end to end and **outperforms the STM32 baseline on
every measured axis**. No `core/` changes were needed for any of it; the only
firmware change in this phase is the watchdog, which was deferred here from
Phase 1.

**Step 1 — register read-back.** `core/` exposes no register-read command and this
target has no SWD path, so a temporary `REP_REG_DUMP` (0xFE) frame was added to
`targets/teensy41/main.cpp`, emitted once a second, reading the registers through
`platform.h`'s own SPI primitives. After `CMD_NRF_SET` (2521 MHz, `AABBCCDDEE`) plus
`CMD_NRF_OPEN_PIPE 1 = 153614FAE1`, all eleven checked registers held their written
values: `RF_CH 0x79`, `RF_SETUP 0x0E`, `EN_RXADDR 0x03`, `EN_AA 0x03`, `SETUP_AW 0x03`,
`SETUP_RETR 0x0C`, `CONFIG 0x0F`, `RX_PW_P0 0x20`, and all three 5-byte address
registers byte-exact (`TX_ADDR`/`RX_ADDR_P0` = `EE DD CC BB AA`, `RX_ADDR_P1` =
`E1 FA 14 36 15`). `STATUS 0x0E` / `FIFO_STATUS 0x11` — both FIFOs empty, no flags.
Expected values were derived by hand from `nrf_configure()`'s exact call order
before the run, not read off the result. Instrumentation removed afterwards
(`loop` back from 0xE4 to 0x38 bytes).

**Step 2 — host protocol parity.** `host_link_test.py` 9/9 on the clean build.
`-DHOST_LINK_SERIAL1` build-verified, zero warnings, sizes identical to Phase 1's
record (`text 23872 / data 9920 / bss 14432`).

**Step 3 — single-device P906.** Passed. See the measurement note below on why the
first numbers were wrong.

**Step 4 — multiceiver.** `pipe_test.py` passed on the Teensy on the first run, all
four gets answered, with the L1060 reply tagged **pipe=1** while the P906 stayed on
pipe 0. Re-run on the STM32 minutes later for an A/B: same values, so the two
adapters are behaviourally equivalent. `pipe_test.py`'s hard-coded port is now
`sys.argv[1]` with the old default.

**Step 5 — full GUI.** Run headless (`QT_QPA_PLATFORM=offscreen`) with a temporary
driver that calls `ConnectionManager.link_panel()` on both panels, which is the code
path `bench_run` does *not* cover: panels poll asynchronously through
`request_realtime_value()` / `register_realtime_value_callback()` rather than
synchronous `get_status()`.

#### Measured results

Single device (P906 only), 20 s, **other adapter parked**, three runs each:

| | req/s (mean) | TX-FIFO overflows / 20 s | failed requests |
|---|---|---|---|
| Teensy 4.1 | 198.2 | 1–3 | 0 |
| STM32F030 | 179.8 | 0 | 0 |

Two devices on one adapter, 60 s, driver-level (synchronous `get_status()`):

| | radio sends | no-acks | overflows | failed requests |
|---|---|---|---|---|
| Teensy 4.1 | 6818 | 101 (1.48%) | 0 | 0 |
| STM32F030 | 6076 | 181 (2.98%) | 0 | 0 |

Two devices, 60 s, **real GUI** headless, `tools/noack_report.py`:

| | P906 samples | L1060 samples | radio sends | no-acks | overflows |
|---|---|---|---|---|---|
| Teensy 4.1 | 8508 | 8265 | 6962 | 133 (1.91%) | 0 |
| STM32F030 | 7296 | 6825 | 6140 | 283 (4.61%) | 0 |

The Teensy delivers ~17% more samples at less than half the no-ack rate. On both
adapters the no-acks are overwhelmingly P906 (`..E2`) traffic and the L1060 (`..E3`)
is nearly clean, so that residue is a bench/device characteristic, not adapter
behaviour.

#### The TX-FIFO overflow, run down

Single-device runs initially showed 8–14 `REP_NRF_FIFO_OVERFLOW` per 20 s on the
Teensy against ~0 on the STM32, which looked like a regression from Phase 1's
decision to defer radio IRQ service to `loop()`. It is not. Two separate causes,
both established by experiment rather than inspection:

1. **Most of it was the test setup.** Both adapters sit in RX on the bench
   address/channel between runs, so the idle one auto-ACKs and consumes packets
   addressed to the device under test. Every measurement above was taken with the
   other adapter *parked* on an unused address/channel. Parking alone took the
   Teensy from 8–14 overflows to 0–1 and raised its rate from ~165 to ~200 req/s.
   **Any future A/B on this bench must park the idle adapter first.**
2. **The small remainder is a request-rate effect, not IRQ deferral.** Throttling
   the Teensy below ~170 req/s drives overflows to exactly zero (two runs at
   164–167 req/s, two at 89 req/s, all zero); leaving it unthrottled at ~198 req/s
   gives 1–3. The onset sits around 180–185 req/s, which is simply above where the
   STM32 can drive the link at all. The mechanism is the pre-existing backpressure
   in `nrf24l01p_transmit_then_receive()`: `nrf_auto_tx_cnt` climbs only when a
   target stops replying and the host's retries stack up, and it is recovered
   transparently — **zero failed requests in every run on either adapter**. This was
   confirmed incidentally when the P906 was put into pairing mode mid-run and the
   overflow warnings immediately returned.

So Phase 1's "stop doing radio work in the ISR" decision costs nothing measurable,
which is what that section predicted.

#### Watchdog (deferred from Phase 1) — implemented and proven

`watchdog_init`/`watchdog_refresh` now drive **WDOG1 directly** rather than through
Teensyduino's `WDT_T4`. That library would have had to be vendored alongside SPI,
while `imxrt.h` — already in the vendored core — declares every register needed, and
the core itself never touches WDOG1 (only `CrashReport.cpp` reads the reset cause).
This also matches the STM32 target, whose `watchdog.c` talks to IWDG registers
directly.

`WT=6` gives (6+1) × 0.5 s = **3.5 s**, the nearest available step to the STM32
target's ~3.3 s IWDG timeout, refreshed on the same 100 ms cadence from `loop()`.
`SRS` and `WDA` are active-low "do not assert now" controls and must be written 1 or
enabling the watchdog resets the part immediately; `WDZST`/`WDBG` are write-once
after reset, so the whole configuration goes in one `WCR` store.

Verified three ways: `host_link_test.py` still 9/9; a 60 s sustained run (12072
requests, 1 failure, 201 req/s) with no spurious reset; and a temporary deliberate
`for(;;)` hang at t=25 s, after which the USB port dropped **3.4 s later** and the
board re-enumerated on its own — against the 3.5 s configured. Hang instrumentation
removed afterwards.

#### Bench notes worth carrying forward

- **Dispatch requires front-panel pairing mode.** A Type 6 sent to a device's known
  unicast address is ACKed at the link layer and then ignored — the device neither
  replies nor moves. Verified directly: after the attempt the P906 still answered at
  its old address and nothing answered at the new one. `bus.auto_match()` therefore
  needs a human at the device, one at a time (two at once collide on the 2478
  broadcast).
- **Pipes 2–5 cannot hold arbitrary addresses.** `RX_ADDR_P2..P5` are single-byte
  registers sharing their upper four bytes with `RX_ADDR_P1`, which is exactly why
  `MDPBus._pipe_address()` derives `base[:4] + (0xE1 + pipe)`. The bench's historical
  addresses (P906 `AABBCCDDEE`, L1060 `153614FAE1`) cannot both be served as pipes
  1 and 2 — attaching the L1060 to pipe 2 silently gives it the effective address
  `AABBCCDDE1` and its connect times out. `pipe_test.py` sidesteps this by using only
  pipe 1, which does have a full 5-byte register.
- **Bench state after this phase:** both devices were re-matched via the Teensy onto
  MDPBus addresses — **P906 → `AA:BB:CC:DD:E2` (pipe 1), L1060 → `AA:BB:CC:DD:E3`
  (pipe 2), both @ 2521 MHz**. This also exercised broadcast discovery and Type 6
  dispatch through the Teensy firmware for the first time. Note these are *not*
  `pipe_test.py`'s hard-coded addresses any more, so that script needs the devices
  paired back (or its constants updated) before it will pass again.
- `gui_source/settings.json` was temporarily pointed at the adapter under test and
  has been restored; the temporary GUI driver script was deleted.
- The headless GUI segfaults during interpreter teardown under
  `QT_QPA_PLATFORM=offscreen`, *after* the event loop exits and all results are
  printed. A PyQt/pyqtgraph teardown artifact, not a run failure.

## Note on the earlier `multi-platform-wip` attempt

That branch's `NRF_Adapter_Porting_Plan.md` concludes the Teensy SPI failed because the RF24
library uses "proven transaction management patterns" the custom layer lacked, and schedules
1–2 weeks of comparative analysis against RF24. That diagnosis does not hold up against the
code. In `nrf_adapter_source/src/platform.cpp` on that branch:

- `platform_spi_transfer()` wraps **each individual byte** in its own
  `beginTransaction`/`endTransaction`, so transactions open and close *inside* an asserted
  CSN, and it does so at **1 MHz** rather than the intended bus speed.
- `platform_spi_transmit_receive()` calls raw `SPI.transfer()` with **no transaction at
  all**, so no SPI settings are applied on the multi-byte half of every register access.
- `nrf24l01p_reset()` there replaced the busy-wait with a flat 10 ms.

Those are ordinary bugs with ordinary fixes. Arduino SPI on Teensy does not need special
handling beyond bracketing whole CSN-asserted transactions — which is what this plan
specifies. **Do not re-run the RF24 comparative-analysis phase.**

Nothing else from that branch is carried forward; its HAL-shim structure is superseded by the
`core/` + `targets/` split above.
