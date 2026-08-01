#ifndef TUSB_CONFIG_H
#define TUSB_CONFIG_H

/*
 * TinyUSB configuration for the F103 CDC host link. Only reached by the
 * HOST_LINK_USB_CDC build -- the Makefile compiles neither this nor any
 * TinyUSB source into the USART1 build.
 *
 * Everything the fsdev port needs beyond this is derived per-MCU by TinyUSB
 * itself: CFG_TUSB_FSDEV_PMA_SIZE resolves to 512 for STM32F1 in
 * Drivers/tinyusb/src/common/tusb_mcu.h, so the PMA budget is not restated
 * here and cannot drift out of sync with the vendored tree.
 */

#define CFG_TUSB_MCU            OPT_MCU_STM32F1
#define CFG_TUSB_OS             OPT_OS_NONE
/* Any non-zero value pulls in TinyUSB's own printf-based logging, which this
   target has nowhere to send -- the host link is the thing being debugged. */
#define CFG_TUSB_DEBUG          0

#define CFG_TUD_ENABLED         1
#define CFG_TUD_MAX_SPEED       OPT_MODE_FULL_SPEED

#define CFG_TUD_ENDPOINT0_SIZE  64

#define CFG_TUD_CDC             1
#define CFG_TUD_MSC             0
#define CFG_TUD_HID             0
#define CFG_TUD_MIDI            0
#define CFG_TUD_VENDOR          0

/* TX is the direction under pressure: uart_write() drops whatever does not fit
   within its deadline (usb_cdc.c), so the deeper FIFO buys real headroom when
   the host pauses. RX only ever holds host commands, which are short and
   drained every loop iteration. */
#define CFG_TUD_CDC_RX_BUFSIZE  256
#define CFG_TUD_CDC_TX_BUFSIZE  512
#define CFG_TUD_CDC_EP_BUFSIZE  64

#endif
