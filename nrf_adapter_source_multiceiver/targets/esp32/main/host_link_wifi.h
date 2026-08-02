#ifndef HOST_LINK_WIFI_H
#define HOST_LINK_WIFI_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * ESP32-private WiFi host link: a TCP server that runs alongside whichever
 * wired link the board selected. host_link_wifi.c owns this; only
 * host_link_mux.c calls it.
 *
 * Safe to call unconditionally on every ESP32 build -- compiled into all of
 * them like wifi_sta.c, gated internally on CONFIG_HOST_LINK_WIFI. The
 * non-WiFi build gets a stub (begin/write are no-ops, read_byte and
 * client_connected report "nothing here"), on the same footing as
 * wifi_sta.c's stub, so host_link_mux.c needs no #ifdef of its own.
 */

/* Starts the listening socket and the RX/TX tasks. Called once from
   host_link_begin(). */
void host_link_wifi_begin(void);

/* Same contract as host_link_write() in platform.h: non-blocking, whole
   frame or nothing, never partially emitted. */
void host_link_wifi_write(const uint8_t *data, size_t len);

/* Same contract as host_link_read_byte() in platform.h. */
int host_link_wifi_read_byte(uint8_t *out);

/* True while a TCP client is attached. host_link_mux.c uses this to decide
   whether the wired link should reclaim the active role. */
bool host_link_wifi_client_connected(void);

/* Free-running count of bytes handed out by host_link_wifi_read_byte(), the
   WiFi-side half of host_rx_byte_count(). */
uint32_t host_link_wifi_rx_byte_count(void);

#endif
