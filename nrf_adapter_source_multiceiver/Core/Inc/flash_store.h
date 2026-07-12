#ifndef FLASH_STORE_H
#define FLASH_STORE_H

#include <stddef.h>
#include <stdint.h>

/*
 * Minimal replacement for the shipped firmware's MiniFlashDB-backed settings
 * (nrf_adapter_source/Modules/MiniFlashDB): a single reserved 1KB flash page
 * (see linker/STM32F030F4Px.ld SETTINGS_FLASH, last page of the 16KB part)
 * holds one record: [magic u32][payload][crc16]. NRF_SAVE erases + rewrites
 * it; boot reads it back if the magic/crc check out. No wear-leveling -- the
 * original didn't expose save as a high-frequency operation either (host
 * only calls it from the settings dialog), so page-erase endurance (~10k
 * cycles) is not a practical concern.
 */

int flash_store_load(void *payload, size_t len);
int flash_store_save(const void *payload, size_t len);

#endif
