# Implementation Plan — F103 native USB CDC host link

Goal: give `targets/stm32f103` a native USB CDC host link over the Blue Pill's onboard USB port, so
the board needs no external USB-UART bridge. Rationale and the research behind it are in
`firmware_toolchain_research.md` §1/§1a — this file is the execution plan only.

**Phase 1 also changes the F030**, which gets no USB from this work. Its radio-servicing model moves
in step with the F103's so the two targets stay structurally identical; the reasoning is in Phase 1.
Anyone treating this as an F103-only plan will miss that.

Paths below are relative to `nrf_adapter_source_multiceiver/` unless stated otherwise. Host-side
scripts run under `venv/bin/python`. Every `file:line` citation was verified against the tree on
2026-07-31 — re-check before relying on one.

---

## The gate

**Byte-identical `.bin` matching is retired.** It existed to prove the F030 bare-metal rewrite and
the four-board ports changed no behaviour, and it did that job. This work is *intentional* behaviour
change, so a gate asserting "the binary did not move" measures nothing. Do not attempt to preserve
it; do not treat a moved `.bin` as a regression signal.

The gate is the seven-step behavioural checklist, per board, that the four-board bring-up ran:

1. Flash and confirm boot (F103: four PC13 blinks).
2. `host_link_test.py --port <port>`, no radio wired.
3. Settings persistence across a **real power cycle** — not `CMD_REBOOT`. A soft reset never drops
   power, so it does not exercise the F103's settings flash page at `0x0800FC00`. Pull the plug.
4. Radio register read-back, radio wired, before any traffic.
5. `pipe_test.py <port>` — two-device pipe test.
6. 60s two-device GUI run, then `tools/noack_report.py` against the baseline table in Phase 1.
7. Watchdog — stall the main loop, confirm reset at roughly the configured timeout.

Steps 5 and 6 are not optional here: they are the only ones that put the host link under sustained
real traffic, which is exactly what a new transport has to prove.

## Sequencing

Phase 1 is a behaviour change to the existing, working USART1 builds of **both** STM32 targets and
**must be validated on hardware on its own before any USB code exists**. If radio servicing and USB
land together, a missed packet has two candidate causes. Kept apart, Phase 1 is a ~15-line diff per
target, testable against the host link that already works.

**Phase 4 partly precedes Phase 3** despite the numbering — the Makefile's source selection has to
change before the first CDC build can link. Phases are numbered by topic, not order.

## Test tooling — already in the tree, do not rewrite

| Step | Tool | Notes |
|---|---|---|
| 2 | `host_link_test.py --port <port>` | Root-level, target-neutral. Its baudrate step really retunes to 115200, reopens the host port at the new rate, and restores 921600 before anything is persisted. |
| 3 | `persistence_test.py --port <port> --phase arm\|verify\|restore` | Target-neutral, splits around a physical power removal. Arms a config unlike both the compiled-in defaults and the bench config, so a stale record cannot fake a pass. |
| 4 | `targets/stm32f103/nrf_regdump.gdb` | Drives R_REGISTER transactions through the firmware's *own* SPI path on a halted target, which is what makes it evidence about the F103 SPI port. Expected values are written into the script ahead of the run, not read off the result. |
| 6 | `gui_source/bench_gui_run.py` | Headless 60s two-device run against `gui_source/settings.json`. |
| 6 | `tools/noack_report.py` | Slice `gui_source/mdp.log` from the run's own `---- NEW SESSION ----` marker first — it accumulates across sessions. |

`targets/teensy3x/reg_dump_check.py` is the no-SWD equivalent of `nrf_regdump.gdb`; the F103 has SWD
and does not need it. Listed so it is not reinvented.

**`pipe_test.py` is gitignored (`.gitignore:2`) and is not in a fresh clone.** The bench working copy
is already on the fixed configuration — P906 `AABBCCDDE2` as primary, L1060 `AABBCCDDE3` on pipe 1,
`FREQ = 2521`, `SKIP_DISCOVERY = True`. Do not regenerate it from an older copy: the original shipped
an obsolete address pair (`AABBCCDDEE` / `153614FAE1`) that fails against every board and would tempt
a re-pair. `gui_source/settings.json` (`.gitignore:20`) and `gui_source/mdp.log` (`.gitignore:22`)
are likewise untracked.

**`nrf_regdump.gdb` halts the core, which drops a live CDC enumeration.** On the USART1 build the
external bridge is unaffected. On the CDC build the host loses the tty for the duration of the halt
and the board needs a replug afterwards — expected, not a USB fault. Run step 4 before steps 5–6
rather than interleaving.

---

## Phase 1 — Convert both STM32 targets to deferred radio servicing

**Both**, not just the F103. The two targets are the same program today: `main.c` differs by two
comments and *no code at all* (`diff -u targets/stm32f030/main.c targets/stm32f103/main.c`), and the
EXTI handlers differ only in vector name — `EXTI2_3_IRQHandler` on the F030's shared vector against
the F103's dedicated `EXTI2_IRQHandler`. Converting only the F103 would introduce the first genuine
*control-flow* divergence between them, so every future change to the radio path would need reasoning
through two concurrency models on near-identical code. Converting both also fixes the same latent
missed-edge hazard on the F030 (see step 2), and retires the "USART1 must sit at NVIC priority 1"
invariant project-wide — it is load-bearing *only* because of in-ISR servicing.

