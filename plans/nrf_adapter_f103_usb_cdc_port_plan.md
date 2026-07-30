# Implementation Plan — F103 native USB CDC host link

**2026-07-30.** Goal: give `targets/stm32f103` a native USB CDC host link over the Blue Pill's
onboard USB port, so the board needs no external USB-UART bridge. Rationale and the research that
reversed the earlier "declined" verdict are in `firmware_toolchain_research.md` §1/§1a — this file
is the execution plan only.

**Phase 1 also changes the F030**, which gets no USB from this work. Its radio-servicing model
moves in step with the F103's so the two targets stay structurally identical; the reasoning is in
Phase 1. Anyone treating this as an F103-only plan will miss that.

Paths below are relative to `nrf_adapter_source_multiceiver/` unless stated otherwise. Host-side
scripts run under `venv/bin/python`.

---

## Gate change — byte-identical matching is retired

**This applies to every phase and supersedes the gates used in the earlier port plans.**

The byte-identical `.bin` gate existed to prove that the F030 bare-metal rewrite and the four-board
ports did not change behaviour. It did that job: every phase of
`nrf_adapter_new_targets_port_plan.md` and `nrf_adapter_teensy41_port_plan.md` either cleared it or
recorded a deliberate substitution. **From here the work is intentional behaviour change, so a gate
asserting "the binary did not move" measures nothing.** Do not attempt to preserve it, and do not
treat a moved `.bin` as a regression signal.

The replacement gate is behavioural, per board: boot blink, `host_link_test.py`, settings
persistence across a reboot, radio register read-back, watchdog refresh. That is what "verified"
means for the rest of this plan.

## Sequencing principle

Phase 1 is a behaviour change to the existing, working USART1 builds of **both** STM32 targets, and
**must be validated on hardware on its own before any USB code exists**. If radio servicing and USB
land together, a missed packet has two candidate causes. Kept apart, Phase 1 is a ~15-line diff per
target, testable against the host link that already works.

Note that **Phase 4 partly precedes Phase 3** despite the numbering: the Makefile's source
selection has to change before the first CDC build can link. The phases are numbered by topic, not
strictly by order.

---

## Phase 1 — Convert both STM32 targets to deferred radio servicing

**Both**, not just the F103. The two targets are the same program today: `main.c` differs by two
comments and *no code at all* (`diff -u targets/stm32f030/main.c targets/stm32f103/main.c`), and
the EXTI handlers differ only in vector name — `EXTI2_3_IRQHandler` on the F030's shared vector
against the F103's dedicated `EXTI2_IRQHandler`. Converting only the F103 would introduce the first
genuine *control-flow* divergence between them, so every future change to the radio path would need
reasoning through two different concurrency models on near-identical code. Converting both keeps
`main.c` structurally identical, leaves `gpio.c` differing only in vector name, and makes all seven
configurations run one model.

Two further gains from doing the F030 as well:

- **It fixes the same latent missed-edge hazard there** (see step 2 below), which the F030 has
  today.
- **It retires the "USART1 must sit at NVIC priority 1" invariant project-wide.** That constraint
  is load-bearing *only* because of in-ISR servicing. Once nothing services in-ISR, it becomes a
  harmless leftover rather than a live invariant someone has to know about and preserve.

The marginal cost is small: Phase 6 already required re-running the F030 checklist as a regression
check, so the board was going on the bench regardless — the difference is that the run now
validates a change instead of confirming a no-op.

Today both EXTI handlers call `protocol_service_radio_irq()` directly
(`targets/stm32f103/gpio.c:108-113`, `targets/stm32f030/gpio.c:90-95`), which is only safe because
`uart_write()`'s spin-wait can be preempted by USART1 at NVIC priority 1 against EXTI's 3. That
coupling is what blocks CDC. The Teensy targets already run the deferred model in production;
mirror it. Apply each step to **both** `targets/stm32f030/` and `targets/stm32f103/`.

1. **`gpio.c`** — the EXTI handler clears the pending bit and sets a `static volatile bool` flag.
   Nothing else.
