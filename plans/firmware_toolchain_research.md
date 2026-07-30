# Firmware Toolchain Research — Native USB Host Link & Vendor-SDK Build Tooling

**2026-07-30.** Analysis only — no code changes made as part of this doc. Written after the
four-board `nrf_adapter_new_targets_port_plan.md` expansion landed, prompted by two questions:
(1) can either STM32 board's onboard USB port replace the external USB-UART bridge, and (2) now
that `nrf_adapter_source_multiceiver` spans six build configurations across three MCU families,
does it still make sense to hand-roll everything bare-metal, or should some of it move to a
vendor SDK — kept in view of the stated goals: CLI-only, no account needed anywhere in the
chain, easily installable on Linux.

**Revised 2026-07-30** after a review pass that checked every claim against the tree. Several
figures and mechanisms were wrong; §2b's conclusion is reversed; a new §2c covers Teensyduino
proper (asked separately from PlatformIO); and a new §1a answers the question that turned out to
decide §1 — whether in-ISR radio servicing is required on the STM32 targets. It is not, which
**reverses §1 as well**: F103 native USB CDC is now viable and planned. §1a also records the
retirement of the byte-identical regression gate. Corrections are marked in place rather than
silently folded in, so the superseded reasoning stays visible.

This is a research record, not a plan. See `nrf_adapter_new_targets_port_plan.md` /
`nrf_adapter_teensy41_port_plan.md` for what's been executed, and
`nrf_adapter_f103_usb_cdc_port_plan.md` for the work these findings produced. The decisions and
their reasoning stay here — the README carries neither.

---

## 1. Native USB host link instead of the external USB-UART bridge

**Question:** both STM32F boards in this project have USB connectors — can the host link move
onto that instead of the external USB-UART adapter on PA9/PA10?

**F030 (`targets/stm32f030`, STM32F030F4Px): not possible at the silicon level.** The
STM32F030x4/x6/x8 family has no USB peripheral at all — only certain other STM32F0 parts
(F042/F070/F072/F078/etc.) include a USB-FS device block. Whatever USB connector the bare dev
board carries, if any, isn't wired to MCU data pins, because there's nothing on the die to wire
it to. The external bridge on this board is structural, not a design choice.

**F103 (`targets/stm32f103`, Blue Pill): possible, already scoped and deliberately deferred.**
The STM32F103C8T6 has a real USB-FS device peripheral on PA11/PA12. This was already evaluated
during the new-targets port plan and explicitly called out as **out of scope**
(`nrf_adapter_new_targets_port_plan.md:205`, `nrf_adapter_source_multiceiver/README.md:174-176`):
implementing it would need a vendored USB device stack plus a CDC glue layer. That earlier note
judged it "more work and more new bug surface than every other phase combined" — **that estimate
is too high; see the first bullet.** What it would take, at a high level:

- A USB device stack — descriptors, endpoint 0 control transfers, PMA buffer management — plus a
  CDC-ACM class layer (line coding, bulk IN/OUT) on top. No USB device code for F1 sits anywhere
  in this repo today, and nothing in the tree is close enough to reuse, unlike the register-level
  UART/SPI/GPIO ports which carried over from the F030 almost verbatim. But "from scratch" is not
  the only option and is what inflated the original estimate: TinyUSB's `stm32_fsdev` port and
  libopencm3 both cover F103 CDC-ACM, are `git clone`-able with no account, and are CLI-buildable
  — so this is a vendor-and-integrate job, not a greenfield stack.
- **Pins and clock are already free — neither is a cost.** `targets/stm32f103/gpio.h` uses
  PA2/3/4/5/6/7/9/10 and PC13, leaving PA11/PA12 untouched; and `system_clock.c:20-33` runs
  HSE 8MHz × 9 = 72MHz, off which the USB peripheral's mandatory 48MHz falls straight out of the
  /1.5 prescaler. No clock rework, no pin conflict.
- **The D+ pull-up is not a factor on this bench.** Blue Pills commonly ship with 10k on R10
  instead of the required 1.5k, which is the usual first objection to using the onboard USB. The
  board in use here had that corrected on receipt. Settled — do not re-raise it.
