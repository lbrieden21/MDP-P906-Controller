# nRF24L01+ register read-back over SWD, for the F103 bring-up step 4.
#
# The radio's registers are inside the radio, not in MCU memory, so SWD cannot
# read them directly -- they have to come back over SPI. read_register() is
# static and -O2 inlines it away, but the primitives it is built from survive
# as real symbols (nrf_csn_low / spi_transfer_byte / nrf_csn_high), so GDB can
# drive an R_REGISTER transaction on the halted target through the firmware's
# own SPI path. That is the point: this exercises the F103 SPI rewrite (older
# IP, no CR2_DS/FRXTH) rather than some parallel host-side implementation.
#
# Usage, with the radio wired (PA2 IRQ, PA3 CSN, PA4 CE, PA5 SCK, PA6 MISO,
# PA7 MOSI) and the board running:
#
#   openocd -f interface/stlink.cfg -f target/stm32f1x.cfg     # terminal 1
#   gdb-multiarch -q -x nrf_regdump.gdb build/MDP_Adapter_Multiceiver.elf
#
# The target is halted for the duration. A halt landing inside the radio IRQ
# handler would leave CSN asserted and skew the first read, so the script
# re-asserts CSN high before starting.

# ---------------------------------------------------------------------------
# Expected values, derived by hand from nrf_configure()'s call order in
# protocol.c:42-63 -- written down BEFORE the first run, so the read-back is
# checked against the derivation and not the other way round.
#
# nrf_configure() runs: reset, power_up, rf_channel, air_data_rate,
# tx_output_power, crc_length, address_widths, tx_address, rx_address,
# rx_payload_widths, retransmit_count, retransmit_delay, receive.
#
# (A) BOOT DEFAULTS -- 2478MHz, 2Mbps, 7dBm, crc16, pw32, arc3, ard250, aw5,
#     address AA:BB:CC:DD:EE. This is the state after a plain power-up with no
#     stored record, and it is what the F030 refactor recorded.
#
#   CONFIG      0x0f   reset 0x08, |0x02 power_up, |0x04 crc16, |0x01 rx_mode
#   EN_AA       0x01   reset default, pipe 0 only
#   EN_RXADDR   0x01   reset default, pipe 0 only
#   SETUP_AW    0x03   aw 5 -> 5-2
#   SETUP_RETR  0x03   arc 3 in low nibble; ard 250 -> ((250/250)-1)<<4 = 0
#   RF_CH       0x4e   2478-2400 = 78
#   RF_SETUP    0x0f   0x07 &0xD7 |1<<3 (2Mbps) = 0x0f, then &0x08 |7 (7dBm)
#   STATUS      0x0e   RX_P_NO = 7 (FIFO empty), no flags latched
#   RX_PW_P0    0x20   32
#   RX_PW_P1    0x00   pipe 1 not opened
#   FIFO_STATUS 0x11   TX_EMPTY | RX_EMPTY
#   RX_ADDR_P0  ee dd cc bb aa on the wire -> AA:BB:CC:DD:EE
#   RX_ADDR_P1  c2 c2 c2 c2 c2 on the wire -> the chip's own reset default
#   TX_ADDR     ee dd cc bb aa on the wire -> AA:BB:CC:DD:EE
#
#   The five values the F030 recorded (RF_CH=0x4e, RF_SETUP=0x0f,
#   EN_RXADDR=0x01, CONFIG=0x0f, FIFO_STATUS=0x11) all fall out of the above,
#   so a matching F103 dump is a like-for-like cross-board result.
#
# (B) BENCH CONFIG -- after CMD_NRF_SET with freq 2521, 2Mbps, 4dBm, crc16,
#     pw32, arc12, ard250, aw5, address AA:BB:CC:DD:E2, then
#     CMD_NRF_OPEN_PIPE(pipe 1, AA:BB:CC:DD:E3). Only the differences:
#
#   EN_AA       0x03   open_rx_pipe ORs in 1<<1
#   EN_RXADDR   0x03   open_rx_pipe ORs in 1<<1
#   SETUP_RETR  0x0c   arc 12; ard 250 still leaves the high nibble 0
#   RF_CH       0x79   2521-2400 = 121
#   RF_SETUP    0x0e   2Mbps keeps bit 3; 4dBm is 6, so bits 2:0 = 110
#   RX_PW_P1    0x20   open_rx_pipe copies payload_length
#   RX_ADDR_P0  e2 dd cc bb aa on the wire -> AA:BB:CC:DD:E2
#   RX_ADDR_P1  e3 dd cc bb aa on the wire -> AA:BB:CC:DD:E3
#   TX_ADDR     e2 dd cc bb aa on the wire -> AA:BB:CC:DD:E2
#
# An all-0x00 or all-0xff dump is not a register mismatch -- it means MISO
# never drove, i.e. wiring or SPI, not configuration.
# ---------------------------------------------------------------------------

target extended-remote localhost:3333
monitor halt

# One-byte register read. $arg0 = register number.
define nrfreg
  set $r = $arg0
  set $_d = (unsigned char) nrf_csn_low()
  set $_d = (unsigned char) spi_transfer_byte($r & 0x1f)
  set $v = (unsigned char) spi_transfer_byte(0xff)
  set $_d = (unsigned char) nrf_csn_high()
  printf "  %-12s (0x%02x) = 0x%02x\n", $arg1, $r, $v
end

# Five-byte address register read. The nRF stores addresses LSB-first, and
# nrf24l01p.c's reverse_address() writes them reversed, so the logical address
# is the register bytes read back in reverse order.
define nrfaddr
  set $r = $arg0
  set $_d = (unsigned char) nrf_csn_low()
  set $_d = (unsigned char) spi_transfer_byte($r & 0x1f)
  set $b0 = (unsigned char) spi_transfer_byte(0xff)
  set $b1 = (unsigned char) spi_transfer_byte(0xff)
  set $b2 = (unsigned char) spi_transfer_byte(0xff)
  set $b3 = (unsigned char) spi_transfer_byte(0xff)
  set $b4 = (unsigned char) spi_transfer_byte(0xff)
  set $_d = (unsigned char) nrf_csn_high()
  printf "  %-12s (0x%02x) = %02x %02x %02x %02x %02x on the wire -> address %02X:%02X:%02X:%02X:%02X\n", $arg1, $r, $b0, $b1, $b2, $b3, $b4, $b4, $b3, $b2, $b1, $b0
end

# Clear a CSN left asserted by a halt inside the IRQ handler.
set $_d = (unsigned char) nrf_csn_high()

printf "\nnRF24L01+ register read-back (via firmware SPI primitives)\n\n"
nrfreg 0x00 "CONFIG"
nrfreg 0x01 "EN_AA"
nrfreg 0x02 "EN_RXADDR"
nrfreg 0x03 "SETUP_AW"
nrfreg 0x04 "SETUP_RETR"
nrfreg 0x05 "RF_CH"
nrfreg 0x06 "RF_SETUP"
nrfreg 0x07 "STATUS"
nrfreg 0x11 "RX_PW_P0"
nrfreg 0x12 "RX_PW_P1"
nrfreg 0x17 "FIFO_STATUS"
printf "\n"
nrfaddr 0x0a "RX_ADDR_P0"
nrfaddr 0x0b "RX_ADDR_P1"
nrfaddr 0x10 "TX_ADDR"
printf "\n"
printf "Expected values are derived by hand in the bring-up notes -- compare\n"
printf "against those, not the other way round.\n\n"
monitor resume
