#ifndef PLATFORM_TEENSY4_H
#define PLATFORM_TEENSY4_H

/*
 * Target-private boot hooks, the Teensy equivalent of the STM32 target's
 * gpio_init/spi_init/uart_init declarations. Everything core/ can see lives in
 * platform.h instead.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Pin directions, SPI bring-up, and the nRF24 IRQ attach. */
void platform_init(void);

/* Brings the host link up at the given rate. Implemented by host_link_mux.cpp,
   which owns every host_link_* symbol platform.h declares plus this one --
   see host_link_mux.cpp for why. The rate is ignored on USB CDC, where the
   host picks it -- see host_link_set_baudrate() in platform.h. */
void host_link_begin(uint32_t baudrate);

/*
 * The wired link, exactly as it looked before the mux arrived -- implemented
 * by platform_teensy4.cpp, consumed only by host_link_mux.cpp. The rate is
 * honoured on Serial1 and ignored on USB CDC, which has no line rate of its
 * own.
 */
void host_link_wired_begin(uint32_t baudrate);
void host_link_wired_write(const uint8_t *data, size_t len);
int host_link_wired_read_byte(uint8_t *out);
void host_link_wired_set_baudrate(uint32_t baudrate);

/* True if the radio has an unserviced interrupt: either a falling edge was
   latched by the ISR, or IRQ is still asserted (it stays low until the STATUS
   flags are cleared, so back-to-back events produce only one edge). Consumes
   the latched edge. */
bool radio_irq_pending(void);

/* CRC16-CCITT, shared between platform_teensy4.cpp's store_load()/store_save()
   and net_eth.cpp's net_ip_config_save()/net_status() -- both round-trip a
   record through the same emulated-EEPROM scheme. */
uint16_t crc16_ccitt(const uint8_t *data, size_t len);

#endif
