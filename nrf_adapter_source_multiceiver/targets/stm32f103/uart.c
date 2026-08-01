#include "uart.h"
#include "platform.h"
#include "stm32f1xx.h"
#include "system_clock.h"

/*
 * USART1, 8N1, IT-driven ring buffers. TX is interrupt-driven rather than
 * blocking so uart_write() hands off a frame in bounded time instead of
 * stalling the main loop for the whole UART frame time.
 *
 * USART1 sits at NVIC priority 1 against EXTI2's 3. That relationship used to
 * be load-bearing: protocol_service_radio_irq() ran inside the EXTI handler,
 * so uart_write()'s spin-wait-for-ring-space depended on the UART ISR being
 * able to preempt it. The radio is now serviced from the main loop (gpio.c),
 * which means nothing spin-waits from interrupt context and the ordering is
 * merely harmless. It is kept as-is rather than reset to the default.
 *
 * This USART generation has SR/DR and no ICR at all -- clearing an error
 * flag is a read of SR followed by a read of DR.
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
    /* 16x oversampling (CR1.OVER8 does not exist on F1 -- it is always 16x):
       USARTDIV = round(PCLK2 / baud). The mantissa+fraction BRR layout is
       the same encoding as the F0, so this one-liner carries over unchanged. */
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
    if (USART1->SR & USART_SR_RXNE) {
        uint8_t b = (uint8_t)USART1->DR; /* read clears RXNE */
        uint16_t next = (uint16_t)((rx_head + 1) % RX_RING_SIZE);
        if (next != rx_tail) {
            rx_ring[rx_head] = b;
            rx_head = next;
        } /* else: ring full, drop byte */
    }
    if ((USART1->CR1 & USART_CR1_TXEIE) && (USART1->SR & USART_SR_TXE)) {
        if (tx_tail != tx_head) {
            USART1->DR = tx_ring[tx_tail];
            tx_tail = (uint16_t)((tx_tail + 1) % TX_RING_SIZE);
        } else {
            USART1->CR1 &= ~USART_CR1_TXEIE;
        }
    }
    /* Clear any error flags (ORE/FE/NE/PE) so USART1 doesn't wedge. There is
       no ICR on F1 -- a read of SR (already done above) followed by a read
       of DR clears them. */
    if (USART1->SR & (USART_SR_ORE | USART_SR_FE | USART_SR_NE | USART_SR_PE)) {
        (void)USART1->DR;
    }
}
