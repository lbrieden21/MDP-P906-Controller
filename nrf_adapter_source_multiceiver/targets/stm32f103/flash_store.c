#include "platform.h"
#include "stm32f1xx.h"

/*
 * platform.h's store_load/store_save for the STM32F103 target: a single
 * reserved 1KB flash page (see linker/STM32F103C8Tx.ld SETTINGS_FLASH, the
 * last page of the 63K code region) holds one record:
 * [magic u32][payload][crc16]. NRF_SAVE erases + rewrites it; boot reads it
 * back if the magic/crc check out.
 *
 * The record layout, crc16_ccitt() and the read-back verify must stay
 * bit-identical across every target -- a settings blob has to be portable
 * regardless of which board it was saved on.
 */

/* Linker symbol marking the SETTINGS_FLASH page origin (see
   linker/STM32F103C8Tx.ld); declared as an unsized array so GCC doesn't
   assume a 4-byte object when we take its address and treat it as a
   40-byte record (-Warray-bounds false positive otherwise). */
extern uint32_t _settings_flash_page[];

#define STORE_ADDR ((uint32_t)_settings_flash_page)
#define STORE_MAGIC 0x50393036U /* "P906" */
#define STORE_MAX_PAYLOAD 32

typedef struct {
    uint32_t magic;
    uint16_t len;
    uint8_t payload[STORE_MAX_PAYLOAD];
    uint16_t crc;
} store_record_t;

static uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static void flash_unlock(void) {
    if (FLASH->CR & FLASH_CR_LOCK) {
        FLASH->KEYR = FLASH_KEY1;
        FLASH->KEYR = FLASH_KEY2;
    }
}

static void flash_lock(void) {
    FLASH->CR |= FLASH_CR_LOCK;
}

static void flash_wait_busy(void) {
    while (FLASH->SR & FLASH_SR_BSY) {}
}

int store_load(void *payload, size_t len) {
    if (len > STORE_MAX_PAYLOAD) {
        return 0;
    }
    const store_record_t *rec = (const store_record_t *)STORE_ADDR;
    if (rec->magic != STORE_MAGIC || rec->len != len) {
        return 0;
    }
    if (crc16_ccitt(rec->payload, len) != rec->crc) {
        return 0;
    }
    for (size_t i = 0; i < len; i++) {
        ((uint8_t *)payload)[i] = rec->payload[i];
    }
    return 1;
}

static void flash_program_halfword(uint32_t addr, uint16_t value) {
    FLASH->CR |= FLASH_CR_PG;
    *(volatile uint16_t *)addr = value;
    flash_wait_busy();
    FLASH->CR &= ~FLASH_CR_PG;
}

int store_save(const void *payload, size_t len) {
    if (len > STORE_MAX_PAYLOAD) {
        return 0;
    }

    store_record_t rec;
    rec.magic = STORE_MAGIC;
    rec.len = (uint16_t)len;
    for (size_t i = 0; i < STORE_MAX_PAYLOAD; i++) {
        rec.payload[i] = (i < len) ? ((const uint8_t *)payload)[i] : 0;
    }
    rec.crc = crc16_ccitt(rec.payload, len);

    flash_unlock();

    flash_wait_busy();
    FLASH->CR |= FLASH_CR_PER;
    FLASH->AR = STORE_ADDR;
    FLASH->CR |= FLASH_CR_STRT;
    flash_wait_busy();
    FLASH->CR &= ~FLASH_CR_PER;

    const uint16_t *src = (const uint16_t *)&rec;
    size_t words = sizeof(rec) / 2;
    for (size_t i = 0; i < words; i++) {
        flash_program_halfword(STORE_ADDR + i * 2, src[i]);
    }

    flash_lock();

    uint8_t check[STORE_MAX_PAYLOAD];
    if (!store_load(check, len)) {
        return 0;
    }
    for (size_t i = 0; i < len; i++) {
        if (check[i] != ((const uint8_t *)payload)[i]) {
            return 0;
        }
    }
    return 1;
}

/* No WiFi on this target. platform.h documents 0 as "this command does not
   apply to me" -- protocol.c answers REP_CMD_FAILED. */
int wifi_creds_save(const char *ssid, const char *pass) {
    (void)ssid;
    (void)pass;
    return 0;
}

int wifi_status(uint8_t *state, uint8_t ip[4], int8_t *rssi, char ssid[33]) {
    (void)state;
    (void)ip;
    (void)rssi;
    (void)ssid;
    return 0;
}
