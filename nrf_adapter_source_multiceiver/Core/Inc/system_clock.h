#ifndef SYSTEM_CLOCK_H
#define SYSTEM_CLOCK_H

#include <stdint.h>

#define SYSTEM_CORE_CLOCK_HZ 48000000UL

void system_clock_init(void);
void systick_init(void);
uint32_t millis(void);
void delay_ms(uint32_t ms);

#endif