Today both EXTI handlers call `protocol_service_radio_irq()` directly
(`targets/stm32f103/gpio.c:108-113`, `targets/stm32f030/gpio.c:90-95`), which is only safe because
`uart_write()`'s spin-wait can be preempted by USART1 at NVIC priority 1 against EXTI's 3. That
coupling is what blocks CDC. The Teensy targets already run the deferred model in production; mirror
it. Apply each step to **both** `targets/stm32f030/` and `targets/stm32f103/`.

1. **`gpio.c`** — the EXTI handler clears the pending bit and sets a `static volatile bool` flag.
   Nothing else.
2. **Add `radio_irq_pending()`**, declared in each target's `gpio.h`. Keep it **target-local** — it
   is *not* part of `platform.h`'s contract, exactly as the Teensy declares it in
   `platform_teensy4.h`. Copy the Teensy implementation's shape
   (`targets/teensy4x/platform_teensy4.cpp:129-133`): return `edge_flag || IRQ pin reads low`,
   clearing the flag on read.
   **The level recheck is load-bearing, not a nicety.** Reading-then-clearing the flag is not atomic,
   so an edge arriving between the two is lost; and EXTI is edge-triggered while the nRF24 holds IRQ
   low until its status bits are cleared. The level test closes both holes — a lost edge still leaves
   the pin low, so the next call picks it up. The current in-ISR code has no equivalent recovery path
   on either target.
3. **`main.c`** — in the `while(1)`, call `if (radio_irq_pending()) { protocol_service_radio_irq(); }`
   **before** `protocol_poll()`, same ordering and reasoning as `targets/teensy4x/main.cpp`'s
   `loop()`: a received payload is sitting in the RX FIFO while host bytes are already buffered. Keep
   the two files' bodies identical, as they are today.
4. **Leave USART1 at NVIC priority 1 on both.** No longer load-bearing, but harmless, and leaving it
   keeps the diff small. Say so in the comment rather than silently retaining it.
5. **Update the now-stale comments** describing the old model: `targets/stm32f103/gpio.c:103-107`,
   `targets/stm32f030/gpio.c:86-89`, and the header block at each target's `uart.c:5-15`.

**Verify before proceeding:** both STM32 boards on their existing USART1 host links, full seven-step
checklist each.

Timing headroom is large but smaller than the per-device cadence suggests. 50Hz is the *per-device*
Type-8 rate; the RX FIFO is shared across pipes, so the real figure on this two-device bench is ~112
radio sends/s — about 9ms of inter-packet spacing and ~27ms of slack against a three-deep FIFO. Still
enormous against a deferral costing microseconds, and the loop body (`protocol_poll()`,
`core/protocol.c:273-278`) does not block at 48MHz any more than at 72MHz.

**Baseline for step 6.** Each board got its own 60s two-device GUI run against the same bench and
radio configuration. The two boards already on the deferred model out-perform the in-ISR F103:

| Config | Servicing model | radio sends / 60s | req/s | no-acks |
|---|---|---|---|---|
| Teensy 4.0 | deferred | 7084 | ~118 | 9 (0.13%) |
| Teensy 4.1 | deferred | 6962 | ~116 | 133 (1.91%) |
| **STM32F103** | **in-ISR** | **6694** | **~112** | **8 (0.12%)** |
| STM32F030 | in-ISR | 6140 | ~102 | 283 (4.61%) |

This is not proof the deferral is free on an M3 — different silicon, different clock — and the no-ack
column is dominated by a known bench characteristic (P906 `..E2` traffic, L1060 `..E3` nearly clean on
every board) rather than adapter behaviour. What it does establish is that the model is not a
throughput risk. **After conversion, expect the F103 at ~112 req/s and a sub-1% no-ack rate.** A
material drop is a finding.

**Measured after conversion, 2026-08-01** (both STM32 targets, two runs each — see Phase 6):

| Config | Servicing model | radio sends / 60s | req/s | no-acks |
|---|---|---|---|---|
| **Teensy 4.1** | deferred | **7373 / 7271 / 7027** | ~120 | **6 / 3 / 9 (0.08% / 0.04% / 0.13%)** |
| **Teensy 3.5** | deferred | **7219 / 6963** | ~118 | **3 / 8 (0.04% / 0.11%)** |
| **Teensy 3.6** | deferred | **6991 / 6707** | ~114 | **2 / 4 (0.03% / 0.06%)** |
| STM32F103 / USART1 | deferred | 7020 *(6464)* | ~117 | 4 (**0.06%**) *(21, 0.32%)* |
| STM32F103 / CDC | deferred | 6864 | ~114 | 12 (0.17%) |
| STM32F030 | deferred | 6416 *(6364)* | ~107 | 5 (**0.08%**) *(7, 0.11%)* |

Both Teensy 3.x rows are **after** the `send_now()` fix found during that retest — before it, the 3.5
gave 3385/3360 sends and ~50 samples/s per device, and the 3.6 3986 and ~64.5 req/s. See the Teensy 3.5 step 6 note in
`nrf_adapter_new_targets_port_plan.md`; it was a real firmware defect, not a bench artifact, and it
is the one case in this whole exercise where a bad number turned out to be the board after all.

Italics are the other run of each pair. **The F103's two runs bracket every figure in both tables**,
which is the real lesson: at F103 scale these differences are RF noise, and the original table's
ranking cannot be read as a ranking. Two exceptions, both real:

- **STM32F030: 4.61% → 0.08–0.11%**, consistent across both runs and orders of magnitude outside the
  spread. Deferring radio servicing was a large win on the slowest target and a wash everywhere else.
