#include "spi.h"
#include "platform.h"
#include "stm32f0xx.h"

/*
 * Reproduces nrf_adapter_source/Core/Src/spi.c: SPI1 master, mode 0, MSB
 * first, software NSS (CSN toggled as a plain GPIO in nrf24l01p.c), 8-bit
 * frames, BaudRatePrescaler /4 -> 48MHz/4 = 12MHz (matches
 * MDP_Adapter.ioc: SPI1.CalculateBaudRate=12.0 MBits/s).
 */
void spi_init(void) {
    RCC->APB2ENR |= RCC_APB2ENR_SPI1EN;

    SPI1->CR1 = SPI_CR1_MSTR | SPI_CR1_BR_0 /* /4 */ | SPI_CR1_SSM | SPI_CR1_SSI;
    SPI1->CR2 = (7U << SPI_CR2_DS_Pos) /* 8-bit */ | SPI_CR2_FRXTH;
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
