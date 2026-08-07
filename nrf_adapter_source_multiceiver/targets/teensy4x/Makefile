TARGET = MDP_Adapter_Multiceiver
BUILD_DIR = build

CC = arm-none-eabi-gcc
CXX = arm-none-eabi-g++
OBJCOPY = arm-none-eabi-objcopy
SIZE = arm-none-eabi-size

ROOT = ../..
CORE_DIR = $(ROOT)/core
FW_DIR = $(ROOT)/Drivers/teensy4
SPI_DIR = $(ROOT)/Drivers/teensy_libs/SPI

# Host link. USB CDC is the primary path; -DHOST_LINK_SERIAL1 swaps in Serial1
# at 921600 for exact behavioural parity with the shipped STM32 adapter.
# Override on the command line: make HOST_LINK=HOST_LINK_SERIAL1
HOST_LINK ?= HOST_LINK_USB_CDC

# Board select: TEENSY41 (default) or TEENSY40. Chooses the chip define, the
# linker script and the teensy_loader_cli --mcu argument -- nothing else.
# pins.h and every platform_teensy4.* source file are identical on both; the
# 4.0's ARDUINO_TEENSY40 gating already lives in the vendored Drivers/teensy4
# core. Switching BOARD requires `make clean` first, same as HOST_LINK: the
# define only reaches the framework objects under build/fw, and make has no
# way to know they are stale otherwise.
# Override on the command line: make BOARD=TEENSY40
BOARD ?= TEENSY41

ifeq ($(BOARD),TEENSY41)
BOARD_DEF = -DARDUINO_TEENSY41
LDSCRIPT = imxrt1062_t41.ld
LOADER_MCU = TEENSY41
else ifeq ($(BOARD),TEENSY40)
BOARD_DEF = -DARDUINO_TEENSY40
LDSCRIPT = imxrt1062.ld
LOADER_MCU = TEENSY40
else
$(error Unknown BOARD '$(BOARD)' -- use TEENSY41 or TEENSY40)
endif

# Ethernet host link. ETH=1 adds -DHOST_LINK_ETH and vendors QNEthernet
# (Drivers/teensy_libs/QNEthernet) into the build -- see host_link_eth.cpp,
# net_eth.cpp and the plan doc's "Firmware: build system" section.
# Override on the command line: make BOARD=TEENSY41 ETH=1
ETH ?= 0

ifeq ($(ETH),1)
ifneq ($(BOARD),TEENSY41)
$(error BOARD=$(BOARD) ETH=1 -- the Teensy 4.0 has no Ethernet pads. Use BOARD=TEENSY41)
endif
endif

QNE_DIR = $(ROOT)/Drivers/teensy_libs/QNEthernet/src

# The vendored trees are git-ignored, not vendored into the repo -- fetch them
# first. Checked against a file rather than the directory, since an
# interrupted fetch can leave an empty directory behind.
ifeq (,$(wildcard $(FW_DIR)/core_pins.h))
$(error Drivers/teensy4 is missing -- run: python3 ../../../tools/fetch_vendor.py --target teensy4x)
endif
ifeq (,$(wildcard $(SPI_DIR)/SPI.h))
$(error Drivers/teensy_libs/SPI is missing -- run: python3 ../../../tools/fetch_vendor.py --target teensy4x)
endif
ifeq ($(ETH),1)
ifeq (,$(wildcard $(QNE_DIR)/QNEthernet.h))
$(error Drivers/teensy_libs/QNEthernet is missing -- run: python3 ../../../tools/fetch_vendor.py --target teensy4x-eth)
endif
endif

MCU = -mcpu=cortex-m7 -mthumb -mfloat-abi=hard -mfpu=fpv5-d16

# ARDUINO/TEENSYDUINO version numbers are what the core's own Makefile declares;
# some core headers gate on them. LAYOUT_US_ENGLISH is required by keylayouts.c
# even though this firmware has no HID.
FW_DEFS = -D__IMXRT1062__ $(BOARD_DEF) -DF_CPU=600000000 \
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
# as the STM32 target.
FW_CFLAGS = $(COMMON) $(FW_INCS) -Wall
FW_CXXFLAGS = $(COMMON) $(FW_INCS) -Wall $(CXXONLY)
APP_CFLAGS = $(COMMON) $(APP_INCS) -Wall -Wextra -std=c11
APP_CXXFLAGS = $(COMMON) $(APP_INCS) -Wall -Wextra $(CXXONLY) -D$(HOST_LINK)

# QNEthernet is compiled like the vendored framework -- PJRC's/lwIP's own
# warning settings via -isystem, not our -Wextra. QNETHERNET_DO_LOOP_IN_YIELD=0
# keeps it off yield(), which delay_ms() (called from nrf24l01p.c's
# transaction paths) would otherwise pull the Ethernet stack into.
QNE_CFLAGS = $(COMMON) $(FW_INCS) -isystem $(QNE_DIR) -Wall -DQNETHERNET_DO_LOOP_IN_YIELD=0
QNE_CXXFLAGS = $(COMMON) $(FW_INCS) -isystem $(QNE_DIR) -Wall $(CXXONLY) -DQNETHERNET_DO_LOOP_IN_YIELD=0

ifeq ($(ETH),1)
# -isystem so QNEthernet's own headers (included from net_eth.cpp and
# host_link_eth.cpp) don't surface warnings under the -Wextra our own code is
# built with -- the same reason FW_DIR/SPI_DIR are -isystem above.
APP_INCS += -isystem $(QNE_DIR)
APP_CXXFLAGS += -DHOST_LINK_ETH
endif

LDFLAGS = $(MCU) -Os -Wl,--gc-sections,--relax -T$(FW_DIR)/$(LDSCRIPT) \
          -Wl,-Map=$(BUILD_DIR)/$(TARGET).map
LIBS = -lm -lstdc++

# The framework objects are linked directly rather than archived: the boot
# header (bootdata.c .flashconfig/.ivt) and the vector table are placed by the
# linker script and referenced by no symbol, so an archive would drop them.
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

ifeq ($(ETH),1)
# QNEthernet is not flat like FW_DIR/SPI_DIR (lwip/, qnethernet/, arch/,
# netif/ subdirectories), so it needs find rather than wildcard. main.cpp is
# filtered out -- it defines its own main() and would collide with the
# core's.
QNE_C_SRCS = $(shell find $(QNE_DIR) -name '*.c')
QNE_CPP_SRCS = $(filter-out $(QNE_DIR)/main.cpp,$(shell find $(QNE_DIR) -name '*.cpp'))
OBJS += $(patsubst $(QNE_DIR)/%.c,$(BUILD_DIR)/qne/%.o,$(QNE_C_SRCS)) \
        $(patsubst $(QNE_DIR)/%.cpp,$(BUILD_DIR)/qne/%.o,$(QNE_CPP_SRCS))
endif

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

# QNEthernet mirrors its own source tree under build/qne/ rather than a flat
# directory, so nested files with matching basenames don't clobber one
# another -- mkdir -p in the recipe rather than an order-only prerequisite,
# since that prerequisite would only create the top-level directory.
$(BUILD_DIR)/qne/%.o: $(QNE_DIR)/%.c
	@mkdir -p $(dir $@)
	$(CC) $(QNE_CFLAGS) -c $< -o $@

$(BUILD_DIR)/qne/%.o: $(QNE_DIR)/%.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(QNE_CXXFLAGS) -c $< -o $@

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
