/*
 * platform.h for the ESP32 target (C6 / H2 / S3 / classic ESP32 -- see the
 * Makefile's BOARD switch and pins.h).
 *
 * This and the host_link_*.c files are the whole boundary: ESP-IDF headers
 * appear here and nowhere else, and core/ links against it unchanged. It is the
 * same role platform_teensy4.cpp plays, in C rather than C++ because ESP-IDF is
 * a C SDK and there is no extern "C" wrapping to do.
 *
 * The host link lives in host_link_usb_jtag.c or host_link_uart0.c, one of
 * which main/CMakeLists.txt selects from the HOST_LINK build variable --
 * following the F103's usb_cdc.c / uart.c precedent. Everything here (SPI,
 * GPIO, the LED, NVS, timing, the watchdog) is common to every board.
 */

#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "pins.h"
#include "platform_esp32.h"
#include "platform.h"

/* ---------------------------------------------------------------------- SPI */

static spi_device_handle_t nrf_spi;
static int spi_freq_khz;

int spi_actual_freq_khz(void) {
    return spi_freq_khz;
}

static void spi_init(void) {
    spi_bus_config_t bus = {
        .mosi_io_num = NRF_MOSI_PIN,
        .miso_io_num = NRF_MISO_PIN,
        .sclk_io_num = NRF_SCK_PIN,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        /* DMA off deliberately: the documented no-DMA transaction limit is 64
           bytes and the largest transfer here is 32, so disabling it removes
           the DMA-capable-buffer requirement from every caller in core/ at no
           cost. */
        .max_transfer_sz = 64,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(NRF_SPI_HOST, &bus, SPI_DMA_DISABLED));

    spi_device_interface_config_t dev = {
        .mode = 0, /* CPOL 0, CPHA 0, MSB first -- the nRF24's only mode */
        .clock_speed_hz = NRF_SPI_HZ,
        /* CSN is a plain GPIO driven by nrf_csn_low()/nrf_csn_high(), NOT the
           driver's managed CS. nrf24l01p.c issues the command byte and the
           payload as two separate spi_transfer*() calls inside one CSN assert
           (nrf24l01p.c:39-42, :52-56, :77-81, :139-142); a per-transaction CS
           would deassert between them and the chip would see two commands. */
        .spics_io_num = -1,
        .queue_size = 1,
    };
    ESP_ERROR_CHECK(spi_bus_add_device(NRF_SPI_HOST, &dev, &nrf_spi));

    /* Requested 10MHz will not divide exactly from either chip's source clock
       (80MHz on the C6, 48MHz on the H2). Read back what was actually
       programmed so bring-up can record it rather than assume. */
    ESP_ERROR_CHECK(spi_device_get_actual_freq(nrf_spi, &spi_freq_khz));
}

uint8_t spi_transfer_byte(uint8_t tx) {
    /* USE_TXDATA/USE_RXDATA keeps the single byte inside the transaction
       struct, so there is no buffer lifetime to think about. */
    spi_transaction_t t = {
        .flags = SPI_TRANS_USE_TXDATA | SPI_TRANS_USE_RXDATA,
        .length = 8,
    };
    t.tx_data[0] = tx;
    ESP_ERROR_CHECK(spi_device_polling_transmit(nrf_spi, &t));
    return t.rx_data[0];
}

/*
 * platform.h specifies 0xFF filler when tx == NULL, and on this driver that
 * must be made explicit: per the IDF docs, "if tx_buffer is NULL and
 * SPI_TRANS_USE_TXDATA is not set, the Write phase is skipped" -- MOSI is not
 * driven at all rather than being driven low. The nRF24 latches command bits
 * off MOSI during a read, so a skipped write phase is the single most likely
 * silent-wrong-data bug in this port.
 */
void spi_transfer(const uint8_t *tx, uint8_t *rx, size_t len) {
    static const uint8_t filler[32] = {
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    };

    while (len > 0) {
        /* The chunk loop only matters if a caller ever asks for more than the
           32-byte filler; today the maximum is a 32-byte payload. */
        size_t n = (len > sizeof(filler)) ? sizeof(filler) : len;

        spi_transaction_t t = {
            .length = n * 8,
            .tx_buffer = tx ? tx : filler,
            .rx_buffer = rx, /* NULL discards, which the driver handles */
        };
        ESP_ERROR_CHECK(spi_device_polling_transmit(nrf_spi, &t));

        if (tx) {
            tx += n;
        }
        if (rx) {
            rx += n;
        }
        len -= n;
    }
}

