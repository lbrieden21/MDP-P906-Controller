#include "uart.h"
#include "platform.h"
#include "stm32f0xx.h"
#include "system_clock.h"

/*
 * Reproduces nrf_adapter_source/Core/Src/usart.c's USART1 config (8N1,
 * default 921600 baud, PA9/10) but IT-driven ring buffers instead of the
 * original's DMA + idle-line detection -- simpler bare-metal, no DMA driver
 * needed.
 *
 * TX is interrupt-driven (TXE), not blocking: uart_send_packet() in
 * protocol.c can run from inside the nRF24 EXTI ISR (nrf_rx_done/
 * nrf_tx_done are called from nrf24l01p_irq()), and a blocking polled TX
 * there would add UART-frame-time latency (tens of us) to every nRF24 IRQ,
 * right on the path the refactor plan calls out as timing-sensitive (50Hz
 * Type-8 polling). USART1 is NVIC priority 1, higher than EXTI2_3's
 * priority 3, so USART1's TXE/RXNE ISR can always preempt and drain these
 * rings even while EXTI2_3's handler is still running -- that's what makes
 * uart_write()'s spin-wait-for-ring-space safe to call from there.
 */

#define RX_RING_SIZE 256
static volatile uint8_t rx_ring[RX_RING_SIZE];
static volatile uint16_t rx_head;
static volatile uint16_t rx_tail;

#define TX_RING_SIZE 256
static volatile uint8_t tx_ring[TX_RING_SIZE];
static volatile uint16_t tx_head;
static volatile uint16_t tx_tail;

static void uart_apply_brr(uint32_t baudrate) {
    /* 16x oversampling (CR1.OVER8=0, the reset default and what the shipped
       firmware used): USARTDIV = round(PCLK / baud). */
    USART1->BRR = (SYSTEM_CORE_CLOCK_HZ + (baudrate / 2)) / baudrate;
}

void uart_init(uint32_t baudrate) {
    RCC->APB2ENR |= RCC_APB2ENR_USART1EN;

    USART1->CR1 = 0;
    uart_apply_brr(baudrate);
    USART1->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE | USART_CR1_RXNEIE;

    NVIC_SetPriority(USART1_IRQn, 1);
    NVIC_EnableIRQ(USART1_IRQn);
}

void uart_set_baudrate(uint32_t baudrate) {
    USART1->CR1 &= ~USART_CR1_UE;
    uart_apply_brr(baudrate);
    USART1->CR1 |= USART_CR1_UE;
}

void uart_write(const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        uint16_t next;
        while ((next = (uint16_t)((tx_head + 1) % TX_RING_SIZE)) == tx_tail) {}
        tx_ring[tx_head] = data[i];
        tx_head = next;
    }
    USART1->CR1 |= USART_CR1_TXEIE;
}

int uart_read_byte(uint8_t *out) {
    if (rx_head == rx_tail) {
        return 0;
    }
    *out = rx_ring[rx_tail];
    rx_tail = (rx_tail + 1) % RX_RING_SIZE;
    return 1;
}

void USART1_IRQHandler(void) {
    if (USART1->ISR & USART_ISR_RXNE) {
        uint8_t b = (uint8_t)USART1->RDR; /* read clears RXNE */
        uint16_t next = (uint16_t)((rx_head + 1) % RX_RING_SIZE);
        if (next != rx_tail) {
            rx_ring[rx_head] = b;
            rx_head = next;
        } /* else: ring full, drop byte */
    }
    if ((USART1->CR1 & USART_CR1_TXEIE) && (USART1->ISR & USART_ISR_TXE)) {
        if (tx_tail != tx_head) {
            USART1->TDR = tx_ring[tx_tail];
            tx_tail = (uint16_t)((tx_tail + 1) % TX_RING_SIZE);
        } else {
            USART1->CR1 &= ~USART_CR1_TXEIE;
        }
    }
    /* Clear any error flags (ORE/FE/NE/PE) so USART1 doesn't wedge. */
    if (USART1->ISR & (USART_ISR_ORE | USART_ISR_FE | USART_ISR_NE | USART_ISR_PE)) {
        USART1->ICR = USART_ICR_ORECF | USART_ICR_FECF | USART_ICR_NCF | USART_ICR_PECF;
    }
}
