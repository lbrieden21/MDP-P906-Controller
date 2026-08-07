#ifndef HOST_LINK_ETH_H
#define HOST_LINK_ETH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Teensy-private Ethernet host link: a TCP server that runs alongside
 * whichever wired link the build selected. host_link_eth.cpp owns this;
 * only host_link_mux.cpp calls it.
 *
 * Safe to call unconditionally on every Teensy 4.x build -- compiled into
 * all of them like net_eth.cpp, gated internally on HOST_LINK_ETH. The
 * non-Ethernet build gets a stub (begin/poll/write are no-ops, read_byte
 * and client_connected report "nothing here"), so host_link_mux.cpp needs
 * no #ifdef of its own.
 */

/* Starts the listening socket. Called once from host_link_begin(). */
void host_link_eth_begin(void);

/* Services the Ethernet stack and the listening socket -- Ethernet.loop(),
   accept(), and link-state bookkeeping. Called once per main.cpp loop()
   iteration regardless of whether this build has Ethernet. */
void host_link_eth_poll(void);

/* Same contract as host_link_write() in platform.h: non-blocking, whole
   frame or nothing, never partially emitted. */
void host_link_eth_write(const uint8_t *data, size_t len);

/* Same contract as host_link_read_byte() in platform.h. */
int host_link_eth_read_byte(uint8_t *out);

/* True while a TCP client is attached. host_link_mux.cpp uses this to decide
   whether the wired link should reclaim the active role. */
bool host_link_eth_client_connected(void);

#endif