- **Teensy 4.1: 1.91% → 0.04–0.13% with the firmware unchanged.** Retested 2026-08-01 at the user's
  suggestion that results had been improving as the *test setup* got fixed, and they were right — the
  original 1.91% was a bench artifact, not the board. It is now the fastest target on the bench
  (7373 sends, 149 samples/s per device) and no longer the outlier the earlier tables make it look.
  **Treat the 1.91% figure as retired wherever it is cited as a reference baseline.** The most likely
  cause is an unparked idle adapter auto-ACKing on the bench address during the original run — the
  known ~18% throughput effect — but that was not isolated at the time and is not being claimed now.

The Teensy 3.5/3.6 are omitted from the table: both run the deferred model at only ~65 req/s with no
cause isolated. If the converted F103 lands near that figure, the unexplained gap has reproduced on a
third board and is worth chasing rather than accepting.

## Phase 2 — Vendor TinyUSB

**Use TinyUSB, not libopencm3.** TinyUSB is a USB stack and nothing else, so it sits alongside the
existing register-level `gpio.c`/`spi.c`/`uart.c` without displacing them. libopencm3 is a full
peripheral library and would overlap and fight that code. TinyUSB is BSD-licensed, `git clone`-able
with no account, and its `stm32_fsdev` port covers the F103's USB-FS device peripheral including PMA
buffer management.

- Vendor verbatim to `Drivers/tinyusb/`, matching the `Drivers/CMSIS` and `Drivers/teensy*`
  convention. Copy only what the build needs: `src/tusb.c`, `src/common/`, `src/device/`,
  `src/class/cdc/`, `src/portable/st/stm32_fsdev/`.

  **Done — TinyUSB 0.21.0** (tag `0.21.0`, commit `dae3f9a3`). Chosen over the older 0.18.0 because
  upstream ships and CI-builds an `stm32f103_bluepill` BSP at this release, so our exact board is on
  the tested path. 0.21.0 is a materially different stack from 0.18.0 — the fsdev DCD was rewritten
  when host mode was added and `usbd_control.c` was folded into `usbd.c` — so do not mix references
  from older TinyUSB examples when writing Phase 3. Also required beyond the list above:
  `src/tusb_option.h`, `src/osal/` (`osal.h` + `osal_none.h`), and `fsdev_common.c`/`.h`, which
  0.18.0 did not have. `hcd_stm32_fsdev.c` and the ch32/at32 headers are deliberately not vendored
  (host mode, other vendors' silicon).
- Compile under `-isystem` so its warnings stay out of our `-Wall -Wextra`, the same treatment the
  Teensy vendored trees get (`targets/teensy4x/Makefile:50-53`).
- Record the upstream version in the README Layout entry, so this tree does not repeat the
  Teensyduino situation where the snapshot version is nowhere in the README.

## Phase 3 — USB clock, interrupts, and the CDC transport

1. **Clock.** The USB peripheral needs exactly 48MHz. `system_clock.c` already runs HSE 8MHz × 9 =
   72MHz, so `USBPRE = 0` (PLL ÷ 1.5) gives it. That is the reset value — **set it explicitly
   anyway** for clarity, before enabling `RCC->APB1ENR |= RCC_APB1ENR_USBEN`. No other clock change
   is needed.
2. **Interrupts.** `targets/stm32f103/startup.c` **already carries the vector slots** —
   `USB_HP_CAN1_TX_IRQHandler` (line 46), `USB_LP_CAN1_RX0_IRQHandler` (47) and
   `USBWakeUp_IRQHandler` (69), all `WEAK_ALIAS`, already placed in the table at lines 114-115 and
   137. No vector-table edit is needed: defining the handler overrides the weak alias. Route
   `USB_LP_CAN1_RX0_IRQHandler` to `tud_int_handler(0)` and give USB an NVIC priority that does not
   starve EXTI2 or USART1.
3. **`targets/stm32f103/usb_cdc.c`** — a new file implementing the three host-link functions from
   `platform.h` so `core/` needs no changes:
   - `uart_read_byte()` → `tud_cdc_available()` / `tud_cdc_read_char()`.
   - `uart_set_baudrate()` → no-op. `platform.h:47-49` already sanctions this ("No-op on targets
     whose host link has no configurable line rate (USB CDC)"), and `CMD_SET_BAUDRATE` still ACKs and
     still persists the value.
   - `uart_write()` → **the one place with a real behavioural decision.** The plan's original
     prescription here — wait for FIFO space under a ~20ms `millis()` deadline, pumping `tud_task()`
     while waiting, then drop the remainder — **was implemented, tested on hardware, and found to be
     wrong.** It is recorded here because the reasoning that produced it is subtly circular.

     **What actually happens: it reboots the board.** `protocol_poll()` (`core/protocol.c:273-278`)
     is an unbounded drain, `while (uart_read_byte(&b)) feed_byte(b);`, and `feed_byte()` calls back
     into `uart_write()` for every dispatched command. `tud_task()` is also what refills the CDC
     **RX** FIFO. So pumping it from inside `uart_write()` feeds the very loop that is calling
     `uart_write()`: with a host that writes without reading, `uart_read_byte()` never runs dry,
     `protocol_poll()` never returns, `main()`'s watchdog refresh is never reached, and the IWDG
     fires. Confirmed on hardware — the board re-enumerated mid-flood under the deliberate
     nothing-reading test, which is exactly the failure the deadline was meant to prevent.

     **Shipped instead: non-blocking and all-or-nothing.** If `tud_cdc_write_available() < len`,
     drop the whole frame and return; otherwise `tud_cdc_write()` + `tud_cdc_write_flush()`. Never
     pump `tud_task()` from here — that is what breaks the feedback loop, because the RX FIFO is
     then refilled only by `main()`'s `usb_cdc_task()` and `protocol_poll()` is guaranteed to run
     dry. Dropping whole frames rather than truncating means the host only ever sees complete frames.
     The cost is theoretical: the TX FIFO holds 512 bytes against ~36-byte frames and USB FS moves
     bulk far faster than the ~4.5 KB/s this link carries, so it does not fill unless the host has
     stopped reading — and dropping is the right answer then. Measured: **throughput went up**, 6864
     radio sends per 60s against 6450 with the deadline version, because the 20ms stalls were real.

     **General lesson for this codebase:** on any target whose host link can block, `uart_write()`
     must not pump a mechanism that also refills RX. `uart.c` is immune only because its TX ring
     drains at line rate.
4. **`main.c`** — call `tud_task()` every loop iteration in the CDC build.

   **Not sufficient on its own — the boot blink also has to pump it.** The USB ISR only queues
   events; everything that answers the host, enumeration included, happens in `tud_task()`. The
   four-blink sequence blocks ~800ms in `delay_ms()` right when the host is enumerating, and
   TinyUSB's event queue is 16 deep and **drops silently** once full (`CFG_TUD_TASK_QUEUE_SZ`,
   `usbd.c:41`, with `CFG_TUSB_DEBUG 0`). The failure that produces is a board that enumerates
   slowly or intermittently, not one that visibly fails. Implemented as a `boot_delay_ms()` that
   pumps the stack, `#define`d straight to `delay_ms()` in the USART1 build.
5. **Descriptors** (`usb_descriptors.c`) — CDC-ACM, one interface. The VID/PID choice drives
   host-side detection; see Phase 5.

**Phase 3 status: implemented, builds clean, not yet on hardware.** Also settled here:

- **VID/PID = `0xCAFE:0x4001`** (Open decision 1), TinyUSB's own example pair for a CDC-only
  device. Deliberately not a registered allocation, which is consistent with Phase 5 shipping the
  CDC build behind an explicit `--port`. Widening host-side autodetect still needs a pair that is
  actually ours — settle that before touching `nrf24_adapter.py:165`, not after.
- **The serial-number string is the 96-bit factory UID** (`0x1FFFF7E8`) as 24 hex chars, so two
  Blue Pills are distinguishable in `/dev/serial/by-id` — which is what the explicit-`--port`
  workflow needs.
- **A D+ disconnect pulse at init is required, not optional.** The F103 has no internal pull-up
  control, so TinyUSB compiles `dcd_connect()`/`dcd_disconnect()` out entirely on this part and the
  stack cannot detach for us. With the Blue Pill's 1.5k pull-up permanently on D+, the host never
  sees a disconnect across an MCU reset and has no reason to re-enumerate. `usb_cdc.c` drives PA12
  low for 5ms as GPIO before handing the pin to the peripheral. This is what makes checklist step 7
  (watchdog) and any `CMD_REBOOT` come back as a fresh tty.
- **Both USB vectors are routed**, not just the low-priority one: `dcd_int_enable()` enables
  `USB_HP_CAN1_TX_IRQn` and `USB_LP_CAN1_RX0_IRQn` together, so both handlers call
  `tud_int_handler(0)`. NVIC priority 2 for both — below USART1's 1, above EXTI2's 3, and below
  SysTick's 0 so `millis()` stays accurate under USB load (the write deadline and the watchdog
  cadence both depend on it). Priorities are set *before* `tusb_init()`, which is the call that
  enables the lines.
