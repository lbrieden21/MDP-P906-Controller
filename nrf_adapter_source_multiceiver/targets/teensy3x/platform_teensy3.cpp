/*
 * platform.h for the Teensy 3.x target (3.5 and 3.6 -- see the Makefile's
 * BOARD switch).
 *
 * This is the whole C/C++ boundary: Arduino headers appear here and nowhere
 * else, and every platform.h entry point is wrapped in extern "C" so core/'s
 * C translation units link against it unchanged. Modelled directly on
 * platform_teensy4.cpp; see that file's comments for anything not called out
 * again here, and Phase 3 of the port plan for the two genuine differences
 * (a real LED, and a Kinetis watchdog with a hard timing window).
 *
 * millis() and delay_ms() are the two exceptions to "implemented here" --
 * millis() is already an extern "C" symbol in the framework with exactly
 * platform.h's signature, so it links straight through.
 */

#include <Arduino.h>
#include <SPI.h>

#include "pins.h"
#include "platform_teensy3.h"

extern "C" {
#include "platform.h"
}

/* ---------------------------------------------------------------- host link */

#if defined(HOST_LINK_SERIAL1)
#define HOST_PORT Serial1
#elif defined(HOST_LINK_USB_CDC)
#define HOST_PORT Serial
#else
#error "Define HOST_LINK_USB_CDC or HOST_LINK_SERIAL1"
#endif

void host_link_begin(uint32_t baudrate) {
    HOST_PORT.begin(baudrate);
}

extern "C" void uart_write(const uint8_t *data, size_t len) {
    HOST_PORT.write(data, len);
}

extern "C" int uart_read_byte(uint8_t *out) {
    int c = HOST_PORT.read();
    if (c < 0) {
        return 0;
    }
    *out = (uint8_t)c;
    return 1;
}

extern "C" void uart_set_baudrate(uint32_t baudrate) {
#if defined(HOST_LINK_SERIAL1)
    HOST_PORT.begin(baudrate);
#else
    /* USB CDC has no line rate of its own -- the host sets one and pyserial
       ignores it. CMD_SET_BAUDRATE still ACKs and still persists the value
       (protocol.c), so only the physical link speed stops responding. */
    (void)baudrate;
#endif
}

/* ---------------------------------------------------------------------- SPI */

/* Mode 0, MSB first, matching the STM32 target's SPI1 setup and the Teensy
   4.x target. The transaction brackets the whole CSN-asserted access rather
   than each byte -- opening or closing a transaction inside an asserted CSN
   is what broke SPI on the earlier multi-platform-wip attempt.

   nrf24l01p_reset() opens with a bare nrf_csn_high(), so endTransaction() runs
   unpaired once at boot. Checked on the 4.1 and it holds here too: SPI.h's
   KINETISK endTransaction() only touches NVIC masks under
   `if (interruptMasksUsed)`, which requires usingInterrupt(), which this
   firmware never calls -- so the unpaired call is a no-op. That branch also
   guards an optional SPI_TRANSACTION_MISMATCH_LED path (SPI.h:36); confirmed
   undefined by this Makefile. */
static const SPISettings nrf_spi_settings(NRF_SPI_HZ, MSBFIRST, SPI_MODE0);

extern "C" uint8_t spi_transfer_byte(uint8_t tx) {
    return SPI.transfer(tx);
}

/* Byte at a time, the same shape as the STM32 target's spi_transfer() and the
   Teensy 4.x target's, so the 0xFF filler on reads matches and the block path
   has no separate semantics to validate. */
extern "C" void spi_transfer(const uint8_t *tx, uint8_t *rx, size_t len) {
    for (size_t i = 0; i < len; i++) {
        uint8_t in = SPI.transfer(tx ? tx[i] : 0xFFU);
        if (rx) {
            rx[i] = in;
        }
    }
}

extern "C" void nrf_csn_low(void) {
    SPI.beginTransaction(nrf_spi_settings);
    digitalWriteFast(NRF_CSN_PIN, LOW);
}

extern "C" void nrf_csn_high(void) {
    digitalWriteFast(NRF_CSN_PIN, HIGH);
    SPI.endTransaction();
}

extern "C" void nrf_ce_low(void) {
    digitalWriteFast(NRF_CE_PIN, LOW);
}

extern "C" void nrf_ce_high(void) {
    digitalWriteFast(NRF_CE_PIN, HIGH);
}

/* Unlike the Teensy 4.x target, this board has a real LED here: SPI0's SCK
   can move off pin 13 (SPI.setSCK(14) in platform_init(), before SPI.begin()),
   freeing pin 13 for the onboard LED. */
extern "C" void led_on(void) {
    digitalWriteFast(LED_PIN, HIGH);
}
extern "C" void led_off(void) {
    digitalWriteFast(LED_PIN, LOW);
}

/* ---------------------------------------------------------------- radio IRQ */

/* Same deferred-to-loop model as the Teensy 4.x target -- see that file's
   comment on why the STM32's in-ISR servicing does not carry over. */
static volatile bool radio_irq_flag = false;

