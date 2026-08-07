/*
 * Owns the platform.h host_link_* contract (plus host_link_begin(), which is
 * Teensy-private -- see platform_teensy4.h) so that symbol no longer belongs
 * to whichever wired implementation the build selected. Compiled into every
 * Teensy 4.x build, Ethernet or not. A direct port of
 * targets/esp32/main/host_link_mux.c -- the routing rule and its rationale
 * transfer verbatim.
 *
 * Routing rule: reads poll both links every call. Writes go to whichever
 * link most recently delivered a byte to host_link_read_byte() -- a TCP
 * client connecting makes Ethernet active once its first byte arrives
 * (typically the CMD_ECHO the host sends every second), and a disconnect
 * hands control straight back to the wired link rather than waiting for it
 * to prove itself with a byte of its own.
 *
 * host_link_eth_client_connected() reporting false forever on a non-Ethernet
 * build (see host_link_eth.cpp's #else stub) is what keeps this file free of
 * any HOST_LINK_ETH conditional of its own: active_link simply never leaves
 * LINK_WIRED there.
 */

#include "host_link_eth.h"
#include "platform_teensy4.h"

extern "C" {
#include "platform.h"
}

typedef enum { LINK_WIRED, LINK_ETH } active_link_t;
static active_link_t active_link = LINK_WIRED;

void host_link_begin(uint32_t baudrate) {
    host_link_wired_begin(baudrate);
    host_link_eth_begin();
}

extern "C" void host_link_write(const uint8_t *data, size_t len) {
    if (active_link == LINK_ETH) {
        host_link_eth_write(data, len);
    } else {
        host_link_wired_write(data, len);
    }
}

extern "C" int host_link_read_byte(uint8_t *out) {
    if (!host_link_eth_client_connected()) {
        /* No Ethernet client attached (including: never has been, on a
           non-Ethernet build) -- the wired link reclaims the active role
           immediately, not only once it next delivers a byte. */
        active_link = LINK_WIRED;
    } else {
        uint8_t byte;
        if (host_link_eth_read_byte(&byte)) {
            active_link = LINK_ETH;
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

extern "C" void host_link_set_baudrate(uint32_t baudrate) {
    host_link_wired_set_baudrate(baudrate);
}
