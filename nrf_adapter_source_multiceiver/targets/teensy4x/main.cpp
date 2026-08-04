/*
 * Teensy 4.x (4.0/4.1) entry point. Mirrors targets/stm32f030/main.c's boot
 * order: bring the host link up at the hard-coded default first, then let
 * protocol_init() switch it if a saved baudrate says otherwise.
 *
 * The framework's own main() (Drivers/teensy4/main.cpp) calls setup() then
 * loop() forever, so there is no vector table or Reset_Handler to write here --
 * that is what startup.c does on the STM32 target.
 */

#include <Arduino.h>

#include "host_link_eth.h"
#include "platform_teensy4.h"

extern "C" {
#include "platform.h"
#include "protocol.h"
}

static uint32_t last_wdg_tick;

void setup() {
    platform_init();
    host_link_begin(921600); /* hard-coded default, matches the shipped
                                firmware; ignored on USB CDC */
    watchdog_init();

    protocol_init();

    /* No boot blink: there is no status LED on this target (pins.h). The
       STM32's four blinks sat between protocol_init() and the first poll, but
       nothing depends on that ~800ms -- the host is not talking yet, and USB
       enumeration takes longer than it did anyway. */

    last_wdg_tick = millis();
}

void loop() {
    /* Radio first: a received payload is sitting in the RX FIFO, whereas host
       bytes are already buffered by the framework and can wait a few
       microseconds. Unlike the STM32 target this runs in thread context, not
       inside the pin ISR -- see platform_teensy4.cpp. */
    if (radio_irq_pending()) {
        protocol_service_radio_irq();
    }

    /* Ethernet.loop() + accept() + link state, whether or not this build has
       Ethernet -- a no-op passthrough without HOST_LINK_ETH (host_link_eth.cpp). */
    host_link_eth_poll();

    protocol_poll();

    if (millis() - last_wdg_tick >= 100) {
        last_wdg_tick = millis();
        watchdog_refresh();
    }
}