static void radio_isr(void) {
    radio_irq_flag = true;
}

bool radio_irq_pending(void) {
    bool edge = radio_irq_flag;
    radio_irq_flag = false;
    return edge || digitalReadFast(NRF_IRQ_PIN) == LOW;
}

/* ----------------------------------------------------------------- settings */

/*
 * Teensy 3.5/3.6's FlexNVM-backed emulated EEPROM (Drivers/teensy3/eeprom.c),
 * 4096 bytes here (Drivers/teensy3/avr/eeprom.h, E2END 0xFFF) standing in for
 * the STM32 target's reserved flash page. Record layout, CRC16-CCITT and the
 * read-back verify are identical to every other target -- see
 * platform_teensy4.cpp's comment on why that identity matters -- so
 * store_load()/store_save() need no core/ changes here either.
 */

#define STORE_ADDR 0 /* offset into the emulated EEPROM */
#define STORE_MAGIC 0x50393036U /* "P906" */
#define STORE_MAX_PAYLOAD 32

typedef struct {
    uint32_t magic;
    uint16_t len;
    uint8_t payload[STORE_MAX_PAYLOAD];
    uint16_t crc;
} store_record_t;

static uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

extern "C" int store_load(void *payload, size_t len) {
    if (len > STORE_MAX_PAYLOAD) {
        return 0;
    }
    store_record_t rec;
    eeprom_read_block(&rec, (const void *)STORE_ADDR, sizeof(rec));
    if (rec.magic != STORE_MAGIC || rec.len != len) {
        return 0;
    }
    if (crc16_ccitt(rec.payload, len) != rec.crc) {
        return 0;
    }
    for (size_t i = 0; i < len; i++) {
        ((uint8_t *)payload)[i] = rec.payload[i];
    }
    return 1;
}

extern "C" int store_save(const void *payload, size_t len) {
    if (len > STORE_MAX_PAYLOAD) {
        return 0;
    }

    store_record_t rec;
    rec.magic = STORE_MAGIC;
    rec.len = (uint16_t)len;
    for (size_t i = 0; i < STORE_MAX_PAYLOAD; i++) {
        rec.payload[i] = (i < len) ? ((const uint8_t *)payload)[i] : 0;
    }
    rec.crc = crc16_ccitt(rec.payload, len);

    eeprom_write_block(&rec, (void *)STORE_ADDR, sizeof(rec));

    uint8_t check[STORE_MAX_PAYLOAD];
    if (!store_load(check, len)) {
        return 0;
    }
    for (size_t i = 0; i < len; i++) {
        if (check[i] != ((const uint8_t *)payload)[i]) {
            return 0;
        }
    }
    return 1;
}

/* ------------------------------------------------------------ misc platform */

extern "C" void delay_ms(uint32_t ms) {
    delay(ms);
}

