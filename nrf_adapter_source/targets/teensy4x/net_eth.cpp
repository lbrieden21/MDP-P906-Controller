/*
 * Ethernet bring-up and IP-config storage. Owns the platform.h
 * net_creds_save()/net_ip_config_save()/net_status() hooks for the Teensy
 * 4.x target, plus net_eth_begin() (Teensy-private, called once from
 * platform_init()).
 *
 * Gated on HOST_LINK_ETH internally rather than by the Makefile's source
 * list, so every Teensy 4.x build links this file and gets a real
 * net_ip_config_save()/net_status() either way -- a non-Ethernet build just
 * gets the #else stub below, on the same footing as every other target's
 * stub.
 *
 * The static-IP record ({ magic u32 | mode u8 | ip[4] | mask[4] | gw[4] |
 * crc16 }, NET_STORE_ADDR=64, clear of store_record_t's fixed 40 bytes at
 * offset 0) round-trips identically regardless of HOST_LINK_ETH. Kept
 * separate from store_record_t because store_load() checks the stored
 * payload length for *exact* equality -- folding this in would silently
 * invalidate every saved radio setting on every board already flashed, and
 * CMD_RESET (which invalidates that record) must not also wipe the network
 * config.
 *
 * The #if HOST_LINK_ETH branch is what makes state ever leave
 * "disconnected": net_eth_begin() there brings QNEthernet up from this same
 * record, and net_status() reports its live state instead of always
 * reporting the record with no link.
 */

#include <Arduino.h>

#include "net_eth.h"
#include "platform_teensy4.h"

extern "C" {
#include "platform.h"
}

#define NET_STORE_ADDR 64
#define NET_STORE_MAGIC 0x4E455430U /* "NET0" */

typedef struct {
    uint32_t magic;
    uint8_t mode;
    uint8_t ip[4], mask[4], gw[4];
    uint16_t crc;
} net_store_record_t;

/* Shared by both branches' net_status(): true (and *out filled) if the
   record's magic and CRC check out. */
static bool net_store_load(net_store_record_t *out) {
    eeprom_read_block(out, (const void *)NET_STORE_ADDR, sizeof(*out));
    return out->magic == NET_STORE_MAGIC && out->crc == crc16_ccitt(&out->mode, 13);
}

/* No WiFi on this target, ever -- platform.h documents 0 as "this command
   does not apply to me" -- protocol.c answers REP_CMD_FAILED. Identical on
   both branches: Ethernet has no concept of station credentials either. */
extern "C" int net_creds_save(const char *ssid, const char *pass) {
    (void)ssid;
    (void)pass;
    return 0;
}

/* Identical on both branches -- the record round-trips through the emulated
   EEPROM whether or not there is a live link to apply it to. Not applied
   live: it takes effect the next time net_eth_begin() runs (boot or
   CMD_RESET), matching the workflow Phase 2 tested (set, power-cycle,
   confirm). */
extern "C" int net_ip_config_save(const net_ip_config_t *cfg) {
    if (cfg->mode > 1) {
        return 0;
    }

    net_store_record_t rec;
    rec.magic = NET_STORE_MAGIC;
    rec.mode = cfg->mode;
    for (int i = 0; i < 4; i++) {
        rec.ip[i] = cfg->ip[i];
        rec.mask[i] = cfg->mask[i];
        rec.gw[i] = cfg->gw[i];
    }
    rec.crc = crc16_ccitt(&rec.mode, 13); /* mode..gw, contiguous */

    eeprom_write_block(&rec, (void *)NET_STORE_ADDR, sizeof(rec));

    net_store_record_t check;
    eeprom_read_block(&check, (const void *)NET_STORE_ADDR, sizeof(check));
    if (check.magic != rec.magic || check.crc != rec.crc || check.mode != rec.mode) {
        return 0;
    }
    for (int i = 0; i < 4; i++) {
        if (check.ip[i] != rec.ip[i] || check.mask[i] != rec.mask[i] ||
            check.gw[i] != rec.gw[i]) {
            return 0;
        }
    }
    return 1;
}

#if defined(HOST_LINK_ETH)

#include <QNEthernet.h>

using namespace qindesign::network;

/* Never blocks: Ethernet.begin() (DHCP) and Ethernet.begin(ip, mask, gw)
   (static) both start asynchronously and return immediately -- neither
   waits for a lease or for link. watchdog_init() has already armed the
   3.5s timeout by the time platform_init() reaches this call, and the first
   refresh doesn't happen until loop() runs, so anything that blocked here
   would self-reset the board before Ethernet ever came up. */
void net_eth_begin(void) {
    net_store_record_t rec;
    if (net_store_load(&rec) && rec.mode == 1) {
        Ethernet.begin(IPAddress(rec.ip), IPAddress(rec.mask), IPAddress(rec.gw));
    } else {
        Ethernet.begin(); /* DHCP -- also the fallback for a blank/corrupt record */
    }
}

static void addr_to_bytes(const IPAddress &addr, uint8_t out[4]) {
    out[0] = addr[0];
    out[1] = addr[1];
    out[2] = addr[2];
    out[3] = addr[3];
}

/* Supported (returns 1) unconditionally, matching the non-Ethernet branch.
   mode always reflects the persisted record (what was configured), not the
   live link. state/ip/mask/gw report the live QNEthernet state once there is
   one; before that (or on a blank record) they fall back to the persisted
   values so a static config is visible even before DHCP/link finishes. */
extern "C" int net_status(net_status_t *out) {
    net_store_record_t rec;
    bool valid = net_store_load(&rec);
    out->mode = valid ? rec.mode : 0;
    out->rssi = 0;
    out->channel = 0;
    out->ssid[0] = '\0';

    bool link = Ethernet.linkState();
    IPAddress ip = Ethernet.localIP();
    bool have_ip = (uint32_t)ip != 0;
    out->state = !link ? 0 : (have_ip ? 2 : 1);

    if (have_ip) {
        addr_to_bytes(ip, out->ip);
        addr_to_bytes(Ethernet.subnetMask(), out->mask);
        addr_to_bytes(Ethernet.gatewayIP(), out->gw);
    } else if (valid) {
        for (int i = 0; i < 4; i++) {
            out->ip[i] = rec.ip[i];
            out->mask[i] = rec.mask[i];
            out->gw[i] = rec.gw[i];
        }
    } else {
        for (int i = 0; i < 4; i++) {
            out->ip[i] = out->mask[i] = out->gw[i] = 0;
        }
    }
    return 1;
}

#else

void net_eth_begin(void) {}

/* Supported (returns 1) as soon as the static-IP record can be read back,
   even with no Ethernet link ever having existed -- platform.h: "returns 0
   only for unsupported, not for not yet connected." state stays
   disconnected on a non-Ethernet build, since there is no link to report. */
extern "C" int net_status(net_status_t *out) {
    net_store_record_t rec;
    if (net_store_load(&rec)) {
        out->mode = rec.mode;
        for (int i = 0; i < 4; i++) {
            out->ip[i] = rec.ip[i];
            out->mask[i] = rec.mask[i];
            out->gw[i] = rec.gw[i];
        }
    } else {
        out->mode = 0;
        for (int i = 0; i < 4; i++) {
            out->ip[i] = out->mask[i] = out->gw[i] = 0;
        }
    }
    out->state = 0;
    out->rssi = 0;
    out->channel = 0;
    out->ssid[0] = '\0';
    return 1;
}

#endif