- `tusb_init(rhport, &rh_init)`, not `tud_init(rhport)` — the latter is deprecated in 0.21.0 and
  warns under `-Wall`.

## Phase 4 — Build wiring

`targets/stm32f103/Makefile:14` is `TARGET_SRCS = $(wildcard *.c)`. **Adding `usb_cdc.c` under that
wildcard compiles it alongside `uart.c` and produces duplicate `uart_write`/`uart_read_byte`
symbols.** This must change before Phase 3 links.

- Introduce `HOST_LINK` with the two values, following the Teensy Makefiles' pattern
  (`targets/teensy4x/Makefile:14-17`). **Default is `HOST_LINK_USB_CDC`** — see Open decision 2,
  which was settled against this section's original recommendation.
- Replace the wildcard with explicit source selection: `uart.c` for the USART1 build, `usb_cdc.c`
  plus the TinyUSB sources and `-isystem` includes for the CDC build.
- **TinyUSB needs its own `-std=gnu11`**, not the target's `-std=c11`. `fsdev_common.c` uses a bare
  `asm("NOP")`, which `-std=c11` rejects because `__STRICT_ANSI__` disables the `asm` keyword. Give
  the TinyUSB objects a separate recipe with `gnu11`; do **not** patch the vendored tree, and do not
  move our own sources off `c11`. Verified during Phase 2: with `gnu11` plus `-isystem`, all six
  TinyUSB translation units compile clean under `-Wall -Wextra`.
- `make clean` is required when switching `HOST_LINK`, same as the Teensy targets — say so in the
  Makefile comment.