- **The in-ISR servicing model must be retired first — but that is a ~15-line change, not a
  blocker.** See §1a below. An intermediate draft of this document treated the in-ISR model as a
  fixed property of the target and concluded CDC was therefore not a drop-in behind `HOST_LINK`.
  That was wrong: the model is inherited from the shipped firmware, not required by anything, and
  converting F103 to the Teensy's deferred model removes the coupling entirely.
- Host-side autodetect breaks: `mdp_controller/nrf24_adapter.py:163-167` matches only the CP210x
  bridge's VID/PID. A native-CDC Blue Pill enumerates under a custom descriptor, so it would need
  an explicit `--port`, same as the Teensy boards already require today — not a regression, just
  parity.
- Flashing is unaffected either way — Blue Pills have no bootloader, flashing is already over
  SWD/ST-Link regardless of the host link.

## 1a. Is in-ISR radio servicing actually required on the STM32 targets?

**No. It is inherited from the shipped firmware, not demanded by anything.** This was the question
that decided §1, so it is worth the evidence:

- **`core/` is already model-agnostic.** `protocol_service_radio_irq()` is a one-line wrapper
  around `nrf24l01p_irq()` (`core/protocol.c:254-256`). The STM32 targets call it from
  `EXTI2_IRQHandler` (`targets/stm32f103/gpio.c:108-113`); the Teensy targets call it from
  `loop()` behind a latched flag. Same function, both contexts, no `#ifdef` anywhere in `core/`.
  Nothing in the protocol or radio layer knows which model it is running under.
- **The vendored comment says "can," not "must."** `targets/teensy4x/platform_teensy4.cpp:114-121`:
  "The STM32 *can* service in-ISR because uart.c puts USART1 at NVIC priority 1 against EXTI2_3's
  3… That priority relationship does not survive the port, and at 600MHz against a 20ms poll
  period the deferral costs microseconds." The priority arrangement *permits* in-ISR servicing; it
  is not required by the workload.
- **The timing budget is enormous either way.** The Type-8 poll runs at 50Hz — 20ms per packet —
  and the nRF24L01+ RX FIFO is three deep, so roughly 60ms of slack before anything is lost. The
  F103 loop body is `protocol_poll()` (drain the UART ring, feed the parser,
  `core/protocol.c:273-278`) plus a `millis()` comparison. Nothing in it blocks. At 72MHz the
  deferral costs on the order of 8× the Teensy's microseconds — still microseconds against 20ms.
- **Same reasoning applies to the F030**, which is at 48MHz against the same 20ms. Its in-ISR
  structure reproduces the shipped HAL firmware's, and the bare-metal rewrite preserved it
  deliberately — that preservation is precisely what the byte-identical gate existed to protect.

**On the byte-identical gate: it served its purpose and is now retired.** It existed to prove that
the F030 bare-metal rewrite and the subsequent ports did not change behaviour, and it did that
job — every phase of `nrf_adapter_new_targets_port_plan.md` and `nrf_adapter_teensy41_port_plan.md`
cleared it or documented a deliberate substitution. From here the work is *intentional behaviour
change*, so a gate that asserts "the binary did not move" no longer measures anything useful. The
replacement gate is the per-board behavioural bring-up checklist: boot blink, `host_link_test.py`,
settings persistence across reboot, radio register read-back, watchdog.

**Conclusion for §1, reversed: F103 native USB CDC is viable and worth doing.** The pins are free,
the 48MHz USB clock falls out of the existing 72MHz PLL, the D+ pull-up is corrected on the bench
board, a vendored stack covers the CDC work, and the one structural objection — in-ISR servicing —
is a ~15-line conversion to a model the Teensy targets already run in production. It is a
sequencing problem, not a wall: convert F103 to deferred servicing and validate that on its own,
then add CDC behind `HOST_LINK`. See `nrf_adapter_f103_usb_cdc_port_plan.md`.

