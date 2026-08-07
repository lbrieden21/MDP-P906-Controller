# Adapter Firmware — Four New MCU Targets (Blue Pill, Teensy 3.5/3.6, Teensy 4.0)

## Context

`nrf_adapter_source_multiceiver` currently builds for two adapters: the shipped STM32F030F4Px dongle and a Teensy 4.1. Commit `477c00d` created the seam that makes more targets cheap — `platform.h` is a 20-function contract, `core/` (`nrf24l01p.c` + `protocol.c`) has **zero** `#ifdef`s and no MCU headers, and each board lives entirely under `targets/<name>/`. Commit `da0adfb` then proved the seam by adding the Teensy 4.1 without touching `core/`, `platform.h`, or the STM32 target at all.

The goal here is to let the adapter firmware run on hardware I already own. This adds four boards across three target directories:

| Board | MCU | Target dir | Host link |
|---|---|---|---|
| STM32 Blue Pill | STM32F103C8T6, Cortex-M3, 72MHz, 64K flash / 20K SRAM | `targets/stm32f103/` (new) | USART1 @921600 |
| Teensy 3.5 | MK64FX512, Cortex-M4F, 120MHz | `targets/teensy3x/` (new) | USB CDC |
| Teensy 3.6 | MK66FX1M0, Cortex-M4F, 180MHz | `targets/teensy3x/` (new) | USB CDC |
| Teensy 4.0 | IMXRT1062, Cortex-M7, 600MHz | `targets/teensy4x/` (fold of `teensy41/`) | USB CDC |

Intended outcome: six build configurations, each hardware-validated on the bench, with `core/` and `platform.h` still unmodified and the STM32F030 `.bin` still byte-identical.

**Decisions made with the user:**
1. **Blue Pill host link: USART1 only.** The board's onboard USB port is wired to the STM32F103's USB-FS device peripheral (PA11/PA12), which would need a vendored USB device stack plus a CDC glue layer — more work and more new bug surface than every other phase combined. USART1 at 921600 through an external USB-UART bridge reuses the validated F030 `uart.c` almost verbatim and makes the Blue Pill a drop-in for the existing dongle.
2. **Teensy 3.5 and 3.6 share one target directory**, with `BOARD ?= TEENSY35` selecting the chip in the Makefile only. No platform code differs between them, so all variation stays in the build file and the target's C++ gains no `#ifdef`s.
3. **Teensy 4.0 support added** to the same scope. It needs no new vendoring and no source changes — only a `BOARD` block, same as the 3.5/3.6 split.
4. **Ordering: STM32 first**, then the Teensy work. Within the Teensy work the 4.0 fold comes before the 3.x port, because it is Makefile-only and establishes the `BOARD ?=` pattern on silicon that is *already* hardware-validated — so the 3.x target inherits a proven pattern instead of debugging two new things at once.

**Standing constraints:** no compat shims or deprecated aliases (full cutover); vendored trees under `Drivers/` stay byte-for-byte upstream — superseded by `plans/devendoring_plan.md`, which moved them out of git entirely (git-ignored, fetched at a pinned version by `tools/fetch_vendor.py`) rather than committed verbatim, see that plan for the current mechanism; `core/` and `platform.h` are not modified; English readme only (`readme_EN.md`, never `readme.md`); `venv/bin/python` for all host-side scripts; user runs all git commits themselves.

**Bench radio configuration — fixed, do not change it.** Every script that talks to the devices — existing harnesses, anything modified during this work, and any new script written along the way — uses the configuration already live in `gui_source/settings.json`:

| | Address | Pipe (MDPBus) |
|---|---|---|
| Adapter base | `AA:BB:CC:DD:EE` | primary (`RX_ADDR_P0`/`TX_ADDR`) |
| P906 (`FE2597EC`) | `AA:BB:CC:DD:E2` | 1 |
| L1060 (`09B93C19`) | `AA:BB:CC:DD:E3` | 2 |

**Channel: 2521 MHz** (`CHANNEL_BYTE` 121) for all of them. Addresses derive from `MDPBus._pipe_address()` — `base[:4] + (0xE1 + pipe)`, `bus.py:98-100`.

Nothing in this plan requires re-matching either device, and nothing in it may introduce a re-match. Re-pairing costs a human at each front panel, one device at a time, and every prior drift from these values has cost bench time recovering from it. A script that wants different values is wrong; fix the script.

## What the investigation established

