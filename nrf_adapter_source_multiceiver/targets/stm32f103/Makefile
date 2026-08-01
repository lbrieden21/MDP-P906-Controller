TARGET = MDP_Adapter_Multiceiver
BUILD_DIR = build

CC = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
SIZE = arm-none-eabi-size

ROOT = ../..
CORE_DIR = $(ROOT)/core
CMSIS_INC = $(ROOT)/Drivers/CMSIS/Include
DEVICE_INC = $(ROOT)/Drivers/CMSIS/Device/ST/STM32F1xx/Include
TU_DIR = $(ROOT)/Drivers/tinyusb/src

# Host link. USB CDC is the default: it uses the Blue Pill's onboard USB port
# via TinyUSB, so an unqualified `make` produces the build that needs no
# external USB-UART bridge wired up. Same default as the Teensy targets.
# HOST_LINK_USART1 selects the original USART1 path on PA9/PA10, which needs
# that bridge. Keep it working -- it is the only host link on this chip with a
# real line rate, so it stays the reference for anything baudrate-related
# (CMD_SET_BAUDRATE is a no-op on CDC by design, see usb_cdc.c) and it is the
# CDC build's only same-silicon A/B partner.
# Switching requires `make clean` first, same as the Teensy targets: HOST_LINK
# changes both the source list and a -D that reaches already-built objects,
# and make has no way to know they are stale.
# Override on the command line: make HOST_LINK=HOST_LINK_USART1
HOST_LINK ?= HOST_LINK_USB_CDC

# Explicit, not $(wildcard *.c): uart.c and usb_cdc.c both define
# host_link_write()/host_link_read_byte()/host_link_set_baudrate(), so a wildcard would pull
# in both and fail to link on duplicate symbols.
COMMON_TARGET_SRCS = flash_store.c gpio.c main.c spi.c startup.c \
                     system_clock.c watchdog.c

ifeq ($(HOST_LINK),HOST_LINK_USB_CDC)
TARGET_SRCS = $(COMMON_TARGET_SRCS) usb_cdc.c usb_descriptors.c
# -isystem for the vendored TinyUSB tree so its warnings stay out of the
# -Wall -Wextra our own code is held to, the same treatment the Teensy targets
# give their vendored cores. Empty in the USART1 build, which must not see
# TinyUSB headers at all.
TU_INC = -isystem $(TU_DIR)
TU_SRCS = $(TU_DIR)/tusb.c \
          $(TU_DIR)/common/tusb_fifo.c \
          $(TU_DIR)/device/usbd.c \
          $(TU_DIR)/class/cdc/cdc_device.c \
          $(TU_DIR)/portable/st/stm32_fsdev/dcd_stm32_fsdev.c \
          $(TU_DIR)/portable/st/stm32_fsdev/fsdev_common.c
else ifeq ($(HOST_LINK),HOST_LINK_USART1)
TARGET_SRCS = $(COMMON_TARGET_SRCS) uart.c
TU_INC =
TU_SRCS =
else
$(error Unknown HOST_LINK '$(HOST_LINK)' -- use HOST_LINK_USART1 or HOST_LINK_USB_CDC)
endif

CORE_SRCS = $(wildcard $(CORE_DIR)/*.c)
TU_OBJS = $(addprefix $(BUILD_DIR)/tu/,$(notdir $(TU_SRCS:.c=.o)))
OBJS = $(patsubst $(CORE_DIR)/%.c,$(BUILD_DIR)/%.o,$(CORE_SRCS)) \
       $(patsubst %.c,$(BUILD_DIR)/%.o,$(TARGET_SRCS)) \
       $(TU_OBJS)

MCU = -mcpu=cortex-m3 -mthumb -mfloat-abi=soft
DEFS = -DSTM32F103xB -D$(HOST_LINK)

CFLAGS = $(MCU) $(DEFS) -I$(ROOT) -I$(CORE_DIR) -I. -I$(CMSIS_INC) -I$(DEVICE_INC) \
         $(TU_INC) -std=c11 -O2 -Wall -Wextra -ffunction-sections -fdata-sections -g

# TinyUSB is GNU C, not ISO C: fsdev_common.c uses a bare `asm("NOP")`, which
# -std=c11 rejects because __STRICT_ANSI__ disables the `asm` keyword. It gets
# -std=gnu11 rather than the vendored tree getting patched -- Drivers/ is a
# verbatim copy. Our own sources stay on -std=c11.
TU_CFLAGS = $(MCU) $(DEFS) -I. -I$(CMSIS_INC) -I$(DEVICE_INC) $(TU_INC) \
            -std=gnu11 -O2 -Wall -Wextra -ffunction-sections -fdata-sections -g

LDFLAGS = $(MCU) -Tlinker/STM32F103C8Tx.ld -specs=nano.specs -specs=nosys.specs \
          -Wl,--gc-sections -Wl,-Map=$(BUILD_DIR)/$(TARGET).map -nostartfiles

all: $(BUILD_DIR)/$(TARGET).elf $(BUILD_DIR)/$(TARGET).hex $(BUILD_DIR)/$(TARGET).bin size

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR) $(BUILD_DIR)/tu

$(BUILD_DIR)/%.o: $(CORE_DIR)/%.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD_DIR)/%.o: %.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

# One rule per TinyUSB source directory rather than a VPATH search, so the
# object list stays flat under build/tu/ without make guessing where a given
# basename came from.
$(BUILD_DIR)/tu/%.o: $(TU_DIR)/%.c | $(BUILD_DIR)
	$(CC) $(TU_CFLAGS) -c $< -o $@

$(BUILD_DIR)/tu/%.o: $(TU_DIR)/common/%.c | $(BUILD_DIR)
	$(CC) $(TU_CFLAGS) -c $< -o $@

$(BUILD_DIR)/tu/%.o: $(TU_DIR)/device/%.c | $(BUILD_DIR)
	$(CC) $(TU_CFLAGS) -c $< -o $@

$(BUILD_DIR)/tu/%.o: $(TU_DIR)/class/cdc/%.c | $(BUILD_DIR)
	$(CC) $(TU_CFLAGS) -c $< -o $@

$(BUILD_DIR)/tu/%.o: $(TU_DIR)/portable/st/stm32_fsdev/%.c | $(BUILD_DIR)
	$(CC) $(TU_CFLAGS) -c $< -o $@

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
