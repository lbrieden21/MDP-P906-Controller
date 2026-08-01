#ifndef PINS_H
#define PINS_H

/*
 * nRF24L01+ wiring, per board. Selected by CONFIG_IDF_TARGET_*, which
 * `idf.py set-target` defines -- the Makefile's BOARD value reaches this file
 * only through that.
 *
 * The maps avoid strapping pins, the USB D+/D- pair, the console UART (U0TXD/
 * U0RXD, which run to the bridge connector) and the addressable RGB LED.
 *
 * Unlike the Teensy targets, SCK/MOSI/MISO are named here rather than owned by
 * the SPI library: ESP32 SPI pins are routed through the GPIO matrix, so they
 * are configuration rather than fixed silicon.
 */

#include "sdkconfig.h"

#if defined(CONFIG_IDF_TARGET_ESP32C6)

/* ESP32-C6-DevKitC-1: one contiguous J3 block, GPIO23 down to GPIO18, which
   puts the whole harness on a single header run.
   Avoided: GPIO4/5 (MTMS/MTDI), GPIO8 (RGB LED and strapping), GPIO9 (BOOT),
   GPIO15 (strapping), GPIO12/13 (USB D-/D+), GPIO16/17 (U0TXD/U0RXD). */
#define NRF_IRQ_PIN 23  /* J3-5  */
#define NRF_CE_PIN 22   /* J3-6  */
#define NRF_CSN_PIN 21  /* J3-7  */
#define NRF_MISO_PIN 20 /* J3-8  */
#define NRF_SCK_PIN 19  /* J3-9  */
#define NRF_MOSI_PIN 18 /* J3-10 */

/* SPI2 is the general-purpose host on this chip. The pins above route through
   the GPIO matrix rather than IOMUX, which caps SPI master at 40MHz -- not
   close to a constraint at 10MHz. */
#define NRF_SPI_HOST SPI2_HOST

#elif defined(CONFIG_IDF_TARGET_ESP32H2)

/* ESP32-H2-DevKitM-1: mixed headers -- the J3 side alone cannot supply six
   safe pins. The SPI trio lands on the FSPI IOMUX pins as a bonus, not by
   requirement.
   Avoided: GPIO2 (MTMS, strapping), GPIO8 (LOG, strapping), GPIO9 (BOOT),
   GPIO23/24 (U0RXD/U0TXD), GPIO26/27 (USB D-/D+), and GPIO13/14, whose header
   entries read 13/N and 14/N -- the XTAL_32K pair, populated or not depending
   on board variant. */
#define NRF_MISO_PIN 0 /* J1-3, FSPIQ   */
#define NRF_SCK_PIN 4  /* J1-9, FSPICLK */
#define NRF_MOSI_PIN 5 /* J1-10, FSPID  */
#define NRF_CSN_PIN 10 /* J3-4  */
#define NRF_CE_PIN 11  /* J3-5  */
#define NRF_IRQ_PIN 12 /* J3-7  */

/* SPI2 is the general-purpose host here, as on the C6. */
#define NRF_SPI_HOST SPI2_HOST

#elif defined(CONFIG_IDF_TARGET_ESP32)

/* NodeMCU-32S / HiLetgo ESP-WROOM-32 (ESP32-D0WD). All six signals sit on one
   header edge on both the 30- and 38-pin boards -- the run is
   23, 22, TX0, RX0, 21, 19, 18, 5, 17, 16, with only U0TXD/U0RXD interrupting
   it, and those now carry the protocol itself.
   Avoided: GPIO0/2/5/12/15 (strapping -- GPIO2 is used only as the LED, driven
   after boot), GPIO1/3 (U0TXD/U0RXD, the host link), GPIO6-11 (SPI flash),
   GPIO34-39 (input-only, so unusable for CE/CSN). */
#define NRF_MOSI_PIN 23 /* VSPI IOMUX */
#define NRF_CE_PIN 22
#define NRF_CSN_PIN 21
#define NRF_MISO_PIN 19 /* VSPI IOMUX */
#define NRF_SCK_PIN 18  /* VSPI IOMUX */
#define NRF_IRQ_PIN 17

