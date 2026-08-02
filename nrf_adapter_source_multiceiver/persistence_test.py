"""Settings-persistence check across a REAL power cycle, target-neutral.

host_link_test.py step 6 only reboots the MCU (SCB_AIRCR / IWDG) -- power never
drops, so a settings record still held in RAM, or a flash page that was never
actually committed, would pass it. This script splits the check around a
physical power removal:

    arm     -- write a distinctive radio config + a non-default baudrate,
               persist both, and leave the board running at 115200
    <human removes power, waits, restores power>
    verify  -- the board must come back up *at 115200* (proving the saved
               baudrate was read at boot) and report the distinctive config
    restore -- CMD_RESET back to compiled-in defaults at 921600

The test values are deliberately unlike both the compiled-in defaults and this
bench's real radio configuration, so neither a stale record nor a store_load()
that silently failed can masquerade as a pass.

Usage:
    python persistence_test.py --port /dev/ttyUSB0 --phase arm
    python persistence_test.py --port /dev/ttyUSB0 --phase verify
    python persistence_test.py --port /dev/ttyUSB0 --phase restore
"""
import argparse
import socket
import sys
import time

import serial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--port", required=True,
    help="serial port, e.g. /dev/ttyUSB0; or tcp://<host>:<port> for the ESP32 WiFi host link",
)
parser.add_argument("--phase", required=True, choices=("arm", "verify", "restore"))
args = parser.parse_args()
PORT = args.port

CMD_REBOOT = 0x00
CMD_RESET = 0x03
CMD_SET_BAUDRATE = 0x04
CMD_NRF_SET = 0x20
CMD_NRF_SAVE = 0x21
CMD_NRF_QUERY = 0x22
CMD_ECHO = 0xFF

REP_NAMES = {
    0x00: "REP_UNKNOWN_CMD", 0x01: "REP_INVALID_CMD", 0x02: "REP_CMD_FAILED",
    0x03: "REP_RESET_DONE", 0x04: "REP_BAUDRATE_SET", 0x20: "REP_NRF_INIT",
    0x21: "REP_NRF_SET_SAVED", 0x22: "REP_NRF_SET_QUERY", 0xFF: "REP_ECHO",
}

DEFAULT_BAUD = 921600
TEST_BAUD = 115200

# CMD_NRF_SET payload: ch-2400, air_data_rate, tx_power, crc, payload_len,
# arc, ard/250, address_width, then the 5 address bytes. Every field differs
# from the compiled-in default, and the address is not one this bench uses.
#      ch    adr  pwr crc  pw  arc ard  aw
TEST_SET = bytes([51, 0, 3, 1, 32, 5, 2, 5, 0x12, 0x34, 0x56, 0x78, 0x9A])
# CMD_NRF_QUERY echoes the same 13 bytes back.
TEST_QUERY = TEST_SET
DEFAULT_QUERY = bytes([78, 1, 7, 2, 32, 3, 1, 5, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE])

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


class _TcpPort:
    """Duck-types the pyserial subset this script uses (write/read/close/
    reset_input_buffer), so open_port() can hand back a socket instead of a
    Serial without the rest of the script caring. baud is accepted and
    ignored -- host_link_wifi.c has no line rate, exactly like USB CDC.
    That also means the DEFAULT_BAUD/TEST_BAUD distinction this script tests
    is a no-op over TCP: verify's negative control (step 3) "legitimately
    fails" here for the same reason host_link_test.py's does on USB-Serial/
    JTAG (see that script) -- both connect at whatever baud is asked and get
    an answer regardless."""

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
        pass  # fresh connection each time; nothing to flush


def open_port(baud):
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


def ping(s, tries=3):
    """CMD_ECHO with retries -- a framer left mid-frame by a wrong-baudrate
    probe needs one throwaway frame to resynchronise."""
    for _ in range(tries):
        send(s, CMD_ECHO)
        cmd, _data = recv(s, timeout=1.0)
        if cmd == 0xFF:
            return True
        s.reset_input_buffer()
    return False


def find_board():
    """Returns the baudrate the board is currently talking at, or None."""
    for baud in (DEFAULT_BAUD, TEST_BAUD):
        s = open_port(baud)
        alive = ping(s)
        s.close()
        if alive:
            return baud
        time.sleep(0.2)
    return None


