#ifndef PLATFORM_H
#define PLATFORM_H

/*
 * The entire contract between core/ (nrf24l01p.c, protocol.c) and the silicon.
 *
 * Every target under targets/ supplies a full implementation of these
 * functions; core/ contains no #ifdefs and no MCU headers, so work on a new
 * target cannot regress an existing one. On the STM32F030 target the
 * implementations live in the file that owns the peripheral (gpio.c, spi.c,
 * uart.c, system_clock.c, watchdog.c, flash_store.c, main.c) rather than in one
 * catch-all file.
 *
 * These are the operations the driver actually performs. Note in particular
 * that the nRF24 control lines are named operations, not a (port, mask) pair:
 * the STM32 GPIO port/mask concept does not survive the platform boundary.
 */

#include <stddef.h>
#include <stdint.h>

/* SPI: master, mode 0, MSB first, 8-bit frames. CSN is not touched here --
   nrf24l01p.c brackets each transaction with nrf_csn_low()/nrf_csn_high(). */
uint8_t spi_transfer_byte(uint8_t tx);
/* Full-duplex block transfer. tx==NULL sends 0xFF filler; rx==NULL discards received bytes. */
void spi_transfer(const uint8_t *tx, uint8_t *rx, size_t len);

/* nRF24L01+ control lines. */
void nrf_csn_low(void);
void nrf_csn_high(void);
void nrf_ce_low(void);
void nrf_ce_high(void);

/* Status LED. Purely cosmetic activity indication driven from the TX/RX paths
   in nrf24l01p.c -- a target with no spare pin may implement these as no-ops. */
void led_on(void);
void led_off(void);

/* Milliseconds since boot, free-running and wrap-safe for (now - then) use. */
uint32_t millis(void);
void delay_ms(uint32_t ms);

/* Host link. */
void host_link_write(const uint8_t *data, size_t len);
/* Pops one buffered RX byte. Returns 1 and fills *out if one was available, else 0. */
int host_link_read_byte(uint8_t *out);
/* No-op on targets whose host link has no configurable line rate (USB CDC).
   CMD_SET_BAUDRATE still ACKs and still persists the value in that case. */
void host_link_set_baudrate(uint32_t baudrate);

/* Network credentials and IP config, WiFi and Ethernet builds only. 0 =
   unsupported or failed, which is how a target says "this command does not
   apply to me" -- protocol.c answers REP_CMD_FAILED for it. Every target
   that carries neither feature stubs all three to return 0.
   ip/mask/gw are dotted-quad, low octet first, throughout. No dns field --
   nothing on either side of this link ever resolves a hostname: the adapter
   only ever accepts inbound connections (never dials out), and the GUI's own
   host-address field is resolved by the OS resolver on the PC running it,
   with no involvement from this protocol. Storing/reporting a DNS server
   here would be config the device stores but never reads.
   net_creds_save(): ssid/pass NULL clears the stored credentials rather than
   setting them. Applies live in addition to persisting, so a provisioning
   command connects without a reboot. WiFi only -- an Ethernet-only target
   stubs this to return 0.
   net_ip_config_save(): persists a static-IP configuration (or a switch back
   to DHCP). Teensy Ethernet only for now -- every other target, WiFi
   included, stubs this to return 0 and reports mode = DHCP from net_status().
   net_status(): fills state (0 disconnected, 1 connecting, 2 connected),
   mode (0 DHCP, 1 static), ip/mask/gw, rssi (dBm; 0 on a wired link) and
   ssid (NUL-terminated, up to 32 chars + terminator; empty on a wired link).
   ip/mask/gw/rssi/ssid are only meaningful when connected; returns 0
   only for "unsupported", not for "not yet connected". */
typedef struct {
    uint8_t mode; /* 0 DHCP, 1 static */
    uint8_t ip[4], mask[4], gw[4];
} net_ip_config_t;

typedef struct {
    uint8_t state; /* 0 disconnected, 1 connecting, 2 connected */
    uint8_t mode;
    uint8_t ip[4], mask[4], gw[4];
    int8_t rssi;    /* dBm; 0 on wired links */
    char ssid[33];  /* NUL-terminated; empty on wired links */
} net_status_t;

int net_creds_save(const char *ssid, const char *pass);
int net_ip_config_save(const net_ip_config_t *cfg);
int net_status(net_status_t *out);

/* Settings storage: one record, payload <= 32 bytes, integrity-checked.
   Both return 1 on success, 0 on failure/absent record. A save with len==0
   invalidates the stored record (CMD_RESET). */
int store_load(void *payload, size_t len);
int store_save(const void *payload, size_t len);

void watchdog_init(void);
void watchdog_refresh(void);

/* Does not return. */
void platform_reboot(void);

#endif
