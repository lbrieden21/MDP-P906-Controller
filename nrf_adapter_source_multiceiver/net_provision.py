"""Network credential provisioning for WiFi and Ethernet host links, over the
wired serial link -- target-neutral in the sense that every target answers
these commands (CMD_NET_CREDS_SET / CMD_NET_QUERY / CMD_NET_CREDS_CLEAR /
CMD_NET_IP_SET, see core/protocol.h), but only a WiFi or Ethernet build does
anything with them. Every other target's platform.h hooks stub to 0, which
this script sees as REP_CMD_FAILED.

Follows host_link_test.py / persistence_test.py's argparse style and shares
their framing helpers and DTR/RTS-low-before-open workaround.

Usage:
    python net_provision.py --port /dev/ttyACM0 --ssid MyNetwork --password hunter2
    python net_provision.py --port /dev/ttyACM0 --status
    python net_provision.py --port /dev/ttyACM0 --clear
    python net_provision.py --port /dev/ttyACM0 --dhcp
    python net_provision.py --port /dev/ttyACM0 --static 192.168.1.50 \\
        --mask 255.255.255.0 --gateway 192.168.1.1
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
parser.add_argument("--dhcp", action="store_true",
                     help="switch to DHCP addressing (Ethernet targets only)")
parser.add_argument("--static", metavar="IP",
                     help="static IP address to provision, with --mask/--gateway "
                          "(Ethernet targets only)")
parser.add_argument("--mask", metavar="MASK", help="static subnet mask, with --static")
parser.add_argument("--gateway", metavar="GW", help="static gateway, with --static")
args = parser.parse_args()
PORT = args.port

static_group = [args.static, args.mask, args.gateway]
actions = [bool(args.ssid or args.password), args.status, args.clear, args.dhcp, any(static_group)]
if sum(actions) != 1:
    parser.error("pick exactly one of: --ssid/--password, --status, --clear, --dhcp, "
                  "--static/--mask/--gateway")
if bool(args.ssid) != bool(args.password):
    parser.error("--ssid and --password must be given together")
if args.ssid and len(args.ssid.encode()) > 32:
    parser.error("--ssid must be at most 32 bytes")
if args.password and len(args.password.encode()) > 64:
    parser.error("--password must be at most 64 bytes")
if any(static_group) and not all(static_group):
    parser.error("--static, --mask and --gateway must be given together")


def parse_ipv4(s, label):
    parts = s.split(".")
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        octets = None
    if octets is None or len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        parser.error(f"{label} must be a dotted-quad IPv4 address")
    return bytes(octets)


if args.static:
    STATIC_IP = parse_ipv4(args.static, "--static")
    STATIC_MASK = parse_ipv4(args.mask, "--mask")
    STATIC_GW = parse_ipv4(args.gateway, "--gateway")

CMD_NET_CREDS_SET = 0x30
CMD_NET_QUERY = 0x31
CMD_NET_CREDS_CLEAR = 0x32
CMD_NET_IP_SET = 0x33

REP_NAMES = {
    0x00: "REP_UNKNOWN_CMD", 0x01: "REP_INVALID_CMD", 0x02: "REP_CMD_FAILED",
    0x30: "REP_NET_ACK", 0x31: "REP_NET_STATUS",
}

STATE_NAMES = {0: "disconnected", 1: "connecting", 2: "connected"}
MODE_NAMES = {0: "DHCP", 1: "static"}

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
    # the C6, and losing that race hands CMD_NET_CREDS_SET's reply slot to the
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
    if data is None or len(data) < 16:
        return None
    state = data[0]
    mode = data[1]
    ip = data[2:6]
    mask = data[6:10]
    gw = data[10:14]
    rssi = data[14] - 256 if data[14] >= 128 else data[14]
    ssid_len = data[15]
    ssid = data[16:16 + ssid_len].decode(errors="replace")
    return state, mode, ip, mask, gw, rssi, ssid


def print_status(data):
    parsed = parse_status(data)
    if parsed is None:
        print("  malformed REP_NET_STATUS payload:", data.hex() if data else None)
        return
    state, mode, ip, mask, gw, rssi, ssid = parsed
    print(f"  state : {STATE_NAMES.get(state, state)}")
    print(f"  mode  : {MODE_NAMES.get(mode, mode)}")
    # A static config is meaningful as soon as it's stored, link or no link --
    # an Ethernet target with no live transport yet can never reach state
    # "connected", so gating this on state alone would hide the very thing
    # being verified. DHCP-leased fields, and rssi/ssid, are only meaningful
    # once actually connected.
    if state == 2 or mode == 1:
        print(f"  ip      : {ip[0]}.{ip[1]}.{ip[2]}.{ip[3]}")
        print(f"  mask    : {mask[0]}.{mask[1]}.{mask[2]}.{mask[3]}")
        print(f"  gateway : {gw[0]}.{gw[1]}.{gw[2]}.{gw[3]}")
    if state == 2 and ssid:
        print(f"  rssi  : {rssi} dBm")
        print(f"  ssid  : {ssid}")


s = open_port()

if args.status:
    print(f"CMD_NET_QUERY on {PORT}")
    send(s, CMD_NET_QUERY)
    cmd, data = recv(s)
    name = REP_NAMES.get(cmd, hex(cmd) if cmd is not None else "no reply")
    print(f"  reply : {name}")
    if cmd == 0x31:
        print_status(data)
    elif cmd == 0x02:
        print("  target has no network support (non-WiFi, non-Ethernet build)")
    s.close()
    sys.exit(0 if cmd == 0x31 else 1)

if args.clear:
    print(f"CMD_NET_CREDS_CLEAR on {PORT}")
    send(s, CMD_NET_CREDS_CLEAR)
    cmd, data = recv(s)
    name = REP_NAMES.get(cmd, hex(cmd) if cmd is not None else "no reply")
    print(f"  reply : {name}")
    s.close()
    sys.exit(0 if cmd == 0x30 else 1)

if args.dhcp:
    print(f"CMD_NET_IP_SET (DHCP) on {PORT}")
    payload = bytes([0]) + bytes(12)  # mode=0; ip/mask/gw unused in DHCP mode
    send(s, CMD_NET_IP_SET, payload)
    cmd, data = recv(s)
    name = REP_NAMES.get(cmd, hex(cmd) if cmd is not None else "no reply")
    print(f"  reply : {name}")
    if cmd == 0x02:
        print("  target has no network support, or Ethernet not built in")
    s.close()
    sys.exit(0 if cmd == 0x30 else 1)

if args.static:
    print(f"CMD_NET_IP_SET (static {args.static}) on {PORT}")
    payload = bytes([1]) + STATIC_IP + STATIC_MASK + STATIC_GW
    send(s, CMD_NET_IP_SET, payload)
    cmd, data = recv(s)
    name = REP_NAMES.get(cmd, hex(cmd) if cmd is not None else "no reply")
    print(f"  reply : {name}")
    if cmd == 0x02:
        print("  target has no network support, or Ethernet not built in")
    s.close()
    sys.exit(0 if cmd == 0x30 else 1)

# --ssid / --password
ssid_b = args.ssid.encode()
pass_b = args.password.encode()
payload = bytes([len(ssid_b)]) + ssid_b + bytes([len(pass_b)]) + pass_b

print(f"CMD_NET_CREDS_SET on {PORT} (ssid={args.ssid!r})")
send(s, CMD_NET_CREDS_SET, payload)
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
    send(s, CMD_NET_QUERY)
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