**The F030 converts to deferred servicing too, despite gaining no USB from it.** The two STM32
targets are the same program today — `main.c` differs by two comments and no code, and the EXTI
handlers differ only in vector name (`EXTI2_3_IRQHandler` on the F030's shared vector against the
F103's dedicated `EXTI2_IRQHandler`). Converting only the F103 would create the first genuine
control-flow divergence between them, so every later change to the radio path would have to be
reasoned through two concurrency models on near-identical code. Converting both keeps them
structurally identical, brings all seven configurations onto one model, fixes the same latent
missed-edge hazard on the F030, and retires the "USART1 at NVIC priority 1" invariant project-wide
— it is load-bearing only while something services in-ISR.

**Decided 2026-07-30**, superseding the "declined" verdict recorded earlier the same day.

---

## 2. Bare-metal vs. vendor SDK, given the CLI / no-account / Linux-installable goals

The bare-metal rewrite (`nrf_adapter_source_multiceiver`, replacing the original CubeMX+HAL
`nrf_adapter_source`) was originally chosen to avoid installing unfamiliar external dependencies.
With six build configs now live across three MCU families, the question is whether that tradeoff
still holds, or whether parts of it should move to vendor tooling — without relitigating what's
already validated: `core/nrf24l01p.c` and `core/protocol.c` are MCU-neutral, own-written, and
`platform.h` is the ~12-function contract every target implements. Neither of these was in
question in either sketch below.

### 2a. STM32 side — STM32Cube HAL

| File | HAL fit |
|---|---|
| `system_clock.c` | Clean win — `HAL_RCC_OscConfig`/`HAL_RCC_ClockConfig` replace hand-derived PLL sequences re-derived per chip family today for no real benefit. |
| `flash_store.c` | Clean win — `HAL_FLASH_Unlock`/`HAL_FLASH_Program`/`HAL_FLASH_Lock` replace the hand-rolled `KEYR`/`CR`/`SR` sequence. Not in any hot path. |
| `gpio.c`, `watchdog.c` | Roughly a wash — HAL is more verbose for the same one-time setup, but not wrong. |
| `spi.c` | Friction — `HAL_SPI_TransmitReceive()` adds state-machine/parameter-validation overhead to a byte-at-a-time loop already running close to the nRF24's 10MHz ceiling. |
| `uart.c` | **The blocker.** See below. |

`uart.c`'s design depends on one hard, hardware-validated invariant, restated in both the F030
source comments and the F103 port plan: USART1 sits at NVIC priority 1, EXTI2 at priority 3, so
`uart_write()`'s spin-wait into a ring buffer can always be safely called *from inside the radio's
EXTI ISR* — the mechanism that lets a UART reply go out synchronously with an nRF24 IRQ without
adding tens of microseconds of latency to the timing-sensitive 50Hz polling path. HAL's UART API
has no equivalent: `HAL_UART_Transmit_IT()` is a state machine that returns `HAL_BUSY` on
re-entry rather than queuing, with completion delivered via `HAL_UART_TxCpltCallback()`. Keeping
current behavior under HAL means bypassing its UART state machine and driving `USART1->TDR/ISR`
directly anyway — at which point HAL adds struct/header overhead over code that has to be
hand-written regardless. (Register naming there is family-specific: `ISR`/`TDR`/`ICR` on the F0's
newer USART IP, `SR`/`DR` and no `ICR` at all on the F103 — see the comment at
`targets/stm32f103/uart.c:13-15`. Doesn't change the argument, but the two ports don't share the
register set the way the BRR one-liner does.)

Flash headroom is worth checking but is **not** the constraint an earlier draft of this section
made it out to be. `arm-none-eabi-size` on `targets/stm32f030/build/*.elf` gives text 6572 +
data 24 = 6596 bytes of the F030F4's 16384 — 40% used, ~9.5KB free. HAL's GPIO/RCC/FLASH/IWDG
modules carry state-struct and error-code overhead a from-scratch build skips, and that should be
measured against the F030 build specifically rather than assumed fine because it fits on the 64KB
F103 (which sits at 6212 bytes) — but there is comfortable room for it. This is a "verify" item,
not a blocker.

Sourcing fits the stated goals regardless of the above: `STM32Cube` HAL C sources are plain files
on ST's GitHub (`STMicroelectronics/STM32CubeF0`, `STM32CubeF1`), `git clone`-able, no account.
Note also that HAL would not be a *new* source of dependency: `Drivers/CMSIS` (835K, ST device
headers for both F0 and F1) is already vendored in this tree from those same STM32Cube repos, so
the "avoid unfamiliar external dependencies" framing is already partly moot — adding HAL is adding
more files from a tree that is here today.
The F0 HAL is already sitting unused in this repo at
`nrf_adapter_source/Drivers/STM32F0xx_HAL_Driver` (846K, from the pre-rewrite shipped firmware).
CubeMX itself — the GUI generator, which does gate its installer behind a myST login — was never
a dependency of the current Makefile-based build and wouldn't become one; nothing here requires
it.

**Conclusion: declined, full *and* partial.** The one place HAL would save real effort (a
from-scratch F1 UART/SPI rewrite) is exactly the peripheral where it can't be used without working
around its own abstraction.

Partial adoption (`system_clock.c` + `flash_store.c` only) was described in the original draft as
"architecturally free" because `platform.h` isolates each peripheral into its own file. **That is
wrong, and the free-ness was the entire case for it:**

- `HAL_Init()` installs its own SysTick at 1kHz with `HAL_GetTick`/`HAL_Delay`, but
  `system_clock.c` already owns `SysTick_Handler`, `millis()` and `delay_ms()` on both STM32
  targets. The result is two tick systems or a refactor of the timing layer — and the timing layer
  is part of the `platform.h` contract that every other target implements. Not isolated to one
  file.
- Integration surface for those two files: vendoring both HAL trees (~846K each), a
  `stm32fNxx_hal_conf.h` per target with the correct module enables, and `HAL_MspInit`
  weak-symbol plumbing.
- `flash_store.c` is settings persistence — record format and CRC16 shared across all six
  configs, hardware-validated. Rewriting a working flash driver for stylistic parity is the worst
  risk-to-reward trade in the tree; the failure mode is silently corrupted saved config, found
  later.
- `system_clock.c` is ~30 lines of write-once PLL setup that will not be touched again. Neither
  file has future maintenance to save.
- The residual is a mixed idiom — HAL for two peripherals, registers for the other four — harder
  to read than either consistent choice, and directly undercutting the README's "Why bare-metal"
  rationale.

**Decided 2026-07-30** in review.

### 2b. Teensy side — PlatformIO

Unlike the STM32 side, the Teensy targets are already on vendored code — `Drivers/teensy3`/
`teensy4` are PJRC's real cores, copied verbatim. What's hand-maintained and duplicated per board
is the *build tooling*: `targets/teensy4x/Makefile` and `targets/teensy3x/Makefile` each carry a
`BOARD ?=` `ifeq` block selecting chip defines/linker script/loader arg by hand, and both target
dirs keep their own copy of the vendored core and SPI library in-repo.

PlatformIO already knows `teensy41`/`teensy40`/`teensy35`/`teensy36` as board IDs, with linker
script, `F_CPU`, and chip defines built in, and manages the core + SPI library as one
version-matched package — which also removes the version-skew risk Phase 3 of the port plan had
to check for by hand (vendored `teensy3/` core against a matching SPI library snapshot). Layout
sketch, keeping today's two-directory family split:

```
targets/teensy4x/
  platformio.ini
  src/{main.cpp, platform_teensy4.cpp, platform_teensy4.h, pins.h}
```

```ini
[env]
platform = teensy
framework = arduino
build_flags = -std=gnu++17 -Wall
build_src_flags = -Wall -Wextra          ; project src only, not the framework
lib_extra_dirs = ../..                   ; NOT ../../core -- see note below

[env:teensy41]
board = teensy41
build_flags = ${env.build_flags} -DHOST_LINK_USB_CDC

[env:teensy40]
board = teensy40
build_flags = ${env.build_flags} -DHOST_LINK_USB_CDC
```

`lib_extra_dirs` must point at the directory *containing* libraries, not at the library — `core/`
holds `.c`/`.h` files directly, so `lib_extra_dirs = ../../core` finds nothing and the LDF picks
up no sources. `../..` (making `core` the library name) or an explicit `build_src_filter` entry is
what actually works.

**What actually collapses is smaller than it looks.** The hand-maintained build tooling here is
*two* files: `targets/teensy4x/Makefile` and `targets/teensy3x/Makefile`. The eight `.ld` scripts
under `Drivers/teensy3`/`teensy4` and both vendored cores are PJRC's own, copied verbatim and
unmodified (`README.md:318`) — nothing in this project maintains them, and they would still be
present (just fetched from a package instead of committed) after a migration. `Drivers/teensy3/
Makefile` and `Drivers/teensy4/Makefile` are likewise PJRC's, unused by our build. So the trade is
two ~140-line files — already written, already hardware-validated on four boards, and static — for
two `.ini` files plus a package manager.

Costs, not just wins:

- **Per-tree warning separation gets harder, though not as hard as first stated.** The current
  Makefile deliberately builds vendored framework code under looser warnings and own code
  (`core/`, `platform_teensyN.cpp`) under `-Wall -Wextra` via `-isystem` — SPI.h drags in
  DMAChannel.h, which trips `-Wdeprecated-copy` on GCC 14. This does *not* need an `extra_scripts`
  SCons hook: `build_src_flags` applies to project `src/` only, and a `library.json` in `core/`
  with a `build.flags` entry covers the core sources. Both are declarative. The split is
  recoverable — it just stops being the one-line `-isystem` it is today.
- **Direct-link-not-archive for `bootdata.c`** (the Teensy 4.x boot header/vector table, which a
  static-archive link step would silently drop) is a documented gotcha in the current Makefile.
  PlatformIO's own Teensy 4.x support already handles this correctly for its existing user base —
  a solved problem in the package, not something to re-solve — but still a "verify, don't assume"
  item on first build.
- **Repo self-containment weakens.** Today, `git clone` + `make` builds fully offline because the
  vendored trees are committed. Under PlatformIO, a fresh checkout needs network access on first
  build to pull the framework and toolchain packages from PlatformIO's registry (no login
  required, but not offline).
