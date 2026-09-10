/*
 * Host link over the native USB-Serial/JTAG controller.
 *
 * Selected by HOST_LINK=HOST_LINK_USB_JTAG, which is the default on every chip
 * that has the peripheral (C5, C6, H2, S3). Classic ESP32 has no native USB of
 * any kind and uses host_link_uart0.c instead; main/CMakeLists.txt compiles
 * exactly one of the two.
 *
 * The controller is already a CDC-ACM device, so there are no descriptors, no
 * managed components and no TinyUSB -- one implementation covers all four
 * chips. Split out of platform_esp32.c unchanged when the second host link
 * arrived, following the F103's usb_cdc.c / uart.c precedent.
 */

#include "driver/usb_serial_jtag.h"
#include "esp_err.h"

#include "platform_esp32.h"
#include "platform.h"

/*
 * 1024-byte rings in both directions. The largest single frame this firmware
 * emits is a pipe-tagged REP_NRF_RECV_OK at 37 bytes, so that is ~27 frames of
 * slack; both sizes must be > 0 for the driver to install.
 */
#define HOST_RING_BYTES 1024

static uint32_t rx_byte_count;

void host_link_wired_begin(uint32_t baudrate) {
    /* Ignored: USB CDC has no line rate of its own, the host picks one. The
       value is still persisted and still ACKed -- see host_link_wired_set_baudrate(). */
    (void)baudrate;

    usb_serial_jtag_driver_config_t cfg = {
        .tx_buffer_size = HOST_RING_BYTES,
        .rx_buffer_size = HOST_RING_BYTES,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&cfg));
}

/*
 * ticks_to_wait = 0 on both directions, and that is load-bearing rather than
 * an optimisation.
 *
 * protocol_poll() drains until host_link_read_byte() runs dry and emits replies
 * from inside that loop, so a blocking write would starve the watchdog refresh
 * whenever the host stopped reading. A full TX ring drops the frame instead --
 * the same trade the F103's usb_cdc.c makes, for the same reason. host_link_wired_write()
 * must not wait, must not pump any driver task, and must not partially emit a
 * frame. host_link_uart0.c preserves the same contract by a different means.
 */
void host_link_wired_write(const uint8_t *data, size_t len) {
    (void)usb_serial_jtag_write_bytes(data, len, 0);
}

int host_link_wired_read_byte(uint8_t *out) {
    if (usb_serial_jtag_read_bytes(out, 1, 0) == 1) {
        rx_byte_count++;
        return 1;
    }
    return 0;
}

uint32_t wired_rx_byte_count(void) {
    return rx_byte_count;
}

void host_link_wired_set_baudrate(uint32_t baudrate) {
    /* No line rate to set. CMD_SET_BAUDRATE still ACKs and still persists the
       value (protocol.c), so the settings record stays portable across every
       target -- identical to the Teensy and F103 CDC builds. This is why
       persistence_test.py's negative-control step "legitimately fails" on this
       configuration and genuinely passes on the UART0 one. */
    (void)baudrate;
}
