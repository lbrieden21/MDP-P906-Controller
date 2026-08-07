#ifndef PINS_H
#define PINS_H

/*
 * nRF24L01+ wiring on the Teensy 4.1.
 *
 * The radio hangs off LPSPI4 -- the default `SPI` object -- on its fixed pins
 * SCK 13, MOSI 11, MISO 12. Those are not configurable and so are not repeated
 * as macros here; the SPI library owns them.
 *
 * There is no status LED on this target. Pin 13 carries the onboard LED *and*
 * LPSPI4's SCK, so it is unavailable, and led_on()/led_off() are no-ops --
 * they only ever drove cosmetic activity indication (see platform.h).
 */

#define NRF_CSN_PIN 10
#define NRF_CE_PIN 9
#define NRF_IRQ_PIN 2

/* 10MHz, the nRF24L01+'s rated SPI ceiling. The STM32 target runs 12MHz
   (48/4, spi.c) which is above spec; it works there, but there is no reason to
   carry the over-spec divisor onto a new target. */
#define NRF_SPI_HZ 10000000

#endif
