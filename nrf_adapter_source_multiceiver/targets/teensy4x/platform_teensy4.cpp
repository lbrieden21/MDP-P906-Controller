/*
 * platform.h for the Teensy 4.x target (4.0 and 4.1 -- see the Makefile's
 * BOARD switch).
 *
 * This is the whole C/C++ boundary: Arduino headers appear here and nowhere
 * else, and every platform.h entry point is wrapped in extern "C" so core/'s
 * C translation units link against it unchanged.
 *
 * millis() and delay_ms() are the two exceptions to "implemented here" --
 * millis() is already an extern "C" symbol in the framework with exactly
 * platform.h's signature, so it links straight through.
 */

#include <Arduino.h>
#include <SPI.h>

#include "net_eth.h"
#include "pins.h"
#include "platform_teensy4.h"

extern "C" {
#include "platform.h"
}

/* ---------------------------------------------------------------- host link */

/* -DHOST_LINK_USB_CDC (default) or -DHOST_LINK_SERIAL1, set by the Makefile.
   Both sit behind the same three host_link_wired_* functions, so switching is
   a compile-time flag rather than a rewrite, and nothing in core/ is
   conditional on it. Renamed from host_link_{begin,write,read_byte,
   set_baudrate} when host_link_mux.cpp arrived to own the platform.h names --
   bodies unchanged, exactly as targets/esp32/main/host_link_usb_jtag.c and
   host_link_uart0.c were treated. */
#if defined(HOST_LINK_SERIAL1)
#define HOST_PORT Serial1
#elif defined(HOST_LINK_USB_CDC)
#define HOST_PORT Serial
#else
#error "Define HOST_LINK_USB_CDC or HOST_LINK_SERIAL1"
#endif

void host_link_wired_begin(uint32_t baudrate) {
    HOST_PORT.begin(baudrate);
}

void host_link_wired_write(const uint8_t *data, size_t len) {
    HOST_PORT.write(data, len);
}

int host_link_wired_read_byte(uint8_t *out) {
    int c = HOST_PORT.read();
    if (c < 0) {
        return 0;
    }
    *out = (uint8_t)c;
    return 1;
}

void host_link_wired_set_baudrate(uint32_t baudrate) {
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

/* Mode 0, MSB first, matching the STM32 target's SPI1 setup. The transaction
   brackets the whole CSN-asserted access rather than each byte -- opening or
   closing a transaction inside an asserted CSN is what broke SPI on the
   earlier multi-platform-wip attempt. */
static const SPISettings nrf_spi_settings(NRF_SPI_HZ, MSBFIRST, SPI_MODE0);

extern "C" uint8_t spi_transfer_byte(uint8_t tx) {
    return SPI.transfer(tx);
}

/* Byte at a time, the same shape as the STM32 target's spi_transfer(), so the
   0xFF filler on reads matches and the block path has no separate semantics to
   validate. 32 bytes at 10MHz is ~26us of clock; the per-byte overhead on a
   600MHz M7 is not worth optimising away. */
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

/* No spare pin for a status LED on this target -- see pins.h. */
extern "C" void led_on(void) {}
extern "C" void led_off(void) {}

/* ---------------------------------------------------------------- radio IRQ */

/* Unlike the STM32 target, no radio work happens in interrupt context here:
   the ISR only latches the edge and loop() calls protocol_service_radio_irq().
   The STM32 can service in-ISR because uart.c puts USART1 at NVIC priority 1
   against EXTI2_3's 3, so the UART ISR preempts and drains the TX ring while
   the radio handler spin-waits on it. That priority relationship does not
   survive the port, and at 600MHz against a 20ms poll period the deferral
   costs microseconds. */
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
 * Teensy 4.1's flash-emulated EEPROM (~4KB, eeprom.c in the vendored core)
 * standing in for the STM32 target's reserved flash page. The record layout is
 * deliberately unchanged -- [magic u32][len u16][payload 32][crc16], same
 * CRC16-CCITT -- so persisted_settings_t round-trips identically on both
 * targets and store_load()/store_save() need no core/ changes.
 *
 * The i.MX RT1062 has no internal flash; this is the FlexSPI self-programming
 * path PJRC's core already solves, and it is one of the two reasons this
 * target uses their core at all (the other being USB CDC).
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

/* Declared in platform_teensy4.h -- net_eth.cpp shares this definition
   rather than carrying a second copy. */
uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
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
 * WDOG1, driven directly rather than through Teensyduino's WDT_T4 -- that is a
 * separate library that would have to be vendored alongside SPI, and imxrt.h
 * (already in the vendored core) declares every register it needs. Same shape
 * as the STM32 target's watchdog.c, which also talks to its IWDG registers
 * directly.
 *
 * WDOG1's timeout granularity is 0.5s: WT=6 gives (6+1) * 0.5 = 3.5s, the
 * nearest step to the STM32 target's ~3.3s IWDG timeout, refreshed on the same
 * 100ms cadence from main.cpp's loop().
 */
extern "C" void watchdog_init(void) {
    /* WDZST/WDBG are write-once after reset, so the whole configuration goes
       in one store. SRS and WDA are active-low "do not assert now" controls --
       they must be written 1, or enabling the watchdog asserts a reset
       immediately. WDE cannot be cleared again except by a reset. */
    WDOG1_WCR = WDOG_WCR_WT(6) | WDOG_WCR_WDE | WDOG_WCR_SRS | WDOG_WCR_WDA |
                WDOG_WCR_WDZST | WDOG_WCR_WDBG;
    watchdog_refresh();
}

extern "C" void watchdog_refresh(void) {
    WDOG1_WSR = 0x5555;
    WDOG1_WSR = 0xAAAA;
}

extern "C" void platform_reboot(void) {
    SCB_AIRCR = 0x05FA0004; /* VECTKEY | SYSRESETREQ */
    while (1) {}
}

/* ---------------------------------------------------------------- boot init */

void platform_init(void) {
    /* Idle levels first, so neither line glitches when it becomes an output --
       same ordering as the STM32 target's gpio_init(). */
    digitalWriteFast(NRF_CSN_PIN, HIGH);
    digitalWriteFast(NRF_CE_PIN, LOW);
    pinMode(NRF_CSN_PIN, OUTPUT);
    pinMode(NRF_CE_PIN, OUTPUT);
    digitalWriteFast(NRF_CSN_PIN, HIGH);
    digitalWriteFast(NRF_CE_PIN, LOW);

    pinMode(NRF_IRQ_PIN, INPUT_PULLUP);

    SPI.begin();

    attachInterrupt(digitalPinToInterrupt(NRF_IRQ_PIN), radio_isr, FALLING);

    /* No-op unless HOST_LINK_ETH is set -- see net_eth.cpp. Last, so the
       radio and the settings store are both already up if it needs either,
       matching platform_esp32.c's placement of wifi_sta_init(). */
    net_eth_begin();
}