- **No new CMSIS core headers needed for the Blue Pill.** `Drivers/CMSIS/Include/` already ships `core_cm3.h` and `core_cm4.h`. Only three ST *device* headers are missing.
- **Teensy 4.0 needs no new vendoring at all.** `Drivers/teensy4/` already contains `imxrt1062.ld` (1984K flash, vs the 4.1's 7936K) and complete `ARDUINO_TEENSY40` gating — `core_pins.h:51` (`CORE_NUM_DIGITAL 40`), `eeprom.c:41` (`E2END 0x437`), `bootdata.c:94`, `pins_arduino.h:96`, `usb_desc.c:118`. Pins 2/9/10/11/12/13 are all valid on a 4.0, so `pins.h` is unchanged.
- **The vendored SPI library already supports Kinetis.** `Drivers/teensy4_libs/SPI/SPI.h:323-684` is the full `KINETISK` branch with explicit `__MK64FX512__`/`__MK66FX1M0__` cases (`:429`) and `setSCK()` (`:637`). The Teensy 3.x target reuses it verbatim — no second SPI copy.
- **Provenance:** `Drivers/teensy4/` is `PaulStoffregen/cores` master `teensy4/` verbatim (verified by diff; the only upstream file omitted is the stray `Blink.cc` sample). `teensy3/` must come from the same snapshot — 139 files, `avr/eeprom.h` giving `E2END 0xFFF` (4096B) for MK64/MK66 — and not from an older `cores` snapshot, whose SPI library differs from the vendored one.
- Toolchain is already installed: `arm-none-eabi-gcc` 14.2, `teensy_loader_cli`, `st-flash`, `openocd`.

## Design rules to hold to

These are the repo's existing invariants, restated because every phase depends on them:

1. `core/` and `platform.h` are **not modified**. If a facility is missing on a board, `platform.h` already permits a documented no-op (see its comments on `led_on`/`led_off` at `platform.h:34-37` and `uart_set_baudrate` at `:47-49`).
2. MCU / framework headers appear only inside `targets/<name>/`.
3. Per-chip variation within one target dir lives in the **Makefile**, not in `#ifdef`s in the C/C++ — following the existing `HOST_LINK` precedent (`targets/teensy41/Makefile:17`, consumed at `platform_teensy41.cpp:29-35`).
4. Vendored trees are compiled at PJRC's own warning level via `-isystem`; our code keeps `-Wall -Wextra` (`targets/teensy41/Makefile:28-42`).

---

## Phase 0 — Shared prep

**Vendor the STM32F1 device headers.** Add, mirroring the exact shape of the existing `Drivers/CMSIS/Device/ST/STM32F0xx/` (three headers + LICENSE, no HAL driver), from `STMicroelectronics/cmsis_device_f1`:

```
Drivers/CMSIS/Device/ST/STM32F1xx/Include/stm32f1xx.h
Drivers/CMSIS/Device/ST/STM32F1xx/Include/stm32f103xb.h
Drivers/CMSIS/Device/ST/STM32F1xx/Include/system_stm32f1xx.h
Drivers/CMSIS/Device/ST/STM32F1xx/LICENSE.txt
```

`STM32F103x8` (64K) and `STM32F103xB` (128K) share `stm32f103xb.h`; we define `STM32F103xB` and cap flash in the linker script. We do not define `USE_HAL_DRIVER`, so `stm32f1xx.h` pulls in no HAL — same as the F0 side.

**Vendor the Teensy 3 core.** Copy `PaulStoffregen/cores` master `teensy3/` verbatim to `Drivers/teensy3/` (139 files, including `mk64fx512.ld`, `mk66fx1m0.ld`, `mk20dx128.c`, `eeprom.c`, `main.cpp`, `DMAChannel.*`, `EventResponder.*`, `pins_arduino.h`). Exclude only the stray sample (`Blink.cc`), matching how `teensy4/` was vendored.

**Rename `Drivers/teensy4_libs/` → `Drivers/teensy_libs/`.** The SPI library is now shared by the Teensy 3.x and Teensy 4.x targets, so the `teensy4_` prefix becomes wrong. One-line change to `SPI_DIR` in the Teensy 4 Makefile; the library source itself is untouched, so no binary changes.

---

## Phase 1 — `targets/stm32f103/` (Blue Pill)

Model it on `targets/stm32f030/`, keeping the one-file-per-peripheral split (`platform.h:9-12` documents that choice). Peripheral *selection* is deliberately identical to the F030 dongle, so an existing nRF24 harness plugs straight in — only the LED pin moves.

**Wiring** (put in a target-private `gpio.h`, same role as `targets/stm32f030/gpio.h`):

| Function | F030 dongle | F103 Blue Pill |
|---|---|---|
| LED | PA0 | **PC13** (onboard, active-low, open-drain, 2MHz) |
| nRF IRQ | PA2 (EXTI2) | PA2 (EXTI2) |
| CSN / CE | PA3 / PA4 | PA3 / PA4 |
| SPI1 SCK/MISO/MOSI | PA5/6/7 | PA5/6/7 (no remap) |
| USART1 TX/RX | PA9/PA10 | PA9/PA10 (no remap) |

### Files, and how much is reuse

**`flash_store.c` — near-verbatim copy.** The F1 flash controller is the same IP as the F0 for our purposes: `FLASH->KEYR/CR/AR/SR`, `FLASH_KEY1/KEY2`, `FLASH_CR_PER/PG/STRT/LOCK`, `FLASH_SR_BSY`, half-word programming. Copy `targets/stm32f030/flash_store.c` changing only the include. The record layout, `crc16_ccitt()` and the read-back verify **must stay bit-identical** so a settings blob is portable across every target. (This duplication is already the accepted cost of the no-conditionals rule — it exists today in both `stm32f030/flash_store.c:26-42` and `platform_teensy41.cpp:151-167`.)

**`watchdog.c` — verbatim copy** apart from the include. F1's IWDG is identical: PR=3 (/32), RLR=4095, ~40kHz LSI → ~3.3s. **Carry the comment at `targets/stm32f030/watchdog.c:4-10` across unchanged** — the `KR=0xCCCC`-first ordering is load-bearing on the F1 too, and that comment records a real debug session (waiting on `IWDG_SR` before starting hangs forever).

**`system_clock.c` — rewrite.** Blue Pills carry an 8MHz crystal: HSE → PLL ×9 → 72MHz SYSCLK/HCLK, APB2 /1 (PCLK2 = 72MHz), **APB1 /2** (PCLK1 = 36MHz, its architectural maximum). `FLASH->ACR = PRFTBE | LATENCY_2` (two wait states above 48MHz) set *before* raising SYSCLK. `SYSTEM_CORE_CLOCK_HZ` becomes `72000000UL`. `systick_init`/`millis`/`delay_ms` copy over unchanged.

**`spi.c` — small but real rewrite.** The F1 SPI block is the older IP: there is **no `CR2_DS` / `CR2_FRXTH`** and no RX FIFO threshold, so `targets/stm32f030/spi.c:15` has no F1 equivalent — drop the `CR2` write entirely and get 8-bit frames from `DFF=0` (the reset default). `CR1 = MSTR | BR_1 (/8 → 9MHz) | SSM | SSI`, then `SPE`. 9MHz sits under the nRF24L01+'s 10MHz ceiling; the F030's 12MHz is above spec and is not carried over — the same call already made for the Teensy 4.1 (`README.md:147-149`). `TXE`/`RXNE` polling and the byte-at-a-time `spi_transfer()` loop are unchanged.

**`uart.c` — ring buffers reused, ISR body rewritten.** Keep the RX/TX ring structure, `uart_write()`'s spin-for-space, `uart_read_byte()`, and the BRR one-liner from `targets/stm32f030/uart.c` verbatim — the F1's mantissa+fraction BRR layout means `(PCLK2 + baud/2)/baud` is still correct (921600 → 78 → 923077, +0.16%). What changes is the register set: F1 has `SR`/`DR`, not `ISR`/`RDR`/`TDR`/`ICR`. Flags become `USART_SR_RXNE`/`TXE`/`ORE`/`FE`/`NE`/`PE`, and there is **no `ICR`** — the error branch at `targets/stm32f030/uart.c:93-95` becomes a read of `SR` followed by a read of `DR` to clear.

**The NVIC priority relationship carries over directly, and must.** `targets/stm32f030/uart.c:6-21` explains why radio-work-in-the-ISR is safe on STM32: USART1 at priority 1 preempts EXTI at priority 3, so `uart_write()`'s spin-wait can always be drained from inside the radio ISR. The M3 has *more* priority bits than the M0, so `NVIC_SetPriority(USART1_IRQn, 1)` and `NVIC_SetPriority(EXTI2_IRQn, 3)` preserve the same ordering. Keep the STM32F030 model — service the radio straight out of the EXTI handler — rather than the Teensy's deferred-to-loop model.

**`gpio.c` — rewrite (the largest single piece).** F1 configures pins through `CRL`/`CRH` 4-bit fields (MODE + CNF), not `MODER`/`OTYPER`/`OSPEEDR`/`AFR`, and there is no per-pin AF number — alternate function is implicit per peripheral. Concretely:

- Clocks: `RCC->APB2ENR |= IOPAEN | IOPCEN | AFIOEN`.
- PC13 LED: output open-drain 2MHz (`CNF=01, MODE=10`). Open-drain and the slow slew matter — PC13 on these boards is current-limited.
- PA2 IRQ: input with pull-up (`CNF=10, MODE=00`, plus the `ODR` bit set).
- PA3/PA4 CSN/CE: output push-pull 2MHz. Set idle levels (CSN high, CE low) *before* switching to outputs, as `targets/stm32f030/gpio.c:15-17` does.
- PA5/PA7 SCK/MOSI: AF push-pull 50MHz. PA6 MISO: input floating.
- PA9 TX: AF push-pull 50MHz. PA10 RX: input floating. (Both in `CRH`.)
- EXTI: route via `AFIO->EXTICR[0]` (EXTI2 field = 0 for port A). This replaces the F0's `SYSCFG->EXTICR`, and unlike the F0 case, enabling `AFIOEN` is **not** optional. Then `EXTI->FTSR |= 1<<2`, clear `RTSR`, `EXTI->IMR |= 1<<2`.
- **IRQ name differs:** F103 has a dedicated `EXTI2_IRQn` / `EXTI2_IRQHandler`, not the F0's shared `EXTI2_3_IRQn`. The handler body (`EXTI->PR` write-1-to-clear, then `protocol_service_radio_irq()`) is otherwise identical to `targets/stm32f030/gpio.c:90-95`.

The six `platform.h` line/LED functions and the `gpio_set`/`gpio_clear` `BSRR` inlines port over as-is (F1 has `BSRR` with the same semantics). The LED stays active-low, so `led_on()` clears and `led_off()` sets, unchanged.

**`startup.c` — new vector table.** Same structure as `targets/stm32f030/startup.c` (weak `Default_Handler` aliases, `.data` copy, `.bss` zero, `SystemInit()`, `main()`) but with the STM32F103xB table: 16 core entries plus 60 IRQs, in `startup_stm32f103xb.s` order. Note `EXTI0`–`EXTI4` are separate slots here, and `USART1` sits at IRQ 37.

**`linker/STM32F103C8Tx.ld`** — copy of the F030 script with:

```
FLASH (rx)         : ORIGIN = 0x08000000, LENGTH = 63K
SETTINGS_FLASH (r) : ORIGIN = 0x0800FC00, LENGTH = 1K
RAM (rwx)          : ORIGIN = 0x20000000, LENGTH = 20K
```

F103C8 is medium-density, so the erase page is 1K — the last-page-reserved scheme from `flash_store.c` transfers exactly. Keep the `_settings_flash_page` and `_estack` symbol definitions.

**`main.c`** — copy of `targets/stm32f030/main.c`, LED port becomes `GPIOC`. Keep the boot order (`systick_init; gpio_init; spi_init; uart_init(921600); watchdog_init; protocol_init`), the four-blink indicator, and the 100ms watchdog cadence in the loop.

**`Makefile`** — copy of `targets/stm32f030/Makefile` with `MCU = -mcpu=cortex-m3 -mthumb -mfloat-abi=soft`, `DEFS = -DSTM32F103xB`, `DEVICE_INC` pointed at `STM32F1xx`, and the new linker script. The `$(wildcard)`-driven source lists need no change.

---

## Phase 2 — `targets/teensy41/` → `targets/teensy4x/` (adds Teensy 4.0)

Pure Makefile work; the platform implementation is already correct for both boards.

- Rename `targets/teensy41/` → `targets/teensy4x/` and `platform_teensy41.{cpp,h}` → `platform_teensy4.{cpp,h}`. Update the include in `main.cpp` and the header's own comment.
- Makefile gains `BOARD ?= TEENSY41`, selecting three things and nothing else:

  | | TEENSY40 | TEENSY41 |
  |---|---|---|
  | chip define | `-DARDUINO_TEENSY40` | `-DARDUINO_TEENSY41` |
  | linker script | `imxrt1062.ld` | `imxrt1062_t41.ld` |
  | loader arg | `--mcu=TEENSY40` | `--mcu=TEENSY41` |

  `F_CPU=600000000`, `-D__IMXRT1062__`, the M7 hard-float flags, and the `build/{fw,core,app}` split all stay as they are. Keep the comment at `Makefile:48-50` — framework objects must still be linked directly rather than archived, or `bootdata.c`'s `.flashconfig`/`.ivt` gets dropped.
- `pins.h` unchanged. `led_on`/`led_off` stay no-ops on both: pin 13 is the onboard LED *and* LPSPI4 SCK on the 4.0 exactly as on the 4.1.
- Settings still fit: the 4.0's emulated EEPROM is 1080 bytes (`Drivers/teensy4/avr/eeprom.h:35`, `E2END 0x437`) against a 40-byte record.
- **Document that `BOARD` switches require `make clean`**, for the same reason `HOST_LINK` already does (`README.md:199-221`) — the define reaches only some objects.
- **Promote `host_link_test.py`** from the target dir to the firmware root and give it a `--port` argument. It only exercises the framer, command dispatch and settings store, so it is target-neutral; with four boards each wanting it, one copy at the root is right.

---

## Phase 3 — `targets/teensy3x/` (Teensy 3.5 / 3.6)

Model on `targets/teensy4x/`: one file, `platform_teensy3.cpp`, is the entire C/C++ boundary with every `platform.h` entry point in `extern "C"`; a small `platform_teensy3.h` holds the target-private boot hooks (`platform_init`, `host_link_begin`, `radio_irq_pending`); `pins.h` holds wiring only.

**Carry these across unchanged from `platform_teensy4.cpp`:**

- The CSN/transaction bracketing. `nrf_csn_low()` does `SPI.beginTransaction()` *then* drives CSN low, and `nrf_csn_high()` the reverse. The comment at `platform_teensy41.cpp:67-70` records that opening or closing a transaction inside an asserted CSN is what broke an earlier port — keep that comment. `nrf24l01p_reset()` opens with a bare `nrf_csn_high()`, so `endTransaction()` runs unpaired once at boot; that was checked and found benign on the 4.1, and **it holds on Kinetis too** — `SPI.h`'s `KINETISK` `endTransaction()` restores NVIC masks only under `if (interruptMasksUsed)`, which requires `usingInterrupt()`, which this firmware never calls. That branch does carry an `#ifdef SPI_TRANSACTION_MISMATCH_LED` path that would drive a pin high on the mismatch — confirm the macro stays undefined.
- Byte-at-a-time `spi_transfer()`, so the `0xFF` read filler matches the STM32 targets and the block path needs no separate validation.
- Deferred IRQ handling: `attachInterrupt(digitalPinToInterrupt(NRF_IRQ_PIN), radio_isr, FALLING)`, the ISR latching only a flag, and `radio_irq_pending()` level-gating as well as edge-gating (the nRF24 holds IRQ low until STATUS is cleared). Keep the note that the STM32's NVIC priority relationship does not survive the port.
- `millis()` links straight through from the framework — do not reimplement.
- The `HOST_LINK_USB_CDC` / `HOST_LINK_SERIAL1` pattern and the no-op `uart_set_baudrate` on CDC.
- `store_load`/`store_save` over `eeprom_read_block`/`eeprom_write_block`, with the identical record format, CRC and read-back verify. Teensy 3.5/3.6 give 4096 bytes here (`Drivers/teensy3/avr/eeprom.h:37`, `E2END 0xFFF`), FlexNVM-backed.
- `platform_reboot()` via `SCB_AIRCR = 0x05FA0004`.

**Two genuine differences from the Teensy 4.x target:**

1. **The LED works here.** On Kinetis, SPI0's SCK can be moved off pin 13: call `SPI.setSCK(14)` *before* `SPI.begin()` (available at `Drivers/teensy_libs/SPI/SPI.h:637`), freeing pin 13 for the onboard LED. So `led_on`/`led_off` become real `digitalWriteFast` calls instead of the no-ops the 4.x targets are stuck with, and the boot blink from the STM32 target can come back. Reflect the SCK choice in `pins.h` (`NRF_SCK_PIN 14`) and document it — it is the one place the Teensy 3.x wiring differs from a 4.x board.
2. **The watchdog is a different peripheral with a hard timing window.** Kinetis `WDOG`, not i.MX `WDOG1`. PJRC's default early hook leaves the watchdog disabled but *reconfigurable* — `mk20dx128.c:674` writes `WDOG_STCTRLH = WDOG_STCTRLH_ALLOWUPDATE` — so `watchdog_init()` can still run from `setup()` at its normal place in the boot order, with no need to override `startup_early_hook()`. That is a deviation from PJRC's intended pattern — `ResetHandler` unlocks and then spends the window on two `nop`s before calling the hook, so configuring from `setup()` means unlocking again. It is valid given `ALLOWUPDATE`, but a `WDOG_UNLOCK` that silently no-ops yields a watchdog that never fires rather than one that misbehaves, which nothing but the watchdog step in Verification would catch. Sequence: `WDOG_UNLOCK = 0xC520; WDOG_UNLOCK = 0xD928;` then, **within 256 bus cycles**, write `WDOG_TOVALH`/`WDOG_TOVALL` (3500 against the 1kHz LPO → 3.5s, matching the Teensy 4.x target) and `WDOG_STCTRLH = WDOGEN | ALLOWUPDATE | WAITEN | STOPEN`. Disable interrupts across unlock→configure so the window cannot be missed. `watchdog_refresh()` is `WDOG_REFRESH = 0xA602; WDOG_REFRESH = 0xB480;`. Both existing targets' watchdog code carries a hard-won gotcha comment — add the 256-cycle window to this one.

Take the unlock→configure ordering from the K64F/K66F reference manual, not from a vendored library. Checked, so it does not get re-litigated: `WDT_T4` is `tonton81/WDT_T4`, i.MX-only (it writes `WDOG1_*` with no architecture gating, so on Kinetis it fails to compile rather than degrading), and PJRC's `cores/teensy3` ships no watchdog API at all — the only `WDOG` code in the core is the `ALLOWUPDATE` write above.

**`Makefile`** — copy of the Phase 2 `teensy4x/Makefile`, `FW_DIR` pointed at `Drivers/teensy3`, `SPI_DIR` at the renamed `Drivers/teensy_libs/SPI`, `MCU = -mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16`, and `BOARD ?= TEENSY35`:

| | TEENSY35 | TEENSY36 |
|---|---|---|
| chip define | `-D__MK64FX512__ -DARDUINO_TEENSY35` | `-D__MK66FX1M0__ -DARDUINO_TEENSY36` |
| `F_CPU` | `120000000` | `180000000` |
| linker script | `mk64fx512.ld` | `mk66fx1m0.ld` |
| loader arg | `--mcu=TEENSY35` | `--mcu=TEENSY36` |

Keep `-DUSB_SERIAL -DLAYOUT_US_ENGLISH -DARDUINO=10813 -DTEENSYDUINO=159`, the direct-link-not-archive rule (the Teensy 3 vector table lives in `mk20dx128.c` and is likewise referenced by no symbol), and the `-isystem` treatment of the vendored trees.

Two build details carried from the Teensy 4.1 port that need restating because they bite harder here:

- **`USING_MAKEFILE` must stay undefined.** Upstream `teensy3/main.cpp` reads that macro exactly as `teensy4/main.cpp` does: defined, the core compiles in its own endless 500ms-on/500ms-off pin-13 blink and `setup()`/`loop()` never run. On the 4.1 that failure was obvious because nothing blinks; here it is **camouflaged**, because bring-up step 1 treats a pin-13 blink as evidence of success. The tell is the pattern — this firmware's indicator is four quick blinks then steady, the core's fallback is a 1Hz square wave forever.
- **Drop `-larm_cortexM4lf_math`**, the way the Teensy 4 Makefile drops `-larm_cortexM7lfsp_math` (a prebuilt CMSIS-DSP library shipped only with Teensyduino). Confirm with `arm-none-eabi-nm -u` that the linked ELF has zero undefined symbols, as was done on the 4.1 — cheap, and the Kinetis core is a different body of code that could reference it.

**One build risk to check early:** the vendored SPI library must match the `cores` snapshot `Drivers/teensy3/` comes from, so version skew between the two has to be ruled out. Vendoring `teensy3/` from the same `cores` master snapshot the SPI library came from is the mitigation. If the `KINETISK` branch still fails to compile, the fallback is a `Drivers/teensy3_libs/SPI/` at a matching version — decide that at the first `make`, not by guessing now.

---

## Phase 4 — Documentation

- `nrf_adapter_source_multiceiver/README.md`: update the Layout block for the three target dirs and the renamed `Drivers/teensy_libs`; add a section per new board covering wiring, deliberate deviations, and build/flash; extend the build/flash sections with the `BOARD=` invocations and the `make clean` requirement. Keep the "how to add a target" paragraph (`README.md:88-91`) — it is still accurate and is now demonstrated four times over.
- **The README must not cite a specific run's measurements or link to a plan document.** A README states what a target is and how it behaves — standing properties like "SPI runs at 9MHz" or "a stalled main loop resets the board in ~3.2s". Validation status, dated benchmark figures, and the method behind them belong here in the plan, and the README does not point at it. The `1.91% vs 4.61%` comparison and its `../nrf_adapter_teensy41_port_plan.md` link have been removed from the Teensy 4.x section on those grounds.
- `readme_EN.md:71-77` is stale from before `477c00d` — it still says `cd nrf_adapter_source_multiceiver && make`, which now fails because there is no Makefile at that path. Fix it to the `cd targets/<t>` form and list the supported boards. **Do not touch `readme.md`** (Chinese).

## Explicitly out of scope

- **Blue Pill USB CDC.** Decided above: USART1 only. `platform.h` already accommodates a CDC variant behind a `HOST_LINK` flag if this is ever wanted as a follow-on.
- **Host-side autodetect.** `mdp_controller/nrf24_adapter.py:163-167` matches only `10C4:EA60` (CP210x). A Blue Pill behind a CP210x bridge autodetects fine; the Teensy boards enumerate as `16C0:0483` and need an explicit port, exactly as the Teensy 4.1 does today. Unchanged behaviour, not a regression — flagged rather than silently widening the change.
- No PyInstaller / packaging work.

---

## Verification

Use `venv/bin/python` for every host-side script.

**Build gate — all six configurations, clean under `-Wall -Wextra`, with size:**

```
cd targets/stm32f030 && make clean && make                    # regression
cd targets/stm32f103 && make clean && make
cd targets/teensy4x  && make clean && make                    # TEENSY41
cd targets/teensy4x  && make clean && make BOARD=TEENSY40
cd targets/teensy3x  && make clean && make                    # TEENSY35
cd targets/teensy3x  && make clean && make BOARD=TEENSY36
```

Confirm the F103 fits its 63K code region and 20K SRAM. The F030 build is ~6.6KB so this is not tight, but read it off the size output rather than assuming.

**Regression gates:**

- **STM32F030 `.bin` must be byte-identical** to its pre-change build (`md5sum` before and after). This is the invariant `README.md:88-91` claims, and nothing here touches that target.
- **Teensy 4.1 `.bin` should be byte-identical too** after the `teensy4x` fold — same `md5sum` gate as the F030, not a weaker substitute. The rename cannot reach the binary: `objcopy -O binary` copies only allocated sections, so the debug paths `-g` embeds never land in the `.bin`; there is no `__FILE__` or `assert()` anywhere in `core/`, `platform.h` or `targets/teensy41/`, so no path strings reach `.rodata` either; and `$(wildcard *.cpp)` still sorts `main.cpp` ahead of `platform_teensy4.cpp`, so link order is unchanged. If it *does* differ, do not settle for comparing `arm-none-eabi-size` output — use the address-independent, rename-aware per-function disassembly diff from the Teensy 4.1 port's Phase 0 gate (which reported 33 functions instruction-identical, 0 removed, 8 added). That localises the change instead of merely flagging one.

**Per-board bring-up, in this order** (each step gates the next):

1. Flash and confirm boot. F103: `st-flash write build/*.bin 0x08000000` via ST-Link (these boards have no bootloader); watch for the four PC13 blinks. Teensy 3.x: `make flash`, confirm the pin-13 boot blink — which is itself the evidence that `setSCK(14)` moved SPI off pin 13. **Check the pattern, not just the presence:** four quick blinks then steady is the firmware; a 1Hz square wave forever is the core's `USING_MAKEFILE` fallback with `setup()` never called (see Phase 3). Teensy 4.0: `make BOARD=TEENSY40 flash`; that target has no blink.
2. `host_link_test.py --port <port>` — framer, command dispatch and the settings store, with no radio wired. Isolates `uart.c`/CDC and `store_load`/`store_save` before the radio can confound anything.
3. **Settings persistence across a real power cycle.** Set a baudrate and address, **remove power**, confirm both are retained. `host_link_test.py`'s `CMD_REBOOT` case only proves persistence across a *soft* reset (`SCB_AIRCR`, or IWDG on the F103) — power never drops, so it does not exercise the F103 flash page (`0x0800FC00`) or Kinetis FlexNVM the way this does.
4. **Radio register read-back, with the radio wired but before any traffic.** Do not skip straight to `pipe_test.py`: this is where an SPI or reset-timing mistake surfaces first, and it is the step that isolates the F103's SPI rewrite (older IP, no `CR2_DS`/`FRXTH`) and the Teensy 3.x's new Kinetis SPI0 + `setSCK(14)`. After `CMD_NRF_SET` plus `CMD_NRF_OPEN_PIPE`, confirm `RF_CH`, `RF_SETUP`, `EN_RXADDR`, `EN_AA`, `SETUP_AW`, `SETUP_RETR`, `CONFIG`, `RX_PW_P0` and all three 5-byte address registers hold their written values. **Derive the expected values by hand from `nrf_configure()`'s call order before the run, not from the result.** The mechanism differs by board:
   - **F103 has SWD** — read them over OpenOCD/GDB, as the F030 refactor did (recorded then: `RF_CH=0x4e`, `RF_SETUP=0x0f`, `EN_RXADDR=0x01`, `CONFIG=0x0f`, `FIFO_STATUS=0x11`).
   - **Teensy 3.x has no SWD path**, and `core/` exposes no register-read command. Re-create the temporary `REP_REG_DUMP` (0xFE) frame in the target's `main.cpp` that the Teensy 4.1 bring-up used — emitted once a second, reading through `platform.h`'s own SPI primitives. It was removed after that phase, so it no longer exists in the tree. Remove it again afterwards and re-check the build size.
   - **Teensy 4.0 can skip this step.** Its platform code is byte-for-byte the already-validated 4.1 implementation and its SPI path is unchanged; the only new variables are the linker script and flash size, which step 1 and the build gate already cover.
5. `pipe_test.py <port>` — two-device pipe test with the radio wired. **Update its constants first or it fails against every board.** `pipe_test.py:15-24` still holds an obsolete address pair (`AABBCCDDEE` / `153614FAE1`) and channel from before the devices moved onto the MDPBus scheme. Bring the script onto the fixed bench configuration at the top of this document — do not re-pair the devices for this test and back again:
   - `P906_ADDR = bytes.fromhex("AABBCCDDE2")` — pipe 1 under `MDPBus._pipe_address()` (`base[:4] + (0xE1 + pipe)`, `bus.py:98-100`), and pipe_test.py's *primary* address (`RX_ADDR_P0`/`TX_ADDR`).
   - `L1060_ADDR = bytes.fromhex("AABBCCDDE3")` — pipe 2 under MDPBus, carried on **pipe 1** here.
   - `FREQ = 2521`; `CHANNEL_BYTE` derives 121.
   - `SKIP_DISCOVERY` stays `True` — the devices are already at these addresses, which is the whole point.

   The idcodes at `pipe_test.py:16,20` already match `settings.json` (P906 `FE2597EC`, L1060 `09B93C19`). Keep the script on **pipe 1 only**: `RX_ADDR_P2..P5` are single-byte registers sharing their upper four bytes with `RX_ADDR_P1`, so pipe 2 cannot hold an independent address. `E2` on P0 and `E3` on P1 are both full 5-byte registers, so this pairing is safe — and it is what the GUI already runs on this bench.
6. GUI, 60s two-device run against the P906 + L1060 bench, then `tools/noack_report.py`. **Run it at least twice** — see the first bullet. Four things needed to read the result correctly:
   - **A single run's no-ack rate is worth nothing.** Measured 2026-08-01: two back-to-back runs with *nothing changed* gave 0.32% and 0.06%. That spread is wider than the gap between any two adapter firmwares ever compared here, so one bad run reads as a regression when it is weather. What is stable and diagnostic is *where* the no-acks land — essentially all on P906 (`..E2`), with L1060 (`..E3`) at exactly 0 on every board. A no-ack on `..E3` would be a real signal.
   - ~~**Recorded baselines: STM32F030 4.61%, Teensy 4.1 1.91%.**~~ **Both retired 2026-08-01.** Re-measured with unchanged firmware and a clean bench, the F030 gives 0.08–0.11% and the Teensy 4.1 gives 0.04–0.13%. Do not compare a new board against the old figures — everything now sits under ~0.2%, and "near 2%" is a finding rather than par.
   - **TX-FIFO overflow onset sits at ~180–185 req/s.** The F103 (72MHz) and Teensy 3.5 (120MHz) will most likely run below that, so **zero overflows on those boards is expected and is not a point in their favour** — it mostly means they cannot drive the link that hard. Compare req/s alongside it.
   - `gui_source/settings.json` gets repointed at the adapter under test and **must be restored**. It is in `.gitignore`, so git will not remind you.
7. **Watchdog** — stall the main loop and confirm reset at roughly the configured timeout (~3.3s F103 IWDG, ~3.5s Teensy 3.x WDOG, ~3.5s Teensy 4.0). Untested watchdog code is worse than none. **Not skippable on Teensy 3.x** — it is the only step that would catch the silent no-fire failure described in Phase 3.

**Bench discipline** (from `README.md:243-246` and prior bench sessions):

- Before A/B-ing adapters against the same devices, **park the idle adapter on an unused address and channel.** An adapter left in RX on the bench address auto-ACKs packets meant for the device under test and measurably corrupts throughput and error-rate numbers.
- Pairing mode is the only way to dispatch to a device, and it needs a human at the front panel **one device at a time** — two in discover mode at once collide on the 2478 broadcast. Step 5 above is arranged so no re-pairing is needed at all.
- Both devices are radio-deaf for ~3–4.5s after power-on; allow for that before calling a connect failure real.
- The headless GUI (`QT_QPA_PLATFORM=offscreen`) segfaults during interpreter teardown, *after* the event loop exits and all results have printed. It is a PyQt/pyqtgraph teardown artifact, not a run failure — do not chase it on each of the four boards.
- If a new Teensy target's SPI misbehaves, **do not open a comparative analysis against the RF24 library.** That was the `multi-platform-wip` branch's diagnosis and it was wrong: the real faults were per-byte `beginTransaction`/`endTransaction` inside an asserted CSN, a 1MHz clock, and multi-byte transfers with no transaction at all. Bracketing whole CSN-asserted transactions is the whole of it.

**Report actual numbers.** If a board underperforms the existing adapters, say so with the measurements rather than attributing it to a guessed cause.

## Measured results — Blue Pill (STM32F103), 2026-07-30

All seven bring-up steps passed. Steps 1–2 were done in a prior session; 3–7 below.

**Step 3 — persistence across a real power cycle.** A distinctive record (ch 2451, address `12:34:56:78:9A`, 1Mbps, 0dBm, crc8, arc 5, ard 500µs, baud 115200 — unlike both the defaults and the bench config, so a stale record cannot fake a pass) was written, then read back over SWD at `0x0800FC00` *before* the power cycle to separate a write failure from a read failure:

```
36 30 39 50  14 00  93 09 00 03 01 20 05 00 f4 01 05 12 34 56 78 9a 00 c2 01 00 ... 0c 98
magic "P906"  len=20  ch=2451          pw=32 arc=5  ard=500  aw=5  addr        baud=115200   crc
```

Matches the hand-derivation of `persisted_settings_t` including its padding byte; CRC16 recomputed independently as `0x980c`. After a physical power removal the board came back **at 115200** (silent at 921600 as a negative control) and reported the saved config. `persistence_test.py --phase arm|verify|restore` automates this and is target-neutral.

**Step 4 — radio register read-back.** All 14 registers matched the values derived by hand beforehand, in both the boot-default and the post-`CMD_NRF_SET` bench configuration — including `RX_ADDR_P1 = C2:C2:C2:C2:C2` in the default case, which is the chip's own reset value rather than anything the firmware writes. The five values the F030 refactor recorded (`RF_CH=0x4e`, `RF_SETUP=0x0f`, `EN_RXADDR=0x01`, `CONFIG=0x0f`, `FIFO_STATUS=0x11`) fall out of the same derivation, so this is a like-for-like cross-board result. Supporting reads: `SPI1_CR1=0x0354`, `SPI1_CR2=0x0000` (DFF=0 — the 8-bit framing this IP gets without `CR2_DS`), `GPIOA_CRL=0xb4b22844`.

Two notes on method. First, nRF registers live in the radio, not MCU memory, so SWD cannot read them directly; `read_register()` is static and `-O2` inlines it away, but `nrf_csn_low`/`spi_transfer_byte`/`nrf_csn_high` survive as symbols, so `targets/stm32f103/nrf_regdump.gdb` drives R_REGISTER transactions through the firmware's own SPI path — which is what actually exercises the SPI rewrite. Second, an all-`0x00`/`0xff` dump means MISO never drove (wiring or SPI), not a configuration mismatch; that is exactly what an unpowered radio produced on the first attempt.

**Step 5 — `pipe_test.py`.** All four gets answered, L1060 tagged `pipe=1` while the P906 stayed on pipe 0, and the P906 was unaffected by the retarget round-trip. The script's obsolete address pair and channel were moved onto the bench configuration first; it is gitignored, so that edit is not tracked.

**Step 6 — 60s two-device GUI run.**

| | P906 samples | L1060 samples | radio sends | no-acks | overflows | failed |
|---|---|---|---|---|---|---|
| Teensy 4.1 | 8508 | 8265 | 6962 | 133 (1.91%) | 0 | 0 |
| STM32F030 | 7296 | 6825 | 6140 | 283 (4.61%) | 0 | 0 |
| **STM32F103** | **7992** | **7971** | **6694** | **8 (0.12%)** | **0** | **0** |

~111 req/s, between the other two adapters on sample count. Two caveats, per this document's own guidance. All 8 no-acks were P906 (`..E2`) traffic with the L1060 (`..E3`) clean — the same bench-characteristic pattern seen on both existing adapters, so the 15× lower rate than the Teensy should not be read as adapter superiority without a same-session A/B. And at ~111 req/s this board sits well below the ~180–185 req/s overflow onset, so zero overflows is expected rather than a merit. `gui_source/settings.json` already pointed at `/dev/ttyUSB0`, which is this adapter, so it needed no repointing or restoring. The removed temporary GUI driver was recreated as `gui_source/bench_gui_run.py`.

**Step 7 — watchdog.** A stalled main loop resets the board in 3.167–3.218s over four consecutive stalls (mean 3.180s) against ~3.28s nominal; the ~3% shortfall implies an LSI near 41.2kHz, comfortably inside its 30–60kHz spec.

**Do not test this with a debugger halt.** OpenOCD sets `DBGMCU_CR` to `0x00000307`, and bit 8 (`DBG_IWDG_STOP`) freezes the IWDG whenever the core is halted — the core sat halted 15s with no reset, which reads as a broken watchdog but proves nothing. Clearing the bit over the telnet interface dropped the SWD link instead. Use a temporary stall in the *target's own* `main.c` (never shared `core/`) emitting a marker frame immediately before `while(1){}`, so the reset is timed directly rather than inferred from boot-to-boot spacing. Reverted afterwards, the build returned to exactly 6188B text / 24B data / 1224B bss and `host_link_test.py` passed 13/13 again.

## Measured results — Teensy 3.5, 2026-07-30

All seven bring-up steps passed. Step 1 required a real fix along the way; steps 2–7 below.

**Step 1 found a genuine firmware bug, not a wiring issue.** The first flash showed *no* LED activity at all — neither the four-blink indicator nor the `USING_MAKEFILE` fallback's 1Hz blink. Isolated via `udevadm monitor --udev --subsystem-match=tty` (plain `ls`/polling was too imprecise): the board was resetting in a tight, consistent loop. Disabling `watchdog_init()` entirely made it stable immediately, isolating the watchdog as the cause rather than SPI/wiring (`SPI.setSCK(14)` was independently confirmed correct by tracing `Drivers/teensy_libs/SPI/SPI.h`'s `sck_pin` table — pin 14 is PJRC's own documented alternate SCK for SPI0 on the K64/K66, not an invented pin). The `WDOG_STCTRLH_CLKSRC` bit set (as the plan originally specified) produced resets whose *measured* period didn't match the plan's assumed 3.5s at all; see Step 7 below for the full derivation. Fix: `CLKSRC` clear, `TOVALL=893` (not the plan's 3500) — committed in `platform_teensy3.cpp`, whose comment carries the derivation forward.