/*
 * Kinetis WDOG, driven directly rather than through a vendored library --
 * checked, so it does not get re-litigated: tonton81/WDT_T4 is i.MX-only (it
 * writes WDOG1_* with no architecture gating, so on Kinetis it fails to
 * compile rather than degrading), and PJRC's cores/teensy3 ships no watchdog
 * API beyond the ALLOWUPDATE write below.
 *
 * PJRC's default early hook (Drivers/teensy3/mk20dx128.c:672-678) leaves the
 * watchdog disabled but reconfigurable, so watchdog_init() runs from setup()
 * at its normal place in the boot order rather than needing its own
 * startup_early_hook() override. That is a deviation from PJRC's intended
 * pattern -- ResetHandler (mk20dx128.c:691-710) unlocks the watchdog and then
 * spends the window on two nops before calling the hook, so configuring here
 * means unlocking a second time. That is valid given ALLOWUPDATE, but the
 * unlock sequence only holds the config window open for 256 bus cycles: a
 * WDOG_UNLOCK that misses it silently no-ops, yielding a watchdog that never
 * fires rather than one that misbehaves -- nothing but the watchdog step in
 * Verification would catch that. Interrupts are disabled across
 * unlock->configure so the window cannot be missed by a preempting ISR.
 *
 * TOVALH:TOVALL is meant to be 3.5s, matching the Teensy 4.x target's WDOG1
 * timeout, refreshed on the same 100ms cadence from main.cpp's loop(). In
 * practice this had to be found empirically rather than from the reference
 * manual's clock math -- see below.
 *
 * CLKSRC is left clear (0). This was not derived from a clean clock-source
 * model; it was measured directly with a stall test (main.cpp's loop
 * replaced with a marker frame + `while(1){}`, no refresh, timed from the
 * host receiving the marker to ttyACM0 dropping):
 *
 *   - CLKSRC set (1), TOVALL=3500: fires in well under 100ms in one bench
 *     build, but was independently measured (in a different bench build,
 *     during normal *refreshed* operation, via ttyACM0 add/remove timing) at
 *     a very consistent ~2.3s reset period. Those two measurements of the
 *     "same" setting disagree by orders of magnitude, which means this bit
 *     is not cleanly selecting a fixed, well-behaved clock the way the
 *     reference manual's LPO-vs-bus-clock description implies -- something
 *     about the 256-bus-cycle unlock->configure window (see below) appears
 *     sensitive to the surrounding code, not just to this bit.
 *   - CLKSRC clear (0), TOVALL=3500: fires reliably and reproducibly (three
 *     stall trials within 2ms of each other) at ~16.54s -- nothing near the
 *     1kHz-LPO-implies-3.5s math, but at least stable and repeatable in this
 *     exact source layout.
 *
 * A second stall-test data point (TOVALL=740 -> measured 2.734s, four trials
 * within 3ms of each other) fits the first (TOVALL=3500 -> 16.539s) to a
 * clean two-point model: measured = TOVAL/rate - offset, solving to rate =~
 * 200.0 counts/sec and offset =~ 0.97s (the boot blink plus protocol_init
 * overhead that elapses, unmeasured, between watchdog_init() and the point
 * the stall test's marker frame -- and so the external timer -- starts).
 * 200 counts/sec is a clean enough number that this is probably a real clock
 * rate on this silicon, just not the one the reference manual's CLKSRC
 * description predicts. TOVALL=893 solves that model for a 3.5s result and
 * was reverified with the same stall test: four trials at 3.498-3.501s
 * (mean 3.499s). If this file's timing-sensitive surroundings change
 * materially (this function's body, or what runs before it in setup()),
 * re-measure rather than trust this comment's numbers.
 *
 * The 3.6 (MK66FX1M0) needs a different TOVALL for the same ~3.5s result --
 * confirmed on real hardware, not assumed from "same family" reasoning.
 * TOVALL=893 (the 3.5's value) measures 5.34s on a 3.6 (four trials
 * 5.342-5.344s). A three-point stall-test calibration on the 3.6 itself
 * (TOVALL=200 -> 1.831s, 893 -> 5.343s, 1786 -> 9.867s, each the tight
 * cluster of four trials with the always-anomalous first post-flash trial
 * discarded) fits a clean model at rate =~ 197.4 counts/sec, offset =~
 * 0.82s -- solving for TOVALL=529, reverified directly: four trials at
 * 3.493-3.499s (mean 3.496s). The rate is close to the 3.5's (~197 vs ~200,
 * within normal LPO part-to-part tolerance); the two boards still need
 * separate constants because "close" isn't "equal" against a hard timing
 * target. This test used an explicit watchdog_refresh() immediately before
 * the marker frame (see the temporary stall harness this constant was
 * derived with), so its offset isn't directly comparable to the 3.5's
 * boot-blink-inclusive offset above -- re-derive per board, don't interpolate
 * across them.
 */
extern "C" void watchdog_init(void) {
    noInterrupts();
    WDOG_UNLOCK = WDOG_UNLOCK_SEQ1;
    WDOG_UNLOCK = WDOG_UNLOCK_SEQ2;
    __asm__ volatile("nop");
    __asm__ volatile("nop");

    WDOG_TOVALH = 0;
#if defined(ARDUINO_TEENSY36)
    WDOG_TOVALL = 529; /* empirically calibrated for the 3.6 -- see the comment above */
#else
    WDOG_TOVALL = 893; /* empirically calibrated for the 3.5 -- see the comment above */
#endif
    WDOG_STCTRLH = WDOG_STCTRLH_WDOGEN | WDOG_STCTRLH_ALLOWUPDATE |
                   WDOG_STCTRLH_WAITEN | WDOG_STCTRLH_STOPEN;
    interrupts();

    watchdog_refresh();
}

extern "C" void watchdog_refresh(void) {
    WDOG_REFRESH = 0xA602;
    WDOG_REFRESH = 0xB480;
}

extern "C" void platform_reboot(void) {
    SCB_AIRCR = 0x05FA0004; /* VECTKEY | SYSRESETREQ */
    while (1) {}
}

/* ---------------------------------------------------------------- boot init */

void platform_init(void) {
    /* Idle levels first, so neither line glitches when it becomes an output --
       same ordering as the STM32 target's gpio_init() and the Teensy 4.x
       target. */
    digitalWriteFast(NRF_CSN_PIN, HIGH);
    digitalWriteFast(NRF_CE_PIN, LOW);
    pinMode(NRF_CSN_PIN, OUTPUT);
    pinMode(NRF_CE_PIN, OUTPUT);
    digitalWriteFast(NRF_CSN_PIN, HIGH);
    digitalWriteFast(NRF_CE_PIN, LOW);

    pinMode(NRF_IRQ_PIN, INPUT_PULLUP);

    digitalWriteFast(LED_PIN, LOW);
    pinMode(LED_PIN, OUTPUT);

    /* Must happen before SPI.begin() -- moves SPI0's SCK off pin 13 so the
       onboard LED is free (pins.h). */
    SPI.setSCK(NRF_SCK_PIN);
    SPI.begin();

    attachInterrupt(digitalPinToInterrupt(NRF_IRQ_PIN), radio_isr, FALLING);
}
