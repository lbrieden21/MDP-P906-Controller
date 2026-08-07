"""Host-link check for the adapter firmware, target-neutral.

Exercises the framer, command dispatch and the settings store (flash page on
STM32, EEPROM-emulation on Teensy) over whatever serial port the board
enumerates as -- USART1-via-bridge on the STM32 targets, USB CDC on the
Teensy targets. Deliberately avoids anything that needs the radio wired up.
"""
import argparse
import socket
import sys
import time

import serial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--port", required=True,
    help="serial port, e.g. /dev/ttyACM0 (Teensy) or /dev/ttyUSB0 (STM32 dongle); "
         "or tcp://<host>:<port> for the ESP32 WiFi host link",
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


class _TcpPort:
    """Duck-types the pyserial subset this script uses (write/read/close/
    reset_input_buffer), so open_port() can hand back a socket instead of a
    Serial without the rest of the script caring. baud is accepted and
    ignored -- host_link_wifi.c has no line rate, exactly like USB CDC (see
    host_link_wired_set_baudrate() on that link)."""

    def __init__(self, host, port, timeout):
        self._sock = socket.create_connection((host, port), timeout=5.0)
        self._sock.settimeout(timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def write(self, data):
        self._sock.sendall(data)

    def flush(self):
        pass

    def read(self, n=1):
        try:
            return self._sock.recv(n)
        except (socket.timeout, TimeoutError):
            return b""

    def close(self):
        self._sock.close()

    def reset_input_buffer(self):
        # Not needed over TCP: this is only ever called right after connect,
        # and a fresh connection has nothing buffered yet -- unlike the
        # serial case, there is no DTR/RTS reset pulse to flush the debris of.
        pass


def open_port(baud=DEFAULT_BAUD):
    if PORT.startswith("tcp://"):
        host, _, port_str = PORT[len("tcp://"):].partition(":")
        return _TcpPort(host, int(port_str), timeout=0.2)

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
    # Drain boot noise (ROM banner, then the boot-time REP_NRF_INIT) until the
    # line goes quiet, rather than a fixed sleep -- a fixed 0.4s sleep was
    # measured racing REP_NRF_INIT's arrival, which lands at exactly 0.4s on
    # the C6, and losing that race hands the first real reply's slot to the
    # boot announcement instead. A single empty read is not enough of a
    # "quiet" signal: on the WROOM-32, the ROM banner (printed at 74880 baud,
    # read here at 921600 and so seen as garbage bytes) leaves a ~0.2-0.4s
    # silent gap before REP_NRF_INIT itself arrives, so bailing on the first
    # empty read stops draining mid-gap and hands REP_NRF_INIT's slot to
    # whatever real command follows. Require 0.6s of continuous silence
    # instead, bounded to a 3s deadline overall.
    deadline = time.time() + 3.0
    quiet_since = None
    while time.time() < deadline:
        if s.read(256):
            quiet_since = None
        else:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= 0.6:
                break
    return s


def reconnect_after_reset(timeout=10.0):
    """Reopens the port after a CMD_REBOOT/CMD_RESET. A wired reboot is fast
    and consistent, so the fixed 4s sleep the wired path has always used is
    left alone. WiFi is neither: association (plus an occasional WPA3-SAE
    comeback-timer retry, measured ~4.6s total on the bench AP) makes a fixed
    sleep flaky, so the TCP case polls instead.

    A bare TCP connect() is not enough of a check: esp_restart() does not
    happen instantly on receipt of CMD_REBOOT, so a connect attempt made
    right away can land in the *old*, soon-to-die process's accept queue and
    "succeed" against a connection that is about to vanish (measured: connect
    returns in <20ms, then a CMD_NRF_QUERY sent on it times out because the
    real reboot happens underneath it). Each attempt therefore has to prove
    the link is live with an actual round trip, matching how the production
    GUI reconnect already treats a bare connect as insufficient -- it waits
    for an ECHO, not just a socket state (see the plan's "Host side: the
    timeout budget" section)."""
    if not PORT.startswith("tcp://"):
        time.sleep(4.0)
        return open_port()

    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            candidate = open_port()
            send(candidate, CMD_ECHO)
            cmd, _ = recv(candidate, timeout=1.0)
            if cmd == 0xFF:  # REP_ECHO
                return candidate
            candidate.close()
            last_err = RuntimeError(f"no live ECHO reply (got {cmd!r})")
        except OSError as e:
            last_err = e
        time.sleep(0.3)
    raise last_err


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
s = reconnect_after_reset()
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
s = reconnect_after_reset()
send(s, CMD_NRF_QUERY)
cmd, data = recv(s)
check("defaults restored", data.hex() if data else None, default.hex())

s.close()
print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