**Step 2 — `host_link_test.py`.** 8/8 PASS: echo, radio-defaults query, baudrate retune, EEPROM save, a soft-reboot round-trip, unrecognized-command handling, `CMD_RESET`.

**Step 3 — persistence across a real power cycle.** `persistence_test.py --phase arm|verify|restore` (target-neutral, from the Blue Pill session). Armed with a distinctive config (baud 115200, radio config `3300030120050205123456789a`) and a real USB unplug/replug. The board came back **at 115200** (not the compiled-in 921600 default) and reported the exact armed radio config, proving both the baudrate and the radio settings survived a genuine power cycle on Kinetis FlexNVM. The negative-control step (silence expected at 921600) legitimately fails on this target and is not a bug: `HOST_LINK_USB_CDC` has no physical line rate, so the board answers regardless of what baud pyserial requests — that check is only meaningful on the Blue Pill's real UART.

**Step 4 — radio register read-back.** No SWD path on this target, so a temporary `REP_REG_DUMP` (0xFE) frame was added to `main.cpp` (removed afterward), emitted once a second and reading through `platform.h`'s own SPI primitives — the same technique used for the Teensy 4.1 bring-up. All 21 register checks passed against values hand-derived from `core/protocol.c`'s `nrf_configure()` and `core/nrf24l01p.c`'s `open_rx_pipe()` call order, in both the boot-default state and after `CMD_NRF_SET` (bench config: freq 2521, 2Mbps, 4dBm, crc16, arc 12, ard 250µs, address `AA:BB:CC:DD:EE`) plus `CMD_NRF_OPEN_PIPE` (pipe 1, P906's `AA:BB:CC:DD:E2`):

