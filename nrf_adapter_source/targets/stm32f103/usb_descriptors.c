#include <stdint.h>
#include <string.h>

#include "stm32f1xx.h"
#include "tusb.h"

/*
 * CDC-ACM descriptors: one CDC function, nothing else. TinyUSB calls each of
 * these back from its control-transfer handling, so they must stay valid for
 * the lifetime of the device -- hence static storage throughout.
 *
 * VID/PID are TinyUSB's example pair (0xCafe / 0x4001, the value its own
 * examples compute for a CDC-only device). They are deliberately not a
 * registered allocation: nothing on the host autodetects this board today
 * (mdp_controller/nrf24_adapter.py autodetects only the CP210x bridge used by
 * the USART1 build), so the CDC build is driven by an explicit --port exactly
 * as the four Teensy targets are. Widening host-side autodetect would need a
 * pair that is actually ours; revisit this before doing that, not after.
 */

#define USB_VID 0xCAFE
#define USB_PID 0x4001

enum {
    ITF_NUM_CDC = 0,
    ITF_NUM_CDC_DATA,
    ITF_NUM_TOTAL
};

/* IN endpoints carry the 0x80 direction bit. EP1 is the CDC notification
   endpoint, EP2 the bulk data pair. The F103's 512-byte PMA holds all of it
   comfortably: 2x64 for EP0, 8 for the notification, 2x64 for bulk data, plus
   the 64-byte buffer descriptor table. */
#define EPNUM_CDC_NOTIF 0x81
#define EPNUM_CDC_OUT   0x02
#define EPNUM_CDC_IN    0x82

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

enum {
    STRID_LANGID = 0,
    STRID_MANUFACTURER,
    STRID_PRODUCT,
    STRID_SERIAL,
    STRID_CDC_ITF
};

/* ------------------------------------------------------------------ device */

static const tusb_desc_device_t desc_device = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0200,

    /* Misc/common/IAD rather than a bare CDC class code: this is what lets
       Windows bind the composite driver without a .inf, and it is what every
       TinyUSB CDC example ships. Linux binds cdc_acm either way. */
    .bDeviceClass       = TUSB_CLASS_MISC,
    .bDeviceSubClass    = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol    = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,

    .idVendor           = USB_VID,
    .idProduct          = USB_PID,
    .bcdDevice          = 0x0100,

    .iManufacturer      = STRID_MANUFACTURER,
    .iProduct           = STRID_PRODUCT,
    .iSerialNumber      = STRID_SERIAL,

    .bNumConfigurations = 1
};

uint8_t const *tud_descriptor_device_cb(void) {
    return (uint8_t const *)&desc_device;
}

/* ----------------------------------------------------------- configuration */

static const uint8_t desc_configuration[] = {
    /* One configuration, bus-powered, 100mA. The macro supplies the
       bus-powered attribute bit itself, so bmAttributes is passed as 0. */
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 100),
    TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, STRID_CDC_ITF, EPNUM_CDC_NOTIF, 8,
                       EPNUM_CDC_OUT, EPNUM_CDC_IN, CFG_TUD_CDC_EP_BUFSIZE),
};

/* wTotalLength is written into the descriptor above from CONFIG_TOTAL_LEN
   while the bytes come from the macros -- a host that is handed a
   wTotalLength disagreeing with the real array fails enumeration in a way
   that looks like a driver problem. Catch it here instead. */
TU_VERIFY_STATIC(sizeof(desc_configuration) == CONFIG_TOTAL_LEN,
                 "configuration descriptor length mismatch");

uint8_t const *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index; /* only one configuration */
    return desc_configuration;
}

/* ----------------------------------------------------------------- strings */

/* The 96-bit factory unique ID, rendered as 24 hex chars. Worth the ~40 bytes
   of code: it is what makes two Blue Pills distinguishable in
   /dev/serial/by-id, and the CDC build is driven by an explicit --port. */
#define STM32F1_UID_BASE 0x1FFFF7E8UL
#define SERIAL_HEX_CHARS 24

static char serial_str[SERIAL_HEX_CHARS + 1];

static void serial_str_init(void) {
    static const char hex[] = "0123456789ABCDEF";
    const volatile uint8_t *uid = (const volatile uint8_t *)STM32F1_UID_BASE;

    for (int i = 0; i < SERIAL_HEX_CHARS / 2; i++) {
        uint8_t b = uid[i];
        serial_str[i * 2] = hex[b >> 4];
        serial_str[i * 2 + 1] = hex[b & 0x0F];
    }
    serial_str[SERIAL_HEX_CHARS] = '\0';
}

static const char *const string_desc[] = {
    [STRID_LANGID]       = NULL, /* handled separately -- not an ASCII string */
    [STRID_MANUFACTURER] = "MDP",
    [STRID_PRODUCT]      = "MDP nRF24 Adapter (Blue Pill)",
    [STRID_SERIAL]       = serial_str,
    [STRID_CDC_ITF]      = "MDP Host Link",
};

/* UTF-16LE payload plus the leading length/type word. */
static uint16_t desc_str[1 + 32];

uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;

    size_t chr_count;

    if (index == STRID_LANGID) {
        desc_str[1] = 0x0409; /* English (US) */
        chr_count = 1;
    } else {
        if (index >= TU_ARRAY_SIZE(string_desc)) {
            return NULL; /* stalls the request, which is the correct answer */
        }

        if (index == STRID_SERIAL && serial_str[0] == '\0') {
            serial_str_init();
        }

        const char *str = string_desc[index];
        chr_count = strlen(str);

        const size_t max_count = TU_ARRAY_SIZE(desc_str) - 1;
        if (chr_count > max_count) {
            chr_count = max_count;
        }

        for (size_t i = 0; i < chr_count; i++) {
            desc_str[1 + i] = (uint16_t)str[i]; /* ASCII -> UTF-16LE */
        }
    }

    desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));
    return desc_str;
}
