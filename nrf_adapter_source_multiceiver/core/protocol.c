#include "protocol.h"
#include "nrf24l01p.h"
#include "platform.h"

typedef struct {
    uint16_t ch_mhz;
    uint8_t air_data_rate;
    uint8_t tx_output_power;
    uint8_t crc_length;
    uint8_t payload_length;
    uint8_t auto_retransmit_count;
    uint16_t auto_retransmit_delay;
    uint8_t address_widths;
    uint8_t address_buf[5];
} nrf_init_t;

typedef struct {
    nrf_init_t nrf;
    uint32_t baudrate;
} persisted_settings_t;

static nrf_init_t nrf_setting = {2478, ADR_2Mbps,        OP_7dBm, 2, 32,
                                 3,    250,  5,    {0xAA, 0xBB, 0xCC, 0xDD, 0xEE}};
static uint32_t s_baudrate = 921600;
static uint8_t nrf_buffer[32];

static void nrf_tx_done(uint8_t status);
static void nrf_rx_done(uint8_t pipe);

static void uart_send_packet(uint8_t cmd, const uint8_t *data1, size_t len1,
                              const uint8_t *data2, size_t len2) {
    uint8_t hdr[4] = {0xAA, 0x66, cmd, (uint8_t)(len1 + len2)};
    host_link_write(hdr, 4);
    if (len1) {
        host_link_write(data1, len1);
    }
    if (len2) {
        host_link_write(data2, len2);
    }
}

static void nrf_configure(nrf_init_t *s) {
    nrf24l01p_reset();
    nrf24l01p_power_up();

    nrf24l01p_set_rf_channel(s->ch_mhz);
    nrf24l01p_set_rf_air_data_rate((air_data_rate)s->air_data_rate);
    nrf24l01p_set_rf_tx_output_power((output_power)s->tx_output_power);
    nrf24l01p_set_crc_length(s->crc_length);
    nrf24l01p_set_address_widths(s->address_widths);
    nrf24l01p_set_tx_address(s->address_buf, s->address_widths);
    nrf24l01p_set_rx_address(s->address_buf, s->address_widths);
    nrf24l01p_rx_set_payload_widths(s->payload_length);
    nrf24l01p_auto_retransmit_count(s->auto_retransmit_count);
    nrf24l01p_auto_retransmit_delay(s->auto_retransmit_delay);

    for (size_t i = 0; i < sizeof(nrf_buffer); i++) {
        nrf_buffer[i] = 0;
    }
    nrf24l01p_receive(nrf_buffer, nrf_rx_done);

    uart_send_packet(REP_NRF_INIT, NULL, 0, NULL, 0);
}

static void nrf_tx_done(uint8_t status) {
    uart_send_packet(status ? REP_NRF_SEND_OK : REP_NRF_SEND_FAIL, NULL, 0, NULL, 0);
}

/* PIPE_TAG_RECV_OK: whether REP_NRF_RECV_OK carries the leading pipe-number
   byte from the multiceiver extension. Was off by default through Phase 0.5
   so this firmware could be validated for parity against the *unmodified*
   nrf24_adapter.py/mdp_p906.py first (per multi_device_refactor_plan.md's
   staged validation). Phase 1 adds bus.py, which now expects and strips the
   pipe-tag prefix on every NRF_RECV_OK frame -- flip back to 0 only if
   testing against pre-Phase-1 driver code. */
#define PIPE_TAG_RECV_OK 1

static void nrf_rx_done(uint8_t pipe) {
#if PIPE_TAG_RECV_OK
    uint8_t pipe_byte = pipe;
    uart_send_packet(REP_NRF_RECV_OK, &pipe_byte, 1, nrf_buffer,
                     nrf24l01p_get_payload_width());
#else
    (void)pipe;
    uart_send_packet(REP_NRF_RECV_OK, nrf_buffer, nrf24l01p_get_payload_width(), NULL, 0);
#endif
}

