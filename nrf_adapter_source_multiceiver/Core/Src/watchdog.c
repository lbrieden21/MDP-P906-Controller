#include "watchdog.h"
#include "stm32f0xx.h"

/* Reproduces nrf_adapter_source/Core/Src/iwdg.c: prescaler /32, reload 4095,
   no window (window=reload disables the window feature) -> ~3.3s timeout on
   the ~40kHz LSI, refreshed every 100ms from the main loop same as before.
   Start (KR=0xCCCC) must come first: that's what forces LSI on per RM0360 --
   PR/RLR writes need LSI running to ever sync into IWDG_SR, so waiting on
   IWDG_SR before starting hangs forever. (Confirmed via live GDB backtrace:
   the very first boot was permanently stuck in this function's SR wait.) */
void watchdog_init(void) {
    IWDG->KR = 0xCCCCU; /* start -- forces LSI on */
    IWDG->KR = 0x5555U; /* unlock PR/RLR write access */
    IWDG->PR = 3U;      /* /32 */
    IWDG->RLR = 4095U;
    while (IWDG->SR != 0U) {}
    IWDG->KR = 0xAAAAU; /* reload */
}

void watchdog_refresh(void) {
    IWDG->KR = 0xAAAAU;
}
