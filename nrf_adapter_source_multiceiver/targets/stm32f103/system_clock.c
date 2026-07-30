#include "system_clock.h"
#include "platform.h"
#include "stm32f1xx.h"

/*
 * Blue Pill boards carry an 8MHz HSE crystal: HSE -> PLL x9 -> 72MHz
 * SYSCLK/HCLK, APB2 /1 (PCLK2 = 72MHz), APB1 /2 (PCLK1 = 36MHz, its
 * architectural maximum). Two flash wait states are required above 48MHz
 * (RM0008), set before raising SYSCLK.
 */
void SystemInit(void) {
    /* LATENCY[2:0] = 010 (2 wait states, required for 48MHz < HCLK <= 72MHz per
       RM0008) -- that encoding is bit1 alone, i.e. the FLASH_ACR_LATENCY_1
       macro (CMSIS names these by bit position, not by wait-state count). */
    FLASH->ACR = FLASH_ACR_PRFTBE | FLASH_ACR_LATENCY_1;

    RCC->CR |= RCC_CR_HSEON;
    while (!(RCC->CR & RCC_CR_HSERDY)) {}

    RCC->CFGR = (RCC->CFGR & ~(RCC_CFGR_PPRE1 | RCC_CFGR_PLLSRC | RCC_CFGR_PLLXTPRE | RCC_CFGR_PLLMULL)) |
                RCC_CFGR_PPRE1_DIV2 |
                RCC_CFGR_PLLSRC /* HSE selected as PLL entry */ |
                (RCC_CFGR_PLLMULL_0 | RCC_CFGR_PLLMULL_1 | RCC_CFGR_PLLMULL_2); /* x9 */

    RCC->CR |= RCC_CR_PLLON;
    while (!(RCC->CR & RCC_CR_PLLRDY)) {}

    RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_SW) | RCC_CFGR_SW_PLL;
    while ((RCC->CFGR & RCC_CFGR_SWS) != RCC_CFGR_SWS_PLL) {}

    /* AHB and APB2 prescalers stay at reset default (/1): HCLK = PCLK2 = 72MHz. */
}

void system_clock_init(void) {
    /* Clock tree is already live by the time main() runs (SystemInit ran from
       Reset_Handler); kept as a distinct call for readability at the main.c call site. */
}

static volatile uint32_t s_tick_ms;

void SysTick_Handler(void) {
    s_tick_ms++;
}

void systick_init(void) {
    SysTick->LOAD = (SYSTEM_CORE_CLOCK_HZ / 1000U) - 1U;
    SysTick->VAL = 0;
    SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk | SysTick_CTRL_TICKINT_Msk |
                    SysTick_CTRL_ENABLE_Msk;
}

uint32_t millis(void) {
    return s_tick_ms;
}

void delay_ms(uint32_t ms) {
    uint32_t start = millis();
    while (millis() - start < ms) {}
}
