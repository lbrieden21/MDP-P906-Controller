#ifndef SPI_H
#define SPI_H

#include <stddef.h>
#include <stdint.h>

void spi1_init(void);
uint8_t spi1_transfer_byte(uint8_t tx);
/* Full-duplex block transfer. tx==NULL sends 0xFF filler; rx==NULL discards received bytes. */
void spi1_transfer(const uint8_t *tx, uint8_t *rx, size_t len);

#endif