2. **Add `radio_irq_pending()`**, declared in each target's `gpio.h`. Keep it **target-local** — it
   is *not* part of `platform.h`'s contract, exactly as the Teensy declares it in
   `platform_teensy4.h`. Copy the Teensy implementation's shape
   (`targets/teensy4x/platform_teensy4.cpp:128-132`): return `edge_flag || IRQ pin reads low`,
   clearing the flag on read.
   **The level recheck is load-bearing, not a nicety.** Reading-then-clearing the flag is not
   atomic, so an edge arriving between the two is lost; and EXTI is edge-triggered while the nRF24
   holds IRQ low until its status bits are cleared. The level test closes both holes — a lost edge
   still leaves the pin low, so the next call picks it up. The current in-ISR code has no
   equivalent recovery path on either target.
3. **`main.c`** — in the `while(1)`, call `if (radio_irq_pending()) { protocol_service_radio_irq(); }`
   **before** `protocol_poll()`, same ordering and same reasoning as `targets/teensy4x/main.cpp`'s
   `loop()`: a received payload is sitting in the RX FIFO while host bytes are already buffered.
   Keep the two files' bodies identical, as they are today.
4. **Leave USART1 at NVIC priority 1 on both.** It is no longer load-bearing but it is harmless,
   and leaving it keeps the diff small. Say so in the comment rather than silently retaining it.
5. **Update the now-stale comments** that document the old model: `targets/stm32f103/gpio.c:103-107`,
   `targets/stm32f030/gpio.c:86-89`, and the header block at each target's `uart.c:5-15`.

**Verify before proceeding:** both STM32 boards on their existing USART1 host links, full
behavioural checklist each, plus a sustained 50Hz Type-8 poll to confirm no dropped packets. Timing
headroom is large on both — 20ms per packet against a three-deep RX FIFO, roughly 60ms of slack,
and the loop body (`protocol_poll()`, `core/protocol.c:273-278`) does not block, at 48MHz as
readily as at 72MHz — but confirm it rather than assume.

## Phase 2 — Vendor TinyUSB

**Use TinyUSB, not libopencm3.** TinyUSB is a USB stack and nothing else, so it sits alongside the
existing register-level `gpio.c`/`spi.c`/`uart.c` without displacing them. libopencm3 is a full
peripheral library and would overlap and fight that code. TinyUSB is BSD-licensed, `git clone`-able
with no account, and its `stm32_fsdev` port covers the F103's USB-FS device peripheral including
PMA buffer management.

- Vendor verbatim to `Drivers/tinyusb/`, matching the `Drivers/CMSIS` and `Drivers/teensy*`
  convention already documented in the Layout block. Copy only what the build needs: `src/tusb.c`,
  `src/common/`, `src/device/`, `src/class/cdc/`, `src/portable/st/stm32_fsdev/`.
- Compile it under `-isystem` so its warnings stay out of our `-Wall -Wextra`, the same treatment
  the Teensy vendored trees get (`targets/teensy4x/Makefile:50-53`).
- Record the upstream version in the Layout entry, so this tree does not repeat the Teensyduino
  situation where the snapshot version is nowhere in the README.

## Phase 3 — USB clock, interrupts, and the CDC transport

1. **Clock.** The USB peripheral needs exactly 48MHz. `system_clock.c` already runs HSE 8MHz × 9 =
   72MHz, so `USBPRE = 0` (PLL ÷ 1.5) gives it. That is the reset value — **set it explicitly
   anyway** for clarity, before enabling `RCC->APB1ENR |= RCC_APB1ENR_USBEN`. No other clock change
   is needed.
2. **Interrupts.** `targets/stm32f103/startup.c` **already carries the vector slots** —
   `USB_HP_CAN1_TX_IRQHandler` (line 46), `USB_LP_CAN1_RX0_IRQHandler` (47) and
   `USBWakeUp_IRQHandler` (69), all `WEAK_ALIAS`, already placed in the table at lines 114-115. No
   vector-table edit is needed: defining the handler overrides the weak alias. Route
   `USB_LP_CAN1_RX0_IRQHandler` to `tud_int_handler(0)` and give USB an NVIC priority that does not
   starve EXTI2 or USART1.
