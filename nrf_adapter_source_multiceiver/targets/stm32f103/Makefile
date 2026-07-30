TARGET = MDP_Adapter_Multiceiver
BUILD_DIR = build

CC = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
SIZE = arm-none-eabi-size

ROOT = ../..
CORE_DIR = $(ROOT)/core
CMSIS_INC = $(ROOT)/Drivers/CMSIS/Include
DEVICE_INC = $(ROOT)/Drivers/CMSIS/Device/ST/STM32F1xx/Include

CORE_SRCS = $(wildcard $(CORE_DIR)/*.c)
TARGET_SRCS = $(wildcard *.c)
OBJS = $(patsubst $(CORE_DIR)/%.c,$(BUILD_DIR)/%.o,$(CORE_SRCS)) \
       $(patsubst %.c,$(BUILD_DIR)/%.o,$(TARGET_SRCS))

MCU = -mcpu=cortex-m3 -mthumb -mfloat-abi=soft
DEFS = -DSTM32F103xB

CFLAGS = $(MCU) $(DEFS) -I$(ROOT) -I$(CORE_DIR) -I. -I$(CMSIS_INC) -I$(DEVICE_INC) \
         -std=c11 -O2 -Wall -Wextra -ffunction-sections -fdata-sections -g

LDFLAGS = $(MCU) -Tlinker/STM32F103C8Tx.ld -specs=nano.specs -specs=nosys.specs \
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
