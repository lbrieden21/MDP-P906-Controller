#include "gpio.h"
#include "platform.h"
#include "protocol.h"
#include "spi.h"
#include "system_clock.h"

/* The only divergence from targets/stm32f030/main.c, which is otherwise
   line-for-line identical: host-link selection. The F030 has no USB
   peripheral on the die and never defines HOST_LINK_USB_CDC, so its build
   takes the same branch it always did. */
#if defined(HOST_LINK_USB_CDC)
#include "usb_cdc.h"
#else
#include "uart.h"
#endif

#if defined(HOST_LINK_USB_CDC)
/* Once usb_cdc_init() has run, tud_task() has to keep being called: the USB
   ISR only queues events, and everything that answers the host -- including
   the control transfers that drive enumeration -- happens in tud_task(). The
   boot blink below is the one place that would otherwise block long enough to
   matter. ~800ms of plain delay_ms() lands in the middle of the host's
   enumeration, and TinyUSB's event queue is 16 deep and drops silently once
   full (CFG_TUD_TASK_QUEUE_SZ, usbd.c), so the failure would be a board that
   enumerates slowly or intermittently rather than one that obviously does
   not. Pumping the stack through the blink keeps that window closed. */
static void boot_delay_ms(uint32_t ms) {
    uint32_t start = millis();
    while (millis() - start < ms) {
        usb_cdc_task();
    }
}
#else
#define boot_delay_ms(ms) delay_ms(ms)
#endif

void Error_Handler(void) {
    __disable_irq();
    while (1) {}
}

void platform_reboot(void) {
    NVIC_SystemReset();
}

int main(void) {
    /* SystemInit() already brought the clock tree to 72MHz from Reset_Handler. */
    systick_init();
    gpio_init();
    spi_init();
#if defined(HOST_LINK_USB_CDC)
    usb_cdc_init(); /* no line rate to set; a saved baudrate is still loaded
                       and re-persisted by protocol_init(), it just has
                       nothing to act on (usb_cdc.c) */
#else
    uart_init(921600); /* hard-coded default; may be switched by a saved
                            baudrate inside protocol_init() */
#endif
    watchdog_init();

    protocol_init();

    for (int i = 0; i < 4; i++) {
        gpio_set(LED_GPIO_Port, LED_Pin);
        boot_delay_ms(100);
        gpio_clear(LED_GPIO_Port, LED_Pin);
        boot_delay_ms(100);
    }
    gpio_set(LED_GPIO_Port, LED_Pin); /* LED is active-low; idle = off */

    uint32_t last_wdg_tick = millis();
    while (1) {
        /* Radio first: a received payload is sitting in the RX FIFO, whereas
           host bytes are already buffered by uart.c's RX ring and can wait a
           few microseconds. */
        if (radio_irq_pending()) {
            protocol_service_radio_irq();
        }

#if defined(HOST_LINK_USB_CDC)
        /* After the radio, so "radio first" still holds, but before
           protocol_poll(), which is what drains the CDC RX FIFO: this is the
           call that moves received bytes out of the endpoint buffer into that
           FIFO, and that services control transfers during enumeration. */
        usb_cdc_task();
#endif

        protocol_poll();
        if (millis() - last_wdg_tick >= 100) {
            last_wdg_tick = millis();
            watchdog_refresh();
        }
    }
}
