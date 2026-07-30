#include "gpio.h"
#include "platform.h"
#include "protocol.h"

/*
 * Peripheral selection (PA2 IRQ, PA3/PA4 CSN/CE, PA5/6/7 SPI1, PA9/10
 * USART1) is deliberately identical to the shipped F030 dongle, so an
 * existing nRF24 harness plugs straight in -- only the LED moves, to the
 * Blue Pill's onboard PC13.
 *
 * F1's GPIO block is CRL/CRH (4-bit CNF+MODE per pin), not
 * MODER/OTYPER/OSPEEDR/AFR, and there is no per-pin AF number -- alternate
 * function is implicit per peripheral, so an AF pin used as an input (MISO,
 * RX) is configured as a plain input, not as "AF input".
 */

#define CRL_PIN_CLEAR(pin) (0xFUL << ((pin) * 4))
#define CRH_PIN_CLEAR(pin) (0xFUL << (((pin) - 8) * 4))

/* CNF[1:0]:MODE[1:0] nibbles, MODE in bits[1:0], CNF in bits[3:2]. */
#define CRX_MODE_INPUT 0x0U
#define CRX_MODE_OUTPUT_2MHZ 0x2U
#define CRX_MODE_OUTPUT_50MHZ 0x3U
#define CRX_CNF_IN_FLOATING (0x1U << 2)
#define CRX_CNF_IN_PUPD (0x2U << 2)
#define CRX_CNF_OUT_PP (0x0U << 2)
#define CRX_CNF_OUT_OD (0x1U << 2)
#define CRX_CNF_AF_PP (0x2U << 2)

void gpio_init(void) {
    RCC->APB2ENR |= RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPCEN | RCC_APB2ENR_AFIOEN;

    /* Idle levels before switching pins to outputs. */
    gpio_set(GPIOA, NRF_CSN_Pin);
    gpio_clear(GPIOA, NRF_CE_Pin);
    gpio_set(GPIOC, LED_Pin); /* LED is active-low; idle = off */

    /* PC13: onboard LED, open-drain output, 2MHz. Open-drain and the slow
       slew matter -- PC13 on these boards is current-limited. */
    GPIOC->CRH = (GPIOC->CRH & ~CRH_PIN_CLEAR(13)) |
                 ((CRX_CNF_OUT_OD | CRX_MODE_OUTPUT_2MHZ) << ((13 - 8) * 4));

    /* PA2: NRF IRQ, input with pull-up. CNF=10 selects pull-up/pull-down;
       ODR high picks pull-up over pull-down. */
    GPIOA->CRL = (GPIOA->CRL & ~CRL_PIN_CLEAR(2)) |
                 ((CRX_CNF_IN_PUPD | CRX_MODE_INPUT) << (2 * 4));
    GPIOA->ODR |= (1U << 2);

    /* PA3/PA4: CSN/CE, push-pull output, 2MHz. */
    for (int pin = 3; pin <= 4; pin++) {
        GPIOA->CRL = (GPIOA->CRL & ~CRL_PIN_CLEAR(pin)) |
                     ((CRX_CNF_OUT_PP | CRX_MODE_OUTPUT_2MHZ) << (pin * 4));
    }

    /* PA5/PA7: SPI1 SCK/MOSI, AF push-pull, 50MHz. PA6: MISO, input floating
       (an AF pin used as an input is configured as a plain input on F1 --
       there is no distinct "AF input" mode). */
    GPIOA->CRL = (GPIOA->CRL & ~CRL_PIN_CLEAR(5)) |
                 ((CRX_CNF_AF_PP | CRX_MODE_OUTPUT_50MHZ) << (5 * 4));
    GPIOA->CRL = (GPIOA->CRL & ~CRL_PIN_CLEAR(6)) |
                 ((CRX_CNF_IN_FLOATING | CRX_MODE_INPUT) << (6 * 4));
    GPIOA->CRL = (GPIOA->CRL & ~CRL_PIN_CLEAR(7)) |
                 ((CRX_CNF_AF_PP | CRX_MODE_OUTPUT_50MHZ) << (7 * 4));

    /* PA9: USART1 TX, AF push-pull, 50MHz. PA10: RX, input floating. */
    GPIOA->CRH = (GPIOA->CRH & ~CRH_PIN_CLEAR(9)) |
                 ((CRX_CNF_AF_PP | CRX_MODE_OUTPUT_50MHZ) << ((9 - 8) * 4));
    GPIOA->CRH = (GPIOA->CRH & ~CRH_PIN_CLEAR(10)) |
                 ((CRX_CNF_IN_FLOATING | CRX_MODE_INPUT) << ((10 - 8) * 4));

    /* EXTI2 -> PA2. Unlike the F0 (where SYSCFG_EXTICR defaults to GPIOA and
       is never touched), routing here goes through AFIO->EXTICR, and
       AFIOEN above is not optional for that register to take effect. */
    AFIO->EXTICR[0] &= ~AFIO_EXTICR1_EXTI2;
    EXTI->FTSR |= (1U << 2);
    EXTI->RTSR &= ~(1U << 2);
    EXTI->IMR |= (1U << 2);

    NVIC_SetPriority(EXTI2_IRQn, 3);
    NVIC_EnableIRQ(EXTI2_IRQn);
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

/* Lives here rather than in core/ because both the vector-table slot and the
   EXTI pending-bit handling are STM32-specific. Radio work still runs inside
   the ISR on this target -- see uart.c on why that is safe here (USART1 at
   NVIC priority 1 preempts EXTI2 at 3). Unlike the F030's shared
   EXTI2_3_IRQn, the F103 has a dedicated EXTI2_IRQn/EXTI2_IRQHandler. */
void EXTI2_IRQHandler(void) {
    if (EXTI->PR & (1U << 2)) {
        EXTI->PR = (1U << 2); /* write-1-to-clear */
        protocol_service_radio_irq();
    }
}
