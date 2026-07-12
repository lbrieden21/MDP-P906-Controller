#ifndef NRF24L01P_H
#define NRF24L01P_H

/*
 * Bare-metal port of nrf_adapter_source/Modules/nrf24l01/nrf24l01p.c
 * (originally mokhwasomssi/stm32_hal_nrf24l01p, itself only ever wrote
 * RX_ADDR_P0 -- single pipe). Behavior of every ported function is
 * unchanged; SPI/GPIO calls are retargeted from HAL to spi.c/gpio.c.
 *
 * New in this port: nrf24l01p_open_rx_pipe() configures RX_ADDR_Pn /
 * RX_PW_Pn / EN_RXADDR / EN_AA for pipes 1-5 (real nRF24 multiceiver),
 * and the rx callback now receives the STATUS.RX_P_NO pipe number instead
 * of a bare success flag, since a receive on an unopened/closed pipe can't
 * happen once we're past FIFO-empty. Pipe 0 is intentionally never opened
 * for device routing: it must keep mirroring TX_ADDR, since ShockBurst
 * auto-ack replies are received on pipe 0 using RX_ADDR_P0 == TX_ADDR.
 */

#include <stddef.h>
#include <stdint.h>

typedef enum { ADR_250kbps = 2, ADR_1Mbps = 0, ADR_2Mbps = 1 } air_data_rate;

typedef enum {
    OP_7dBm = 7,
    OP_4dBm = 6,
    OP_3dBm = 5,
    OP_1dBm = 4,
    OP_0dBm = 3,
    OP_m4dBm = 2,
    OP_m6dBm = 1,
    OP_m12dBm = 0
} output_power;

void nrf24l01p_reset(void);

void nrf24l01p_rx_mode(void);
void nrf24l01p_tx_mode(void);
void nrf24l01p_idle_mode(void);

void nrf24l01p_power_up(void);
void nrf24l01p_power_down(void);

/* rx_callback(pipe): pipe = STATUS.RX_P_NO (0-5) of the packet just placed in rx_payload. */
void nrf24l01p_receive(uint8_t *rx_payload, void (*rx_callback)(uint8_t pipe));
void nrf24l01p_transmit(uint8_t *tx_payload, void (*tx_callback)(uint8_t status));
uint8_t nrf24l01p_transmit_then_receive(uint8_t *tx_payload,
                                        void (*tx_callback)(uint8_t status));
void nrf24l01p_irq(void);

uint8_t nrf24l01p_get_status(void);
uint8_t nrf24l01p_get_fifo_status(void);
uint8_t nrf24l01p_get_payload_width(void);

void nrf24l01p_rx_set_payload_widths(uint8_t bytes);

void nrf24l01p_flush_rx_fifo(void);
void nrf24l01p_flush_tx_fifo(void);

void nrf24l01p_clear_rx_dr(void);
void nrf24l01p_clear_tx_ds(void);
void nrf24l01p_clear_max_rt(void);

void nrf24l01p_set_rf_channel(uint16_t mhz);
void nrf24l01p_set_rf_tx_output_power(output_power dbm);
void nrf24l01p_set_rf_air_data_rate(air_data_rate bps);

void nrf24l01p_set_crc_length(uint8_t bytes);
void nrf24l01p_set_address_widths(uint8_t bytes);
void nrf24l01p_set_rx_address(uint8_t *address, size_t width);
void nrf24l01p_set_tx_address(uint8_t *address, size_t width);
void nrf24l01p_auto_retransmit_count(uint8_t cnt);
void nrf24l01p_auto_retransmit_delay(uint16_t us);

/* pipe: 1-5. address/width: same host-order convention as set_rx_address
   (address[width-1] is the byte that lands in the single-byte P2-P5 registers). */
uint8_t nrf24l01p_open_rx_pipe(uint8_t pipe, uint8_t *address, size_t width);

#endif
