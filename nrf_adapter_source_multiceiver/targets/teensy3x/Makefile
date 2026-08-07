TARGET = MDP_Adapter_Multiceiver
BUILD_DIR = build

CC = arm-none-eabi-gcc
CXX = arm-none-eabi-g++
OBJCOPY = arm-none-eabi-objcopy
SIZE = arm-none-eabi-size

ROOT = ../..
CORE_DIR = $(ROOT)/core
FW_DIR = $(ROOT)/Drivers/teensy3
SPI_DIR = $(ROOT)/Drivers/teensy_libs/SPI

# Host link. USB CDC is the primary path; -DHOST_LINK_SERIAL1 swaps in Serial1
# at 921600 for exact behavioural parity with the shipped STM32 adapter.
# Override on the command line: make HOST_LINK=HOST_LINK_SERIAL1
HOST_LINK ?= HOST_LINK_USB_CDC

# Board select: TEENSY35 (default) or TEENSY36. Chooses the chip define,
# F_CPU, the linker script and the teensy_loader_cli --mcu argument -- nothing
# else. pins.h and every platform_teensy3.* source file are identical on both.
# Switching BOARD requires `make clean` first, same as HOST_LINK: the define
# only reaches the framework objects under build/fw, and make has no way to
# know they are stale otherwise.
# Override on the command line: make BOARD=TEENSY36
BOARD ?= TEENSY35

ifeq ($(BOARD),TEENSY35)
BOARD_DEF = -D__MK64FX512__ -DARDUINO_TEENSY35
FCPU = 120000000
LDSCRIPT = mk64fx512.ld
LOADER_MCU = TEENSY35
else ifeq ($(BOARD),TEENSY36)
BOARD_DEF = -D__MK66FX1M0__ -DARDUINO_TEENSY36
FCPU = 180000000
LDSCRIPT = mk66fx1m0.ld
LOADER_MCU = TEENSY36
else
$(error Unknown BOARD '$(BOARD)' -- use TEENSY35 or TEENSY36)
endif

# The vendored trees are git-ignored, not vendored into the repo -- fetch them
# first. Checked against a file rather than the directory, since an
# interrupted fetch can leave an empty directory behind.
ifeq (,$(wildcard $(FW_DIR)/core_pins.h))
$(error Drivers/teensy3 is missing -- run: python3 ../../../tools/fetch_vendor.py --target teensy3x)
endif
ifeq (,$(wildcard $(SPI_DIR)/SPI.h))
$(error Drivers/teensy_libs/SPI is missing -- run: python3 ../../../tools/fetch_vendor.py --target teensy3x)
endif

MCU = -mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16

# ARDUINO/TEENSYDUINO version numbers are what the core's own Makefile declares;
# some core headers gate on them. LAYOUT_US_ENGLISH is required by keylayouts.c
# even though this firmware has no HID. USING_MAKEFILE must stay undefined --
# Drivers/teensy3/main.cpp compiles in its own endless pin-13 blink and never
# calls setup()/loop() if it is defined (see platform_teensy3.cpp / main.cpp).
FW_DEFS = $(BOARD_DEF) -DF_CPU=$(FCPU) \
          -DUSB_SERIAL -DLAYOUT_US_ENGLISH -DARDUINO=10813 -DTEENSYDUINO=159

FW_INCS = -I$(ROOT) -I$(CORE_DIR) -I$(FW_DIR) -I$(SPI_DIR) -I.
# -isystem for the vendored trees so their warnings do not surface under the
# -Wextra our own code is built with (SPI.h drags in DMAChannel.h, which trips
# -Wdeprecated-copy on GCC 14).
APP_INCS = -I$(ROOT) -I$(CORE_DIR) -isystem $(FW_DIR) -isystem $(SPI_DIR) -I.

COMMON = $(MCU) $(FW_DEFS) -O2 -g -ffunction-sections -fdata-sections
CXXONLY = -std=gnu++17 -felide-constructors -fno-exceptions -fpermissive -fno-rtti

# The vendored framework is compiled on PJRC's own warning settings -- it is
# copied verbatim and we do not want to churn it. Our code gets -Wextra, same
# as the STM32 and Teensy 4.x targets.
FW_CFLAGS = $(COMMON) $(FW_INCS) -Wall
FW_CXXFLAGS = $(COMMON) $(FW_INCS) -Wall $(CXXONLY)
APP_CFLAGS = $(COMMON) $(APP_INCS) -Wall -Wextra -std=c11
APP_CXXFLAGS = $(COMMON) $(APP_INCS) -Wall -Wextra $(CXXONLY) -D$(HOST_LINK)

