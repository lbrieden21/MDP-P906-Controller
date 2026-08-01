/*
 * ESP32 entry point. Mirrors targets/stm32f030/main.c's boot order: bring the
 * host link up at the hard-coded default first, then let protocol_init()
 * switch it if a saved baudrate says otherwise.
 *
 * ESP-IDF calls app_main() from a FreeRTOS task rather than running a bare
 * loop, so there is no vector table or Reset_Handler to write here -- that is
 * what startup.c does on the STM32 targets. app_main() never returns.
 */

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "platform_esp32.h"

#include "platform.h"
#include "protocol.h"

static const char *TAG = "adapter";

/*
 * How long the loop may run without yielding before it gives the idle task a
 * tick anyway. Bounds idle-task starvation under sustained load.
 */
#define YIELD_DEADLINE_MS 10

/* Watchdog refresh cadence, the same 100ms every other target uses. */
#define WDG_PERIOD_MS 100

void app_main(void) {
    platform_init();
    host_link_begin(921600); /* hard-coded default, matches the shipped
                                firmware. A real line rate on the UART0 host
                                link; ignored on USB-Serial/JTAG. */
    watchdog_init();

    protocol_init();

    /* Goes nowhere in the default build: the console is off (CONFIG_ESP_CONSOLE_NONE)
       because it needs a second USB cable that nothing reads. `make CONSOLE=1`
       puts it on UART0 -> the bridge connector, and never on the native port
       carrying the protocol (CONFIG_ESP_CONSOLE_SECONDARY_NONE, which holds in
       both builds). On the DevKits, which have no usable status LED, this line
       is the boot indicator bring-up step 1 looks for -- which is why step 1 is
       one of the two steps built with CONSOLE=1. The WROOM-32 boards cannot
       have a console at all (UART0 is their protocol link) and use the GPIO2
       LED for that instead. */
    ESP_LOGI(TAG, "adapter up, SPI %d kHz", spi_actual_freq_khz());

    uint32_t last_wdg = millis();
    uint32_t last_yield = millis();

    for (;;) {
        bool worked = false;

        /* Radio first: a received payload is sitting in the RX FIFO, whereas
           host bytes are already buffered by the USB driver and can wait. */
        if (radio_irq_pending()) {
            protocol_service_radio_irq();
            worked = true;
        }

        /* protocol_poll() returns void and lives in core/, which is not
           modified, so the loop cannot ask it whether it consumed anything.
           host_rx_byte_count() is a free-running counter incremented per byte
           handed out by host_link_read_byte(); comparing it either side of the call
           is how this loop knows whether there was work. */
        uint32_t rx_before = host_rx_byte_count();
        protocol_poll();
        if (host_rx_byte_count() != rx_before) {
            worked = true;
        }

        if (millis() - last_wdg >= WDG_PERIOD_MS) {
            last_wdg = millis();
            watchdog_refresh();
        }

        /*
         * This target is the first where yielding is mandatory rather than
         * optional -- every other target's loop is stateless and never gives
         * the CPU up. Under FreeRTOS the idle task still has to run, and on
         * the single-core C6 and H2 there is no second core to escape to.
         *
         * But an unconditional vTaskDelay(1) would be wrong: it puts a 1ms
         * floor on every iteration of a link the Teensy 4.1 turns at ~118
         * req/s (and a 10ms floor at the default FREERTOS_HZ=100). So yield
         * only when there was nothing to do -- a busy link then runs at full
         * speed, an idle one costs at most 1ms of added latency -- with the
         * YIELD_DEADLINE_MS clause bounding idle starvation under sustained
         * load.
         */
        if (!worked || millis() - last_yield >= YIELD_DEADLINE_MS) {
            last_yield = millis();
            vTaskDelay(1); /* 1ms at CONFIG_FREERTOS_HZ=1000 */
        }
    }
}
