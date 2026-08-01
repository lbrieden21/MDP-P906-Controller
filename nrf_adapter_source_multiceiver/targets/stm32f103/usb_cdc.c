#include "usb_cdc.h"

#include "platform.h"
#include "stm32f1xx.h"
#include "tusb.h"

/*
 * Native USB CDC host link over the Blue Pill's onboard USB port -- the
 * HOST_LINK_USB_CDC alternative to uart.c's USART1 path. This file implements
 * the same three platform.h host-link functions and nothing else; core/ cannot
 * tell the two apart.
 *
 * The one real behavioural difference from uart.c is in host_link_write(): on
 * USART1 the TX ring always drains, because the wire is always clocking. On
 * CDC the host may simply stop reading, at which point the FIFO fills and
 * stays full. See the deadline discussion there.
 */

/* The F103 has exactly one USB device controller. */
#define USB_RHPORT 0

/* PA12 is USB D+. The Blue Pill wires its 1.5k pull-up to D+ permanently --
   the F103 has no internal pull-up control, so TinyUSB's dcd_connect()/
   dcd_disconnect() are compiled out on this part and the stack cannot detach
   for us. Without help the host therefore never sees a disconnect across an
   MCU reset and has no reason to re-enumerate. Driving D+ low briefly before
   the peripheral takes the pin over fakes that detach. This is what makes a
   reset (including a watchdog reset, checklist step 7) come back as a fresh
   tty instead of a wedged one. */
#define USB_DP_PIN 12
#define USB_DP_DISCONNECT_MS 5

/* CRH nibble encodings, matching gpio.c's. */
#define CRH_NIBBLE(pin) (((pin) - 8) * 4)
#define CRH_OUTPUT_PP_2MHZ 0x2UL  /* CNF=00 push-pull, MODE=10 */
#define CRH_INPUT_FLOATING 0x4UL  /* CNF=01 floating,  MODE=00 */

static void usb_dp_disconnect_pulse(void) {
    GPIOA->CRH = (GPIOA->CRH & ~(0xFUL << CRH_NIBBLE(USB_DP_PIN))) |
                 (CRH_OUTPUT_PP_2MHZ << CRH_NIBBLE(USB_DP_PIN));
    GPIOA->BSRR = (1U << USB_DP_PIN) << 16; /* drive low */
    delay_ms(USB_DP_DISCONNECT_MS);

    /* Hand the pin back. The USB peripheral drives PA11/PA12 itself once
       enabled; on F1 that requires no alternate-function selection, only that
       the pins are not held as GPIO outputs. */
    GPIOA->CRH = (GPIOA->CRH & ~(0xFUL << CRH_NIBBLE(USB_DP_PIN))) |
                 (CRH_INPUT_FLOATING << CRH_NIBBLE(USB_DP_PIN));
}

void usb_cdc_init(void) {
    /* The USB peripheral needs exactly 48MHz and has no divider of its own
       beyond USBPRE. system_clock.c runs the PLL at 72MHz, so USBPRE=0
       (PLL/1.5) is the only setting that yields 48MHz. That is also the reset
       value; it is written explicitly so the requirement is visible here
       rather than implied by silence. */
    RCC->CFGR &= ~RCC_CFGR_USBPRE;
    RCC->APB1ENR |= RCC_APB1ENR_USBEN;

    usb_dp_disconnect_pulse();

    /* Priority must be set before tusb_init(): the fsdev driver's
       dcd_int_enable() enables both USB NVIC lines itself as the last step of
       device-stack init, so there is no later window in which the line is
       enabled but still at its reset priority.
       2 sits below USART1's 1 (unused in this build) and above EXTI2's 3.
       Preempting EXTI2 is harmless -- that handler only latches a flag, and
       gpio.c's radio_irq_pending() rechecks the IRQ level, so a delayed or
       coalesced edge is recovered rather than lost. SysTick stays at its reset
       priority of 0 and preempts USB, which keeps millis() -- and therefore
       the write deadline below and the watchdog cadence in main() -- accurate
       regardless of how long the stack spends in its ISR. */
    NVIC_SetPriority(USB_HP_CAN1_TX_IRQn, 2);
    NVIC_SetPriority(USB_LP_CAN1_RX0_IRQn, 2);

    /* tusb_init(rhport, rh_init), not the older tud_init(rhport) -- the latter
       is deprecated in TinyUSB 0.21.0 and warns. */
    const tusb_rhport_init_t rh_init = {
        .role = TUSB_ROLE_DEVICE,
        .speed = TUSB_SPEED_FULL,
    };
    tusb_init(USB_RHPORT, &rh_init);
}

