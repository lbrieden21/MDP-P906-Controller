/*
 * Host link over Ethernet: a TCP server that runs *alongside* whichever
 * wired link the build selected, never instead of it. host_link_mux.cpp is
 * the only caller; it decides which link is active and routes
 * host_link_write()/host_link_read_byte() accordingly.
 *
 * Compiled into every Teensy 4.x build, like net_eth.cpp: the #else stub
 * below is what lets host_link_mux.cpp call every function here
 * unconditionally, with host_link_eth_client_connected() reporting false
 * forever on a non-Ethernet build, which is all the mux needs to never
 * route to this link.
 */

#include "host_link_eth.h"

#if defined(HOST_LINK_ETH)

#include <QNEthernet.h>

using namespace qindesign::network;

#define HOST_LINK_ETH_PORT 9000 /* matches the ESP32 WiFi host link */

static EthernetServer server(HOST_LINK_ETH_PORT);
static EthernetClient client;

void host_link_eth_begin(void) {
    server.begin();
}

/* Ethernet.loop() first, ahead of accept()/read() -- QNETHERNET_DO_LOOP_IN_YIELD=0
   (Makefile) means nothing else drives the stack, so this is the only place
   incoming segments and connection teardown get processed. */
void host_link_eth_poll(void) {
    Ethernet.loop();

    /* A new connection replaces the old one rather than being refused, so a
       crashed GUI does not lock the adapter out for a keepalive timeout.
       Bytes left buffered on a replaced client are deliberately not
       flushed -- core/protocol.c's feed_byte() is a resyncing byte-stream
       parser that treats them as noise ahead of the next real frame. */
    EthernetClient incoming = server.accept();
    if (incoming) {
        if (client) {
            client.stop();
        }
        client = incoming;
        client.setNoDelay(true);
    }
}

/* Same contract as the wired links: check space up front, then write whole
   or drop whole, never block or partially emit -- protocol_poll() emits
   replies from inside its own drain loop, so a blocking write here would
   starve the watchdog refresh exactly as it would on a wired link.

   flush() is not a nicety -- QNEthernetClient::write() queues via
   altcp_write() without calling altcp_output(), so without it the reply sits
   unsent until lwIP's 250ms delayed-ACK timer fires, past the client's RTO.
   See the plan doc's Phase 0 root-cause writeup; dropping this call
   reintroduces that ~1400x latency regression. */
void host_link_eth_write(const uint8_t *data, size_t len) {
    if (!client || (size_t)client.availableForWrite() < len) {
        return;
    }
    client.write(data, len);
    client.flush();
}

int host_link_eth_read_byte(uint8_t *out) {
    if (!client) {
        return 0;
    }
    int c = client.read();
    if (c < 0) {
        return 0;
    }
    *out = (uint8_t)c;
    return 1;
}

bool host_link_eth_client_connected(void) {
    return (bool)client;
}

#else

void host_link_eth_begin(void) {}

void host_link_eth_poll(void) {}

void host_link_eth_write(const uint8_t *data, size_t len) {
    (void)data;
    (void)len;
}

int host_link_eth_read_byte(uint8_t *out) {
    (void)out;
    return 0;
}

bool host_link_eth_client_connected(void) {
    return false;
}

#endif
