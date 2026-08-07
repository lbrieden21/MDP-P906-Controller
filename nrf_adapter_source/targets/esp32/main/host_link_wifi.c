/*
 * Host link over WiFi: a TCP server that runs *alongside* whichever wired
 * link the board selected (host_link_usb_jtag.c or host_link_uart0.c), never
 * instead of it. host_link_mux.c is the only caller; it decides which link
 * is active and routes host_link_write()/host_link_read_byte() accordingly.
 *
 * Compiled into every ESP32 build, like wifi_sta.c: the #else stub below is
 * what lets host_link_mux.c call every function here unconditionally, with
 * host_link_wifi_client_connected() reporting false forever on a non-WiFi
 * build, which is all the mux needs to never route to this link.
 */

#include "sdkconfig.h"

#if CONFIG_HOST_LINK_WIFI

#include <errno.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#include "host_link_wifi.h"

static const char *TAG = "host_link_wifi";

/* Same 1024 bytes each way as the wired links (host_link_usb_jtag.c,
   host_link_uart0.c) -- the largest single frame this firmware emits is a
   pipe-tagged REP_NRF_RECV_OK at 37 bytes, so this is ~27 frames of slack. */
#define HOST_RING_BYTES 1024
#define RX_CHUNK_BYTES 256

static StreamBufferHandle_t rx_stream;
static StreamBufferHandle_t tx_stream;

/* -1 = no client. Written only from rx_task (on accept/disconnect), read
   from rx_task, tx_task and host_link_wifi_client_connected() -- a bare int
   assignment is atomic on this silicon and the readers only ever use it to
   decide "do I have somewhere to send/is there anyone there", never to
   synchronise a sequence, so no lock is needed. */
static volatile int client_fd = -1;

static uint32_t rx_byte_count;

static void set_nodelay(int fd) {
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
}

/*
 * Owns both the listening socket and whichever client is currently attached.
 * select() across both is what lets a new connection interrupt a stale one
 * -- "a new connection replaces the old one rather than being refused" (plan
 * doc) -- without a third task: a plain blocking recv() on the client fd
 * alone could never notice a pending accept() while parked in an old
 * client's read.
 *
 * Stale bytes left behind in rx_stream/tx_stream by a replaced connection
 * are deliberately not flushed on accept. The framer this protocol shares
 * with every other target (core/protocol.c's feed_byte()) is a resyncing
 * byte-stream parser with no message-boundary assumption -- that is the
 * property that let TCP drop in with zero framing changes in the first
 * place -- so a handful of orphaned bytes ahead of the new client's first
 * real frame are noise it already knows how to skip past, on both ends of
 * the link.
 */
static void rx_task(void *arg) {
    (void)arg;

    int listen_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listen_fd < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        vTaskDelete(NULL);
        return;
    }

    int reuse = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_port = htons(CONFIG_HOST_LINK_WIFI_PORT),
    };
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 ||
        listen(listen_fd, 1) != 0) {
        ESP_LOGE(TAG, "bind/listen on port %d failed: errno %d", CONFIG_HOST_LINK_WIFI_PORT, errno);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "listening on port %d", CONFIG_HOST_LINK_WIFI_PORT);

    uint8_t buf[RX_CHUNK_BYTES];

    for (;;) {
        int fd = client_fd;

        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(listen_fd, &rfds);
        int maxfd = listen_fd;
        if (fd >= 0) {
            FD_SET(fd, &rfds);
            if (fd > maxfd) {
                maxfd = fd;
            }
        }

        if (select(maxfd + 1, &rfds, NULL, NULL, NULL) <= 0) {
            continue;
        }

        if (FD_ISSET(listen_fd, &rfds)) {
            struct sockaddr_in peer;
            socklen_t peer_len = sizeof(peer);
            int new_fd = accept(listen_fd, (struct sockaddr *)&peer, &peer_len);
            if (new_fd >= 0) {
                set_nodelay(new_fd);
                int old_fd = client_fd;
                client_fd = new_fd; /* new connection replaces old, not refused */
                if (old_fd >= 0) {
                    shutdown(old_fd, SHUT_RDWR);
                    close(old_fd);
                }
                ESP_LOGI(TAG, "client connected");
            }
        }

        fd = client_fd;
        if (fd >= 0 && FD_ISSET(fd, &rfds)) {
            int n = recv(fd, buf, sizeof(buf), 0);
            if (n > 0) {
                xStreamBufferSend(rx_stream, buf, (size_t)n, portMAX_DELAY);
            } else {
                close(fd);
                if (client_fd == fd) {
                    client_fd = -1;
                }
                ESP_LOGI(TAG, "client disconnected");
            }
        }
    }
}

/* Blocked in xStreamBufferReceive(portMAX_DELAY), draining to send(). lwIP
   permits send() and recv() on one fd from two tasks, so this and rx_task
   never contend. */
static void tx_task(void *arg) {
    (void)arg;
    uint8_t buf[HOST_RING_BYTES];

    for (;;) {
        size_t n = xStreamBufferReceive(tx_stream, buf, sizeof(buf), portMAX_DELAY);
        int fd = client_fd;
        if (fd < 0 || n == 0) {
            continue; /* no client to deliver to; drop, same as the wired links do on a full ring */
        }
        size_t sent = 0;
        while (sent < n) {
            int rc = send(fd, buf + sent, n - sent, 0);
            if (rc <= 0) {
                break; /* client gone; rx_task's select() notices and cleans up */
            }
            sent += (size_t)rc;
        }
    }
}

void host_link_wifi_begin(void) {
    rx_stream = xStreamBufferCreate(HOST_RING_BYTES, 1);
    tx_stream = xStreamBufferCreate(HOST_RING_BYTES, 1);
    xTaskCreate(rx_task, "host_link_wifi_rx", 4096, NULL, 5, NULL);
    xTaskCreate(tx_task, "host_link_wifi_tx", 4096, NULL, 5, NULL);
}

/*
 * Same contract host_link_usb_jtag.c and host_link_uart0.c keep: check space
 * up front, then write whole or drop whole, never block. protocol_poll()
 * emits replies from inside its own drain loop, so a blocking write here
 * would starve the watchdog refresh exactly as it would on either wired
 * link.
 */
void host_link_wifi_write(const uint8_t *data, size_t len) {
    if (tx_stream == NULL || xStreamBufferSpacesAvailable(tx_stream) < len) {
        return;
    }
    xStreamBufferSend(tx_stream, data, len, 0);
}

int host_link_wifi_read_byte(uint8_t *out) {
    if (rx_stream == NULL) {
        return 0;
    }
    if (xStreamBufferReceive(rx_stream, out, 1, 0) == 1) {
        rx_byte_count++;
        return 1;
    }
    return 0;
}

bool host_link_wifi_client_connected(void) {
    return client_fd >= 0;
}

uint32_t host_link_wifi_rx_byte_count(void) {
    return rx_byte_count;
}

#else /* !CONFIG_HOST_LINK_WIFI */

#include "host_link_wifi.h"

void host_link_wifi_begin(void) {}

void host_link_wifi_write(const uint8_t *data, size_t len) {
    (void)data;
    (void)len;
}

int host_link_wifi_read_byte(uint8_t *out) {
    (void)out;
    return 0;
}

bool host_link_wifi_client_connected(void) {
    return false;
}

uint32_t host_link_wifi_rx_byte_count(void) {
    return 0;
}

#endif
