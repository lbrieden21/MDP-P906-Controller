#ifndef GPIO_H
#define GPIO_H

#include "stm32f0xx.h"

#define GPIO_PIN_0 (1U << 0)
#define GPIO_PIN_2 (1U << 2)
#define GPIO_PIN_3 (1U << 3)
#define GPIO_PIN_4 (1U << 4)
#define GPIO_PIN_5 (1U << 5)
#define GPIO_PIN_6 (1U << 6)
#define GPIO_PIN_7 (1U << 7)
#define GPIO_PIN_9 (1U << 9)
#define GPIO_PIN_10 (1U << 10)

/* Pin mapping, recovered from nrf_adapter_source/Core/Inc/main.h (shipped HAL firmware) */
#define LED_Pin GPIO_PIN_0
#define LED_GPIO_Port GPIOA
#define NRF_IRQ_Pin GPIO_PIN_2
#define NRF_IRQ_GPIO_Port GPIOA
#define NRF_CSN_Pin GPIO_PIN_3
#define NRF_CSN_GPIO_Port GPIOA
#define NRF_CE_Pin GPIO_PIN_4
#define NRF_CE_GPIO_Port GPIOA
/* SPI1: PA5 SCK, PA6 MISO, PA7 MOSI (AF0). USART1: PA9 TX, PA10 RX (AF1). */

void gpio_init(void);
static inline void gpio_set(GPIO_TypeDef *port, uint32_t pin_mask) {
    port->BSRR = pin_mask;
}
static inline void gpio_clear(GPIO_TypeDef *port, uint32_t pin_mask) {
    port->BSRR = pin_mask << 16;
}

#endif
