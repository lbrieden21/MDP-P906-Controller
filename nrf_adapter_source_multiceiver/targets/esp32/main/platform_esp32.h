#ifndef PLATFORM_ESP32_H
#define PLATFORM_ESP32_H

/*
 * Target-private boot hooks, the ESP32 equivalent of platform_teensy4.h.
 * Everything core/ can see lives in platform.h instead.
 */

#include <stdbool.h>
#include <stdint.h>

/* NVS, pin directions, SPI bring-up, the status LED where one exists, and the
   nRF24 IRQ attach. */
void platform_init(void);

/* Brings the host link up. Implemented by whichever of host_link_usb_jtag.c or
   host_link_uart0.c the build selected. The rate is honoured on UART0 and
   ignored on USB-Serial/JTAG, which has no line rate of its own -- exactly as
   on the other CDC targets. */
void host_link_begin(uint32_t baudrate);

/* True if the radio has an unserviced interrupt: either a falling edge was
   latched by the ISR, or IRQ is still asserted (it stays low until the STATUS
   flags are cleared, so back-to-back events produce only one edge). Consumes
   the latched edge. */
bool radio_irq_pending(void);

/* Free-running count of bytes handed out by host_link_read_byte(). main.c reads it
   either side of protocol_poll() to decide whether the loop did any work this
   iteration -- protocol_poll() returns void and lives in core/, which is not
   modified, so there is no other way to ask. Compared for inequality only, so
   the wrap at 2^32 is a non-event. Lives with the host link, since both
   implementations need it. */
uint32_t host_rx_byte_count(void);

/* Achieved SPI clock in kHz, as reported by spi_device_get_actual_freq().
   Recorded during bring-up: the requested 10MHz divides differently from the
   C6's 80MHz source than from the H2's 48MHz one. Returns 0 before
   platform_init() has run. */
int spi_actual_freq_khz(void);

#endif
