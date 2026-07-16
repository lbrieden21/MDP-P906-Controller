"""
Attributes 'NRF send no ack' warnings in a --debug (TRACE-level) mdp.log to
the device + MDP packet type that lost the packet.

Sends are serialized behind MDPBus's lock (one nrf_send() completes -- ack
or no-ack -- before the next begins), so each 'NRF send no ack' response can
be matched to the single most recent NRF_TX ('Sent aa 55 10 ...') line, and
that packet's destination device to the most recent TX-retarget
('Sent aa 55 24 ...') line's address. Device identity for an address is
recovered from the 'opened pipe N -> address' / 'Attached device ... to
pipe N' lines plus which mdp_p906/mdp_l1060 module logged for that pipe.

Usage: venv/bin/python tools/noack_report.py [path/to/mdp.log]
"""

import re
import sys
from datetime import datetime
from pathlib import Path

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
ATTACH_RE = re.compile(r"Attached device \(idcode: ([0-9A-Fa-f]+)\) to pipe (\d+)")
OPENED_PIPE_RE = re.compile(r"NRF24-Adapter opened pipe (\d+) -> ([0-9A-Fa-f:]+)")
MODULE_RE = re.compile(r"\| (mdp_controller\.mdp_\w+):")
SENT_RE = re.compile(r"Sent ((?:[0-9a-fA-F]{2} ?)+)$")
NOACK_RE = re.compile(r"NRF Response: NRF send no ack")
SENDOK_RE = re.compile(r"NRF Response: NRF send success")

BURST_GAP_S = 2.0  # no-acks closer together than this are one burst


def _parse_ts(line: str):
    m = TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")


def analyze(path: Path):
    pipe_addr = {}
    idcode_to_pipe = {}
    addr_last_to_label = {}
    pending_attach_idcode = None
    current_target = None
    pending_send = None  # (ts, label, pkt_type)

    counts = {}  # (label, pkt_type) -> {"sent": int, "noack": int}
    noack_events = []  # (ts, label, pkt_type)
    total_sent = 0
    total_noack = 0

    for line in path.read_text(errors="replace").splitlines():
        ts = _parse_ts(line)

        if m := ATTACH_RE.search(line):
            idcode, pipe = m.group(1).upper(), int(m.group(2))
            idcode_to_pipe[idcode] = pipe
            pending_attach_idcode = idcode
            continue

        if m := OPENED_PIPE_RE.search(line):
            pipe = int(m.group(1))
            pipe_addr[pipe] = bytes.fromhex(m.group(2).replace(":", ""))
            continue

        if pending_attach_idcode is not None and (m := MODULE_RE.search(line)):
            modname = m.group(1)
            if modname in ("mdp_controller.mdp_p906", "mdp_controller.mdp_l1060"):
                label = "P906" if modname.endswith("p906") else "L1060"
                pipe = idcode_to_pipe.get(pending_attach_idcode)
                if pipe is not None and pipe in pipe_addr:
                    addr_last_to_label[pipe_addr[pipe][-1]] = label
                pending_attach_idcode = None
            continue

        if m := SENT_RE.search(line):
            tokens = m.group(1).split()
            if len(tokens) >= 4 and tokens[0].lower() == "aa" and tokens[1].lower() == "55":
                cmd = tokens[2].lower()
                length = int(tokens[3], 16)
                data = tokens[4 : 4 + length]
                if cmd == "24" and data:
                    current_target = bytes(int(b, 16) for b in data)
                elif cmd == "10" and data:
                    pkt_type = int(data[0], 16)
                    last_byte = current_target[-1] if current_target else None
                    label = addr_last_to_label.get(
                        last_byte, f"unknown@{last_byte:02X}" if last_byte is not None else "unknown"
                    )
                    key = (label, pkt_type)
                    counts.setdefault(key, {"sent": 0, "noack": 0})["sent"] += 1
                    total_sent += 1
                    pending_send = (ts, label, pkt_type)
            continue

        if NOACK_RE.search(line):
            if pending_send is not None:
                _, label, pkt_type = pending_send
                counts[(label, pkt_type)]["noack"] += 1
                total_noack += 1
                noack_events.append((ts, label, pkt_type))
                pending_send = None
            continue

        if SENDOK_RE.search(line):
            pending_send = None
            continue

    return counts, noack_events, total_sent, total_noack


def report(path: Path):
    counts, noack_events, total_sent, total_noack = analyze(path)

    print(f"{path}")
    print(f"Total radio sends: {total_sent}, no-acks: {total_noack}", end="")
    if total_sent:
        print(f" ({100 * total_noack / total_sent:.2f}%)")
    else:
        print()
    print()

    print("Per (device, packet type):")
    for (label, pkt_type), c in sorted(counts.items(), key=lambda kv: (-kv[1]["noack"], kv[0])):
        if c["sent"] == 0:
            continue
        pct = 100 * c["noack"] / c["sent"]
        print(f"  {label:8s} Type-{pkt_type:<3d} {c['noack']:4d}/{c['sent']:<5d} ({pct:.2f}%)")

    if not noack_events:
        return
    print()
    print("Bursts (no-acks within {:.1f}s of each other):".format(BURST_GAP_S))
    burst = [noack_events[0]]
    bursts = []
    for ev in noack_events[1:]:
        prev_ts = burst[-1][0]
        if ev[0] is not None and prev_ts is not None and (ev[0] - prev_ts).total_seconds() <= BURST_GAP_S:
            burst.append(ev)
        else:
            bursts.append(burst)
            burst = [ev]
    bursts.append(burst)

    for b in bursts:
        ts0 = b[0][0]
        ts1 = b[-1][0]
        span = (ts1 - ts0).total_seconds() if ts0 and ts1 else 0.0
        devices = ", ".join(sorted({f"{lbl} Type-{t}" for _, lbl, t in b}))
        when = ts0.strftime("%H:%M:%S.%f")[:-3] if ts0 else "?"
        print(f"  {len(b)} no-ack(s) in {span:.2f}s starting {when} -- {devices}")


if __name__ == "__main__":
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("gui_source/mdp.log")
    if not log_path.exists():
        print(f"Log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    report(log_path)
