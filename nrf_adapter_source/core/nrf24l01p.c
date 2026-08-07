#include "nrf24l01p.h"
#include "platform.h"

#define NRF_MODE_IDLE 0
#define NRF_MODE_RX 1
#define NRF_MODE_TX 2

#define CMD_R_REGISTER 0x00
#define CMD_W_REGISTER 0x20
#define CMD_R_RX_PAYLOAD 0x61
#define CMD_W_TX_PAYLOAD 0xA0
#define CMD_FLUSH_TX 0xE1
#define CMD_FLUSH_RX 0xE2
#define CMD_NOP 0xFF

#define REG_CONFIG 0x00
#define REG_EN_AA 0x01
#define REG_EN_RXADDR 0x02
#define REG_SETUP_AW 0x03
#define REG_SETUP_RETR 0x04
#define REG_RF_CH 0x05
#define REG_RF_SETUP 0x06
#define REG_STATUS 0x07
#define REG_RX_ADDR_P0 0x0A
#define REG_TX_ADDR 0x10
#define REG_RX_PW_P0 0x11
#define REG_FIFO_STATUS 0x17
#define REG_DYNPD 0x1C
#define REG_FEATURE 0x1D

static uint8_t nrf_mode = NRF_MODE_IDLE;
static uint8_t nrf_auto = 0;
static uint8_t nrf_auto_tx_cnt = 0;
static uint8_t *nrf_rx_payload = NULL;
static void (*nrf_tx_callback)(uint8_t) = NULL;
static void (*nrf_rx_callback)(uint8_t) = NULL;
static uint8_t payload_length = 32;

static void read_register_multi(uint8_t reg, uint8_t *buffer, uint8_t len) {
    nrf_csn_low();
    spi_transfer_byte(CMD_R_REGISTER | reg);
    spi_transfer(NULL, buffer, len);
    nrf_csn_high();
}

static uint8_t read_register(uint8_t reg) {
    uint8_t v;
    read_register_multi(reg, &v, 1);
    return v;
}

static void write_register_multi(uint8_t reg, const uint8_t *value, uint8_t len) {
    nrf_csn_low();
    spi_transfer_byte(CMD_W_REGISTER | reg);
    spi_transfer(value, NULL, len);
    nrf_csn_high();
}

static void write_register(uint8_t reg, uint8_t value) {
    write_register_multi(reg, &value, 1);
}

void nrf24l01p_receive(uint8_t *rx_payload, void (*rx_callback)(uint8_t)) {
    nrf_rx_payload = rx_payload;
    nrf_rx_callback = rx_callback;
    nrf_auto = 0;
    if (nrf_mode != NRF_MODE_RX) {
        nrf24l01p_rx_mode();
    }
}

void nrf24l01p_transmit(uint8_t *tx_payload, void (*tx_callback)(uint8_t)) {
    nrf_tx_callback = tx_callback;
    nrf_auto = 0;
    if (nrf_mode != NRF_MODE_TX) {
        nrf24l01p_tx_mode();
    }
    nrf_csn_low();
    spi_transfer_byte(CMD_W_TX_PAYLOAD);
    spi_transfer(tx_payload, NULL, payload_length);
    nrf_csn_high();
    led_on();
}

uint8_t nrf24l01p_transmit_then_receive(uint8_t *tx_payload,
                                        void (*tx_callback)(uint8_t)) {
    uint8_t ret = 1;
    nrf_tx_callback = tx_callback;
    nrf_auto = 1;
    if ((nrf_auto_tx_cnt >= 3 && (nrf24l01p_get_fifo_status() & 0x20)) ||
        nrf_auto_tx_cnt >= 6) {
        nrf24l01p_idle_mode();
        nrf24l01p_flush_tx_fifo();
        nrf24l01p_clear_max_rt();
        nrf24l01p_clear_tx_ds();
        ret = 0;
        nrf_auto_tx_cnt = 0;
    }
    if (nrf_mode != NRF_MODE_TX) {
        nrf24l01p_tx_mode();
    }
    nrf_csn_low();
    spi_transfer_byte(CMD_W_TX_PAYLOAD);
    spi_transfer(tx_payload, NULL, payload_length);
    nrf_csn_high();
    led_on();
    nrf_auto_tx_cnt++;
    return ret;
}

