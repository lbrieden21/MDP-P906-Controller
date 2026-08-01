#include "gpio.h"
#include "platform.h"

/*
 * Reproduces nrf_adapter_source/Core/Src/gpio.c + spi.c/usart.c MspInit pin
 * config, minus HAL: PA0 LED (open-drain out), PA2 NRF IRQ (input, pull-up,
 * falling-edge EXTI), PA3 CSN / PA4 CE (push-pull out), PA5/6/7 SPI1 AF0,
 * PA9/10 USART1 AF1.
 */
void gpio_init(void) {
    RCC->AHBENR |= RCC_AHBENR_GPIOAEN;
    RCC->APB2ENR |= RCC_APB2ENR_SYSCFGEN; /* needed for the EXTI2->PA line mux, even at its reset default */

    /* Idle levels before switching pins to outputs, matching shipped firmware. */
    gpio_set(GPIOA, LED_Pin | NRF_CSN_Pin);
    gpio_clear(GPIOA, NRF_CE_Pin);

    /* PA0: LED, open-drain output, low speed */
    GPIOA->MODER = (GPIOA->MODER & ~(3U << (0 * 2))) | (1U << (0 * 2));
    GPIOA->OTYPER |= (1U << 0);
    GPIOA->OSPEEDR &= ~(3U << (0 * 2));

    /* PA2: NRF IRQ, input, pull-up */
    GPIOA->MODER &= ~(3U << (2 * 2));
    GPIOA->PUPDR = (GPIOA->PUPDR & ~(3U << (2 * 2))) | (1U << (2 * 2));

    /* PA3/PA4: CSN/CE, push-pull output, low speed, no pull */
    for (int pin = 3; pin <= 4; pin++) {
        GPIOA->MODER = (GPIOA->MODER & ~(3U << (pin * 2))) | (1U << (pin * 2));
        GPIOA->OTYPER &= ~(1U << pin);
        GPIOA->OSPEEDR &= ~(3U << (pin * 2));
        GPIOA->PUPDR &= ~(3U << (pin * 2));
    }

    /* PA5/6/7: SPI1 SCK/MISO/MOSI, AF0, push-pull, high speed */
    for (int pin = 5; pin <= 7; pin++) {
        GPIOA->MODER = (GPIOA->MODER & ~(3U << (pin * 2))) | (2U << (pin * 2));
        GPIOA->OTYPER &= ~(1U << pin);
        GPIOA->OSPEEDR |= (3U << (pin * 2));
        GPIOA->PUPDR &= ~(3U << (pin * 2));
        GPIOA->AFR[0] &= ~(0xFU << (pin * 4));
    }

    /* PA9/10: USART1 TX/RX, AF1, push-pull, high speed */
    for (int pin = 9; pin <= 10; pin++) {
        GPIOA->MODER = (GPIOA->MODER & ~(3U << (pin * 2))) | (2U << (pin * 2));
        GPIOA->OTYPER &= ~(1U << pin);
        GPIOA->OSPEEDR |= (3U << (pin * 2));
        GPIOA->PUPDR &= ~(3U << (pin * 2));
        GPIOA->AFR[1] = (GPIOA->AFR[1] & ~(0xFU << ((pin - 8) * 4))) |
                        (1U << ((pin - 8) * 4));
    }

    /* EXTI2: falling edge on PA2 (SYSCFG default already routes EXTIx to
       GPIOA on reset, matching the shipped firmware which never touches
       SYSCFG_EXTICR either). */
    EXTI->FTSR |= (1U << 2);
    EXTI->RTSR &= ~(1U << 2);
    EXTI->IMR |= (1U << 2);

    NVIC_SetPriority(EXTI2_3_IRQn, 3);
    NVIC_EnableIRQ(EXTI2_3_IRQn);
}

/* platform.h nRF24 control lines and status LED. The LED is active-low. */
void nrf_csn_low(void) {
    gpio_clear(NRF_CSN_GPIO_Port, NRF_CSN_Pin);
}
void nrf_csn_high(void) {
    gpio_set(NRF_CSN_GPIO_Port, NRF_CSN_Pin);
}
void nrf_ce_low(void) {
    gpio_clear(NRF_CE_GPIO_Port, NRF_CE_Pin);
}
void nrf_ce_high(void) {
    gpio_set(NRF_CE_GPIO_Port, NRF_CE_Pin);
}
void led_on(void) {
    gpio_clear(LED_GPIO_Port, LED_Pin);
}
void led_off(void) {
    gpio_set(LED_GPIO_Port, LED_Pin);
}

/* ---------------------------------------------------------------- radio IRQ */

/* No radio work happens in interrupt context: the ISR clears the EXTI pending
   bit and latches the edge, and main()'s loop calls
   protocol_service_radio_irq(). The handler lives here rather than in core/
   because both the vector-table slot and the pending-bit handling are
   STM32-specific. EXTI2_3_IRQn is shared across lines 2 and 3 on this part;
   only line 2 is enabled. */
static volatile bool radio_irq_flag = false;

void EXTI2_3_IRQHandler(void) {
    if (EXTI->PR & (1U << 2)) {
        EXTI->PR = (1U << 2); /* write-1-to-clear */
        radio_irq_flag = true;
    }
}

/* The level recheck is not belt-and-braces. Reading then clearing the flag is
   not atomic, so an edge landing between the two is lost; and EXTI is
   edge-triggered while the nRF24 holds IRQ low until its STATUS bits are
   cleared, so back-to-back events produce only one edge. Both holes close the
   same way -- a missed edge still leaves the pin low, so the next call picks
   it up. */
bool radio_irq_pending(void) {
    bool edge = radio_irq_flag;
    radio_irq_flag = false;
    return edge || (NRF_IRQ_GPIO_Port->IDR & NRF_IRQ_Pin) == 0;
}