| | boot-default | post-set |
|---|---|---|
| CONFIG | 0x0F | 0x0F |
| RF_CH | 0x4E | 0x79 |
| RF_SETUP | 0x0F | 0x0E |
| EN_RXADDR / EN_AA | 0x01 | 0x03 |
| SETUP_RETR | 0x03 | 0x0C |
| RX_ADDR_P1 | `c2c2c2c2c2` (chip POR default) | `e2ddccbbaa` |

`RX_ADDR_P0`/`TX_ADDR` matched `eeddccbbaa` (the bench base address, reversed) in both states, and `RX_ADDR_P1`'s untouched-default value matches what the F103 bring-up saw on its own chip — a like-for-like cross-board result, and direct proof the `setSCK(14)` SPI0 pin move is electrically sound, not just non-hanging.

**Step 5 — `pipe_test.py`.** All four gets answered, L1060 tagged `pipe=1` while the P906 stayed on pipe 0, and the P906 was unaffected by the retarget round-trip. `/dev/ttyUSB0` (Blue Pill) was disconnected at the time, so no idle-adapter parking was needed.

**Step 6 — 60s two-device GUI run.**

| | P906 samples | L1060 samples | radio sends | no-acks | overflows | failed |
|---|---|---|---|---|---|---|
| Teensy 4.1 | 8508 | 8265 | 6962 | 133 (1.91%) | 0 | 0 |
| STM32F030 | 7296 | 6825 | 6140 | 283 (4.61%) | 0 | 0 |
| STM32F103 | 7992 | 7971 | 6694 | 8 (0.12%) | 0 | 0 |
| **Teensy 3.5** | **3852** | **3837** | **3923** | **7 (0.18%)** | **0** | **0** |

