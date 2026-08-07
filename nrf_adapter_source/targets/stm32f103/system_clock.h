#ifndef SYSTEM_CLOCK_H
#define SYSTEM_CLOCK_H

#define SYSTEM_CORE_CLOCK_HZ 72000000UL

/* Bring-up only. millis()/delay_ms() are declared in platform.h. */
void system_clock_init(void);
void systick_init(void);

#endif