- **Older toolchain than the system's.** PlatformIO ships its own `arm-none-eabi-gcc` package
  (Teensyduino-aligned, 11.3.x era) rather than the system's 14.2. The visible cost is the
  byte-identical regression gate — `.bin` diffing against the pre-migration Make build won't hold
  — but the quieter one is losing three major releases of GCC diagnostics on our own code, which
  is the code the `-Wextra` split above exists to police.
  **The gate half of this cost is now void** — see §1a: byte-identical matching has been retired
  as a gate across the project, so "the `.bin` moves" no longer counts against anything. The GCC
  diagnostics half stands, and the decision below rests on the other four grounds regardless.
- One small win in the other direction: `__rtc_localtime` stops being our problem. PlatformIO's
  Teensy builder supplies it the way the Arduino recipe does, retiring that `date +%s` hack and
  making the teensy3x build reproducible again.

**Conclusion: net negative today — don't do it.** The application logic (`core/`, `platform.h`,
each target's `platform_teensyN.cpp`, `pins.h`) genuinely wouldn't move; feasibility was never the
issue. But the savings are two static, validated files, and against that sit: fully-offline
`git clone && make` gone, a three-major-version GCC downgrade, `pio` itself becoming a Python
package-manager dependency (which cuts directly against the goal that motivated the bare-metal
rewrite in the first place), and a four-board bench re-verification — T4.1, T4.0, T3.5, T3.6 — to
land firmware that behaves identically to what is already flashed and working.