static void nrf24l01p_tx_irq(void) {
    uint8_t status = nrf24l01p_get_status();

    if (status & 0x20) { /* TX_DS */
        nrf24l01p_clear_tx_ds();
        led_off();
        nrf_tx_callback(1);
        if (nrf_auto_tx_cnt) {
            nrf_auto_tx_cnt--;
        }
    } else if (status & 0x10) { /* MAX_RT */
        nrf24l01p_flush_tx_fifo();
        nrf24l01p_clear_max_rt();
        led_off();
        nrf_tx_callback(0);
        nrf_auto_tx_cnt = 0;
    }

    if (nrf_auto) {
        if (!nrf_auto_tx_cnt) {
            nrf24l01p_rx_mode();
        }
    }
}

static void nrf24l01p_rx_irq(void) {
    while (!(nrf24l01p_get_fifo_status() & 0x01)) {
        led_on();
        nrf_csn_low();
        uint8_t status = spi_transfer_byte(CMD_R_RX_PAYLOAD);
        spi_transfer(NULL, nrf_rx_payload, payload_length);
        nrf_csn_high();
        nrf24l01p_clear_rx_dr();
        nrf_rx_callback((status >> 1) & 0x07);
        led_off();
    }
}

void nrf24l01p_reset(void) {
    nrf_csn_high();
    /* Settle delay before the first register write. This was a cycle-count
       busy-wait (`for (volatile int i = 0; i < 0xffff; i++) {}`), which is
       meaningless across cores -- it measured 20490us on the 48MHz Cortex-M0
       (SysTick-timed over 100 repetitions, read back over SWD; reproduced
       exactly on two runs) and would be a small fraction of that on a faster
       part. Rounded up to a whole millisecond, so every target inherits at
       least the settle time the STM32 build was validated with. */
    delay_ms(21);
    nrf_ce_low();
    nrf_mode = NRF_MODE_IDLE;

    write_register(REG_CONFIG, 0x08);
    write_register(REG_EN_AA, 0x01);
    write_register(REG_EN_RXADDR, 0x01);
    write_register(REG_SETUP_AW, 0x03);
    write_register(REG_SETUP_RETR, 0x03);
    write_register(REG_RF_CH, 0x02);
    write_register(REG_RF_SETUP, 0x07);
    write_register(REG_STATUS, 0x7E);
    for (uint8_t p = 0; p <= 5; p++) {
        write_register(REG_RX_PW_P0 + p, 0x00);
    }
    write_register(REG_FIFO_STATUS, 0x11);
    write_register(REG_DYNPD, 0x00);
    write_register(REG_FEATURE, 0x00);

    nrf24l01p_flush_rx_fifo();
    nrf24l01p_flush_tx_fifo();
}

void nrf24l01p_rx_mode(void) {
    nrf_ce_low();
    write_register(REG_CONFIG, read_register(REG_CONFIG) | 0x01);
    nrf_mode = NRF_MODE_RX;
    nrf_ce_high();
}

void nrf24l01p_tx_mode(void) {
    nrf_ce_low();
    write_register(REG_CONFIG, read_register(REG_CONFIG) & 0xFE);
    nrf_mode = NRF_MODE_TX;
    nrf_ce_high();
}

void nrf24l01p_idle_mode(void) {
    nrf_ce_low();
    nrf_mode = NRF_MODE_IDLE;
}

void nrf24l01p_flush_rx_fifo(void) {
    nrf_csn_low();
    spi_transfer_byte(CMD_FLUSH_RX);
    nrf_csn_high();
}

void nrf24l01p_flush_tx_fifo(void) {
    nrf_csn_low();
    spi_transfer_byte(CMD_FLUSH_TX);
    nrf_csn_high();
}

uint8_t nrf24l01p_get_status(void) {
    nrf_csn_low();
    uint8_t status = spi_transfer_byte(CMD_NOP);
    nrf_csn_high();
    return status;
}

uint8_t nrf24l01p_get_fifo_status(void) {
    return read_register(REG_FIFO_STATUS);
}

uint8_t nrf24l01p_get_payload_width(void) {
    return payload_length;
}

void nrf24l01p_rx_set_payload_widths(uint8_t bytes) {
    write_register(REG_RX_PW_P0, bytes);
    payload_length = bytes;
}

