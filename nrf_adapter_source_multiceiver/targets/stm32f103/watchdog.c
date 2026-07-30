#include "platform.h"
#include "stm32f1xx.h"

/* Prescaler /32, reload 4095, no window (window=reload disables the window
   feature) -> ~3.3s timeout on the ~40kHz LSI, refreshed every 100ms from
   the main loop.
   Start (KR=0xCCCC) must come first: that's what forces LSI on --
   PR/RLR writes need LSI running to ever sync into IWDG_SR, so waiting on
   IWDG_SR before starting hangs forever. */
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