This flips if the board matrix grows well past four Teensy variants, or if tracking upstream
Teensyduino releases becomes a routine activity rather than a one-off. Neither is true now.

### 2c. Teensy side — Teensyduino itself

PlatformIO's Teensy support is a *repackaging* of Teensyduino (`framework-arduinoteensy`), and it
lags PJRC's releases. So the more direct question is what PJRC's own package would give us.

Teensyduino is three separable pieces, and **this repo already runs two of them**:

| Piece | Status here |
|---|---|
| Cores — `cores/teensy3`, `cores/teensy4`, bundled SPI library | Already vendored verbatim as `Drivers/teensy3`, `Drivers/teensy4`, `Drivers/teensy_libs/SPI` (`README.md:109-114`) |
| Board definitions — `boards.txt`/`platform.txt`: chip defines, linker-script selection, F_CPU and USB-type menus, compile/link/upload recipes | **Not used** — `targets/teensy{3,4}x/Makefile` is a hand-written replacement for exactly this |
| Tools — `teensy_loader_cli`, bundled `arm-none-eabi-gcc` | Loader already used by `make flash`; toolchain is the system's 14.2 instead |

So "adopt Teensyduino" means only: stop hand-maintaining the build recipe. Look at what
`targets/teensy4x/Makefile:47` hardcodes — `-D__IMXRT1062__ -DARDUINO_TEENSY41 -DF_CPU=600000000
-DUSB_SERIAL -DLAYOUT_US_ENGLISH -DARDUINO=10813 -DTEENSYDUINO=159` — every one of those is a
`boards.txt` value transcribed by hand, as are the linker-script `ifeq` blocks and the
`__rtc_localtime` defsym.