static void handle_command(uint8_t cmd, uint8_t *data, size_t len) {
    switch (cmd) {
        case CMD_REBOOT:
            platform_reboot();
            break;

        case CMD_RESET:
            (void)store_save(NULL, 0); /* len=0 write invalidates the stored record */
            uart_send_packet(REP_RESET_DONE, NULL, 0, NULL, 0);
            delay_ms(100);
            platform_reboot();
            break;

        case CMD_SET_BAUDRATE:
            if (len < 3) {
                uart_send_packet(REP_INVALID_CMD, NULL, 0, NULL, 0);
                break;
            }
            s_baudrate = (uint32_t)data[2] + (uint32_t)data[1] * 100 +
                         (uint32_t)data[0] * 10000;
            uart_send_packet(REP_BAUDRATE_SET, NULL, 0, NULL, 0);
            delay_ms(100);
            host_link_set_baudrate(s_baudrate);
            {
                persisted_settings_t ps = {nrf_setting, s_baudrate};
                store_save(&ps, sizeof(ps));
            }
            break;

        case CMD_NRF_TX:
            for (size_t i = 0; i < sizeof(nrf_buffer); i++) {
                nrf_buffer[i] = 0;
            }
            for (size_t i = 0; i < len && i < sizeof(nrf_buffer); i++) {
                nrf_buffer[i] = data[i];
            }
            if (!nrf24l01p_transmit_then_receive(nrf_buffer, nrf_tx_done)) {
                uart_send_packet(REP_NRF_FIFO_OVERFLOW, NULL, 0, NULL, 0);
            }
            break;

        case CMD_NRF_SET:
            if (len < 13) {
                uart_send_packet(REP_INVALID_CMD, NULL, 0, NULL, 0);
                break;
            }
            nrf_setting.ch_mhz = (uint16_t)(data[0] + 2400);
            nrf_setting.air_data_rate = data[1];
            nrf_setting.tx_output_power = data[2];
            nrf_setting.crc_length = data[3];
            nrf_setting.payload_length = data[4];
            nrf_setting.auto_retransmit_count = data[5];
            nrf_setting.auto_retransmit_delay = (uint16_t)(data[6] * 250);
            nrf_setting.address_widths = data[7];
            for (int i = 0; i < 5; i++) {
                nrf_setting.address_buf[i] = data[8 + i];
            }
            nrf_configure(&nrf_setting);
            break;

        case CMD_NRF_SAVE: {
            persisted_settings_t ps = {nrf_setting, s_baudrate};
            if (store_save(&ps, sizeof(ps))) {
                uart_send_packet(REP_NRF_SET_SAVED, NULL, 0, NULL, 0);
            } else {
                uart_send_packet(REP_CMD_FAILED, NULL, 0, NULL, 0);
            }
            break;
        }

        case CMD_NRF_QUERY: {
            uint8_t out[13];
            out[0] = (uint8_t)(nrf_setting.ch_mhz - 2400);
            out[1] = nrf_setting.air_data_rate;
            out[2] = nrf_setting.tx_output_power;
            out[3] = nrf_setting.crc_length;
            out[4] = nrf_setting.payload_length;
            out[5] = nrf_setting.auto_retransmit_count;
            out[6] = (uint8_t)(nrf_setting.auto_retransmit_delay / 250);
            out[7] = nrf_setting.address_widths;
            for (int i = 0; i < 5; i++) {
                out[8 + i] = nrf_setting.address_buf[i];
            }
            uart_send_packet(REP_NRF_SET_QUERY, out, sizeof(out), NULL, 0);
            break;
        }

        case CMD_NRF_OPEN_PIPE: {
            if (len < 6) {
                uart_send_packet(REP_INVALID_CMD, NULL, 0, NULL, 0);
                break;
            }
            uint8_t pipe = data[0];
            if (pipe < 1 || pipe > 5 ||
                !nrf24l01p_open_rx_pipe(pipe, data + 1, nrf_setting.address_widths)) {
                uart_send_packet(REP_CMD_FAILED, NULL, 0, NULL, 0);
                break;
            }
            uart_send_packet(REP_NRF_PIPE_OPENED, &pipe, 1, NULL, 0);
            break;
        }

        case CMD_NRF_SET_TX_TARGET: {
            if (len < 5) {
                uart_send_packet(REP_INVALID_CMD, NULL, 0, NULL, 0);
                break;
            }
            nrf24l01p_set_tx_address(data, nrf_setting.address_widths);
            nrf24l01p_set_rx_address(data, nrf_setting.address_widths);
            uart_send_packet(REP_NRF_TX_TARGET_SET, NULL, 0, NULL, 0);
            break;
        }

        case CMD_ECHO:
            uart_send_packet(REP_ECHO, NULL, 0, NULL, 0);
            break;

        default:
            uart_send_packet(REP_UNKNOWN_CMD, NULL, 0, NULL, 0);
            break;
    }
}

/* Byte-wise framer for host->adapter frames: 0xAA 0x55 <cmd> <len> <data...> */
static void feed_byte(uint8_t b) {
    static uint8_t state = 0, cmd = 0;
    static uint8_t buf[128];
    static size_t dl = 0, bp = 0;

    switch (state) {
        case 0:
            state = (b == 0xAA) ? 1 : 0;
            break;
        case 1:
            state = (b == 0x55) ? 2 : 0;
            break;
        case 2:
            cmd = b;
            state = 3;
            break;
        case 3:
            if (b == 0) {
                state = 0;
                handle_command(cmd, NULL, 0);
            } else {
                dl = b;
                bp = 0;
                state = 4;
            }
            break;
        case 4:
            if (bp < sizeof(buf)) {
                buf[bp++] = b;
            }
            if (bp >= dl) {
                state = 0;
                handle_command(cmd, buf, dl);
            }
            break;
        default:
            state = 0;
            break;
    }
}

void protocol_service_radio_irq(void) {
    nrf24l01p_irq();
}

void protocol_init(void) {
    /* uart_init(921600) has already run at this point (main.c) -- mirrors
       the shipped firmware's boot order: bring UART up at the hard-coded
       default baud first, then switch it if a saved baudrate says otherwise. */
    persisted_settings_t ps;
    if (store_load(&ps, sizeof(ps))) {
        nrf_setting = ps.nrf;
        if (ps.baudrate && ps.baudrate != s_baudrate) {
            s_baudrate = ps.baudrate;
            host_link_set_baudrate(s_baudrate);
        }
    }
    nrf_configure(&nrf_setting);
}

void protocol_poll(void) {
    uint8_t b;
    while (host_link_read_byte(&b)) {
        feed_byte(b);
    }
}