void nrf_csn_low(void) {
    gpio_set_level(NRF_CSN_PIN, 0);
}

void nrf_csn_high(void) {
    gpio_set_level(NRF_CSN_PIN, 1);
}

void nrf_ce_low(void) {
    gpio_set_level(NRF_CE_PIN, 0);
}

void nrf_ce_high(void) {
    gpio_set_level(NRF_CE_PIN, 1);
}

/*
 * The status LED is per-board: the DevKits' only LED is an addressable RGB on a
 * strapping pin needing RMT to drive, so they stay no-ops as on the Teensy 4.x,
 * while the WROOM-32 boards have a plain LED on GPIO2 and drive it for real.
 * nrf24l01p.c already pulses these around every radio operation, so on those
 * boards this becomes the activity indicator -- and, having no console at all,
 * their only sign of life. See pins.h.
 */
#ifdef NRF_LED_PIN
void led_on(void) {
    gpio_set_level(NRF_LED_PIN, 1);
}

void led_off(void) {
    gpio_set_level(NRF_LED_PIN, 0);
}
#else
void led_on(void) {}
void led_off(void) {}
#endif

/* ---------------------------------------------------------------- radio IRQ */

static volatile bool radio_irq_flag;

/*
 * IRAM_ATTR, and the service is installed with ESP_INTR_FLAG_IRAM, because NVS
 * writes disable the flash cache and a non-IRAM ISR cannot run while it is
 * disabled -- a CMD_NRF_SAVE could otherwise swallow a radio edge. The
 * level-recheck in radio_irq_pending() would recover it, but relying on that
 * when the fix is one attribute is the wrong trade.
 */
static void IRAM_ATTR radio_isr(void *arg) {
    (void)arg;
    radio_irq_flag = true;
}

bool radio_irq_pending(void) {
    /* Edge-plus-level, the same gate every other target uses: read-then-clear
       is not atomic, and the nRF24 holds IRQ low until STATUS is cleared, so a
       coalesced or lost edge still leaves the pin low for the next call. */
    bool edge = radio_irq_flag;
    radio_irq_flag = false;
    return edge || gpio_get_level(NRF_IRQ_PIN) == 0;
}

/* ----------------------------------------------------------------- settings */

/*
 * NVS, one blob. The [magic][len][payload][crc16] record the four ARM targets
 * share is deliberately NOT carried over: NVS already provides its own
 * integrity checking and wear levelling, so the record would be a CRC inside a
 * CRC. The observable behaviour persistence_test.py actually checks is
 * unchanged -- store_load() returns 0 for absent-or-corrupt, and
 * persisted_settings_t round-trips byte-for-byte.
 */
#define NVS_NS "p906"
#define NVS_KEY "settings"

int store_load(void *payload, size_t len) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        return 0;
    }

    size_t stored = 0;
    if (nvs_get_blob(h, NVS_KEY, NULL, &stored) != ESP_OK || stored != len) {
        nvs_close(h);
        return 0;
    }
    esp_err_t err = nvs_get_blob(h, NVS_KEY, payload, &stored);
    nvs_close(h);
    return err == ESP_OK ? 1 : 0;
}

int store_save(const void *payload, size_t len) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) {
        return 0;
    }

    /* protocol.c:96 uses a len==0 save as CMD_RESET's invalidate, so this must
       erase the record rather than write an empty blob -- otherwise the next
       store_load() would find a zero-length record instead of nothing.
       ESP_ERR_NVS_NOT_FOUND means it was already absent, which is success. */
    if (len == 0) {
        esp_err_t err = nvs_erase_key(h, NVS_KEY);
        if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
            nvs_close(h);
            return 0;
        }
        err = nvs_commit(h);
        nvs_close(h);
        return err == ESP_OK ? 1 : 0;
    }

    if (nvs_set_blob(h, NVS_KEY, payload, len) != ESP_OK ||
        nvs_commit(h) != ESP_OK) {
        nvs_close(h);
        return 0;
    }
    nvs_close(h);

    /* Read back and compare, matching the verify every other target's
       store_save() does. */
    uint8_t check[64];
    if (len > sizeof(check) || !store_load(check, len)) {
        return 0;
    }
    return memcmp(check, payload, len) == 0 ? 1 : 0;
}

/* ------------------------------------------------------------------- timing */

uint32_t millis(void) {
    /* Free-running, wraps at ~49.7 days, wrap-safe for (now - then). */
    return (uint32_t)(esp_timer_get_time() / 1000);
}