void nrf24l01p_clear_rx_dr(void) {
    write_register(REG_STATUS, nrf24l01p_get_status() | 0x40);
}

void nrf24l01p_clear_tx_ds(void) {
    write_register(REG_STATUS, nrf24l01p_get_status() | 0x20);
}

void nrf24l01p_clear_max_rt(void) {
    write_register(REG_STATUS, nrf24l01p_get_status() | 0x10);
}

void nrf24l01p_power_up(void) {
    write_register(REG_CONFIG, read_register(REG_CONFIG) | 0x02);
}

void nrf24l01p_power_down(void) {
    write_register(REG_CONFIG, read_register(REG_CONFIG) & 0xFD);
}

void nrf24l01p_set_crc_length(uint8_t bytes) {
    uint8_t cfg = read_register(REG_CONFIG);
    if (bytes == 1) {
        cfg &= 0xFB;
    } else if (bytes == 2) {
        cfg |= 0x04;
    }
    write_register(REG_CONFIG, cfg);
}

void nrf24l01p_set_address_widths(uint8_t bytes) {
    write_register(REG_SETUP_AW, bytes - 2);
}

static void reverse_address(uint8_t *out5, const uint8_t *address, size_t width) {
    for (size_t i = 0; i < 5; i++) {
        out5[i] = 0;
    }
    for (size_t i = 0; i < width; i++) {
        out5[i] = address[width - 1 - i];
    }
}

void nrf24l01p_set_rx_address(uint8_t *address, size_t width) {
    uint8_t addr[5];
    reverse_address(addr, address, width);
    write_register_multi(REG_RX_ADDR_P0, addr, 5);
}

void nrf24l01p_set_tx_address(uint8_t *address, size_t width) {
    uint8_t addr[5];
    reverse_address(addr, address, width);
    write_register_multi(REG_TX_ADDR, addr, 5);
}

uint8_t nrf24l01p_open_rx_pipe(uint8_t pipe, uint8_t *address, size_t width) {
    if (pipe < 1 || pipe > 5) {
        return 0;
    }
    uint8_t addr[5];
    reverse_address(addr, address, width);
    if (pipe == 1) {
        write_register_multi(REG_RX_ADDR_P0 + pipe, addr, 5);
    } else {
        write_register(REG_RX_ADDR_P0 + pipe, addr[0]);
    }
    write_register(REG_RX_PW_P0 + pipe, payload_length);
    write_register(REG_EN_RXADDR, read_register(REG_EN_RXADDR) | (1U << pipe));
    write_register(REG_EN_AA, read_register(REG_EN_AA) | (1U << pipe));
    return 1;
}

void nrf24l01p_auto_retransmit_count(uint8_t cnt) {
    uint8_t v = (read_register(REG_SETUP_RETR) & 0xF0) | (cnt & 0x0F);
    write_register(REG_SETUP_RETR, v);
}

void nrf24l01p_auto_retransmit_delay(uint16_t us) {
    if (us < 250) {
        us = 250;
    }
    uint8_t v = (read_register(REG_SETUP_RETR) & 0x0F) | ((((us / 250) - 1) & 0x0F) << 4);
    write_register(REG_SETUP_RETR, v);
}

void nrf24l01p_set_rf_channel(uint16_t mhz) {
    write_register(REG_RF_CH, (uint8_t)(mhz - 2400));
}

void nrf24l01p_set_rf_tx_output_power(output_power dbm) {
    uint8_t v = read_register(REG_RF_SETUP) & 0x08;
    v |= dbm & 0x07;
    write_register(REG_RF_SETUP, v);
}

void nrf24l01p_set_rf_air_data_rate(air_data_rate bps) {
    uint8_t v = read_register(REG_RF_SETUP) & 0xD7;
    switch (bps) {
        case ADR_1Mbps:
            break;
        case ADR_2Mbps:
            v |= 1 << 3;
            break;
        case ADR_250kbps:
            v |= 1 << 5;
            break;
    }
    write_register(REG_RF_SETUP, v);
}

void nrf24l01p_irq(void) {
    if (nrf_mode == NRF_MODE_RX) {
        nrf24l01p_rx_irq();
    } else if (nrf_mode == NRF_MODE_TX) {
        nrf24l01p_tx_irq();
    }
}
