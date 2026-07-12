TARGET = MDP_Adapter_Multiceiver
BUILD_DIR = build

CC = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
SIZE = arm-none-eabi-size

CORE_INC = Core/Inc
CMSIS_INC = Drivers/CMSIS/Include
DEVICE_INC = Drivers/CMSIS/Device/ST/STM32F0xx/Include

SRCS = $(wildcard Core/Src/*.c)
OBJS = $(patsubst Core/Src/%.c,$(BUILD_DIR)/%.o,$(SRCS))

MCU = -mcpu=cortex-m0 -mthumb -mfloat-abi=soft
DEFS = -DSTM32F030x6

CFLAGS = $(MCU) $(DEFS) -I$(CORE_INC) -I$(CMSIS_INC) -I$(DEVICE_INC) \
         -std=c11 -O2 -Wall -Wextra -ffunction-sections -fdata-sections -g

LDFLAGS = $(MCU) -Tlinker/STM32F030F4Px.ld -specs=nano.specs -specs=nosys.specs \
          -Wl,--gc-sections -Wl,-Map=$(BUILD_DIR)/$(TARGET).map -nostartfiles

all: $(BUILD_DIR)/$(TARGET).elf $(BUILD_DIR)/$(TARGET).hex $(BUILD_DIR)/$(TARGET).bin size

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/%.o: Core/Src/%.c | $(BUILD_DIR)
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