- **Flash check — measured.** F103/USART1 is 6252 bytes of 65536 post-Phase-1 (`arm-none-eabi-size`;
  the plan's earlier 6212 figure predates the deferred-servicing change). **F103/CDC links at 16868
  text + 48 data = 16916 bytes, 26% of 65536**, with bss 2280 of 20480 (11%). So CDC costs ~10.7KB
  of flash over USART1 — inside the 8-12KB the plan expected. The USART1 build is byte-for-byte the
  same size after the restructure, which is the regression check on Phase 4's source selection.

**Phase 4 status: done, as far as Phase 3 needed it.** `HOST_LINK ?= HOST_LINK_USB_CDC` (Open
decision 2), explicit source lists replacing the wildcard, per-directory rules putting TinyUSB
objects under `build/tu/`, and the `make clean` note in the Makefile comment. Both STM32 targets and
all four Teensy configurations still build clean under `-Wall -Wextra`, so the Phase 6 build gate
passes at seven of seven.

**Consequence of the CDC default for every checklist run below: an unqualified `make` on the F103
now produces the CDC build.** Anywhere this plan says "the USART1 build", that is
`make clean && make HOST_LINK=HOST_LINK_USART1` and a `make clean` on the way back.

## Phase 5 — Host side

`mdp_controller/nrf24_adapter.py:165` autodetects only `10C4:EA60` (the CP210x bridge). A CDC build
enumerates under its own VID/PID, so **the CDC build needs an explicit `--port`, exactly as the four
Teensy targets do today.** That is the shipped behaviour for this phase — parity, not a regression,
and the USART1 build's autodetect is unaffected.

Widening autodetect to the new VID/PID is a one-line change and can follow later; it needs the VID/PID
settled first (see Open decisions).

## Phase 6 — Verification

### F103/CDC hardware results (2026-07-31)

Run against the fixed bench config, single adapter present (the PL2303 bridge was unplugged, so no
idle adapter needed parking). Firmware = default `make`, i.e. `HOST_LINK_USB_CDC`, 16884 bytes.

| Step | Result |
|---|---|
| 1 — boot | **PASS.** Four PC13 blinks visually confirmed by the user, on reset and across a real unplug/replug. Enumerates from cold boot as `cafe:4001`, `/dev/ttyACM0`, reset-to-responsive 1.000s (n=3). |
| 2 — `host_link_test.py` | **ALL PASS** over CDC (baudrate step ACK-only, as expected). |
| 3 — persistence, real power cycle | **SKIPPED on CDC, by decision.** Two of its three checks are vacuous here (`uart_set_baudrate()` is a no-op, so the board answers at any rate and the negative control cannot fail), leaving only the radio-config check. It belongs on the USART1 build, which is the only place the baudrate half of the settings record is testable at all. Not a gap in the CDC evidence — a step that does not apply to this transport. |
| 4 — `nrf_regdump.gdb` | **PASS.** Radio reads back through the F103's own SPI path: `RX_ADDR_P0`/`TX_ADDR` = `AA:BB:CC:DD:EE`, `RX_ADDR_P1` still `C2:C2:C2:C2:C2` (no pipe opened yet — correct post-`CMD_RESET` state), `CONFIG 0x0f`, `RF_CH 0x4e`. |
| 5 — `pipe_test.py` | **PASS.** Both devices answer, pipe 1 open does not disturb pipe 0, TX retarget both ways clean. |
| 6 — 60s GUI run | **PASS, better than the in-ISR baseline.** 6864 radio sends, ~114 req/s, 12 no-acks (**0.17%**) against the table's 6694 / ~112 / 0.12%. Per-device 137.1/s and 136.7/s, link 4.5 KB/s, adapter-reported error rate 0.00%. All no-acks on `..E2`; `..E3` clean at 0/3500 — the known bench characteristic, reproduced exactly. No sign of the Teensy 3.5/3.6 ~65 req/s anomaly. |
| 7 — watchdog | **PASS.** See CDC behaviour 3 below — 3.152s mean over 5 stalls, no CDC penalty. |
| CDC failure mode | **PASS after a fix.** 254,800 commands flooded with nothing reading, then the port held open unread 5s: board neither wedged nor rebooted (USB device number unchanged), answered immediately after. **The first attempt failed** and drove the `uart_write()` redesign in Phase 3 item 3. |

**Enumeration recovers without a physical replug.** The plan expected `nrf_regdump.gdb`'s core halt
to need one. It does drop the tty, but a software reset afterwards re-enumerates on its own — the D+
disconnect pulse makes `CMD_REBOOT`, `st-flash reset` and watchdog resets all come back as a fresh
`ttyACM0`. Observed repeatedly (USB device number incrementing each time). A physical unplug/replug
also comes back clean, on the same `/dev/serial/by-id` path.

**F103/CDC is done.** Every checklist step that applies to this transport has passed on hardware.

### F103/USART1 regression results (2026-08-01)

Same board, cable moved from its own USB port to the PL2303 bridge on `/dev/ttyUSB0`. Firmware =
`make clean && make HOST_LINK=HOST_LINK_USART1`, **6252 bytes — byte-for-byte the size recorded
before Phase 4**, which is the check on the restructured source selection. Single adapter on the
bench again (no CDC device enumerated), so nothing needed parking.

| Step | Result |
|---|---|
| 1 — boot | **PASS.** Board answers on `/dev/ttyUSB0` after a cold `st-flash --reset`. |
| 2 — `host_link_test.py` | **ALL PASS.** Including the two sub-steps CDC cannot evidence: `CMD_SET_BAUDRATE` really retunes USART1 to 115200 and the host follows it across, then back to 921600. |
| 3 — persistence, real power cycle | **Not run — dropped from this work entirely.** See CDC behaviour 1. |
| 4 — `nrf_regdump.gdb` | **PASS.** Every register matches the hand-derived boot-default table exactly: `CONFIG 0x0f`, `EN_AA`/`EN_RXADDR` `0x01`, `SETUP_AW 0x03`, `SETUP_RETR 0x03`, `RF_CH 0x4e`, `RF_SETUP 0x0f`, `STATUS 0x0e`, `RX_PW_P0 0x20`, `RX_PW_P1 0x00`, `FIFO_STATUS 0x11`, `RX_ADDR_P0`/`TX_ADDR` = `AA:BB:CC:DD:EE`, `RX_ADDR_P1` = `C2:C2:C2:C2:C2`. |
| 5 — `pipe_test.py` | **PASS.** Both devices answer, pipe 1 open leaves pipe 0 undisturbed, TX retarget clean both ways. |
| 6 — 60s GUI run | **PASS, best F103 numbers recorded.** 7020 radio sends, 8478/8478 samples (140.8/s each), 4 no-acks (**0.06%**), link 4.6 KB/s, 0.00% adapter-reported errors. Beats both the in-ISR baseline (6694 / 0.12%) and the CDC build (6864 / 0.17%). |
| 7 — watchdog | **PASS. 3.151s, five stalls, zero spread** (`min = max = mean`). |

**Run-to-run RF variance is larger than the difference between any two of these builds.** The first
60s run gave 6464 sends and 21 no-acks (0.32%) — worse than every recorded figure — and the second,
back to back with nothing changed, gave the 7020 / 0.06% above. 8 of the first run's 21 no-acks were
a single 0.18s burst on Type-9. Both runs put *every* no-ack on `..E2`, with `..E3` at 0/3300 and
0/3600 — the known bench characteristic, reproduced twice more. Treat a single run's no-ack rate as
evidence of nothing; the ranking in the Phase 1 baseline table is inside this noise.

**The watchdog measurement needed a method fix, and the corrected figure cross-checks the CDC one to
1ms.** The stall was placed at the top of the loop under `-DWATCHDOG_STALL_TEST`, which sits *before*
the 100ms watchdog refresh, so up to 100ms of the timeout had already elapsed when the stall began —
that first attempt read 3.071s. Calling `watchdog_refresh()` immediately before hanging makes the
host-side period exactly `stall_at_ms + T_iwdg`, and it then read **3.151s against CDC's 3.152s on
the same silicon**. That is independent confirmation of Phase 6's finding that CDC costs the watchdog
nothing — the two host links land 1ms apart. Both are under the original bring-up's 3.167–3.218s,
which used a stall with an unmeasured offset of exactly this kind; the ~20ms is method, not drift.

**Switching `HOST_LINK` without `make clean` fails at link time, loudly.** Confirmed deliberately:
`ld` reports *"(uart_init): Unknown destination type (ARM/Thumb) in build/main.o"* and a dangerous
relocation. Worth recognising, because the message names a relocation problem rather than a stale
build — but the important part is that it cannot silently produce a mixed image.

### F030 results (2026-08-01)

Board swapped onto the bench in place of the Blue Pill, same PL2303 bridge on `/dev/ttyUSB0`, ST-LINK
attached at the same time (the bare F030 dev board takes both at once — the "SWD and UART are
mutually exclusive" note applies to the original dongle module, which this bench does not have).
Firmware = plain `make`, 6628 bytes. Full 16KB flash backup taken first; the settings page at the top
of the 16KB part is well clear of the image.

This is **not** a regression check — Phase 1 changed the F030's servicing model too, so it is under
test in its own right.

| Step | Result |
|---|---|
| 1 — boot | **PASS.** Answers on `/dev/ttyUSB0` after a cold reset, and reboots cleanly five times running during step 7. |
| 2 — `host_link_test.py` | **ALL PASS**, baudrate retune included. |
| 3 — persistence, real power cycle | **Not run — dropped from this work entirely.** See CDC behaviour 1. |
| 4 — `nrf_regdump.gdb` | **PASS** — see the STATUS note below. Every other register matches the boot-default table exactly, including `RX_ADDR_P0`/`TX_ADDR` = `AA:BB:CC:DD:EE`. The F103's script is target-neutral and ran unmodified against the F030 ELF (with `target/stm32f0x.cfg`); all three SPI primitives survive `-O2` here too. |
| 5 — `pipe_test.py` | **PASS.** Both devices answer, pipe 1 open leaves pipe 0 undisturbed, TX retarget clean both ways. |
| 6 — 60s GUI run | **PASS, and the biggest win in the whole port.** Run 1: 6364 sends, 7 no-acks (**0.11%**), 7524/7503 samples. Run 2: 6416 sends, 5 no-acks (**0.08%**), 126/s. Against the in-ISR baseline of 6140 / ~102 req/s / 283 no-acks (**4.61%**) — a ~40× reduction in no-acks, reproduced across both runs. |
| 7 — watchdog | **PASS. 3.185s** (3.184–3.186, n=5). |

**Deferring radio servicing mattered far more on the F030 than anywhere else.** Every other target
moved by fractions of a percent; this one went from 4.61% no-acks to under 0.11%, twice. That is
orders of magnitude outside the run-to-run spread measured on the F103 above, so unlike the F103's
numbers it is a real effect and not noise. It also fits: the M0 at 48MHz was doing the whole radio
transaction inside the ISR, on the slowest target on the bench.

**One bad SPI read, on the first dump only.** `STATUS` came back `0xff` in the first
`nrf_regdump.gdb` run and `0x0e` in the three immediately after, with nothing changed. `0xff` was not
a real register value: it claims `TX_FULL` while `FIFO_STATUS`, read moments later in the same dump,
says `TX_EMPTY`, and it has all three interrupt flags latched after a `CMD_RESET`. Nor was it the
documented all-ones "MISO never drove" signature — every other register in that same dump read
correctly. **The F030 clocks SPI at 12MHz, above the nRF24L01+'s 10MHz ceiling**, which is already
recorded in the README as a known over-spec condition not carried to the F103 (9MHz) or the Teensys
(10MHz); a single corrupted byte is exactly what that would look like. One observation is not enough
to pin it on the clock, and it did not recur in ~24 further register reads, so this is logged rather
than chased.

**Regression pass complete.** Nothing from the hardware gate is outstanding.


**Build gate:** seven configurations, all clean under `-Wall -Wextra`, with `arm-none-eabi-size`
recorded — f030, f103/USART1, f103/CDC, teensy41, teensy40, teensy35, teensy36.

**Hardware gate:**

- ~~**After Phase 1, before Phase 2 starts:**~~ **Ran after Phase 6 instead, on F103/USART1 only** —
  the CDC bring-up needed the bench first and the bridge was unplugged for it. Not the intended
  order: the CDC work landed on top of an unverified Phase 1. It came out clean (see the USART1
  results above), so the risk did not materialise, but the sequencing is the thing to do differently
  next time. **F030 ran the same day and also passed** — it changed servicing model too, so it was
  under test rather than a regression check, and it turned out to be the target Phase 1 helped most.
- **After Phase 3-4:** F103/CDC — enumeration from cold boot, re-enumeration after unplug/replug,
  then the full checklist over the CDC port. **Read the CDC expectations below first.**
- **The CDC failure mode to test deliberately:** run the firmware with the port enumerated but nothing
  reading, and confirm the board neither wedges nor reboots. That exercises the `uart_write()`
  deadline from Phase 3.
- **Regression:** ~~F103/USART1 and F030 still pass after the CDC work lands.~~ **Both PASS
  2026-08-01.** Neither shares code with `usb_cdc.c`, but Phase 4 restructured how the USART1 build
  selects its sources — and that build came back at 6252 bytes, the same size as before it.

### Three CDC behaviours that look like failures and are not

All three are established on the Teensy targets, which have run `HOST_LINK_USB_CDC` in production
since the 4.1 port.

1. **`persistence_test.py`'s `verify` phase partly goes vacuous.** It runs three checks: (1) board
   answers at 115200 after the power cycle, (2) `CMD_NRF_QUERY` returns the saved radio config rather
   than the defaults, (3) negative control — board is *silent* at 921600. `uart_set_baudrate()` is a
   no-op on CDC, so there is no physical line rate and the board answers whatever pyserial asks for:
   **check 3 fails by design, check 1 passes for the wrong reason, and check 2 is the only real
   evidence.** Consequence: the CDC build cannot demonstrate that the saved *baudrate* survives a
   power cycle at all, only the radio config.
   **Step 3 is dropped from this work entirely — the user's call, 2026-07-31.** Not just skipped on
   CDC: it is not owed on the USART1 build either. The flash-page code (`flash_store.c`) is unchanged
   by this work and was already validated on this board during the USART1 bring-up, so re-running it
   would re-test code nothing here touched. Do not reintroduce it as a loose end.
2. **`host_link_test.py`'s baudrate step becomes ACK-only.** On CDC the retune-and-reopen is harmless
   but proves nothing: a pass says `CMD_SET_BAUDRATE` ACKed and the value persisted, not that any rate
   changed. That evidence comes from the USART1 build.
3. ~~**The watchdog step will measure ~3.8s, not the ~3.2s the USART1 build measures.**~~
   **Measured, and this prediction was wrong — there is no CDC penalty.** The expectation was
   ~3.2s IWDG plus ~0.58–0.6s USB re-enumeration, on the assumption that enumeration serialises
   after the reset. It does not: enumeration runs concurrently with the boot path and completes
   inside the existing four-blink sequence, which pumps the USB stack (`boot_delay_ms()`, Phase 3
   item 4). Measured on the CDC build with the marker-frame technique, n=5 stalls: marker-to-marker
   **3.145–3.156s, mean 3.152s**, against the USART1 build's 3.167–3.218s IWDG — i.e. the same
   number, so the IWDG is firing on schedule and re-enumeration costs nothing measurable.
   Reset-to-responsive on the production firmware is a flat **1.000s** (n=3), which is the whole
   boot path including enumeration.
   Still true and still worth stating: **the watchdog is what recovers the board**, and materially
   more than the USART1 figure would be a finding.
   Time it with the marker-frame technique — `uart_write` a marker immediately before `while(1){}` in
   the *target's own* `main.c`, never in shared `core/` — not by boot-to-boot spacing.
   **Never test this with a debugger halt.** OpenOCD sets `DBGMCU_CR` bit 8 (`DBG_IWDG_STOP`), which
   freezes the IWDG whenever the core is halted; the core sat halted 15s with no reset during the
   USART1 bring-up, which reads as a broken watchdog and proves nothing.

### Bench discipline for step 6

- `gui_source/settings.json` must point at the adapter under test and **must be restored** afterwards.
  It is gitignored, so git will not remind you.
- **Park the idle adapter on an unused address and channel** before A/B-ing the CDC and USART1 builds
  against the same devices — an adapter left in RX on the bench address auto-ACKs packets meant for
  the board under test and corrupts the numbers. Physically disconnecting it also works.
- Both devices are radio-deaf for ~3–4.5s after power-on. The headless GUI segfaults during
  interpreter teardown *after* all results have printed — a PyQt/pyqtgraph artifact, not a run failure.
- The bench radio configuration is fixed: adapter base `AA:BB:CC:DD:EE`, P906 `..E2` on pipe 1, L1060
  `..E3` on pipe 2, channel 2521. **Nothing in this plan may re-pair either device.** A script wanting
  different values is wrong; fix the script.

## Phase 7 — README updates

`nrf_adapter_source_multiceiver/README.md`. **Current state only** — no decision history, no dates, no
"we evaluated and chose", and **no links to this plan or to `firmware_toolchain_research.md`**.

- **`README.md:168-174`** — the paragraph under `## STM32 Blue Pill target` says USART1 "is the
  **only** host link" and that the onboard USB "would need a vendored USB device stack plus a CDC glue
  layer for no real benefit over reusing the validated USART1 path." Both clauses become false.
  Replace with what the CDC build is, plus the `make clean` requirement when switching `HOST_LINK`.
- **`README.md:183-188`** — the "**The radio is serviced in the ISR**" bullet. After Phase 1 no target
  services the radio in interrupt context, so remove it rather than rewriting it; the same applies to
  its mirror image, the Teensy 4.x "Radio IRQ runs in thread context" bullet at `README.md:219`. One
  model now covers every configuration, which makes it a shared design note
  (`README.md:123`, `## Design notes / deviations from the shipped firmware`), not a per-target
  deviation. Check whether the `uart.c` NVIC priority discussion is still cited anywhere as a live
  constraint.
- **Layout block** — a `Drivers/tinyusb` entry with its upstream version.
- **`## Building` / `## Flashing`** — the `HOST_LINK=` invocation for the F103.

Two unrelated README gaps worth closing in the same pass since the file is open:

- **Name the Teensyduino snapshot.** `README.md:109-117` says the cores are "copied verbatim" and that
  teensy3 is the "same snapshot as teensy4/" but never which one. It is **Teensyduino 1.59**, per
  `-DTEENSYDUINO=159` in `targets/teensy4x/Makefile:47`, `targets/teensy3x/Makefile:50`,
  `Drivers/teensy4/Makefile:70` and `Drivers/teensy3/Makefile:56`. Keep to the terse Layout style.
- **Add a vendored-core refresh procedure** after `## Flashing`: refresh for a specific core-level bug
  rather than on a schedule; download the Teensyduino release from pjrc.com (no account); diff
  `cores/teensy3`, `cores/teensy4`, `libraries/SPI` against `Drivers/` and treat anything resembling a
  local modification as a finding, since the trees are verbatim copies; copy over and bump
  `-DARDUINO=`/`-DTEENSYDUINO=` in both target Makefiles; rebuild all configurations; re-run the
  per-board checklist. Worth a line that `teensy_loader_cli` is the only part of the distribution
  needed day to day and builds standalone from PJRC's GitHub.
  **Call out the Kinetis watchdog constants in that procedure.** `WDOG_TOVALL` in
  `targets/teensy3x/platform_teensy3.cpp` is `698` on the 3.5 and `688` on the 3.6; neither came from
  the reference manual, both from multi-point stall-test calibration on the specific chip, and the
  file's own comment warns to re-measure if its timing-sensitive surroundings change materially. A
  core refresh or compiler change is exactly that. Checklist step 7 is what catches it, and a silently
  mis-timed watchdog is the one failure nothing else in the checklist would surface. **Say how to
  measure it, too**: stall at a fixed `millis()` and time the *period between answering windows*, never
  the marker-frame-to-port-disconnect interval — the latter also counts USB teardown after the reset
  and over-measures by ~0.8–1.0s. That mistake is what made both these constants wrong until
  2026-08-01.

---

## Open decisions

Both settled in Phase 3; kept here with their reasoning.

1. **VID/PID for the CDC descriptor.** Drives whether host-side autodetect can ever be widened.
   TinyUSB's example pair works for bring-up; decide before Phase 5 is revisited.
   **Decided: `0xCAFE:0x4001`, TinyUSB's example pair.** Not a registered allocation, which is the
   thing to fix first if autodetect is ever widened.
2. **Default `HOST_LINK` for the F103.** The recommendation here was `HOST_LINK_USART1`, so an
   unqualified `make` built the validated path and CDC was opt-in.
   **Decided: `HOST_LINK_USB_CDC`** — the user's call, on the grounds that the whole point of this
   work is not needing the external USB-UART bridge, and a default that still requires either the
   bridge or a remembered `HOST_LINK=` flag gives that up at the last step. Matches the Teensy
   targets' default too.

   What that costs, and how it is covered: the USART1 build is the CDC build's only same-silicon
   A/B partner — identical target, identical radio path, differing in transport alone — so it stays
   the reference for the Phase 1 baseline table and the only place a baudrate regression on this
   chip can surface at all (see CDC behaviour 1). None of that requires it to be the *default*, only
   that it keep building and stay runnable with an explicit
   `make clean && make HOST_LINK=HOST_LINK_USART1`.

   In the end this cost nothing outstanding. Step 3 was dropped from the work entirely (CDC
   behaviour 1), and the baudrate half of step 2 — the one thing CDC cannot evidence on its own —
   was run on the USART1 build on 2026-08-01 and passed, along with the rest of the checklist. Making
   CDC the default did not cost any coverage; it only moved which build needs the explicit flag.

## Out of scope

- **USB on the F030.** No USB peripheral on the die. (It *is* converted to deferred radio servicing in
  Phase 1 — that part is in scope.)
- PlatformIO, `arduino-cli`/Teensyduino build-recipe adoption, STM32Cube HAL — all declined, see
  `firmware_toolchain_research.md` §2.
- Substituting `core/nrf24l01p.c` for a community library such as RF24.
- PyInstaller/packaging.

## Commit

Leave committing to the user. Natural split, one per validated step:

```
stm32: defer radio servicing to the main loop on both targets
f103: native USB CDC host link behind HOST_LINK
```
