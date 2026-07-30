#include "spi.h"
#include "platform.h"
#include "stm32f1xx.h"

/*
 * SPI1 master, mode 0, MSB first, software NSS (CSN toggled as a plain GPIO
 * in nrf24l01p.c).
 *
 * This SPI IP has no CR2_DS/FRXTH RX FIFO threshold, so 8-bit frames come
 * from DFF=0 (the reset default) instead of an explicit CR2 write. BR_1
 * divides the 72MHz PCLK2 by 8 -> 9MHz, deliberately under the nRF24L01+'s
 * 10MHz ceiling rather than the faster-but-out-of-spec prescaler a naive
 * max-baud choice would pick.
 */
void spi_init(void) {
    RCC->APB2ENR |= RCC_APB2ENR_SPI1EN;

    SPI1->CR1 = SPI_CR1_MSTR | SPI_CR1_BR_1 /* /8 -> 9MHz */ | SPI_CR1_SSM | SPI_CR1_SSI;
    SPI1->CR1 |= SPI_CR1_SPE;
}

static inline uint8_t spi_dr_byte(void) {
    return *(volatile uint8_t *)&SPI1->DR;
}

uint8_t spi_transfer_byte(uint8_t tx) {
    while (!(SPI1->SR & SPI_SR_TXE)) {}
    *(volatile uint8_t *)&SPI1->DR = tx;
    while (!(SPI1->SR & SPI_SR_RXNE)) {}
    return spi_dr_byte();
}

void spi_transfer(const uint8_t *tx, uint8_t *rx, size_t len) {
    for (size_t i = 0; i < len; i++) {
        uint8_t out = tx ? tx[i] : 0xFFU;
        uint8_t in = spi_transfer_byte(out);
        if (rx) {
            rx[i] = in;
        }
    }
}
