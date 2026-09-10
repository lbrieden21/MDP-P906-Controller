#ifndef PLATFORM_ESP32_H
#define PLATFORM_ESP32_H

/*
 * Target-private boot hooks, the ESP32 equivalent of platform_teensy4.h.
 * Everything core/ can see lives in platform.h instead.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* NVS, pin directions, SPI bring-up, the status LED where one exists, and the
   nRF24 IRQ attach. */
void platform_init(void);

/* Brings the host link up. Implemented by host_link_mux.c, which owns every
   host_link_* symbol platform.h declares plus this one and
   host_rx_byte_count() below -- see host_link_mux.c for why. */
void host_link_begin(uint32_t baudrate);

/*
 * The wired link, exactly as it looked before the mux arrived -- implemented
 * by whichever of host_link_usb_jtag.c or host_link_uart0.c the build
 * selected, and consumed only by host_link_mux.c. The rate is honoured on
 * UART0 and ignored on USB-Serial/JTAG, which has no line rate of its own --
 * exactly as on the other CDC targets.
 */
void host_link_wired_begin(uint32_t baudrate);
void host_link_wired_write(const uint8_t *data, size_t len);
int host_link_wired_read_byte(uint8_t *out);
void host_link_wired_set_baudrate(uint32_t baudrate);
uint32_t wired_rx_byte_count(void);

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
   C6's 80MHz source, the H2's 48MHz one and the C5's 160MHz one. Returns 0
   before platform_init() has run. */
int spi_actual_freq_khz(void);

#endif
