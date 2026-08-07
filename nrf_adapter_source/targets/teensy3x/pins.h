#ifndef PINS_H
#define PINS_H

/*
 * nRF24L01+ wiring on the Teensy 3.5 / 3.6.
 *
 * The radio hangs off SPI0 -- the default `SPI` object -- but unlike the
 * Teensy 4.x target, Kinetis SPI0 can move its SCK signal off pin 13 via
 * SPI.setSCK() (Drivers/teensy_libs/SPI/SPI.h:637), called in platform_init()
 * before SPI.begin(). That frees pin 13 for the onboard LED, so this is the
 * one place the 3.x wiring differs from a 4.x board: MOSI 11 / MISO 12 stay
 * at their defaults, but SCK moves to 14 and led_on()/led_off() are real.
 */

#define NRF_CSN_PIN 10
#define NRF_CE_PIN 9
#define NRF_IRQ_PIN 2
#define NRF_SCK_PIN 14
#define LED_PIN 13

/* 10MHz, the nRF24L01+'s rated SPI ceiling -- same as the Teensy 4.x target. */
#define NRF_SPI_HZ 10000000

#endif
