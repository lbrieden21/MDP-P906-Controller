# Miniware MDP-P906 Digital Power Supply Controller

Wireless control of the power supply without the MDP-M01 display module, supporting P905 as well (tested, see [#1](https://github.com/ElluIFX/MDP-P906-Controller/issues/1) and [#2](https://github.com/ElluIFX/MDP-P906-Controller/issues/2)).

![1732027356466](image/readme_EN/1732027356466.png)

![1721844452863](image/readme/1721844452863.png)

**Hardware note:** the AliExpress USB-NRF24L01 dongle module described in the
Prerequisite section below was the original basis for this project's adapter
firmware, but it's no longer part of the actual hardware in use — by the time
that stage of work started, the module wasn't readily available anymore. All
ongoing work, including the [nrf_adapter_source_multiceiver/](nrf_adapter_source_multiceiver/)
bare-metal firmware, is developed and bench-tested against a bare
STM32F030F4P6 dev board (same MCU) paired with an external USB-to-serial
adapter for the host link. The dongle's shipped firmware is still the source
the pin mapping and protocol were recovered from, so the Prerequisite section
is kept for that lineage and for anyone who already has the original module.

## Acknowledgements

The protocol part used in this project is derived from  [leommxj/mdp_commander](https://github.com/leommxj/mdp_commander). Without this project, I would have had no way to test the communication protocol between M01 and P906.

A lot of time was spent optimizing the communication quality based on this project, ultimately achieving stable and long-term data acquisition at up to 160fps.

![1721847380188](image/readme/1721847380188.png)

## Features

### Python3 API

- Set output/voltage/current
- Read device status
- Real-time reading of ADC measurement values from the output

### PyQt5 GUI

- Basic parameter setting, preset group management, setting modification
- Data acquisition, plotting, analysis, and saving up to 100Hz (adjustable)
- PID constant power control
- Parameter scanning (voltage/current)
  - Plotting scanning response curves (for discovering load characteristics)
- Function generator (sine/square/triangle/sawtooth/random)
- Operation sequence (single or loop execution of action sequences)
- Battery simulator (supports custom battery voltage curves/capacity/internal resistance/series-connection settings)
- MDP-L1060 electronic load support: mode-aware parameter sweep, sequence automation, and battery discharge testing (see [L1060 Auxiliary Tools](#l1060-electronic-load-auxiliary-tools) below)
- Multi-device support: connect and monitor several power supplies at once, each with its own panel and independent link/unlink control
- Data floating window
- Customizable waveform buffer length
- Material Design style with two color themes
- i18n support (zh-CN/en-US)
- Portable executable files ready to use (Win/Linux / Source Code Cross-Platform)

## Usage Instructions

### Prerequisite of the Prerequisite

Although the following text says that this project requires buying a module, if you already have an STM32 + NRF24L01 combo, you can port this project to your device by adapting the pin definitions. The current adapter firmware ([nrf_adapter_source_multiceiver/](nrf_adapter_source_multiceiver/)) is bare-metal (direct CMSIS register access, no CubeMX/HAL), so porting means editing the pin/clock setup directly in that source rather than regenerating from a `.ioc` file.

I won't include the specific circuit I reverse-engineered here; you can directly refer to the pin definitions in [nrf_adapter_source_multiceiver/targets/stm32f030/gpio.h](nrf_adapter_source_multiceiver/targets/stm32f030/gpio.h).

### Prerequisite

This project requires a USB to NRF24L01 module, sold for $5.67 on [AliExpress](https://www.aliexpress.com/item/1005006003453078.html?spm=a2g0o.productlist.main.7.3828oWBqoWBqd6&algo_pvid=27999fdf-f812-4149-b2a8-251e95c1cc29), as shown below:

![1721841880272](image/readme_EN/1721841880272.png)

This module has an independent PA amplifier, allowing for a communication range of up to two meters compared to the Arduino Nano RF used by leommxj. However, the module uses its protocol to implement a wireless serial port, which is not compatible with the original NRF24L01 data stream required to control the device.

![1722007003614](image/readme/1722007003614.png)

Fortunately, the module uses a genuine STM32F030F4P6 as the main controller, allowing us to write our programs to repurpose its hardware.

### Modification Method

**Important:** the Python driver in this repo ([mdp_controller/bus.py](mdp_controller/bus.py)) now always uses the nRF24L01+'s hardware RX pipes to address devices (`CMD_NRF_OPEN_PIPE`, 0x23) — even for a single device. That command only exists in the bare-metal multiceiver firmware under [nrf_adapter_source_multiceiver/](nrf_adapter_source_multiceiver/); the original shipped firmware (and the old pre-built release image) doesn't support it and will no longer work with this driver. There is currently no pre-built image for the multiceiver firmware, so it has to be compiled and flashed yourself over SWD.

Pry open the module's case and flip it over to see the test points as shown in the image below:

![1721840680045](image/readme/1721840680045.png)

Build the firmware (needs `arm-none-eabi-gcc`) — for this module, that's the
STM32F030 target specifically:

```sh
cd nrf_adapter_source_multiceiver/targets/stm32f030
sudo apt-get install gcc-arm-none-eabi
make          # -> build/MDP_Adapter_Multiceiver.{elf,hex,bin}
```

The same firmware also supports an STM32 Blue Pill, four Teensy boards
(3.5, 3.6, 4.0, 4.1), and four ESP32 boards (ESP32-C6, ESP32-H2, ESP32-S3,
and classic ESP32/ESP-WROOM-32) if you'd rather build your own adapter than
modify this module — see
[nrf_adapter_source_multiceiver/README.md](nrf_adapter_source_multiceiver/README.md)
for the full board list and per-target build/flash instructions.

Flash it over SWD with an ST-LINK V2 — wire `SWCLK`/`SWDIO`/`GND`/`3V3` from the ST-LINK to the module's test points as shown below. The `BOOT0`/`3V3` short and serial bootloader from the old method are **not** used here.

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

See [nrf_adapter_source_multiceiver/README.md](nrf_adapter_source_multiceiver/README.md) for full build/flash details and firmware design notes.

### Control by API

Refer to the code and the comments.

[test_main.py](./test_main.py) for example.

[mdp_p906.py](./mdp_controller/mdp_p906.py) for complete API.

### Control by GUI

I have released a PyInstaller packaged version, you can just download and run it. Everything is out of the box.

#### Multiple Devices

The GUI can drive more than one device at a time. Open **Connection Settings**, use the **+**/**-** buttons next to the device selector to add or remove a device, and configure each one's IDCODE/color/channel there. Every device gets its own panel (stacked in the left column) with its own **LINK/UNLINK** button, so devices can be connected and disconnected independently of each other — the radio adapter itself stays shared and opens/closes automatically as needed.

This is implemented using the adapter's nRF24L01+ hardware RX pipes to tell devices apart, so it requires the [multiceiver adapter firmware](#modification-method) and is capped at **5 devices per adapter** (pipes 1-5; pipe 0 is reserved for the adapter's own transmit ACKs).

#### Connecting over WiFi (ESP32 adapters)

An ESP32-C6, ESP32-S3 or classic ESP32 (WROOM-32) adapter built with `HOST_LINK_WIFI` (see
[nrf_adapter_source_multiceiver/README.md](nrf_adapter_source_multiceiver/README.md)) can be
driven over the LAN instead of USB, once it's been provisioned with WiFi credentials over its
wired link. In **Connection Settings**, set **Connection Type** to **WiFi (TCP)** and enter the
adapter's **Host Address** (its DHCP-assigned IP) and port (9000 by default) instead of picking
a serial port/baud rate. The USB/serial fields are disabled while WiFi is selected, and vice
versa. Anything else about the GUI — multi-device support, panels, LINK/UNLINK — works exactly
the same regardless of which transport the adapter is reached over.

#### L1060 Electronic Load Auxiliary Tools

The L1060 panel has a Preset tab plus three automated-run tabs, each usable in any of the load's CC/CV/CR/CP modes:

- **Sweep** — steps the target from a start to a stop value, dwelling at each step while the chosen response channel (voltage/current/power/resistance) is recorded live. The commanded-target-vs-response curve appears as a toggleable block in the main window's graph row (like the Discharge voltage-vs-Ah curve), hidden until a sweep has been run.
- **Sequence** — runs an editable list of Delay/Wait/Set-mode-target actions, single-run or looped, with save/load to a text file (same editor pattern as the P906's sequence tool).
- **Discharge** — runs a battery discharge test at a fixed mode/target and integrates elapsed time, Ah, and Wh. A minimum terminal-voltage cutoff is **required** before Start (there is no universally safe default across battery chemistries/series counts); maximum Ah, Wh, and duration are optional additional stop conditions. Results can be exported to CSV and plotted (terminal voltage vs. discharged Ah).

Each tool has a **"Leave load on when finished/stopped"** checkbox (unchecked by default) that only applies to a normal completion or that tool's own Stop button. It never overrides a protection fault, a manual Load Off, or a panel disconnect — those always force the load off. If the L1060's protection latches (OVP/OCP/OPP/UVP/OTP) while a tool is running, the run stops immediately and the load is switched off; the panel stays connected, but a physical press of the **Run** button on the device itself is required to clear the latch before output can be re-enabled.

#### GUI Environment Variables

- `MDP_ENABLE_LOG`: Enable debug log output (or use `--debug` parameter)
- `MDP_FORCE_ENGLISH`: Force use English UI (or use `--english` parameter)
- `MDP_SIM_MODE`: Enable simulation mode, allowing testing UI functions without connecting to real device (or use `--sim` parameter)

## References (Thanks)

Protocol implementation from [leommxj/mdp_commander](https://github.com/leommxj/mdp_commander)

NRF24L01P driver implementation from [mokhwasomssi/stm32_hal_nrf24l01p](https://github.com/mokhwasomssi/stm32_hal_nrf24l01p)

Font from [be5invis/Sarasa-Gothic](https://github.com/be5invis/Sarasa-Gothic)