if args.phase == "arm":
    print("0. locating the board")
    baud = find_board()
    if baud is None:
        print(f"  FAIL  no response on {PORT} at {DEFAULT_BAUD} or {TEST_BAUD}")
        sys.exit(1)
    print(f"  board is at {baud}")
    if baud != DEFAULT_BAUD:
        print("  note: board was already at the test baudrate -- a previous arm run")
        print("        was not restored. Continuing; 'restore' phase will clean up.")
    s = open_port(baud)

    print("1. CMD_NRF_SET (distinctive radio config, RAM only)")
    send(s, CMD_NRF_SET, TEST_SET)
    cmd, data = recv(s)
    check("reply", REP_NAMES.get(cmd), "REP_NRF_INIT")

    print("2. CMD_NRF_QUERY reflects it before any save")
    send(s, CMD_NRF_QUERY)
    cmd, data = recv(s)
    check("payload", data.hex() if data else None, TEST_QUERY.hex())

    print("3. CMD_NRF_SAVE (commit to the settings store)")
    send(s, CMD_NRF_SAVE)
    cmd, data = recv(s)
    check("reply", REP_NAMES.get(cmd), "REP_NRF_SET_SAVED")

    print(f"4. CMD_SET_BAUDRATE {TEST_BAUD} (persists the record again, with the new rate)")
    send(s, CMD_SET_BAUDRATE, bytes([11, 52, 0]))
    cmd, data = recv(s)
    check("reply", REP_NAMES.get(cmd), "REP_BAUDRATE_SET")
    s.close()
    time.sleep(0.3)
    s = open_port(TEST_BAUD)

    print(f"5. link alive at {TEST_BAUD} before power is removed")
    check("echo", ping(s), True)
    s.close()

    print()
    if fails:
        print(f"{len(fails)} FAILURES: {fails}")
        print("Do NOT power-cycle yet -- arm did not complete.")
        sys.exit(1)
    print("ARMED.")
    print(f"  saved radio config : {TEST_QUERY.hex()}")
    print(f"  saved baudrate     : {TEST_BAUD}")
    print()
    print("Now REMOVE POWER from the board (unplug whichever of the ST-Link or the")
    print("USB-serial adapter is supplying VCC -- only one is connected), wait ~5s,")
    print("then restore power and run:")
    print(f"  venv/bin/python nrf_adapter_source_multiceiver/persistence_test.py --port {PORT} --phase verify")
    sys.exit(0)

elif args.phase == "verify":
    print(f"1. board answers at {TEST_BAUD} after the power cycle")
    print("   (a lost record would boot it at the hard-coded 921600 instead)")
    s = open_port(TEST_BAUD)
    alive = ping(s)
    check("echo at 115200", alive, True)

    print("2. CMD_NRF_QUERY returns the saved radio config, not the defaults")
    send(s, CMD_NRF_QUERY)
    cmd, data = recv(s)
    check("reply", REP_NAMES.get(cmd), "REP_NRF_SET_QUERY")
    got = data.hex() if data else None
    check("payload", got, TEST_QUERY.hex())
    if got == DEFAULT_QUERY.hex():
        print("        ^ that is the compiled-in default: store_load() found no valid record.")
    s.close()
    time.sleep(0.3)

    print(f"3. negative control: board is silent at {DEFAULT_BAUD}")
    s = open_port(DEFAULT_BAUD)
    send(s, CMD_ECHO)
    cmd, data = recv(s, timeout=1.0)
    check("no reply at 921600", REP_NAMES.get(cmd) if cmd is not None else None, None)
    s.close()

    print()
    print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
    if not fails:
        print()
        print("Settings survived a real power cycle. Restore defaults with:")
        print(f"  venv/bin/python nrf_adapter_source_multiceiver/persistence_test.py --port {PORT} --phase restore")
    sys.exit(1 if fails else 0)

else:  # restore
    print("0. locating the board")
    baud = find_board()
    if baud is None:
        print(f"  FAIL  no response on {PORT} at {DEFAULT_BAUD} or {TEST_BAUD}")
        sys.exit(1)
    print(f"  board is at {baud}")
    s = open_port(baud)

    print("1. CMD_RESET (invalidates the stored record, then reboots)")
    send(s, CMD_RESET)
    cmd, data = recv(s)
    check("reply", REP_NAMES.get(cmd), "REP_RESET_DONE")
    s.close()
    time.sleep(4.0)

    print(f"2. board is back on the compiled-in defaults at {DEFAULT_BAUD}")
    s = open_port(DEFAULT_BAUD)
    check("echo", ping(s), True)
    send(s, CMD_NRF_QUERY)
    cmd, data = recv(s)
    check("payload", data.hex() if data else None, DEFAULT_QUERY.hex())
    s.close()

    print()
    print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
    sys.exit(1 if fails else 0)
