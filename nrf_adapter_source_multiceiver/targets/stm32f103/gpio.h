#ifndef GPIO_H
#define GPIO_H

/* STM32F103-target-private. The port/mask concept does not cross the
   platform.h boundary -- core/ sees only nrf_csn_low() and friends, which
   gpio.c implements in terms of these. */

#include "stm32f1xx.h"

#define GPIO_PIN_2 (1U << 2)
#define GPIO_PIN_3 (1U << 3)
#define GPIO_PIN_4 (1U << 4)
#define GPIO_PIN_5 (1U << 5)
#define GPIO_PIN_6 (1U << 6)
#define GPIO_PIN_7 (1U << 7)
#define GPIO_PIN_9 (1U << 9)
#define GPIO_PIN_10 (1U << 10)
#define GPIO_PIN_13 (1U << 13)

/* Pin mapping -- see targets/stm32f103/gpio.c for the wiring rationale
   (LED moves to the onboard PC13, everything else matches the F030 dongle). */
#define LED_Pin GPIO_PIN_13
#define LED_GPIO_Port GPIOC
#define NRF_IRQ_Pin GPIO_PIN_2
#define NRF_IRQ_GPIO_Port GPIOA
#define NRF_CSN_Pin GPIO_PIN_3
#define NRF_CSN_GPIO_Port GPIOA
#define NRF_CE_Pin GPIO_PIN_4
#define NRF_CE_GPIO_Port GPIOA
/* SPI1: PA5 SCK, PA6 MISO, PA7 MOSI. USART1: PA9 TX, PA10 RX. No remap on
   either peripheral, so no AFIO_MAPR write is needed. */

void gpio_init(void);
static inline void gpio_set(GPIO_TypeDef *port, uint32_t pin_mask) {
    port->BSRR = pin_mask;
}
static inline void gpio_clear(GPIO_TypeDef *port, uint32_t pin_mask) {
    port->BSRR = pin_mask << 16;
}

#endif
