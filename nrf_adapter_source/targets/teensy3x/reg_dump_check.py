"""Bring-up step 4 for the Teensy 3.x target: nRF register read-back with no
SWD path available. Relies on the temporary REP_REG_DUMP (0xFE) frame added
to main.cpp -- emitted once a second, reading through platform.h's own SPI
primitives. Remove that frame (and this script) once this step passes.

Captures one dump in the boot-default state, then sends CMD_NRF_SET with the
bench radio config plus one CMD_NRF_OPEN_PIPE (pipe 1, P906's address) and
captures a second dump. Both are checked against values hand-derived from
core/protocol.c's nrf_configure() and core/nrf24l01p.c's open_rx_pipe() call
order.

Usage: venv/bin/python reg_dump_check.py --port /dev/ttyACM0
"""
import argparse
import sys
import time

import serial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", required=True)
args = parser.parse_args()

CMD_NRF_SET = 0x20
CMD_NRF_OPEN_PIPE = 0x23
CMD_REBOOT = 0x00
REP_NRF_INIT = 0x20
REP_NRF_PIPE_OPENED = 0x23
REP_REG_DUMP = 0xFE

# Bench config from mdp_controller/bus.py's MDPBus.__init__ / gui_source/settings.json:
# freq=2473, air_data_rate=2Mbps, tx_output_power=4dBm, crc16, payload=32,
# arc=12, ard=250us, address_width=5, address=0E:4C:B9:EF:E0 (adapter base).
BENCH_SET = bytes([73, 1, 6, 2, 32, 12, 1, 5, 0x0E, 0x4C, 0xB9, 0xEF, 0xE0])
P906_ADDR = bytes.fromhex("0E4CB9EFE1")  # pipe 1, MDPBus._pipe_address()

fails = []


def send(s, cmd, data=b""):
    s.write(bytes([0xAA, 0x55, cmd, len(data)]) + data)
    s.flush()


def recv_any(s, timeout=3.0):
    """Reads one 0xAA 0x66 <cmd> <len> <data> frame, whatever cmd it is."""
    deadline = time.time() + timeout
    state = 0
    cmd = None
    while time.time() < deadline:
        b = s.read(1)
        if not b:
            continue
        b = b[0]
        if state == 0:
            state = 1 if b == 0xAA else 0
        elif state == 1:
            state = 2 if b == 0x66 else (1 if b == 0xAA else 0)
        elif state == 2:
            cmd = b
            state = 3
        elif state == 3:
            data = s.read(b) if b else b""
            return cmd, data
    return None, None


def recv_specific(s, want_cmd, timeout=3.0):
    """Skips unsolicited REP_REG_DUMP frames while waiting for a command reply."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd, data = recv_any(s, timeout=deadline - time.time())
        if cmd == want_cmd:
            return data
        if cmd is None:
            return None
    return None


def recv_dump(s, timeout=3.0):
    return recv_specific(s, REP_REG_DUMP, timeout)


def parse_dump(data):
    return {
        "CONFIG": data[0],
        "EN_AA": data[1],
        "EN_RXADDR": data[2],
        "SETUP_AW": data[3],
        "SETUP_RETR": data[4],
        "RF_CH": data[5],
        "RF_SETUP": data[6],
        "RX_PW_P0": data[7],
        "RX_ADDR_P0": data[8:13].hex(),
        "RX_ADDR_P1": data[13:18].hex(),
        "TX_ADDR": data[18:23].hex(),
    }


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (expected {want!r})"))
    if not ok:
        fails.append(label)


def main():
    # DTR/RTS low before the open -- see host_link_test.py's open_port(). This
    # script is board-agnostic despite living under teensy3x, and the WROOM-32
    # bring-up runs it over a USB-to-UART bridge whose control lines drive
    # EN/IO0.
    s = serial.Serial(baudrate=921600, timeout=0.5)
    s.dtr = False
    s.rts = False
    s.port = args.port
    s.open()
    time.sleep(0.4)
    s.reset_input_buffer()

    print("1. boot-default register dump")
    data = recv_dump(s)
    if data is None:
        print("  FAIL  no REP_REG_DUMP frame received")
        sys.exit(1)
    reg = parse_dump(data)
    print(f"  raw: {reg}")
    check("CONFIG", reg["CONFIG"], 0x0F)
    check("RF_CH", reg["RF_CH"], 0x4E)
    check("RF_SETUP", reg["RF_SETUP"], 0x0F)
    check("EN_RXADDR", reg["EN_RXADDR"], 0x01)
    check("EN_AA", reg["EN_AA"], 0x01)
    check("SETUP_AW", reg["SETUP_AW"], 0x03)
    check("SETUP_RETR", reg["SETUP_RETR"], 0x03)
    check("RX_PW_P0", reg["RX_PW_P0"], 0x20)
    check("TX_ADDR", reg["TX_ADDR"], "eeddccbbaa")
    check("RX_ADDR_P0", reg["RX_ADDR_P0"], "eeddccbbaa")

    print()
    print("2. CMD_NRF_SET (bench config)")
    send(s, CMD_NRF_SET, BENCH_SET)
    check("reply", recv_specific(s, REP_NRF_INIT), b"")

    print("3. CMD_NRF_OPEN_PIPE (pipe 1, P906 address)")
    send(s, CMD_NRF_OPEN_PIPE, bytes([1]) + P906_ADDR)
    check("reply", recv_specific(s, REP_NRF_PIPE_OPENED), bytes([1]))

    print()
    print("4. post-set register dump")
    data = recv_dump(s)
    reg = parse_dump(data)
    print(f"  raw: {reg}")
    check("CONFIG", reg["CONFIG"], 0x0F)
    check("RF_CH", reg["RF_CH"], 0x49)
    check("RF_SETUP", reg["RF_SETUP"], 0x0E)
    check("EN_RXADDR", reg["EN_RXADDR"], 0x03)
    check("EN_AA", reg["EN_AA"], 0x03)
    check("SETUP_AW", reg["SETUP_AW"], 0x03)
    check("SETUP_RETR", reg["SETUP_RETR"], 0x0C)
    check("RX_PW_P0", reg["RX_PW_P0"], 0x20)
    check("TX_ADDR", reg["TX_ADDR"], "e0efb94c0e")
    check("RX_ADDR_P0", reg["RX_ADDR_P0"], "e0efb94c0e")
    check("RX_ADDR_P1", reg["RX_ADDR_P1"], "e1efb94c0e")

    print()
    send(s, CMD_REBOOT)
    s.close()

    print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