/*
 * WROVER caveat: GPIO16/17 are consumed by PSRAM on ESP32-WROVER modules. This
 * map is for WROOM-32 and must not be carried to a WROVER board without moving
 * IRQ. WROVER is explicitly out of scope.
 */

/*
 * SPI3 (VSPI), not SPI2. SPI2 on classic ESP32 is HSPI, whose IOMUX pins
 * include GPIO12 -- MTDI, the flash-voltage strapping pin. VSPI's IOMUX lands
 * on 18/19/23, which are free, so the whole SPI trio above is on IOMUX rather
 * than routed through the GPIO matrix.
 */
#define NRF_SPI_HOST SPI3_HOST

/*
 * The first real status LED on this target. These boards have a plain LED on
 * GPIO2, and no console at all (UART0 is the protocol link), so this is their
 * only sign of life -- bring-up step 1 leans on it. nrf24l01p.c already pulses
 * led_on()/led_off() around every radio operation, exactly as on the STM32
 * targets, so it doubles as a radio-activity indicator.
 *
 * GPIO2 is a strapping pin: it must be low or floating while the ROM samples
 * the straps at boot. platform_init() only takes it over afterwards, which is
 * what makes that safe -- and it is why the LED must never be driven high
 * earlier than that.
 */
#define NRF_LED_PIN 2

#elif defined(CONFIG_IDF_TARGET_ESP32S3)

/* ESP32-S3-DevKitC-1-N8R8: one contiguous J1 block, GPIO9 through GPIO14,
   which is the chip's default FSPI IOMUX group -- SCK/MISO/MOSI land on their
   named FSPI signals as a bonus, the same way the H2's map did, and CE/IRQ
   take the two FSPI signals (HD/WP) this firmware has no use for.
   Avoided: GPIO0/3/45/46 (strapping -- the vendor header table omits GPIO3's
   strapping role, but it is documented as one in the ESP32-S3 TRM, alongside
   the plan's original GPIO0/45/46), GPIO19/20 (USB D-/D+), GPIO43/44
   (U0TXD/U0RXD, the bridge console), GPIO35-37 (octal PSRAM, present on this
   N8R8 module even though it is left disabled in sdkconfig.defaults.esp32s3),
   and GPIO38 (the RGB LED on this v1.1 board, silkscreened on the unit on the
   bench; earlier v1.0 boards put it on GPIO48 instead, which is unused here
   regardless). */
#define NRF_IRQ_PIN 9   /* J1-15, FSPIHD  */
#define NRF_CE_PIN 14   /* J1-20, FSPIWP  */
#define NRF_CSN_PIN 10  /* J1-16, FSPICS0 */
#define NRF_MISO_PIN 13 /* J1-19, FSPIQ   */
#define NRF_SCK_PIN 12  /* J1-18, FSPICLK */
#define NRF_MOSI_PIN 11 /* J1-17, FSPID   */

/* SPI2 (FSPI) is the general-purpose host here too, as on the C6/H2. */
#define NRF_SPI_HOST SPI2_HOST

#else
#error "Unsupported IDF target -- add a pin map above"
#endif

/* 10MHz, the nRF24L01+'s rated SPI ceiling, same as the Teensy targets.
   This is a request, not a guarantee: the driver divides down from a
   chip-dependent source clock (80MHz on the C6 and classic ESP32, 48MHz on the
   H2), so the boards will not all land on the same number. platform_esp32.c
   reads the achieved frequency back with spi_device_get_actual_freq() and it is
   recorded during bring-up; every board must land at or under 10MHz. */
#define NRF_SPI_HZ 10000000

/* NRF_SPI_HOST is per-board, above -- SPI2 on the C6/H2/S3, SPI3 on classic
   ESP32, where SPI2's IOMUX would land on the MTDI strapping pin.

   NRF_LED_PIN is also per-board, and defined only where a usable LED exists.
   The DevKits' only LED is an addressable RGB on a strapping pin needing RMT to
   drive, so led_on()/led_off() stay no-ops there, as on the Teensy 4.x -- the
   UART console is a strictly better boot indicator than a blink anyway, and
   they have one. The WROOM-32 boards have neither an RGB LED nor a console, so
   they define it and get the real implementation. */

#endif