3. **`targets/stm32f103/usb_cdc.c`** — a new file implementing the three host-link functions from
   `platform.h` so `core/` needs no changes:
   - `uart_read_byte()` → `tud_cdc_available()` / `tud_cdc_read_char()`.
   - `uart_set_baudrate()` → no-op. `platform.h:47-49` already sanctions this ("No-op on targets
     whose host link has no configurable line rate (USB CDC)"), and `CMD_SET_BAUDRATE` still ACKs
     and still persists the value.
   - `uart_write()` → **the one place with a real behavioural decision.** `tud_cdc_write()` plus
     `tud_cdc_write_flush()`, pumping `tud_task()`, under a `millis()` deadline, then drop the
     remainder. It must **never** spin indefinitely: with no host reading, the CDC FIFO fills and an
     unbounded wait trips the ~3.3s IWDG and reboots the board. Pick a deadline well under that
     (~50ms is ample against a 20ms poll period). This differs from the UART path, where the ring
     always drains — document it in the source, because "output silently dropped when no host is
     attached" is a real semantic the UART build does not have.
4. **`main.c`** — call `tud_task()` every loop iteration in the CDC build.
5. **Descriptors** (`usb_descriptors.c`) — CDC-ACM, one interface. The VID/PID choice drives
   host-side detection; see Phase 5.

## Phase 4 — Build wiring

`targets/stm32f103/Makefile:14` is `TARGET_SRCS = $(wildcard *.c)`. **Adding `usb_cdc.c` under that
wildcard compiles it alongside `uart.c` and produces duplicate `uart_write`/`uart_read_byte`
symbols.** This must change before Phase 3 links.

- Introduce `HOST_LINK ?= HOST_LINK_USART1` with `HOST_LINK_USB_CDC` as the alternative, following
  the Teensy Makefiles' pattern (`targets/teensy4x/Makefile:14-17`).
- Replace the wildcard with explicit source selection: `uart.c` for the USART1 build, `usb_cdc.c`
  plus the TinyUSB sources and `-isystem` includes for the CDC build.
- `make clean` is required when switching `HOST_LINK`, same as the Teensy targets — say so in the
  Makefile comment.
- **Flash check:** F103 currently sits at 6212 bytes of 65536 (`arm-none-eabi-size`). TinyUSB CDC
  should add roughly 8-12KB, leaving plenty of room — but measure it rather than assuming, and
  record the new figure.

## Phase 5 — Host side

`mdp_controller/nrf24_adapter.py` autodetects only `10C4:EA60` (the CP210x bridge). A CDC build
enumerates under its own VID/PID, so **the CDC build needs an explicit `--port`, exactly as the
four Teensy targets do today.** Treat that as the shipped behaviour for this phase — it is parity,
not a regression, and the USART1 build's autodetect is unaffected.

Widening autodetect to the new VID/PID is a one-line change and can follow later; it needs the
VID/PID settled first (see Open decisions).

## Phase 6 — Verification

**Build gate:** seven configurations, all clean under `-Wall -Wextra`, with `arm-none-eabi-size`
recorded — f030, f103/USART1, f103/CDC, teensy41, teensy40, teensy35, teensy36.

**Hardware gate**, per the retired-byte-gate note above:

- **After Phase 1, before Phase 2 starts:** F103/USART1 **and F030**, full checklist each plus
  sustained 50Hz polling. Both targets changed servicing model in Phase 1, so both are under test
  here rather than one being a regression check.
- **After Phase 3-4:** F103/CDC — enumeration from cold boot; re-enumeration after unplug/replug;
  `host_link_test.py` over the CDC port; settings persistence across reboot; radio register
  read-back; sustained 50Hz polling; watchdog refresh unaffected.
- **The specific CDC failure mode to test deliberately:** run the firmware with the port enumerated
  but nothing reading, and confirm the board neither wedges nor reboots. That exercises the
  `uart_write()` deadline from Phase 3.
- **Regression:** F103/USART1 and F030 still pass after the CDC work lands — neither shares code
  with `usb_cdc.c`, but the Phase 4 Makefile restructuring touches how the USART1 build selects its
  sources, so confirm rather than assume.

## Phase 7 — README updates

`nrf_adapter_source_multiceiver/README.md`. **Current state only** — no decision history, no dates,
no "we evaluated and chose", and **no links to this plan or to
`firmware_toolchain_research.md`**. State what the firmware is and what constrains it.

From this work:

- **`## STM32 Blue Pill target`** — the host link is now selectable; document both, and the
  `make clean` requirement when switching.
- **`README.md:173-178`** — currently says the onboard USB "would need a vendored USB device stack
  plus a CDC glue layer for no real benefit over reusing the validated USART1 path." That becomes
  false. Replace with what the CDC build is.
- **`README.md:187-191`** — the in-ISR servicing bullet. After Phase 1 **no target services the
  radio in interrupt context**, so the bullet's whole premise ("unlike every Teensy target… instead
  of deferring to `loop()`") is gone. Remove it rather than rewriting it: every configuration now
  latches the IRQ and services from the main loop, which is uniform enough that it belongs in the
  shared design-notes section, not as a per-target deviation. Check
  `README.md:127` (`## Design notes / deviations from the shipped firmware`) for where the general
  statement fits, and check whether the `uart.c` priority discussion is still cited anywhere as a
  live constraint.
- **Layout block** — a `Drivers/tinyusb` entry with its upstream version.
- **`## Building` / `## Flashing`** — the `HOST_LINK=` invocation for the F103.

Two independent README gaps, unrelated to this work but worth closing in the same pass since the
file is open (they carry over from the superseded toolchain-policy plan):

- **Name the Teensyduino snapshot.** `README.md:109-116` says the cores are "copied verbatim" and
  that teensy3 is the "same snapshot as teensy4/" but never which one. It is **Teensyduino 1.59**,
  per `-DTEENSYDUINO=159` in `targets/teensy4x/Makefile:47`, `targets/teensy3x/Makefile:50`,
  `Drivers/teensy4/Makefile:70` and `Drivers/teensy3/Makefile:56`. Keep to the terse Layout style.
- **Add a vendored-core refresh procedure**, as a new section after `## Flashing`: refresh for a
  specific core-level bug rather than on a schedule; download the Teensyduino release from pjrc.com
  (no account); diff `cores/teensy3`, `cores/teensy4`, `libraries/SPI` against `Drivers/` and treat
  anything resembling a local modification as a finding, since the trees are verbatim copies
  (`README.md:318`); copy over and bump `-DARDUINO=`/`-DTEENSYDUINO=` in both target Makefiles;
  rebuild all configurations; re-run the per-board bring-up checklist. Also worth a line:
  `teensy_loader_cli` is the only part of the Teensyduino distribution needed day to day, and it
  builds standalone from PJRC's GitHub.

---

## Open decisions

Not blocking — pick during implementation, but pick deliberately:

1. **VID/PID for the CDC descriptor.** Drives whether host-side autodetect can ever be widened.
   TinyUSB's example pair works for bring-up; decide before Phase 5 is revisited.
2. **Default `HOST_LINK` for the F103.** Recommend keeping `HOST_LINK_USART1` as the default so an
   unqualified `make` builds the validated path, with CDC opt-in. The Teensy targets take the
   opposite default because USB is their only sensible link.

## Out of scope

- **USB on the F030.** No USB peripheral on the die. (It *is* converted to deferred radio
  servicing in Phase 1 — that part is in scope.)
- PlatformIO, `arduino-cli`/Teensyduino build-recipe adoption, STM32Cube HAL — all declined, see
  `firmware_toolchain_research.md` §2.
- Substituting `core/nrf24l01p.c` for a community library such as RF24.
- PyInstaller/packaging.

## Verification of this plan's own citations

Every `file:line` above was verified against the tree on 2026-07-30 and will drift. Re-check at
implementation time rather than trusting it — particularly `targets/stm32f103/gpio.c:108-113`,
`targets/stm32f030/gpio.c:90-95`, `targets/stm32f103/Makefile:14`, `targets/stm32f103/startup.c`'s
USB vector slots, and `README.md:173-178` / `README.md:187-191`, which are the anchors the phases
key off.

## Commit

Leave committing to the user. Natural split, one per validated step:

```
stm32: defer radio servicing to the main loop on both targets
f103: native USB CDC host link behind HOST_LINK
```