# __rtc_localtime: referenced by mk20dx128.c (KINETISK-only -- the Teensy 4.x
# target has no RTC block and never needs this) to seed the RTC with a
# compile-time Unix timestamp on a fresh power-up. Nothing in the vendored
# tree defines it, so the link fails without this. The value only affects the
# fallback timestamp the RTC starts from before anything else sets it, so the
# build time is fine -- it just means this target isn't byte-identical build
# to build. That's fine: only the STM32F030 and Teensy 4.x targets carry a
# byte-identical gate.
LDFLAGS = $(MCU) -Os -Wl,--gc-sections,--relax -T$(FW_DIR)/$(LDSCRIPT) \
          -Wl,--defsym=__rtc_localtime=$(shell date +%s) \
          -Wl,-Map=$(BUILD_DIR)/$(TARGET).map
# No -larm_cortexM4lf_math: that prebuilt CMSIS-DSP library ships only with
# Teensyduino, the same reason the Teensy 4.x Makefile drops its M7 equivalent.
# Nothing in core/ or this target references it; confirmed via
# arm-none-eabi-nm -u against the linked ELF (zero undefined symbols).
LIBS = -lm -lstdc++

# The framework objects are linked directly rather than archived: the vector
# table (mk20dx128.c) is placed by the linker script and referenced by no
# symbol, so an archive would drop it -- same reasoning as the Teensy 4.x
# Makefile's bootdata.c note.
FW_C_SRCS = $(wildcard $(FW_DIR)/*.c)
FW_CPP_SRCS = $(wildcard $(FW_DIR)/*.cpp) $(wildcard $(SPI_DIR)/*.cpp)
FW_S_SRCS = $(wildcard $(FW_DIR)/*.S)
CORE_SRCS = $(wildcard $(CORE_DIR)/*.c)
APP_SRCS = $(wildcard *.cpp)

OBJS = $(patsubst $(FW_DIR)/%.c,$(BUILD_DIR)/fw/%.o,$(FW_C_SRCS)) \
       $(patsubst $(FW_DIR)/%.cpp,$(BUILD_DIR)/fw/%.o,$(filter $(FW_DIR)/%,$(FW_CPP_SRCS))) \
       $(patsubst $(SPI_DIR)/%.cpp,$(BUILD_DIR)/fw/%.o,$(filter $(SPI_DIR)/%,$(FW_CPP_SRCS))) \
       $(patsubst $(FW_DIR)/%.S,$(BUILD_DIR)/fw/%.o,$(FW_S_SRCS)) \
       $(patsubst $(CORE_DIR)/%.c,$(BUILD_DIR)/core/%.o,$(CORE_SRCS)) \
       $(patsubst %.cpp,$(BUILD_DIR)/app/%.o,$(APP_SRCS))

all: $(BUILD_DIR)/$(TARGET).elf $(BUILD_DIR)/$(TARGET).hex size

$(BUILD_DIR)/fw $(BUILD_DIR)/core $(BUILD_DIR)/app:
	mkdir -p $@

$(BUILD_DIR)/fw/%.o: $(FW_DIR)/%.c | $(BUILD_DIR)/fw
	$(CC) $(FW_CFLAGS) -c $< -o $@

$(BUILD_DIR)/fw/%.o: $(FW_DIR)/%.cpp | $(BUILD_DIR)/fw
	$(CXX) $(FW_CXXFLAGS) -c $< -o $@

$(BUILD_DIR)/fw/%.o: $(SPI_DIR)/%.cpp | $(BUILD_DIR)/fw
	$(CXX) $(FW_CXXFLAGS) -c $< -o $@

$(BUILD_DIR)/fw/%.o: $(FW_DIR)/%.S | $(BUILD_DIR)/fw
	$(CC) $(FW_CFLAGS) -c $< -o $@

$(BUILD_DIR)/core/%.o: $(CORE_DIR)/%.c | $(BUILD_DIR)/core
	$(CC) $(APP_CFLAGS) -c $< -o $@

$(BUILD_DIR)/app/%.o: %.cpp | $(BUILD_DIR)/app
	$(CXX) $(APP_CXXFLAGS) -c $< -o $@

$(BUILD_DIR)/$(TARGET).elf: $(OBJS)
	$(CXX) $(OBJS) $(LDFLAGS) $(LIBS) -o $@

$(BUILD_DIR)/$(TARGET).hex: $(BUILD_DIR)/$(TARGET).elf
	$(OBJCOPY) -O ihex -R .eeprom $< $@

size: $(BUILD_DIR)/$(TARGET).elf
	$(SIZE) $<

# -s soft-reboots into the bootloader over USB, so no button press is needed as
# long as the running firmware still enumerates as USB serial. Drop -s (and
# press the button) if the board is wedged or running a non-USB_SERIAL build.
flash: $(BUILD_DIR)/$(TARGET).hex
	teensy_loader_cli --mcu=$(LOADER_MCU) -s -w -v $<

clean:
	rm -rf $(BUILD_DIR)

.PHONY: all clean size flash
