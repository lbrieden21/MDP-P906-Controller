"""WiFi credential provisioning for the ESP32 WiFi host link, over the wired
serial link -- target-neutral in the sense that every target answers these
commands (CMD_WIFI_SET / CMD_WIFI_QUERY / CMD_WIFI_CLEAR, see core/protocol.h),
but only an ESP32 WiFi build (`make HOST_LINK=HOST_LINK_WIFI`) does anything
with them. Every other target's platform.h hooks stub to 0, which this script
sees as REP_CMD_FAILED.

Follows host_link_test.py / persistence_test.py's argparse style and shares
their framing helpers and DTR/RTS-low-before-open workaround.

Usage:
    python wifi_provision.py --port /dev/ttyACM0 --ssid MyNetwork --password hunter2
    python wifi_provision.py --port /dev/ttyACM0 --status
    python wifi_provision.py --port /dev/ttyACM0 --clear
"""
import argparse
import sys
import time

import serial

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--port", required=True, help="serial port, e.g. /dev/ttyACM0")
parser.add_argument("--ssid", help="network SSID to provision (up to 32 bytes)")
parser.add_argument("--password", help="network password to provision (up to 64 bytes)")
parser.add_argument("--status", action="store_true", help="query and print connection status")
parser.add_argument("--clear", action="store_true", help="clear stored credentials")
args = parser.parse_args()
PORT = args.port

actions = [bool(args.ssid or args.password), args.status, args.clear]
if sum(actions) != 1:
    parser.error("pick exactly one of: --ssid/--password, --status, --clear")
if bool(args.ssid) != bool(args.password):
    parser.error("--ssid and --password must be given together")
if args.ssid and len(args.ssid.encode()) > 32:
    parser.error("--ssid must be at most 32 bytes")
if args.password and len(args.password.encode()) > 64:
    parser.error("--password must be at most 64 bytes")

CMD_WIFI_SET = 0x30
CMD_WIFI_QUERY = 0x31
CMD_WIFI_CLEAR = 0x32

REP_NAMES = {
    0x00: "REP_UNKNOWN_CMD", 0x01: "REP_INVALID_CMD", 0x02: "REP_CMD_FAILED",
    0x30: "REP_WIFI_SET", 0x31: "REP_WIFI_STATUS",
}

STATE_NAMES = {0: "disconnected", 1: "connecting", 2: "connected"}

DEFAULT_BAUD = 921600


def send(s, cmd, data=b""):
    s.write(bytes([0xAA, 0x55, cmd, len(data)]) + data)
    s.flush()


def recv(s, timeout=5.0):
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


def open_port(baud=DEFAULT_BAUD):
    # DTR and RTS must be low BEFORE the open, not after -- see
    # host_link_test.py for the measured reason (ESP-WROOM-32's bridge lines
    # drive EN/IO0, so a default open reboots the adapter intermittently).
    s = serial.Serial(baudrate=baud, timeout=0.2)
    s.dtr = False
    s.rts = False
    s.port = PORT
    s.open()
    # Drain boot noise (ROM banner, then the boot-time REP_NRF_INIT) until the
    # line goes quiet, rather than a fixed sleep -- a fixed 0.4s sleep was
    # measured racing REP_NRF_INIT's arrival, which lands at exactly 0.4s on
    # the C6, and losing that race hands CMD_WIFI_SET's reply slot to the
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


def parse_status(data):
    if data is None or len(data) < 7:
        return None
    state = data[0]
    ip = data[1:5]
    rssi = data[5] - 256 if data[5] >= 128 else data[5]
    ssid_len = data[6]
    ssid = data[7:7 + ssid_len].decode(errors="replace")
    return state, ip, rssi, ssid


def print_status(data):
    parsed = parse_status(data)
    if parsed is None:
        print("  malformed REP_WIFI_STATUS payload:", data.hex() if data else None)
        return
    state, ip, rssi, ssid = parsed
    print(f"  state : {STATE_NAMES.get(state, state)}")
    if state == 2:
        print(f"  ip    : {ip[0]}.{ip[1]}.{ip[2]}.{ip[3]}")
        print(f"  rssi  : {rssi} dBm")
        print(f"  ssid  : {ssid}")


s = open_port()

if args.status:
    print(f"CMD_WIFI_QUERY on {PORT}")
    send(s, CMD_WIFI_QUERY)
    cmd, data = recv(s)
    name = REP_NAMES.get(cmd, hex(cmd) if cmd is not None else "no reply")
    print(f"  reply : {name}")
    if cmd == 0x31:
        print_status(data)
    elif cmd == 0x02:
        print("  target has no WiFi support (non-ESP32, or a non-WiFi ESP32 build)")
    s.close()
    sys.exit(0 if cmd == 0x31 else 1)

if args.clear:
    print(f"CMD_WIFI_CLEAR on {PORT}")
    send(s, CMD_WIFI_CLEAR)
    cmd, data = recv(s)
    name = REP_NAMES.get(cmd, hex(cmd) if cmd is not None else "no reply")
    print(f"  reply : {name}")
    s.close()
    sys.exit(0 if cmd == 0x30 else 1)

# --ssid / --password
ssid_b = args.ssid.encode()
pass_b = args.password.encode()
payload = bytes([len(ssid_b)]) + ssid_b + bytes([len(pass_b)]) + pass_b

print(f"CMD_WIFI_SET on {PORT} (ssid={args.ssid!r})")
send(s, CMD_WIFI_SET, payload)
cmd, data = recv(s)
name = REP_NAMES.get(cmd, hex(cmd) if cmd is not None else "no reply")
print(f"  reply : {name}")
if cmd != 0x30:
    print("  provisioning failed" if cmd == 0x02 else "  unexpected reply")
    s.close()
    sys.exit(1)

print("  connecting", end="", flush=True)
connected = False
for _ in range(20):
    time.sleep(0.5)
    print(".", end="", flush=True)
    send(s, CMD_WIFI_QUERY)
    cmd, data = recv(s, timeout=2.0)
    parsed = parse_status(data) if cmd == 0x31 else None
    if parsed and parsed[0] == 2:
        connected = True
        break
print()

if connected:
    print("  connected:")
    print_status(data)
else:
    print("  still not connected after 10s -- check the credentials, or check")
    print("  status again later with --status")

s.close()
sys.exit(0 if connected else 1)