void delay_ms(uint32_t ms) {
    /*
     * Must yield, not spin. protocol.c:98 and :110 sit between an
     * uart_send_packet() and a platform_reboot()/host_link_set_baudrate(), and that
     * 100ms is what lets the USB driver's ISR actually drain the TX ring
     * before the chip restarts.
     *
     * Depends on CONFIG_FREERTOS_HZ=1000 (sdkconfig.defaults) for 1ms
     * granularity; at the IDF default of 100 this would round
     * nrf24l01p.c:158's delay_ms(21) power-up settle up to 30ms.
     */
    vTaskDelay(pdMS_TO_TICKS(ms));
}

/* ----------------------------------------------------------------- watchdog */

/*
 * The Task Watchdog, at 3.5s -- matching the Teensy targets rather than the
 * F030's ~3.3s IWDG, for the reason the README already gives: no target hits
 * the reference exactly and this closes the whole 3.15-3.51s spread.
 *
 * Two things about this that are NOT true of an IWDG:
 *
 * - trigger_panic = true is load-bearing. Without it the TWDT prints a warning
 *   and continues -- a watchdog that never resets, which is worse than none.
 *   CONFIG_ESP_TASK_WDT_PANIC=y in sdkconfig.defaults sets the same thing for
 *   the startup-time configuration; this sets it for ours.
 * - The TWDT is interrupt-driven and task-scoped, so it covers a stalled
 *   protocol loop but not a stall with interrupts disabled. ESP-IDF's separate
 *   Interrupt Watchdog covers that case and is on by default, so the combined
 *   coverage is arguably wider than a single IWDG -- but it is two mechanisms,
 *   not one.
 *
 * reconfigure() rather than init() because CONFIG_ESP_TASK_WDT_INIT defaults
 * to y, so the TWDT is already running at startup (watching the idle tasks at
 * the Kconfig default of 5s); calling init() on it would fail with
 * ESP_ERR_INVALID_STATE. idle_core_mask = 0 is what takes the idle tasks off
 * it, which matters because main.c's loop deliberately starves idle for up to
 * 10ms at a time.
 */
#define WATCHDOG_TIMEOUT_MS 3500

void watchdog_init(void) {
    esp_task_wdt_config_t cfg = {
        .timeout_ms = WATCHDOG_TIMEOUT_MS,
        .idle_core_mask = 0,
        .trigger_panic = true,
    };
    ESP_ERROR_CHECK(esp_task_wdt_reconfigure(&cfg));
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    watchdog_refresh();
}

void watchdog_refresh(void) {
    esp_task_wdt_reset();
}

void platform_reboot(void) {
    esp_restart();
    while (1) {} /* esp_restart() does not return; keeps the compiler happy */
}

/* ---------------------------------------------------------------- boot init */

void platform_init(void) {
    /* NVS first: protocol_init() calls store_load() before anything else. */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    /* Idle levels before the pins become outputs, so neither line glitches --
       same ordering as the STM32 target's gpio_init(). gpio_set_level() on a
       not-yet-configured pin latches the output register, which takes effect
       the moment the direction is set. */
    gpio_set_level(NRF_CSN_PIN, 1);
    gpio_set_level(NRF_CE_PIN, 0);

    gpio_config_t out = {
        .pin_bit_mask = (1ULL << NRF_CSN_PIN) | (1ULL << NRF_CE_PIN)
#ifdef NRF_LED_PIN
                        | (1ULL << NRF_LED_PIN)
#endif
        ,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&out));

    gpio_set_level(NRF_CSN_PIN, 1);
    gpio_set_level(NRF_CE_PIN, 0);
#ifdef NRF_LED_PIN
    /* GPIO2 is a strapping pin on classic ESP32 (it must be low or floating at
       boot to enter flash-download mode). Driving it only from here, after the
       ROM has already sampled the straps, is what makes it safe to use. */
    led_off();
#endif

    /* IRQ is open-drain active-low on the nRF24, hence the internal pull-up. */
    gpio_config_t irq = {
        .pin_bit_mask = 1ULL << NRF_IRQ_PIN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_NEGEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&irq));

    spi_init();

    ESP_ERROR_CHECK(gpio_install_isr_service(ESP_INTR_FLAG_IRAM));
    ESP_ERROR_CHECK(gpio_isr_handler_add(NRF_IRQ_PIN, radio_isr, NULL));
}