~65.6 req/s — noticeably below every other board (all in the ~103–116 req/s range), despite this being the fastest core besides the Teensy 4.x. No cause has been isolated for the gap; reported as a plain measurement rather than attributed to a guess.

> **SOLVED 2026-08-01, and it was a real firmware defect on this target — not the bench.** `uart_write()` in `platform_teensy3.cpp` called `HOST_PORT.write()` without `send_now()`. Teensy 3.x's `usb_serial_write()` only hands a packet to the USB hardware once it reaches `CDC_TX_SIZE` (64B); a partial packet instead arms `usb_cdc_transmit_flush_timer = TRANSMIT_FLUSH_TIMEOUT`, **5ms** (`Drivers/teensy3/usb_serial.c:229-234`). Every reply this firmware sends is far under 64B, so every one waited out that timer — and since the host does not issue the next request until the reply lands, the whole link ran at the flush timer's cadence. The Teensy 4.x core has no equivalent per-write gate, which is why only the 3.x boards showed it, and the F103's TinyUSB path already called `tud_cdc_write_flush()` for the same reason.
>
> Retested on a Teensy 3.5, same bench, single adapter: **before** 3385 / 3360 radio sends and ~50 samples/s per device over two runs; **after** adding one `HOST_PORT.send_now()`, **7219 / 6963 sends and 147.1 / 140.1 samples/s**, with no-acks at 0.04% and 0.11%. That puts it level with every other target (F103 7020, Teensy 4.1 7373). Two runs each way, per the run-to-run variance caveat — and this gap is ~2×, far outside it.
>
> **The Teensy 3.6 inherits the same fix and has not been retested.** It shares `platform_teensy3.cpp` and recorded the same ~64.5 req/s, so the same result is expected but is not evidence until measured. All 7 no-acks were P906 (`..E2`) traffic, L1060 clean — the same bench-characteristic pattern seen on every other board. At ~65.6 req/s this board sits far below the ~180–185 req/s overflow onset, so zero overflows is expected, not a merit. `gui_source/settings.json` already pointed at `/dev/ttyACM0`, so no repointing was needed. The mdp.log from this run carried a prior session's entries too; `tools/noack_report.py` was run against a sliced copy (from the run's own `---- NEW SESSION ----` marker onward) to avoid conflating the two.

