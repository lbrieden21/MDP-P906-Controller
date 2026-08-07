TARGET = MDP_Adapter_Multiceiver
BUILD_DIR = build

CC = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
SIZE = arm-none-eabi-size

ROOT = ../..
CORE_DIR = $(ROOT)/core
CMSIS_INC = $(ROOT)/Drivers/CMSIS/Include
DEVICE_INC = $(ROOT)/Drivers/CMSIS/Device/ST/STM32F0xx/Include

# The vendored trees are git-ignored, not vendored into the repo -- fetch them
# first. Checked against a file rather than the directory, since an
# interrupted fetch can leave an empty directory behind.
ifeq (,$(wildcard $(CMSIS_INC)/core_cm0.h))
$(error Drivers/CMSIS/Include is missing -- run: python3 ../../../tools/fetch_vendor.py --target stm32f030)
endif
ifeq (,$(wildcard $(DEVICE_INC)/stm32f0xx.h))
$(error Drivers/CMSIS/Device/ST/STM32F0xx is missing -- run: python3 ../../../tools/fetch_vendor.py --target stm32f030)
endif

CORE_SRCS = $(wildcard $(CORE_DIR)/*.c)
TARGET_SRCS = $(wildcard *.c)
OBJS = $(patsubst $(CORE_DIR)/%.c,$(BUILD_DIR)/%.o,$(CORE_SRCS)) \
       $(patsubst %.c,$(BUILD_DIR)/%.o,$(TARGET_SRCS))

MCU = -mcpu=cortex-m0 -mthumb -mfloat-abi=soft
DEFS = -DSTM32F030x6

CFLAGS = $(MCU) $(DEFS) -I$(ROOT) -I$(CORE_DIR) -I. -I$(CMSIS_INC) -I$(DEVICE_INC) \
         -std=c11 -O2 -Wall -Wextra -ffunction-sections -fdata-sections -g

LDFLAGS = $(MCU) -Tlinker/STM32F030F4Px.ld -specs=nano.specs -specs=nosys.specs \
          -Wl,--gc-sections -Wl,-Map=$(BUILD_DIR)/$(TARGET).map -nostartfiles

all: $(BUILD_DIR)/$(TARGET).elf $(BUILD_DIR)/$(TARGET).hex $(BUILD_DIR)/$(TARGET).bin size

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/%.o: $(CORE_DIR)/%.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/%.o: %.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/$(TARGET).elf: $(OBJS)
	$(CC) $(OBJS) $(LDFLAGS) -o $@

$(BUILD_DIR)/$(TARGET).hex: $(BUILD_DIR)/$(TARGET).elf
	$(OBJCOPY) -O ihex $< $@

$(BUILD_DIR)/$(TARGET).bin: $(BUILD_DIR)/$(TARGET).elf
	$(OBJCOPY) -O binary $< $@

size: $(BUILD_DIR)/$(TARGET).elf
	$(SIZE) $<

clean:
	rm -rf $(BUILD_DIR)

.PHONY: all clean size
