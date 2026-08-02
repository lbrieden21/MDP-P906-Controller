/*
 * Host link over UART0.
 *
 * Selected by HOST_LINK=HOST_LINK_UART0, which is the default -- and the only
 * option -- on classic ESP32 (ESP32-D0WD / WROOM-32): that silicon has neither
 * USB-Serial/JTAG nor USB-OTG, so the protocol goes out UART0 through the
 * board's CP2102/CH340 bridge. It is also buildable on the C6/H2/S3 as a way to
 * exercise this file on a board that is already hardware-validated.
 *
 * UART0 cannot be both the protocol link and the ESP-IDF console, so the
 * Makefile rejects CONSOLE=1 with this host link. That conflict is the reason
 * making the console opt-in (Phase 3) had to land before this file did.
 *
 * This is the first configuration on this target where the link has a real line
 * rate, which makes host_link_set_baudrate() a real operation and
 * persistence_test.py's negative-control step meaningful.
 */

#include "driver/uart.h"
#include "esp_err.h"
#include "soc/uart_pins.h"

#include "platform_esp32.h"
#include "platform.h"

/* Same 1024 bytes each way as the USB-Serial/JTAG link, for the same reason:
   the largest single frame this firmware emits is a pipe-tagged
   REP_NRF_RECV_OK at 37 bytes, so this is ~27 frames of slack. The TX ring
   must be non-zero, or uart_write_bytes() writes straight to the FIFO and
   blocks until it has shifted out -- which is exactly what must not happen
   here (see host_link_write()). */
#define HOST_RING_BYTES 1024

/* Bound on how long host_link_set_baudrate() will wait for the last frame at
   the old rate to leave the shift register. Generous: at 921600 the ACK that
   precedes it is a few bytes. It exists so a stuck line cannot park us here
   long enough to starve the watchdog refresh. */
#define TX_DRAIN_TIMEOUT_MS 100

static uint32_t rx_byte_count;

void host_link_wired_begin(uint32_t baudrate) {
    uart_config_t cfg = {
        .baud_rate = (int)baudrate,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    /* Note the argument order: rx_buffer_size comes before tx_buffer_size. */
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, HOST_RING_BYTES,
                                        HOST_RING_BYTES, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_0, &cfg));

    /* U0TXD_GPIO_NUM/U0RXD_GPIO_NUM come from soc/uart_pins.h, so this is the
       silicon's own UART0 map per chip (GPIO1/3 on classic ESP32, GPIO16/17 on
       the C6) rather than a number this port has to keep in step. No RTS/CTS:
       flow control is disabled above, and on the WROOM-32 boards those bridge
       lines drive EN/IO0 for auto-reset, not the UART. */
    ESP_ERROR_CHECK(uart_set_pin(UART_NUM_0, U0TXD_GPIO_NUM, U0RXD_GPIO_NUM,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
}

/*
 * The free-size check is the load-bearing part, and it is what preserves the
 * contract host_link_usb_jtag.c states: uart_write_bytes() BLOCKS once the TX
 * ring is full, and protocol_poll() emits replies from inside its drain loop,
 * so a blocking write there starves the watchdog refresh whenever the host
 * stops reading. Dropping the whole frame is also what keeps this from
 * partially emitting one -- which uart_tx_chars(), the obvious non-blocking
 * alternative, would do.
 */
void host_link_wired_write(const uint8_t *data, size_t len) {
    size_t free_bytes = 0;
    if (uart_get_tx_buffer_free_size(UART_NUM_0, &free_bytes) != ESP_OK) {
        return;
    }
    if (free_bytes < len) {
        return; /* drop the frame whole; never a partial one */
    }
    (void)uart_write_bytes(UART_NUM_0, data, len);
}

int host_link_wired_read_byte(uint8_t *out) {
    /* ticks_to_wait = 0: same non-blocking contract as the USB-Serial/JTAG
       link, since protocol_poll() drains until this runs dry. */
    if (uart_read_bytes(UART_NUM_0, out, 1, 0) == 1) {
        rx_byte_count++;
        return 1;
    }
    return 0;
}

uint32_t wired_rx_byte_count(void) {
    return rx_byte_count;
}

/*
 * A real retune, for the first time on this target -- every other ESP32 and
 * Teensy configuration, and the F103's CDC build, only ACK and persist.
 *
 * The wait is not optional: protocol.c has already queued the REP_BAUDRATE_SET
 * ACK and slept 100ms, but that sleep only guarantees the ring drained into the
 * FIFO. Reprogramming the divisor while a byte is still in the shift register
 * corrupts it, and that byte is the ACK the host is waiting on.
 */
void host_link_wired_set_baudrate(uint32_t baudrate) {
    (void)uart_wait_tx_done(UART_NUM_0, pdMS_TO_TICKS(TX_DRAIN_TIMEOUT_MS));
    (void)uart_set_baudrate(UART_NUM_0, baudrate);
}
