/*
 * Teensy 3.x (3.5/3.6) entry point. Mirrors targets/teensy4x/main.cpp, which
 * itself mirrors targets/stm32f030/main.c's boot order: bring the host link up
 * at the hard-coded default first, then let protocol_init() switch it if a
 * saved baudrate says otherwise.
 *
 * The framework's own main() (Drivers/teensy3/main.cpp) calls setup() then
 * loop() forever -- PROVIDED -DUSING_MAKEFILE is *not* defined, or that main()
 * runs its own fallback blink and setup()/loop() never execute at all. See the
 * Makefile for why that macro must stay undefined.
 */

#include <Arduino.h>

#include "platform_teensy3.h"

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

    /* Boot blink: four quick flashes then steady, distinguishing a working
       firmware from the core's own USING_MAKEFILE fallback (a 1Hz square wave
       forever) if that macro is ever accidentally defined -- see the
       Makefile. Unlike the Teensy 4.x target, this board has a real LED
       (pins.h), so the STM32 target's indicator comes back here. */
    for (int i = 0; i < 4; i++) {
        led_on();
        delay(100);
        led_off();
        delay(100);
    }

    last_wdg_tick = millis();
}

void loop() {
    /* Radio first: a received payload is sitting in the RX FIFO, whereas host
       bytes are already buffered by the framework and can wait a few
       microseconds. Unlike the STM32 target this runs in thread context, not
       inside the pin ISR -- see platform_teensy3.cpp. */
    if (radio_irq_pending()) {
        protocol_service_radio_irq();
    }

    protocol_poll();

    if (millis() - last_wdg_tick >= 100) {
        last_wdg_tick = millis();
        watchdog_refresh();
    }
}
