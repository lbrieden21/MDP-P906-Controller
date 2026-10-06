# Miniware MDP-P906 / MDP-L1060 Controller

Wireless control of the MDP-P906 power supply and the MDP-L1060 electronic load without the MDP-M01 display module. The P905 is supported as well (tested upstream, see [#1](https://github.com/ElluIFX/MDP-P906-Controller/issues/1) and [#2](https://github.com/ElluIFX/MDP-P906-Controller/issues/2)).

![1732027356466](image/readme_EN/1732027356466.png)

![1721844452863](image/readme/1721844452863.png)

## About this fork

This repository is a fork of [ElluIFX/MDP-P906-Controller](https://github.com/ElluIFX/MDP-P906-Controller), created by Ellu. It was forked at upstream commit `85623d2` (March 2025). Everything up to that commit, including the original P906 controller, GUI, protocol work and adapter firmware, is Ellu's work, and the fork would not exist without it.

The main reason for the fork was to add support for the **MDP-L1060 electronic load**. Supporting a second device type led to further changes:

- Multi-device support: several P906/L1060 devices can run from one radio adapter at the same time.
- A bare-metal (direct CMSIS register access) multiceiver rewrite of the adapter firmware in [nrf_adapter_source/](nrf_adapter_source/). It replaces the original STM32CubeMX + HAL + Keil MDK project, which is still available in this repository's git history at the `init` commit. The new firmware runs on STM32, Teensy and ESP32 boards, with USB/serial, WiFi and Ethernet host links.
- New GUI tools (P906 battery charging and the L1060 Sweep, Sequence and Discharge tools), a reworked graph-capture model, and background status polling.

**AI-written code.** Since the fork, nearly all of the code and planning in this repository (about 99.9%) has been written by various OpenAI ChatGPT and Anthropic Claude models under my guidance. I directed the work, tested it on real hardware and decided what to keep. The models wrote the code, the documentation and the plans in [plans/](plans/).

## Acknowledgements

The protocol implementation is derived from [leommxj/mdp_commander](https://github.com/leommxj/mdp_commander). The original author has said that without it, there would have been no way to work out the communication protocol between the M01 and the P906.

Upstream, the original author spent a lot of time optimizing communication quality on top of that protocol work and reached stable, long-term data acquisition at up to 160fps.

![1721847380188](image/readme/1721847380188.png)

## Features

### Python3 API

- P906: set output/voltage/current, read device status, and read the output ADC measurements in real time
- L1060: select the CC/CV/CR/CP mode, set targets, switch the load on/off, and read status, targets and real-time measurements
- Several devices can share one adapter (`MDPBus`), each on its own nRF24 RX pipe

### PyQt5 GUI

- Basic parameter setting, preset group management, setting modification
- Data acquisition, plotting, analysis, and saving up to 100Hz (adjustable)
- PID constant power control
  - Max V is the safety limit: if the load stops drawing current, measured power drops to zero and the controller drives the output up to Max V. Set it to something your load can tolerate rather than leaving it at 30V.
- Parameter scanning (voltage/current)
  - Plotting scanning response curves (for discovering load characteristics)
- Operation sequence (single or loop execution of action sequences)
- Battery simulator (supports custom battery voltage curves/capacity/internal resistance/series-connection settings; ships with Li-ion, LiFePO4, lead-acid, NiMH, alkaline, and zinc-carbon discharge curves)
- Battery charger: chemistry-preset CC/CV (or NiMH −ΔV) charging with cutoff-current/time/Ah/Wh stop conditions and CSV export (see [P906 Battery Charge](#p906-battery-charge) below)
- MDP-L1060 electronic load support: mode-aware parameter sweep, sequence automation, and battery discharge testing (see [L1060 Auxiliary Tools](#l1060-electronic-load-auxiliary-tools) below)
- Multi-device support: connect and monitor several power supplies at once, each with its own panel and independent link/unlink control
- Data floating window
- Customizable waveform buffer length
- Material Design style with two color themes
- i18n support (zh-CN/en-US)
- Simulation mode for trying the GUI without hardware

### Adapter firmware

- Bare-metal nRF24L01+ multiceiver firmware for STM32F030 (the original USB dongle), STM32F103 Blue Pill, Teensy 3.5/3.6/4.0/4.1, and ESP32-C5/C6/H2/S3/classic ESP32. Every board except the original dongle needs a separate nRF24L01+ module wired to it (see [Adapter Hardware](#adapter-hardware)).
- Host link over USB/serial on every board, over WiFi on the ESP32 boards that have it, and over Ethernet on the Teensy 4.1

## Usage Instructions

### Adapter Hardware

Every setup needs an nRF24L01+ radio adapter running the firmware in [nrf_adapter_source/](nrf_adapter_source/). You can get one in two ways:

- **Build your own** from a supported microcontroller board (STM32F030, STM32 Blue Pill, Teensy 3.5/3.6/4.0/4.1, ESP32-C5/C6/H2/S3/classic ESP32) plus a separate nRF24L01+ radio module wired to it over SPI. None of these boards has an nRF24 radio built in. The radio module is a separate part, unlike the original USB dongle below, which combines the MCU and the radio in one unit. Inexpensive nRF24L01+ modules [like this one](https://www.aliexpress.us/item/3256809067567814.html) work well and easily reach across a room. See [nrf_adapter_source/README.md](nrf_adapter_source/README.md) for the board list, the radio wiring for each board, and build/flash instructions for each target.
- **Modify the USB-to-NRF24L01 dongle** that the original project was built around, as described below. It is the only supported option with the radio built in. It is no longer readily available, so this section mainly helps people who already own one. The adapter firmware's STM32F030 pin mapping was recovered from the dongle's shipped firmware.

If you already have a different STM32 + NRF24L01 combination, you can port the firmware by adapting the pin definitions. The firmware is bare-metal (no CubeMX/HAL), so a port means editing the pin/clock setup directly in the source. The STM32F030 target's [gpio.h](nrf_adapter_source/targets/stm32f030/gpio.h) is the reference for the original module's pinout. The original author did not publish the reverse-engineered circuit.

#### The original USB-NRF24L01 dongle

The dongle was sold for $5.67 on [AliExpress](https://www.aliexpress.com/item/1005006003453078.html?spm=a2g0o.productlist.main.7.3828oWBqoWBqd6&algo_pvid=27999fdf-f812-4149-b2a8-251e95c1cc29), as shown below:

![1721841880272](image/readme_EN/1721841880272.png)

This module has an independent PA amplifier and reaches up to two meters, compared with the Arduino Nano RF that leommxj used. However, it uses its own protocol to implement a wireless serial port, which is not compatible with the raw NRF24L01 data stream needed to control the devices.

![1722007003614](image/readme/1722007003614.png)

Fortunately, the module uses a genuine STM32F030F4P6 as its main controller, so it can be reflashed with this project's firmware.

#### Modification Method

**Important:** the Python driver in this repo ([mdp_controller/bus.py](mdp_controller/bus.py)) always uses the nRF24L01+'s hardware RX pipes to address devices (`CMD_NRF_OPEN_PIPE`, 0x23) — even for a single device. That command only exists in the bare-metal multiceiver firmware under [nrf_adapter_source/](nrf_adapter_source/). The dongle's original shipped firmware and the upstream project's pre-built release image don't support it and don't work with this driver. There is no pre-built image for the multiceiver firmware, so you have to compile it and flash it yourself over SWD.

Pry open the module's case and flip it over to see the test points as shown in the image below:

![1721840680045](image/readme/1721840680045.png)

Build the firmware for the STM32F030 target (needs `arm-none-eabi-gcc`):

```sh
sudo apt-get install gcc-arm-none-eabi
python3 tools/fetch_vendor.py --target stm32f030   # fetches Drivers/CMSIS, git-ignored not committed
cd nrf_adapter_source/targets/stm32f030
make          # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}
```

Flash it over SWD with an ST-LINK V2 — wire `SWCLK`/`SWDIO`/`GND`/`3V3` from the ST-LINK to the module's test points as shown below. This method does **not** use the `BOOT0`/`3V3` short or the serial bootloader.

![1721841339876](image/readme/1721841339876.png)

```sh
# stlink-tools
sudo apt-get install stlink-tools
st-flash write build/MDP_Adapter_Multiceiver.bin 0x08000000

# or OpenOCD
sudo apt-get install openocd
openocd -f interface/stlink.cfg -f target/stm32f0x.cfg \
  -c "program build/MDP_Adapter_Multiceiver.elf verify reset exit"
```

If the module still has its original read/write protection set, `st-flash` will refuse to write — run `st-flash erase` first (mass-erases and drops protection back to level 0), or with OpenOCD: `-c "stm32f0x unlock 0; reset halt"`.

Alternatively, [STM32 CubeProgrammer](https://www.st.com/en/development-tools/stm32cubeprog.html) can flash the same `.bin`/`.hex` over the same SWD wiring, and has its own "remove read-out protection" option in the GUI if needed.

See [nrf_adapter_source/README.md](nrf_adapter_source/README.md) for full build/flash details and firmware design notes.

### Control by API

Refer to the code and the comments.

- [test_main.py](./test_main.py): P906 example
- [test_l1060.py](./test_l1060.py): L1060 example (runs against the simulator by default)
- [mdp_p906.py](./mdp_controller/mdp_p906.py) and [mdp_l1060.py](./mdp_controller/mdp_l1060.py): the full device APIs
- [bus.py](./mdp_controller/bus.py): `MDPBus`, the shared adapter that devices are attached to by pipe

### Control by GUI

The GUI runs from source. Create a virtual environment, install the dependencies, and start `gui_source/mdp_main.py`:

```sh
python3 -m venv venv
venv/bin/pip install PyQt5 pyqtgraph PyQtDarkTheme-fork numpy loguru rich pyserial simple-pid superqt
venv/bin/pip install xcffib   # Linux only
venv/bin/pip install numba    # optional: faster graph rendering
venv/bin/python gui_source/mdp_main.py
```

On Windows, install `pywin32` instead of `xcffib` and run `venv\Scripts\python gui_source\mdp_main.py`. Settings are saved to `gui_source/settings.json`.

#### Graph capture

Linking a device does not start the graph — the panel's LCDs and other readouts update as soon as it's linked, but the graph stays empty until capture is started.

- **Start/Stop** (in the graph section's toolbar, next to Fit/Keep/Clear) starts or stops writing samples into the graph. Pressing Start again after a Stop continues the same timeline with a visible break in the line rather than resetting to t=0; **Clear** is the only way to reset the timeline to t=0.
- **Auto-start/Auto-stop** — the graph section's toolbar has an Auto-start and an Auto-stop control, each with a mode (Off/Voltage/Current) and a threshold. The mode and threshold are saved per device. Auto-start fires on a rising crossing (the device's reading goes from below the threshold to at or above it) and begins capture. Auto-stop fires on a falling crossing (from at or above the threshold to below it) and ends capture. In the default **Shared** graph layout, Auto-start and Auto-stop each have their own **Device** selector, so capture can start on one device's reading and stop on another's. Other linked devices are captured but never trigger anything. In the **Per Device** graph layout (see [Multiple Devices](#multiple-devices)), each device's graph section starts and stops on that device's own readings.
- The Start/Stop button shows **Armed** (yellow) when capture is stopped and an auto-start is configured for a linked trigger device, with the start condition as its tooltip. While capture is running it shows **Stop** (green), with the stop condition (if any) as its tooltip.
- **REC** is unrelated to graph capture: it always writes its raw full-rate CSV regardless of whether the graph is running.

#### Multiple Devices

The GUI can drive more than one device at a time. Open **Connection Settings** and use the **+**/**-** buttons next to the device selector to add or remove a device. Choose its type (P906 or L1060) when adding it, then configure its IDCODE, LED color and M01 channel there. Every device gets its own panel (stacked in the left column) with its own **LINK/UNLINK** button, so devices can be connected and disconnected independently of each other — the radio adapter itself stays shared and opens/closes automatically as needed.

Connecting runs in the background: the panel shows *Connecting...* while the rest of the window, including every already-linked panel, keeps updating, and clicking **LINK/UNLINK** again cancels it. A connect keeps trying for up to 8 s before reporting failure, because both the P906 and the L1060 don't answer the radio for roughly 3-4.5 s after power-on, so linking a device you have just switched on still succeeds. **AutoMatch** in Connection Settings likewise runs in the background, with the button showing *Pairing...* until it finishes. While any device is linked, connecting or pairing, adding or removing devices and starting another auto-match are refused.

With many devices the stacked panels get cramped. Set **Graphic Settings → Device Layout** to **Single Device** to show one panel at a time using the full column height, with a row of device buttons above it to switch between them. Devices that aren't shown stay linked and keep capturing while the graph is running. Linked devices are marked with a dot on their button.

By default every device shares one graph area, with its own channel chip on each block (**Graphic Settings → Graph Layout: Shared**). Set it to **Per Device** to give every device its own complete graph section instead — its own channel buttons, Start/Stop/Keep/Clear, buffer slider and timeline, with no chips since each section only ever shows its own device. Graph Layout combines with Device Layout independently: **Show All** stacks one section per device, while **Single Device** shows only the selected device's section (switching devices swaps which section is shown; hidden devices keep capturing in the background). Switching Graph Layout clears all graph data, since a shared timeline and per-device timelines can't be merged into each other.

This is implemented using the adapter's nRF24L01+ hardware RX pipes to tell devices apart, so it requires the [multiceiver adapter firmware](#adapter-hardware) and is capped at **5 devices per adapter** (pipes 1-5; pipe 0 is reserved for the adapter's own transmit ACKs).

#### Sharing devices with a real MDP-M01

Each device's address is derived as `<adapter address>[:4] + (0xE0 + pipe)` — the same scheme an MDP-M01 uses: the hub keeps `<base>E0` for itself and hands its slot *k* the address `<base> + (0xE1 + k)`, all on one shared RF channel. Set the adapter's **Address** and **Channel** to your M01's values and a device the M01 has paired is already where this software expects it (pipe 1 = the M01's first slot, pipe 2 = its second, and so on), so devices can move between the M01 and this controller without being re-matched at the front panel. Without an M01, any address and channel will do.

#### Connecting over WiFi (ESP32 adapters)

An ESP32-C5, ESP32-C6, ESP32-S3 or classic ESP32 (WROOM-32) adapter built with `HOST_LINK_WIFI` (see
[nrf_adapter_source/README.md](nrf_adapter_source/README.md)) can be
driven over the LAN instead of USB, once it's been provisioned with WiFi credentials over its
wired link. In **Connection Settings**, set **Connection Type** to **WiFi (TCP)** and enter the
adapter's **Host Address** (its DHCP-assigned IP) and port (9000 by default) instead of picking
a serial port/baud rate. The USB/serial fields are disabled while WiFi is selected, and vice
versa. Anything else about the GUI — multi-device support, panels, LINK/UNLINK — works exactly
the same regardless of which transport the adapter is reached over.

The same **Connection Type: WiFi (TCP)** setting also reaches a Teensy 4.1 adapter built with
`ETH=1` (see the same README's Teensy 4.x section) — there's no separate "Ethernet" transport
option, since both are just a TCP socket to the adapter's IP on port 9000.

#### P906 Battery Charge

The P906 panel's Battery Charge tab runs a real CC/CV (or NiMH −ΔV) charge, configured from a chemistry preset:

- **Chemistry presets** — Li-ion/LiPo, LiFePO4, lead-acid, and NiMH/NiCd, each with per-cell default voltages/currents scaled by the cell count you set. **Apply Preset** fills in every derived value (CV/CC targets, precharge threshold/current, cutoff current, float voltage, −ΔV threshold/hold-off), and every value stays editable afterward.
- **Phases** — Precharge (for chemistries with a discharged-cell threshold) → CC → CV → Float (lead-acid only, when enabled) → Done. The CC→CV transition follows the P906's own reported `cv` mode, not a voltage/current estimate.
- **Stop conditions** — current tapering down to the cutoff current while in CV (Li-ion/LiFePO4/lead-acid), a −ΔV dip after a hold-off period (NiMH/NiCd), or optional maximum time/Ah/Wh limits, each of which applies in every phase including Float. The charge also stops if the output is switched off (manually or by the device) or the device reports a fault.
- **Lead-acid float** — a checkbox chooses whether the charge switches to a float voltage after the absorption stage and continues indefinitely (until Stop or a limit fires) or stops outright once current reaches the cutoff.
- While running, the tab shows phase, elapsed time, mAh, and Wh, and the finished run can be exported to CSV. Once a charge has been started, **Ah**, **Wh**, and **Chg Curve** (measured voltage vs. charged Ah) toggles appear in the graph row and are plotted while graph capture is running; they stay until the graph buffer is cleared with no charge running.

**Where the P906 measures voltage.** Like every other reading in this GUI, the charge controller uses the voltage the P906 reports at its own output terminals — there is no remote-sense compensation. The battery's actual terminal voltage will read slightly lower than the P906's number by the drop across your charge leads at whatever current is flowing.

#### L1060 Electronic Load Auxiliary Tools

The L1060 panel has a Presets tab plus three automated-run tabs, each usable in any of the load's CC/CV/CR/CP modes:

- **Discharge** — runs a battery discharge test at a fixed mode/target and integrates elapsed time, Ah, and Wh. A minimum voltage cutoff is **required** before Start (there is no universally safe default across battery chemistries/series counts); maximum Ah, Wh, and duration are optional additional stop conditions. Results can be exported to CSV and plotted (measured voltage vs. discharged Ah).
- **Sweep** — steps the target from a start to a stop value, dwelling at each step while the chosen response channel (voltage/current/power/resistance) is recorded live while graph capture is running. The commanded-target-vs-response curve appears as a toggleable block in the main window's graph row (like the Discharge voltage-vs-Ah curve), hidden until a sweep has been run.
- **Sequence** — runs an editable list of Delay/Wait/Set-mode-target actions, single-run or looped, with save/load to a text file (same editor pattern as the P906's sequence tool).

**The load's own targets win.** The panel's CC/CV/CR/CP targets follow what the L1060 reports, including changes made on its front panel; the panel only writes a target you change in it. A target read back from the device can take a few seconds to show after connecting. Editing another mode's target while the load is on holds that edit until the load is next switched off, matching the device's "turn off before SET" rule.

**Where the L1060 measures voltage.** The L1060 has a single voltage measurement, and its remote-sense leads relocate it: with the sense leads connected the reading is taken at the sense terminals, without them it is taken at the load's own terminals. The difference is the drop across your load leads — tens of millivolts at typical currents. This affects everything derived from voltage, including the Discharge cutoff and its Wh total, and the Sweep tool's voltage/power/resistance response channels. For a battery discharge test, clipping the sense leads to the cell is usually what you want, since it excludes the lead drop from the cutoff. Nothing in the protocol reports whether the sense leads are connected, so the software cannot detect or warn about this — set your cutoff for the wiring you are actually using. Measured current is unaffected.

**CR mode holds a fixed current.** When a CR target is set, the L1060 computes the current from the voltage present at that moment (I = V/R) and holds it; the current does not follow later changes in the source voltage. A CR run is therefore only a true resistance while the source voltage stays put. A Discharge in CR mode runs at a constant current of V₀/R, where V₀ is the battery voltage at the start, and pairing CR with anything that moves the P906's voltage (constant power control, the battery simulator, a voltage sequence) will not behave like a resistor. Use CC when the source voltage is expected to change.

Each tool has a **"Leave load on when finished/stopped"** checkbox (unchecked by default) that only applies to a normal completion or that tool's own Stop button. It never overrides a protection fault, a manual Load Off, or a panel disconnect — those always force the load off. If the L1060's protection latches (OVP/OCP/OPP/UVP/OTP) while a tool is running, the run stops immediately and the load is switched off; the panel stays connected, but a physical press of the **Run** button on the device itself is required to clear the latch before output can be re-enabled.

#### GUI Environment Variables

- `MDP_ENABLE_LOG`: Enable debug log output (or use `--debug` parameter)
- `MDP_FORCE_ENGLISH=1`: Force use English UI (or use `--english` parameter)
- `MDP_SIM_MODE`: Enable simulation mode, allowing testing UI functions without connecting to real device (or use `--sim` parameter)

## References (Thanks)

Original project: [ElluIFX/MDP-P906-Controller](https://github.com/ElluIFX/MDP-P906-Controller) by Ellu

Protocol implementation from [leommxj/mdp_commander](https://github.com/leommxj/mdp_commander)

NRF24L01P driver implementation from [mokhwasomssi/stm32_hal_nrf24l01p](https://github.com/mokhwasomssi/stm32_hal_nrf24l01p)

Font from [be5invis/Sarasa-Gothic](https://github.com/be5invis/Sarasa-Gothic)
