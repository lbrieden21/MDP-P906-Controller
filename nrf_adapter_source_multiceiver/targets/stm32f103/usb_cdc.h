#ifndef USB_CDC_H
#define USB_CDC_H

/* Bring-up only. uart_write()/uart_read_byte()/uart_set_baudrate() are
   declared in platform.h -- usb_cdc.c is the HOST_LINK_USB_CDC implementation
   of exactly the same three functions uart.c implements for USART1, which is
   why core/ needs no conditional. The two files are never compiled together;
   the Makefile picks one. */
void usb_cdc_init(void);

/* Must be called every main-loop iteration: TinyUSB's device stack does all
   of its non-ISR work here. Target-private, not part of platform.h. */
void usb_cdc_task(void);

#endif
