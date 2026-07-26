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
Core/Inc, Core/Src   Application code (no HAL): startup/vector table,
                     clock init, GPIO, SPI1, USART1, nRF24L01+ driver,
                     flash-backed settings store, UART framing + command
                     dispatch, main().
Drivers/CMSIS        Copied verbatim from nrf_adapter_source/Drivers/CMSIS
                     (ST-provided register definitions only, no HAL driver
                     folder — this is the whole point of "no HAL").
linker/              STM32F030F4Px.ld: 15KB code region + reserved last 1KB
                     flash page for settings (see flash_store.c).
```

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

## Building

```sh
sudo apt-get install gcc-arm-none-eabi   # arm-none-eabi-gcc 14.2, if not already installed
make          # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}
make clean
```

## Flashing

Via ST-LINK V2 over SWD (no BOOT0/3V3 short needed — that's only for the
UART bootloader method in `readme_EN.md`, which doesn't apply here). Wire
SWCLK/SWDIO/GND/3V3 from the ST-LINK to the target; SWD uses PA13/PA14,
which this firmware never touches (see pin mapping above), so there's no
conflict with the app pins.

```sh
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
