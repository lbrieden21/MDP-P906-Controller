# ESP32 target (C5 / C6 / H2 / S3).
#
# This is the one target in the tree that is NOT self-contained: it needs
# ESP-IDF installed, and this Makefile is a thin wrapper over `idf.py` rather
# than a build of its own. Every other target compiles against the system
# arm-none-eabi-gcc and a verbatim copy of its framework under Drivers/;
# ESP-IDF is a multi-gigabyte SDK with its own package manager and Kconfig
# build, so it is pinned and recorded instead of vendored. See the README.
#
# Required once:
#   git clone -b v5.5.5 --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf
#   cd ~/esp/esp-idf && ./install.sh esp32,esp32c5,esp32c6,esp32h2,esp32s3
#   python $IDF_PATH/tools/idf_tools.py install cmake ninja
# The last line is not optional on Linux: install.sh marks cmake and ninja
# "on_request" there, and without them idf.py fails with the misleading
# message `"cmake" must be available on the PATH`.
#
# Then either source ~/esp/esp-idf/export.sh, or just set IDF_PATH -- idf.py
# does not need export.sh sourced if IDF_PATH is set and its tools are on PATH.

TARGET = MDP_Adapter_Multiceiver

# Board select: ESP32C6 (default), ESP32H2, ESP32S3 or ESP32 (classic
# ESP-WROOM-32). Chooses the IDF target, and through it the toolchain, the
# sdkconfig, the pin map in pins.h and the default host link below.
# Unlike every other target in this tree, switching BOARD does NOT require
# `make clean`: the build directory and the sdkconfig are both per-configuration
# by construction (see IDF_ARGS below).
# Override on the command line: make BOARD=ESP32H2
BOARD ?= ESP32C6

ifeq ($(BOARD),ESP32C5)
IDF_TARGET = esp32c5
else ifeq ($(BOARD),ESP32C6)
IDF_TARGET = esp32c6
else ifeq ($(BOARD),ESP32H2)
IDF_TARGET = esp32h2
else ifeq ($(BOARD),ESP32S3)
IDF_TARGET = esp32s3
else ifeq ($(BOARD),ESP32)
IDF_TARGET = esp32
else
$(error Unknown BOARD '$(BOARD)' -- use ESP32C5, ESP32C6, ESP32H2, ESP32S3 or ESP32)
endif

# Host link select. The C6/H2/S3 default to the native USB-Serial/JTAG
# controller; classic ESP32 has neither that nor USB-OTG -- no native USB of any
# kind -- so UART0 through the board's CP2102/CH340 bridge is both its only
# option and its default.
#
# HOST_LINK_UART0 is also buildable on the C6/H2/S3, which is the cheapest way
# to exercise it: those boards are already hardware-validated and keep their
# native port free as an out-of-band channel while the protocol runs on the
# bridge.
#
# HOST_LINK_WIFI is a third, orthogonal thing: it does NOT choose the wired
# implementation above (that still resolves per board exactly as it does
# today -- USB-Serial/JTAG on C6/S3, UART0 on classic ESP32, automatically,
# because HOST_LINK_USB_JTAG's Kconfig `depends on SOC_USB_SERIAL_JTAG_SUPPORTED`
# is false there). It only turns on CONFIG_HOST_LINK_WIFI, which brings up a
# WiFi station and a TCP host link that runs *alongside* whichever wired link
# was already selected -- see host_link_mux.c and the plan doc's "Firmware:
# a host-link mux" section. Not offered on the H2, which has no WiFi at all.
ifeq ($(BOARD),ESP32)
HOST_LINK ?= HOST_LINK_UART0
else
HOST_LINK ?= HOST_LINK_USB_JTAG
endif

ifeq ($(HOST_LINK),HOST_LINK_USB_JTAG)
ifeq ($(BOARD),ESP32)
$(error BOARD=ESP32 cannot use HOST_LINK_USB_JTAG -- classic ESP32 (ESP32-D0WD) \
has no USB-Serial/JTAG controller and no USB-OTG. Use HOST_LINK_UART0)
endif
HOST_LINK_ID =
HOST_LINK_FRAGMENT =
else ifeq ($(HOST_LINK),HOST_LINK_UART0)
HOST_LINK_ID = -uart0
HOST_LINK_FRAGMENT = ;sdkconfig.uart0
else ifeq ($(HOST_LINK),HOST_LINK_WIFI)
ifeq ($(BOARD),ESP32H2)
$(error BOARD=ESP32H2 cannot use HOST_LINK_WIFI -- the H2 has no WiFi at all, \
802.15.4 + BLE only (see the board table in the plan doc). Use \
HOST_LINK_USB_JTAG or HOST_LINK_UART0)
endif
HOST_LINK_ID = -wifi
HOST_LINK_FRAGMENT = ;sdkconfig.wifi
else
$(error Unknown HOST_LINK '$(HOST_LINK)' -- use HOST_LINK_USB_JTAG, HOST_LINK_UART0 \
or HOST_LINK_WIFI)
endif

