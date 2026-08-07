# Third-party code

This repository is licensed Unlicense at the top level (`LICENSE`), but that
grant never described everything under it. This file is the license
inventory the top-level Unlicense doesn't provide.

## `nrf_adapter_source_multiceiver/Drivers/`

Not committed to git — git-ignored and fetched on demand by
`tools/fetch_vendor.py` at the pins recorded in `tools/vendor.json`. See
`nrf_adapter_source_multiceiver/README.md` ("Layout" and "Building") for how
to fetch them and `plans/devendoring_plan.md` for why they're handled this
way.

| Tree | Upstream | Pin | SPDX license | Linked into |
|---|---|---|---|---|
| `Drivers/CMSIS/Include` | [`STMicroelectronics/cmsis-core`](https://github.com/STMicroelectronics/cmsis-core) | tag `v5.4.0` | Apache-2.0 | every STM32 target |
| `Drivers/CMSIS/Device/ST/STM32F0xx` | [`STMicroelectronics/cmsis-device-f0`](https://github.com/STMicroelectronics/cmsis-device-f0) | tag `v2.3.7` | Apache-2.0 | stm32f030 |
| `Drivers/CMSIS/Device/ST/STM32F1xx` | [`STMicroelectronics/cmsis-device-f1`](https://github.com/STMicroelectronics/cmsis-device-f1) | tag `v4.3.5` | Apache-2.0 | stm32f103 |
| `Drivers/teensy3` | [`PaulStoffregen/cores`](https://github.com/PaulStoffregen/cores) | commit `7f107ee0a309f3813ed13f0d8f615497eca2ee49` | MIT (PJRC variant — see below) | teensy3x |
| `Drivers/teensy4` | [`PaulStoffregen/cores`](https://github.com/PaulStoffregen/cores) | commit `7f107ee0a309f3813ed13f0d8f615497eca2ee49` | MIT (PJRC variant — see below) | teensy4x |
| `Drivers/teensy_libs/SPI` | [`PaulStoffregen/SPI`](https://github.com/PaulStoffregen/SPI) | commit `7c83d0726746b652af37319e70cd3932c253ecae` | **GPL-2.0-only OR LGPL-2.1-only** | teensy3x, teensy4x |
| `Drivers/teensy_libs/QNEthernet` | [`ssilverman/QNEthernet`](https://github.com/ssilverman/QNEthernet) | tag `v0.36.0` | **AGPL-3.0-or-later** | teensy4x `ETH=1` only |
| `Drivers/tinyusb` | [`hathach/tinyusb`](https://github.com/hathach/tinyusb) | tag `0.21.0` | MIT | stm32f103 USB CDC host link |

**AGPL-3.0-or-later (QNEthernet) applies to any firmware image built with
`ETH=1`.** Section 13 of that license (the network-interaction clause) is
in scope for such an image the moment it's reachable over a network — which
is the entire point of the Ethernet host link. Every other build
configuration in this tree — including every other Teensy 4.x build — does
not link QNEthernet and is unaffected.

**GPL-2.0-or-LGPL-2.1 (PJRC's SPI library) is linked into every Teensy image**
(3.5, 3.6, 4.0, 4.1), `ETH=1` or not — `nrf24l01p.c`'s SPI transactions go
through it on both cores. LGPL-2.1 is the practically relevant half of that
dual grant for a statically-linked embedded image; GPL-2.0 is the more
restrictive alternative under the same "OR".

**MIT (PJRC's cores, `teensy3`/`teensy4`)** ship a non-standard clause 2:
any build system that offers a list of target devices to build for must
list similar PJRC.COM devices alongside them. `targets/teensy3x/Makefile`
and `targets/teensy4x/Makefile` each hard-code a single `BOARD` selection
(no device picker of the kind the clause addresses), so this doesn't obligate
anything further here, but it's a real term of the license text, not
boilerplate to skip past.

**Apache-2.0 (CMSIS)** requires the license text and a NOTICE file (if
upstream ships one) to accompany redistribution; both ship as part of each
tree's fetch (`LICENSE.txt`/`LICENSE.md` per `tools/vendor.json`).

## `nrf_adapter_source/Drivers/`

Out of scope for the de-vendoring project (`plans/devendoring_plan.md`) and
still committed to git. `Drivers/CMSIS` and `Drivers/STM32F0xx_HAL_Driver`
each ship an ST `LICENSE.txt`. This is the legacy Keil project's HAL/CMSIS
tree; four of its libraries carry no version marker at all, so their
provenance can't be pinned the way the multiceiver tree's can. The licensing
story here stays incomplete.

## `gui_source/qframelesswindow/`

A genuine **fork** of [`zhiyiYo/PyQt-Frameless-Window`](https://github.com/zhiyiYo/PyQt-Frameless-Window)
(GPL-3.0-only), with local additions — an added `FullscreenButton` and five
`set_*_btn_enabled` methods that don't exist upstream. It cannot be
re-downloaded the way `Drivers/` can, since it no longer matches any single
upstream revision, and it remains vendored (committed to git) for that
reason.

## `gui_source/richuru.py`

No upstream marker present in the file; not addressed by this inventory.

## `.upx/`

Not addressed by this inventory.

## What this file doesn't cover

Git history is not discussed here. The five commits that once carried the
`nrf_adapter_source_multiceiver/Drivers/` trees listed above were rewritten
out of this repository's history (`plans/devendoring_plan.md`, Phase 4) —
by the time this file exists, there is nothing left in history for it to
disclose.
