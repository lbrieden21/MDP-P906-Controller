#ifndef PLATFORM_TEENSY4_H
#define PLATFORM_TEENSY4_H

/*
 * Target-private boot hooks, the Teensy equivalent of the STM32 target's
 * gpio_init/spi_init/uart_init declarations. Everything core/ can see lives in
 * platform.h instead.
 */

#include <stdbool.h>
#include <stdint.h>

/* Pin directions, SPI bring-up, and the nRF24 IRQ attach. */
void platform_init(void);

/* Brings the host link up at the given rate. The rate is ignored on USB CDC,
   where the host picks it -- see uart_set_baudrate() in platform.h. */
void host_link_begin(uint32_t baudrate);

/* True if the radio has an unserviced interrupt: either a falling edge was
   latched by the ISR, or IRQ is still asserted (it stays low until the STATUS
   flags are cleared, so back-to-back events produce only one edge). Consumes
   the latched edge. */
bool radio_irq_pending(void);

#endif