# Which connector `make flash` uses depends on the board:
#
#  - C6/H2/S3: the *native* USB-Serial/JTAG connector, the same one that carries
#    the protocol. /dev/ttyACM* is not a stable handle -- the native port
#    renumbers across a hardware reset and there are several boards on this
#    bench -- so prefer the by-id path, which is MAC-derived and per-board:
#      make flash PORT=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00
#  - Classic ESP32 (WROOM-32): the bridge connector, which is also the protocol
#    link and the only connector there is. That is a /dev/ttyUSB*, and plain
#    CP2102 modules often ship with no unique serial string, so by-id may not
#    distinguish two of them -- /dev/serial/by-path/ (physical USB port) does:
#      make BOARD=ESP32 flash PORT=/dev/serial/by-path/<...>-port0
PORT ?= /dev/ttyACM0

# Console select: 0 (default) or 1. The ESP-IDF console -- boot log, ESP_LOG*,
# panic dumps -- comes out UART0 to the board's USB-to-UART bridge connector,
# which is a second cable no host tooling reads. It is a bring-up tool, not part
# of the running configuration, so it is off unless asked for:
#   make CONSOLE=1                        # bring-up steps 1 and 7
# Reflash the default build before putting a board on the bench.
CONSOLE ?= 0

# Every build configuration gets its own sdkconfig and build directory, and
# CONSOLE and HOST_LINK both have to be part of that identity for the same
# reason BOARD is: sdkconfig.defaults is consulted only for symbols ABSENT from
# the generated sdkconfig, so adding or removing a fragment against an existing
# one silently does nothing. Separate configs make the flags actually take
# effect. HOST_LINK is itself a Kconfig choice set by sdkconfig.uart0, so it is
# subject to exactly that trap and not exempt from it.
#
# The default build's CONFIG_ID is plain $(IDF_TARGET), so the C6 stays
# `esp32c6` and existing build directories and sdkconfigs are not invalidated by
# either flag existing. CONSOLE=1 implies HOST_LINK_USB_JTAG (the combination
# with UART0 is rejected above), so `-console` alone is unambiguous.
#
# NOTE: changing sdkconfig.defaults ITSELF still requires `make distclean` --
# per-configuration ids isolate configurations from each other, not from a stale
# defaults edit.
ifeq ($(CONSOLE),1)
ifeq ($(HOST_LINK),HOST_LINK_UART0)
$(error CONSOLE=1 conflicts with HOST_LINK_UART0 -- UART0 cannot be both the \
protocol link and the ESP-IDF console. On classic ESP32 there is no second \
port to move either onto; on the C6/H2/S3 build the console with the default \
HOST_LINK_USB_JTAG instead)
endif
ifeq ($(HOST_LINK)-$(BOARD),HOST_LINK_WIFI-ESP32)
$(error CONSOLE=1 conflicts with BOARD=ESP32 HOST_LINK_WIFI -- on classic \
ESP32 the wired link of a WiFi build is UART0 (no USB-Serial/JTAG exists on \
that chip), and UART0 cannot be both the protocol link and the ESP-IDF \
console. CONSOLE=1 with HOST_LINK_WIFI is fine on the C6/S3, where the wired \
link stays USB-Serial/JTAG)
endif
CONFIG_ID = $(IDF_TARGET)-console$(HOST_LINK_ID)
SDKCONFIG_DEFAULTS = sdkconfig.defaults;sdkconfig.console$(HOST_LINK_FRAGMENT)
else ifeq ($(CONSOLE),0)
CONFIG_ID = $(IDF_TARGET)$(HOST_LINK_ID)
SDKCONFIG_DEFAULTS = sdkconfig.defaults$(HOST_LINK_FRAGMENT)
else
$(error Unknown CONSOLE '$(CONSOLE)' -- use 0 or 1)
endif

