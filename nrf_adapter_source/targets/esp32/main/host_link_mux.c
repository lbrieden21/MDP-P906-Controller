/*
 * Owns the platform.h host_link_* contract (plus host_link_begin() and
 * host_rx_byte_count(), which are ESP32-private -- see platform_esp32.h) so
 * that symbol no longer belongs to whichever wired implementation the build
 * selected. Compiled into every ESP32 build, WiFi or not.
 *
 * Routing rule: reads poll both links every call. Writes go to whichever
 * link most recently delivered a byte to host_link_read_byte() -- a TCP
 * client connecting makes WiFi active once its first byte arrives (typically
 * the CMD_ECHO the host sends every second), and a disconnect hands control
 * straight back to the wired link rather than waiting for it to prove
 * itself with a byte of its own.
 *
 * host_link_wifi_client_connected() reporting false forever on a non-WiFi
 * build (see host_link_wifi.c's #else stub) is what keeps this file free of
 * any CONFIG_HOST_LINK_WIFI conditional of its own: active_link simply never
 * leaves LINK_WIRED there.
 */

#include "platform_esp32.h"
#include "platform.h"
#include "host_link_wifi.h"

typedef enum { LINK_WIRED, LINK_WIFI } active_link_t;
static active_link_t active_link = LINK_WIRED;

void host_link_begin(uint32_t baudrate) {
    host_link_wired_begin(baudrate);
    host_link_wifi_begin();
}

void host_link_write(const uint8_t *data, size_t len) {
    if (active_link == LINK_WIFI) {
        host_link_wifi_write(data, len);
    } else {
        host_link_wired_write(data, len);
    }
}

int host_link_read_byte(uint8_t *out) {
    if (!host_link_wifi_client_connected()) {
        /* No WiFi client attached (including: never has been, on a non-WiFi
           build) -- the wired link reclaims the active role immediately,
           not only once it next delivers a byte. */
        active_link = LINK_WIRED;
    } else {
        uint8_t byte;
        if (host_link_wifi_read_byte(&byte)) {
            active_link = LINK_WIFI;
            *out = byte;
            return 1;
        }
    }

    if (host_link_wired_read_byte(out)) {
        active_link = LINK_WIRED;
        return 1;
    }
    return 0;
}

void host_link_set_baudrate(uint32_t baudrate) {
    host_link_wired_set_baudrate(baudrate);
}

uint32_t host_rx_byte_count(void) {
    return wired_rx_byte_count() + host_link_wifi_rx_byte_count();
}
