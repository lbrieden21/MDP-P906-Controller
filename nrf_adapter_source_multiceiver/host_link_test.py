"""Host-link check for the adapter firmware, target-neutral.

Exercises the framer, command dispatch and the settings store (flash page on
STM32, EEPROM-emulation on Teensy) over whatever serial port the board
enumerates as -- USART1-via-bridge on the STM32 targets, USB CDC on the
Teensy targets. Deliberately avoids anything that needs the radio wired up.
"""
import argparse
import sys
import time

import serial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--port", required=True,
    help="serial port, e.g. /dev/ttyACM0 (Teensy) or /dev/ttyUSB0 (STM32 dongle)",
)
args = parser.parse_args()
PORT = args.port

CMD_REBOOT = 0x00
CMD_RESET = 0x03
CMD_SET_BAUDRATE = 0x04
CMD_NRF_SAVE = 0x21
CMD_NRF_QUERY = 0x22
CMD_ECHO = 0xFF

REP_NAMES = {
    0x00: "REP_UNKNOWN_CMD", 0x01: "REP_INVALID_CMD", 0x02: "REP_CMD_FAILED",
    0x03: "REP_RESET_DONE", 0x04: "REP_BAUDRATE_SET", 0x20: "REP_NRF_INIT",
    0x21: "REP_NRF_SET_SAVED", 0x22: "REP_NRF_SET_QUERY", 0xFF: "REP_ECHO",
}

fails = []


def send(s, cmd, data=b""):
    s.write(bytes([0xAA, 0x55, cmd, len(data)]) + data)
    s.flush()


def recv(s, timeout=2.0):
    """Reads one 0xAA 0x66 <cmd> <len> <data> frame."""
    deadline = time.time() + timeout
    state = 0
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


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f"  (expected {want!r})"))
    if not ok:
        fails.append(label)


DEFAULT_BAUD = 921600


def open_port(baud=DEFAULT_BAUD):
    # DTR and RTS must be low BEFORE the open, not after -- after is too late,
    # the pulse has already happened. On a board whose protocol link runs
    # through a USB-to-UART bridge (the ESP-WROOM-32), those lines drive EN and
    # IO0, so a default open reboots the adapter intermittently: 4/6 and 3/6
    # replies measured, against 12/12 with the lines low first. Raising the
    # settle below does not help. Harmless on every CDC target, none of which
    # reads the control lines.
    s = serial.Serial(baudrate=baud, timeout=0.2)
    s.dtr = False
    s.rts = False
    s.port = PORT
    s.open()
    time.sleep(0.4)
    s.reset_input_buffer()
    return s


s = open_port()

print("1. CMD_ECHO")
send(s, CMD_ECHO)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd, hex(cmd) if cmd is not None else None), "REP_ECHO")

print("2. CMD_NRF_QUERY (compiled-in defaults)")
send(s, CMD_NRF_QUERY)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_NRF_SET_QUERY")
check("payload len", len(data) if data else 0, 13)
# defaults from protocol.c: 2478MHz, 2Mbps, 7dBm, crc2, pw32, arc3, ard250,
# aw5, addr AA BB CC DD EE
default = bytes([78, 1, 7, 2, 32, 3, 1, 5, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE])
check("payload", data.hex() if data else None, default.hex())

print("3. CMD_SET_BAUDRATE (UART targets really retune; CDC targets only ACK)")
send(s, CMD_SET_BAUDRATE, bytes([11, 52, 0]))  # 11*10000 + 52*100 + 0 = 115200
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_BAUDRATE_SET")
# protocol.c applies the new rate 100ms after sending the ACK, so the host has
# to follow it across. On CDC targets host_link_set_baudrate() is a documented
# no-op and pyserial's rate is ignored, so reopening is correct on every
# target -- but it is only *observable* on the USART ones. Leaving the host at
# the old rate silently orphans the link on those, which is what the original
# 92160 value did.
s.close()
time.sleep(0.3)
s = open_port(115200)

print("4. link still alive at the new baudrate")
send(s, CMD_ECHO)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_ECHO")

# Back to the default before anything is persisted, so the stored record and
# every later step agree with open_port() and the firmware's own boot rate.
send(s, CMD_SET_BAUDRATE, bytes([92, 16, 0]))  # 92*10000 + 16*100 + 0 = 921600
cmd, data = recv(s)
check("restored to 921600", REP_NAMES.get(cmd), "REP_BAUDRATE_SET")
s.close()
time.sleep(0.3)
s = open_port()

print("5. CMD_NRF_SAVE (EEPROM write)")
send(s, CMD_NRF_SAVE)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_NRF_SET_SAVED")

print("6. CMD_REBOOT, then re-query -> settings survived the power cycle")
send(s, CMD_REBOOT)
s.close()
time.sleep(4.0)
s = open_port()
send(s, CMD_NRF_QUERY)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_NRF_SET_QUERY")
check("payload", data.hex() if data else None, default.hex())

print("7. unknown command is rejected, framer stays in sync")
send(s, 0x7E)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_UNKNOWN_CMD")
send(s, CMD_ECHO)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_ECHO")

print("8. CMD_RESET clears the stored record (also undoes step 3's saved baudrate)")
send(s, CMD_RESET)
cmd, data = recv(s)
check("reply", REP_NAMES.get(cmd), "REP_RESET_DONE")
s.close()
time.sleep(4.0)
s = open_port()
send(s, CMD_NRF_QUERY)
cmd, data = recv(s)
check("defaults restored", data.hex() if data else None, default.hex())

s.close()
print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