CLI-only usage is via `arduino-cli` (a single Go binary) plus PJRC's board-manager index — no
account, no GUI, Linux-native:

```sh
arduino-cli core install teensy:avr --additional-urls https://www.pjrc.com/teensy/package_teensy_index.json
arduino-cli compile -b teensy:avr:teensy41:usb=serial,speed=600 .
arduino-cli upload  -b teensy:avr:teensy41 .
```

**What it would buy:** the board matrix becomes a board-ID string instead of an `ifeq` block
(Teensy 3.2 or LC would be one word); USB type and clock become menu options rather than hardcoded
defines, which matters only if dual serial — host link plus a debug console — is ever wanted, since
`-DUSB_SERIAL` is baked in today; core + SPI + linker scripts arrive version-matched, retiring the
Phase 3 skew check done by hand; and `__rtc_localtime` is supplied properly.

**What it costs:** the same three as PlatformIO — first build needs network, PJRC's bundled GCC
replaces the system 14.2, and `arduino-cli` wants sketch-shaped layout so `core/` must become a
library or be symlinked in — plus one that is *worse* than under PlatformIO: there is no
`build_src_flags` equivalent. `--build-property compiler.cpp.extra_flags=` applies to everything
`arduino-cli` compiles, framework included, so the `-isystem` warning split has no clean
counterpart at all.

**Conclusion: don't adopt it as build tooling — treat it as upstream.** The one thing the vendored
snapshot cannot do for itself is update. `Drivers/` is a Teensyduino 1.59-era snapshot (that's what
`-DTEENSYDUINO=159` in both target Makefiles, and `Drivers/teensy4/Makefile:70`, declare). If a
core-level bug ever bites — USB stack, `HardwareSerial` edge case, a newer-GCC incompatibility —
PJRC's release is where the fix comes from, and the refresh is a diff-and-copy against `Drivers/`,
not a build-system migration. That is a "when there is a reason" operation; there is no known core
bug today, all four boards are validated, and `teensy_loader_cli` — the only piece needed day to
day — is already in hand.

---

## Standing constraint carried into any future work here

Regardless of which of the above (if any) gets picked up: **do not replace `core/nrf24l01p.c`
with a community library (e.g. RF24)** as part of a "go vendor" move. That substitution was tried
before on the `multi-platform-wip` branch and produced a wrong diagnosis of an SPI timing bug —
the real faults were per-byte `beginTransaction`/`endTransaction` calls inside an asserted CSN, a
1MHz clock, and multi-byte transfers with no transaction bracketing at all
(`nrf_adapter_source_multiceiver/README.md`). The hand-rolled radio driver and its CSN/transaction
discipline should survive any build-system migration untouched.