**Step 7 — watchdog.** Confirms the Step 1 fix's calibration: a stalled main loop (temporary marker-frame-then-`while(1){}` in `main.cpp`, reverted afterward) resets the board at 3.498–3.501s over four consecutive stalls (mean 3.499s) against a 3.5s target — timed directly via serial reads to the marker frame and the subsequent `ttyACM0` disconnect, not inferred from boot-to-boot spacing. `TOVALL=893` was derived from two stall-test data points rather than the reference manual's clock math, which did not hold up on this silicon (see `platform_teensy3.cpp`'s comment for the derivation). Reverted afterward, the build returned to exactly 17392B text / 0B data / 3288B bss and `host_link_test.py` passed 8/8 again.

> **Superseded 2026-08-01.** The 3.499s above is a measurement artifact — timing to the `ttyACM0` *disconnect* also counts USB teardown after the reset. `TOVALL=893` really fired at 4.471s. The current value is **698**, verified at 3.491s; see the 3.6's step 7 below for the corrected method and the full re-measurement of both boards.

## Measured results — Teensy 3.6, 2026-07-31

All seven bring-up steps passed. Step 7 required a real fix — the 3.5's `TOVALL` did not transfer.

**Step 1 — boot.** Four quick blinks then steady, confirmed by direct observation of the board (no `USING_MAKEFILE` fallback).

**Step 2 — `host_link_test.py`.** 8/8 PASS.

**Step 3 — persistence across a real power cycle.** `persistence_test.py --phase arm|verify|restore`. Armed with a distinctive config (baud 115200, radio config `3300030120050205123456789a`) and a real USB unplug/replug. The board came back at 115200 and reported the exact armed radio config. The negative-control check (silence expected at 921600) legitimately fails on this target for the same reason it did on the 3.5: `HOST_LINK_USB_CDC` has no physical line rate, so the board answers regardless of what baud pyserial requests.

**Step 4 — radio register read-back.** Same temporary `REP_REG_DUMP` (0xFE) technique used on the 3.5, re-added to `main.cpp` and removed afterward (build size confirmed to return to exactly 17688B text / 0B data / 3344B bss). All 21 register checks passed in both the boot-default and post-`CMD_NRF_SET`/`CMD_NRF_OPEN_PIPE` states, matching the same values recorded for the 3.5 byte-for-byte (including `RX_ADDR_P1 = c2c2c2c2c2` as the chip's own POR default).

**Step 5 — `pipe_test.py`.** All four gets answered, L1060 tagged `pipe=1`, P906 unaffected by the retarget round-trip. `/dev/ttyUSB0` (Blue Pill) was disconnected for this run — no idle-adapter parking was needed.

**Step 6 — 60s two-device GUI run.**

| | P906 samples | L1060 samples | radio sends | no-acks | overflows | failed |
|---|---|---|---|---|---|---|
| Teensy 4.1 | 8508 | 8265 | 6962 | 133 (1.91%) | 0 | 0 |
| STM32F030 | 7296 | 6825 | 6140 | 283 (4.61%) | 0 | 0 |
| STM32F103 | 7992 | 7971 | 6694 | 8 (0.12%) | 0 | 0 |
| Teensy 3.5 | 3852 | 3837 | 3923 | 7 (0.18%) | 0 | 0 |
| Teensy 3.6 | 3900 | 3894 | 3986 | 13 (0.33%) | 0 | 0 |

~64.5 req/s, in line with the Teensy 3.5's ~65.6 req/s and, like the 3.5, well below every other board (~103–116 req/s). All 13 no-acks were P906 (`..E2`) traffic, L1060 clean — the same bench-characteristic pattern seen on every other board. Zero overflows is expected at this rate, not a merit. `gui_source/settings.json` already pointed at `/dev/ttyACM0`, so no repointing was needed.

> **Cause found on the 3.5 and fixed in shared code — see the `send_now()` note under the Teensy 3.5's step 6 above.** The missing `HOST_PORT.send_now()` was in `platform_teensy3.cpp`, which both boards share, so the 3.6's ~64.5 req/s had the same explanation.
>
> **Confirmed on 3.6 hardware 2026-08-01: 6991 / 6707 radio sends and 140.5 / 133.5 samples/s per device over two runs, with 2 and 4 no-acks (0.03% / 0.06%)** — against 3986 sends and ~64.5 req/s before. Same ~2× as the 3.5, and it puts both 3.x boards level with every other target. `host_link_test.py` ALL PASS and `pipe_test.py` clean both ways on the same firmware.

**Step 7 — watchdog: the 3.5's `TOVALL=893` does not transfer.** The first stall test (identical marker-frame-then-`while(1){}` technique) measured a consistent **5.343s** (four trials 5.342–5.344s) against the 3.5's own 3.499s for the same register value — not measurement noise, and not explained by "same platform code" assumptions. A three-point stall-test calibration run directly on this chip (`TOVALL=200 -> 1.831s`, `893 -> 5.343s`, `1786 -> 9.867s`, each the tight cluster of four trials with the first post-flash trial discarded as a consistent outlier across all three runs) fits a clean linear model at **rate ≈ 197.4 counts/sec, offset ≈ 0.82s** — close to but not identical to the 3.5's own ~200.0 counts/sec, ~0.97s model (the offsets aren't directly comparable across the two derivations: this run's harness added an explicit `watchdog_refresh()` immediately before the marker, which the original 3.5 derivation did not do). Solving for a 3.5s result gives `TOVALL=529`, reverified directly on hardware: four trials at 3.493–3.499s (mean 3.496s).

> **RE-MEASURED 2026-08-01, and both boards' `TOVALL` values look wrong — the original calibration measured the wrong quantity.** A different stall method (stall at a fixed `millis()`, then time the *period between reply bursts*, which is one full reset-to-reset cycle) gives, on the same firmware: **3.6 with `TOVALL=529` → 2.698s** (n=5, ±4ms) and **3.5 with `TOVALL=893` → 4.471s** (n=5, ±11ms). Neither is 3.5s, and the two boards are ~1.8s apart — the opposite of what the per-board branch was introduced to achieve.
>
> Those numbers are almost exactly `TOVALL / rate` with no offset term: `893/200.0 = 4.465` vs 4.471 measured, `529/197.4 = 2.680` vs 2.698. Predicting from that, `TOVALL=700` on the 3.6 should give ~3.55s — **measured 3.564s** (n=5, ±8ms). Two points on the 3.6 fit `T = TOVALL/197.5 + 0.019s`, i.e. a ~19ms measurement overhead rather than the 0.82s offset the original three-point fit produced.
>
> **The likely explanation for the discrepancy:** the original method timed from a marker frame to the **`ttyACM0` disconnect**, which includes USB teardown after the reset. That inflates the reading by roughly the offset those fits absorbed (~0.8–1.0s), so a real ~2.7s timeout measured as "3.499s". The period method never sees teardown, because a full cycle counts every stage exactly once.
>
> **Recommended:** `TOVALL` ≈ **688** (3.6) and ≈ **698** (3.5) — both boards land near ~197–201 counts/sec, so the large split between 529 and 893 is itself suspect.
>
> **APPLIED AND VERIFIED, both boards on the bench together, 2026-08-01** (3.6 on `ttyACM0`, 3.5 on `ttyACM1`, the idle one parked off-channel each time via a temporary `CMD_NRF_SET` — RAM only, never `CMD_NRF_SAVE`):
>
> | board | `TOVALL` | measured | trials |
> |---|---|---|---|
> | Teensy 3.6 | 688 | **3.505s** (3.502–3.509) | n=5 |
> | Teensy 3.5 | 698 | **3.491s** (3.490–3.492) | n=4, first post-flash trial (3.446s) discarded as the usual outlier |
>
> Both are within ±10ms of the 3.5s target, against 2.698s and 4.471s before. The predictive model held to ~2ms on both boards, which is the strongest evidence that `T = TOVALL/rate + ~19ms` — not the old `TOVALL/rate − offset` — is the right description of this silicon. Production firmware rebuilt clean per board (3.5: 17492B/0/3288B, 3.6: 17788B/0/3344B), flashed, `host_link_test.py` ALL PASS on both, and a 30s continuous-echo watch on both boards simultaneously showed 120 replies and zero drops each — the 100ms refresh cadence still outruns the shorter timeout in normal operation. `platform_teensy3.cpp`'s comment now carries the corrected derivation and an explicit warning against the disconnect-timing method.

Because the 3.5 and 3.6 provably cannot share one `TOVALL` and hit the same real-world timeout (893 gives 5.34s on the 3.6; 529 would give ~1.68s on the 3.5 by its own model — a regression on already-validated hardware), `platform_teensy3.cpp`'s `watchdog_init()` now branches on `ARDUINO_TEENSY36` — the same chip-select macro the Makefile's `BOARD_DEF` already injects, following the existing `HOST_LINK_SERIAL1`/`HOST_LINK_USB_CDC` precedent in the same file rather than introducing a new mechanism. This is a real, hardware-confirmed exception to Phase 3's "no platform code differs between them" assumption, not a violation of the project's actual Makefile-driven-macro design rule. Both `BOARD` configurations were rebuilt clean after the change and matched their pre-change sizes exactly (3.5: 17392B/0/3288B; 3.6: 17688B/0/3344B), and `host_link_test.py` passed 8/8 again on the 3.6 with the real firmware.

> **The per-board branch survives the 2026-08-01 recalibration, but the reasoning above does not.** "893 gives 5.34s on the 3.6" was measured with the disconnect method and is not a real figure. On the corrected method the two constants are **688 and 698** — a 1.4% split, not 893-vs-529, because the two chips' watchdog clocks differ by only ~1.4% (~197.5 vs ~200 counts/sec). The branch stays because 698 on a 3.6 would give ~3.53s and 688 on a 3.5 ~3.46s: both would still be *acceptable*, so this is now a precision choice rather than the "regression on already-validated hardware" the original text described. Keep it — the constants are measured per board and cost nothing — but do not cite the old 5.34s figure.

## Measured results — Teensy 4.0, 2026-07-31

All seven bring-up steps passed (step 4 skipped per plan — 4.0 shares the already-validated 4.1 platform code and SPI path). Board enumerated as `16c0:0483` ("Teensyduino Serial") once the real firmware was running, versus `16c0:0486` ("Teensyduino RawHID") under the factory-default test program that ships on a new board — the absence of a `/dev/ttyACM*` before flashing was that RawHID-vs-CDC distinction, not a wiring or driver problem; `lsusb` confirmed the board was fully enumerated throughout.

**Step 1 — boot.** `make BOARD=TEENSY40 flash`. No blink on this target (`pins.h` — pin 13 is SCK), so boot was confirmed by `/dev/ttyACM0` appearing and `host_link_test.py` passing. `teensy_loader_cli`'s `--mcu` must match `BOARD` on every invocation — running `make flash` without repeating `BOARD=TEENSY40` silently falls back to the `TEENSY41` default and flashes the correct `.hex` under the wrong `--mcu` (caught and re-flashed correctly before proceeding; harmless here since the image is far under either chip's flash size, but worth calling out since the Makefile has no cross-check).

**Step 2 — `host_link_test.py`.** 8/8 PASS, with two of the intermediate assertions initially reading as failures: the pre-`CMD_RESET` `CMD_NRF_QUERY` returned `79010602200c0105aabbccddee` instead of the expected compiled-in-defaults payload. This was stale emulated-EEPROM content already present in flash before this firmware was ever loaded (Teensy's EEPROM emulation lives in flash and isn't erased by flashing a new image) — not a firmware defect: the same query after `CMD_RESET` returned exactly `4e01070220030105aabbccddee`, the expected default, proving the store/load path and compiled defaults are both correct. A second full run after the watchdog test (below) passed clean start to finish with no stale-data artifact.

**Step 3 — persistence across a real power cycle.** `persistence_test.py --phase arm|verify|restore`, armed with radio config `3300030120050205123456789a` and baud 115200, board physically unplugged and replugged. Came back at 115200 and reported the exact armed config. The negative-control check (silence expected at 921600) fails here for the same documented reason as the Teensy 3.x targets: `HOST_LINK_USB_CDC` has no physical line rate, so the board answers regardless of what baud pyserial requests — not a persistence failure.

**Step 5 — `pipe_test.py`.** All gets answered correctly: P906 on pipe 0 unaffected by opening pipe 1 or by the L1060 retarget/retarget-back round trip; L1060 answered cleanly on pipe 1.

**Step 6 — 60s two-device GUI run**, via `bench_gui_run.py` against `gui_source/settings.json` (already pointed at `/dev/ttyACM0`, no repointing needed). No-ack figures below are scoped to just this run's log window (`gui_source/mdp.log` accumulates across sessions; the full-log aggregate mixes in the F103/3.5/3.6 bring-ups).

| | P906 samples | L1060 samples | radio sends | no-acks | overflows | failed |
|---|---|---|---|---|---|---|
| Teensy 4.1 | 8508 | 8265 | 6962 | 133 (1.91%) | 0 | 0 |
| STM32F030 | 7296 | 6825 | 6140 | 283 (4.61%) | 0 | 0 |
| STM32F103 | 7992 | 7971 | 6694 | 8 (0.12%) | 0 | 0 |
| Teensy 3.5 | 3852 | 3837 | 3923 | 7 (0.18%) | 0 | 0 |
| Teensy 3.6 | 3900 | 3894 | 3986 | 13 (0.33%) | 0 | 0 |
| Teensy 4.0 | 8589 | 8565 | 7084 | 9 (0.13%) | 0 | 0 |

~118 req/s, in line with the Teensy 4.1's ~116 req/s (expected — same platform code and host link) and the best no-ack rate of any board tested. 6 of the 9 no-acks were P906 (`..E2`) Type-8 traffic, 2 were L1060 (`..E3`) Type-8, 1 was P906 Type-7 — still overwhelmingly the same bench-characteristic pattern (P906 dominant) seen on every other board. Zero overflows at ~118 req/s is consistent with the ~180–185 req/s onset, not a merit specific to this board.

**Step 7 — watchdog.** First pass used a blind echo-poll (no marker) and got a rough **4.067s** downtime — cruder than the marker-frame technique used on the F103/Teensy 3.x targets, since the polling loop only bounds when the stall began rather than timing it precisely. Redone properly with the same marker-frame technique: `loop()` gets `if (millis() > 2000) { uart_write("WDSTALL\n", 8); while(1){} }`, and a host script times marker-receipt to the first successful echo after reset. Five trials at the shipped `WT=6` (nominal 3.5s) clustered tightly at **4.078–4.128s** (mean 4.115s) — reproducible, but sitting well above nominal, unlike the Teensy 3.5/3.6 which landed within 0.1% of their targets.

Rather than report that gap unexplained, a second calibration point was taken at `WT=0` (nominal 0.5s): five trials clustered at **1.077–1.079s**. Fitting a line across the two `WT` settings gives a step size of **~0.506s/count** — within ~1% of the 0.5s WDOG1 spec, so the hardware timeout itself is accurate — against a **~0.58–0.6s fixed offset** present at both points. That fixed offset is reset-to-responsive overhead (boot + full USB CDC re-enumeration on the host side), not part of the watchdog timeout proper; it does not exist on the F103 (external USB-UART bridge never resets) and was apparently small enough to be invisible on the Kinetis-based Teensy 3.5/3.6. **`WT=6`'s real-world reset-to-recovery time is ~4.1s, not 3.5s**, purely because of that fixed USB re-enumeration cost — the watchdog itself fires on schedule. Both `WT=0` and the stall/marker code were reverted after use; the rebuilt real firmware matched its pre-change size exactly (20800B/8896B/13856B) and `host_link_test.py` passed 8/8 again.
