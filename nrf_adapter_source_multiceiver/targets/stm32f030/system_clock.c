#include "system_clock.h"
#include "platform.h"
#include "stm32f0xx.h"

/*
 * Reproduces the clock tree from nrf_adapter_source/Core/Src/main.c
 * SystemClock_Config(): HSI(8MHz) -> /2 -> PLL x12 -> 48MHz SYSCLK/HCLK/PCLK.
 * Confirmed against MDP_Adapter.ioc: RCC.PLLCLKFreq_Value=48000000.
 */
void SystemInit(void) {
    /* 1 wait state required above 24MHz (RM0360). Set before raising SYSCLK. */
    FLASH->ACR |= FLASH_ACR_LATENCY; /* 1-bit field on F0: 1 = one wait state */

    RCC->CR |= RCC_CR_HSION;
    while (!(RCC->CR & RCC_CR_HSIRDY)) {}

    RCC->CFGR = (RCC->CFGR & ~(RCC_CFGR_PLLSRC | RCC_CFGR_PLLMUL)) |
                RCC_CFGR_PLLSRC_HSI_DIV2 | RCC_CFGR_PLLMUL12;

    RCC->CR |= RCC_CR_PLLON;
    while (!(RCC->CR & RCC_CR_PLLRDY)) {}

    RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_SW) | RCC_CFGR_SW_PLL;
    while ((RCC->CFGR & RCC_CFGR_SWS) != RCC_CFGR_SWS_PLL) {}

    /* AHB/APB prescalers stay at reset default (/1): HCLK = PCLK = 48MHz. */
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
