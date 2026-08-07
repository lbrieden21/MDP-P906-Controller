#ifndef PROTOCOL_H
#define PROTOCOL_H

/*
 * Host <-> adapter framing and command set, ported from
 * the shipped firmware's Core/Src/main.c (handle_uart_command/parse_uart_data)
 * to match mdp_controller/nrf24_adapter.py's CMD/RESPONSE enums exactly.
 * Frame: 0xAA 0x55 <cmd> <len> <data...>  (host -> adapter)
 *        0xAA 0x66 <cmd> <len> <data...>  (adapter -> host)
 *
 * New in this port (multiceiver): CMD_NRF_OPEN_PIPE / REP_NRF_PIPE_OPENED,
 * and REP_NRF_RECV_OK payload now leads with a pipe-number byte (from
 * STATUS.RX_P_NO) before the raw nRF24 payload, so the host can attribute a
 * response to a device by hardware pipe instead of guessing from packet
 * content/timing (see multi_device_refactor_plan.md, "Adapter firmware").
 *
 * Also new: CMD_NRF_SET_TX_TARGET / REP_NRF_TX_TARGET_SET. CMD_NRF_SET is a
 * full radio reconfigure (nrf24l01p_reset() included) -- fine when it only
 * ever ran once per connection, but multi-device sends need to retarget
 * TX_ADDR/RX_ADDR_P0 (required to match for ShockBurst ACK reception) to
 * whichever device is being addressed *without* wiping pipes 1-5 each time.
 * This just calls the existing set_tx_address/set_rx_address register
 * writes directly, nothing else.
 *
 * Also new (network host links, WiFi and Ethernet): CMD_NET_CREDS_SET /
 * CMD_NET_QUERY / CMD_NET_CREDS_CLEAR / CMD_NET_IP_SET, so a network-capable
 * adapter can be provisioned over the wired link before it can be reached
 * over IP. platform.h's net_creds_save() / net_ip_config_save() / net_status()
 * hooks answer REP_CMD_FAILED on every target that does not implement them,
 * so these four commands exist on every target but only do anything on a
 * WiFi or Ethernet build. (Named CMD_WIFI_* through the WiFi-only era; full
 * cutover to CMD_NET_* when Ethernet needed the same commands.)
 */

enum {
    CMD_REBOOT = 0x00,
    CMD_RESET = 0x03,
    CMD_SET_BAUDRATE = 0x04,
    CMD_NRF_TX = 0x10,
    CMD_NRF_SET = 0x20,
    CMD_NRF_SAVE = 0x21,
    CMD_NRF_QUERY = 0x22,
    CMD_NRF_OPEN_PIPE = 0x23,
    CMD_NRF_SET_TX_TARGET = 0x24,
    CMD_NET_CREDS_SET = 0x30,
    CMD_NET_QUERY = 0x31,
    CMD_NET_CREDS_CLEAR = 0x32,
    CMD_NET_IP_SET = 0x33,
    CMD_ECHO = 0xFF,
};

enum {
    REP_UNKNOWN_CMD = 0x00,
    REP_INVALID_CMD = 0x01,
    REP_CMD_FAILED = 0x02,
    REP_RESET_DONE = 0x03,
    REP_BAUDRATE_SET = 0x04,

    REP_NRF_SEND_OK = 0x10,
    REP_NRF_SEND_FAIL = 0x11,
    REP_NRF_RECV_OK = 0x12,
    REP_NRF_RECV_FAIL = 0x13,
    REP_NRF_FIFO_OVERFLOW = 0x15,

    REP_NRF_INIT = 0x20,
    REP_NRF_SET_SAVED = 0x21,
    REP_NRF_SET_QUERY = 0x22,
    REP_NRF_PIPE_OPENED = 0x23,
    REP_NRF_TX_TARGET_SET = 0x24,

    REP_NET_ACK = 0x30,    /* answers CMD_NET_CREDS_SET, CMD_NET_CREDS_CLEAR and CMD_NET_IP_SET alike */
    REP_NET_STATUS = 0x31,

    REP_ECHO = 0xFF,
};

void protocol_init(void);
/* Drains all currently-buffered UART RX bytes through the framer/dispatcher. Call from the main loop. */
void protocol_poll(void);
/* Services a pending nRF24 IRQ. The target decides where this runs -- straight
   out of the pin's interrupt handler (STM32F030) or deferred to the main loop. */
void protocol_service_radio_irq(void);

#endif
