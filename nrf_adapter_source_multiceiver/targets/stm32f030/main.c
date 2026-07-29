#include "gpio.h"
#include "platform.h"
#include "protocol.h"
#include "spi.h"
#include "system_clock.h"
#include "uart.h"

void Error_Handler(void) {
    __disable_irq();
    while (1) {}
}

void platform_reboot(void) {
    NVIC_SystemReset();
}

int main(void) {
    /* SystemInit() already brought the clock tree to 48MHz from Reset_Handler. */
    systick_init();
    gpio_init();
    spi_init();
    uart_init(921600); /* hard-coded default, matches shipped firmware; may be
                            switched by a saved baudrate inside protocol_init() */
    watchdog_init();

    protocol_init();

    for (int i = 0; i < 4; i++) {
        gpio_set(LED_GPIO_Port, LED_Pin);
        delay_ms(100);
        gpio_clear(LED_GPIO_Port, LED_Pin);
        delay_ms(100);
    }
    gpio_set(LED_GPIO_Port, LED_Pin); /* LED is active-low; idle = off */

    uint32_t last_wdg_tick = millis();
    while (1) {
        protocol_poll();
        if (millis() - last_wdg_tick >= 100) {
            last_wdg_tick = millis();
            watchdog_refresh();
        }
    }
}