void usb_cdc_task(void) {
    tud_task();
}

/* The fsdev driver services both USB vectors. startup.c already carries both
   slots as WEAK_ALIAS entries, so defining the handlers here overrides them
   and no vector-table edit is needed. */
void USB_HP_CAN1_TX_IRQHandler(void) {
    tud_int_handler(USB_RHPORT);
}

void USB_LP_CAN1_RX0_IRQHandler(void) {
    tud_int_handler(USB_RHPORT);
}

/* ---------------------------------------------------------------- platform.h */

void host_link_write(const uint8_t *data, size_t len) {
    /* Nothing is enumerated yet: there is no host to receive this, and
       buffering it would only mean delivering stale telemetry to whoever
       connects later. Drop it. */
    if (!tud_mounted()) {
        return;
    }

    /* All-or-nothing, and never blocking. Both halves of that are load-bearing.
     *
     * Never blocking: protocol_poll() (core/protocol.c) is an unbounded drain --
     * `while (host_link_read_byte(&b)) feed_byte(b);` -- and feed_byte() calls back
     * into host_link_write() for each dispatched command. An earlier version of this
     * function waited for FIFO space under a 20ms deadline and pumped
     * tud_task() while waiting. That pump is also what refills the CDC *RX*
     * FIFO, so with a host that writes without reading, host_link_read_byte() never
     * ran dry, protocol_poll() never returned, and main()'s watchdog refresh
     * was starved until the IWDG fired at ~3.2s. Measured on hardware: the
     * board rebooted mid-flood. uart.c cannot hit this because its TX ring
     * always drains at line rate, so the wait is never long enough for the
     * host to outrun the drain. Not pumping here breaks the loop: the RX FIFO
     * is refilled only by main()'s usb_cdc_task(), so protocol_poll() is
     * guaranteed to run dry and return.
     *
     * All-or-nothing: a partial write would put a truncated frame on the wire
     * for the host framer to resync past. Dropping the whole frame instead
     * means the host only ever sees complete frames or nothing.
     *
     * What this costs: a frame produced while the FIFO is full is dropped
     * outright rather than waited on. In normal operation the FIFO does not
     * fill -- it holds 512 bytes against ~36-byte frames, and USB FS moves
     * bulk data far faster than the ~4.3 KB/s this link actually carries. A
     * full FIFO means the host has stopped reading, and dropping is the right
     * answer then anyway. uart.c has no equivalent path. */
    if (tud_cdc_write_available() < len) {
        return;
    }

    tud_cdc_write(data, len);
    tud_cdc_write_flush();
}

int host_link_read_byte(uint8_t *out) {
    if (!tud_cdc_available()) {
        return 0;
    }

    int32_t c = tud_cdc_read_char();
    if (c < 0) {
        return 0;
    }

    *out = (uint8_t)c;
    return 1;
}

void host_link_set_baudrate(uint32_t baudrate) {
    /* USB CDC has no line rate of its own -- the host names one and it means
       nothing on this side. platform.h sanctions the no-op; CMD_SET_BAUDRATE
       still ACKs and protocol.c still persists the value, so the saved
       settings record stays identical to the USART1 build's. */
    (void)baudrate;
}
