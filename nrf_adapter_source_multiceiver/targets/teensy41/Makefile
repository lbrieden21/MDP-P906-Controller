TARGET = MDP_Adapter_Multiceiver
BUILD_DIR = build

CC = arm-none-eabi-gcc
CXX = arm-none-eabi-g++
OBJCOPY = arm-none-eabi-objcopy
SIZE = arm-none-eabi-size

ROOT = ../..
CORE_DIR = $(ROOT)/core
FW_DIR = $(ROOT)/Drivers/teensy4
SPI_DIR = $(ROOT)/Drivers/teensy4_libs/SPI

# Host link. USB CDC is the primary path; -DHOST_LINK_SERIAL1 swaps in Serial1
# at 921600 for exact behavioural parity with the shipped STM32 adapter.
# Override on the command line: make HOST_LINK=HOST_LINK_SERIAL1
HOST_LINK ?= HOST_LINK_USB_CDC

MCU = -mcpu=cortex-m7 -mthumb -mfloat-abi=hard -mfpu=fpv5-d16

# ARDUINO/TEENSYDUINO version numbers are what the core's own Makefile declares;
# some core headers gate on them. LAYOUT_US_ENGLISH is required by keylayouts.c
# even though this firmware has no HID.
FW_DEFS = -D__IMXRT1062__ -DARDUINO_TEENSY41 -DF_CPU=600000000 \
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

LDFLAGS = $(MCU) -Os -Wl,--gc-sections,--relax -T$(FW_DIR)/imxrt1062_t41.ld \
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
	teensy_loader_cli --mcu=TEENSY41 -s -w -v $<

clean:
	rm -rf $(BUILD_DIR)

.PHONY: all clean size flash