# Separate build dir and sdkconfig per configuration. `idf.py set-target`
# rewrites both, so sharing either between BOARD values would silently rebuild
# the wrong one.
#
# SDKCONFIG_DEFAULTS is a CMake list, semicolon-separated, and every entry in it
# must exist -- a missing one is a hard FATAL_ERROR, which is why sdkconfig.console
# is a committed file and the Makefile switches the list rather than the file.
# IDF additionally probes "<entry>.$(IDF_TARGET)" for each entry and skips it if
# absent, which is how sdkconfig.defaults.esp32c6 keeps being picked up.
SDKCONFIG = sdkconfig.$(CONFIG_ID)
IDF_ARGS = -B build/$(CONFIG_ID) -D SDKCONFIG=$(SDKCONFIG) \
           -D SDKCONFIG_DEFAULTS="$(SDKCONFIG_DEFAULTS)"

# The one place this target is not self-contained. Say so out loud rather than
# letting `idf.py: command not found` be the diagnostic.
ifndef IDF_PATH
$(error IDF_PATH is not set -- source ~/esp/esp-idf/export.sh, or export IDF_PATH=~/esp/esp-idf)
endif

# set-target is a one-off, NOT part of every build: idf.py runs a fullclean as
# a dependency of set-target, so folding it into `all` would make every `make`
# a from-scratch ~1000-object rebuild. Ordering it behind the sdkconfig file
# means it runs once per chip -- on the first build, or after `make distclean`
# -- and every build after that is incremental.
#
# Every target below that invokes idf.py depends on $(SDKCONFIG) for the same
# reason `all` does: without it, a target run before any sdkconfig file exists
# for this CONFIG_ID never calls set-target at all, and idf.py silently
# configures for its own default target (classic esp32) instead of erroring --
# hardware-verified against the C5 bring-up, where `make BOARD=ESP32C5 size`
# on a from-scratch tree built and reported success for an esp32 (Xtensa)
# image under the esp32c5 filenames, and esptool caught the mismatch only at
# flash time.
all: $(SDKCONFIG)
	idf.py $(IDF_ARGS) build

$(SDKCONFIG):
	idf.py $(IDF_ARGS) set-target $(IDF_TARGET)

flash: $(SDKCONFIG)
	idf.py $(IDF_ARGS) -p $(PORT) flash

# Console, boot log and panic dumps come out the *bridge* connector, not PORT.
# Only useful against a CONSOLE=1 image; the default build puts nothing here,
# and a HOST_LINK_UART0 build cannot have a console at all.
# Pass the bridge tty explicitly: make CONSOLE=1 monitor MONITOR_PORT=/dev/ttyUSB0
# Note that opening the bridge port resets the board -- the CP2102N's DTR/RTS
# drive EN/BOOT. That is fine for watching a boot, but it means you cannot
# attach a console mid-run without rebooting the adapter.
MONITOR_PORT ?= /dev/ttyUSB0
monitor: $(SDKCONFIG)
	idf.py $(IDF_ARGS) -p $(MONITOR_PORT) monitor

size: $(SDKCONFIG)
	idf.py $(IDF_ARGS) size

menuconfig: $(SDKCONFIG)
	idf.py $(IDF_ARGS) menuconfig

# Removes this BOARD+CONSOLE configuration's build directory only; every other
# configuration's build and sdkconfig is untouched.
clean:
	rm -rf build/$(CONFIG_ID) sdkconfig.$(CONFIG_ID).old

# Removes every configuration. Needed whenever sdkconfig.defaults, sdkconfig.console
# or sdkconfig.wifi changes, since IDF will not re-read them for symbols an
# existing generated sdkconfig already carries.
distclean:
	rm -rf build sdkconfig.esp32 sdkconfig.esp32c5 sdkconfig.esp32c6 sdkconfig.esp32h2 \
	       sdkconfig.esp32s3 sdkconfig.esp32-uart0 sdkconfig.esp32c5-uart0 \
	       sdkconfig.esp32c6-uart0 sdkconfig.esp32h2-uart0 sdkconfig.esp32s3-uart0 \
	       sdkconfig.esp32c5-console sdkconfig.esp32c6-console sdkconfig.esp32h2-console \
	       sdkconfig.esp32s3-console \
	       sdkconfig.esp32-wifi sdkconfig.esp32c5-wifi sdkconfig.esp32c6-wifi sdkconfig.esp32s3-wifi \
	       sdkconfig.esp32c5-console-wifi sdkconfig.esp32c6-console-wifi sdkconfig.esp32s3-console-wifi \
	       sdkconfig.esp32*.old

.PHONY: all flash monitor size menuconfig clean distclean
