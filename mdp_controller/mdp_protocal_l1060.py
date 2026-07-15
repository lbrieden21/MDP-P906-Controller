from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from mdp_controller.mdp_protocal import (  # noqa: F401 (re-exported, protocol-generic)
    calc_checksum,
    gen_call_for_id,
    gen_dispatch_ch_addr,
    gen_packet,
    gen_set_led_color,
    parse_type5_response,
    parse_type6_response,
)

MODE_CODES = {"CC": 0, "CV": 1, "CR": 2, "CP": 3}
MODE_NAMES = {v: k for k, v in MODE_CODES.items()}

TYPE10_PAGE_FAMILIES = {
    "0203": "CC",
    "0303": "CV",
    "0d03": "CR",
    "0e03": "CP",
}

TYPE8_WRAP = 4096
TYPE9_NORMAL_WORD = 0x0003
TYPE9_PROTECTION_LATCHED_WORD = 0x0103


def _common_prefix(idcode: bytes, m01_channel: int, blink: bool) -> str:
    return "{}{:02x}{:02x}".format(idcode.hex(), m01_channel, 1 if blink else 0)


def _decode_base100(value_bytes: bytes) -> Optional[int]:
    """Decode bytes that each hold one base-100 decimal digit pair (0-99)."""
    if any(b > 99 for b in value_bytes):
        return None
    result = 0
    for b in value_bytes:
        result = result * 100 + b
    return result


def _decode_bcd_tenths(value_bytes: bytes) -> Optional[float]:
    text = value_bytes.hex()
    if any(ch not in "0123456789" for ch in text):
        return None
    return int(text) / 10.0


def _decode_bcd_thousandths(value_bytes: bytes) -> Optional[float]:
    text = value_bytes.hex()
    if any(ch not in "0123456789" for ch in text):
        return None
    return int(text) / 1000.0


def _decode_digit_voltage(byte_a: int, byte_b: int) -> Optional[float]:
    text = "{:02x}{:02x}".format(byte_a, byte_b)
    if any(ch not in "0123456789" for ch in text):
        return None
    return int(text) / 1000.0


# ---------------------------------------------------------------------------
# Type 7: get / live measurement (exact base-100 digit-pair decode -- no
# gain/offset correction needed, unlike P906). Setters for all four target
# registers (CC/CV/CR/CP) live in the Type 10 section below -- see the note
# there on why CV/CC used to ride this channel and no longer do.
# ---------------------------------------------------------------------------


@lru_cache()
def gen_get_type7(idcode: bytes, m01_channel: int = 0, blink: bool = True) -> bytes:
    return gen_packet(7, bytes.fromhex(_common_prefix(idcode, m01_channel, blink)))


def _parse_sample_slots(sample_data: bytes) -> List[dict]:
    """
    5-byte base-100 slots: [volts, 10mV, 0.1mV, 0.1A, 1mA], oldest-first. The
    last slot may be a truncated 4-byte tail (voltage only, no mA digit).
    """
    slots = []
    for offset in range(0, len(sample_data), 5):
        slot = sample_data[offset : offset + 5]
        if len(slot) < 4:
            continue
        v_raw = _decode_base100(slot[0:3])
        i_raw = _decode_base100(slot[3:5]) if len(slot) == 5 else None
        voltage = round(v_raw * 0.0001, 4) if v_raw is not None else None
        current = round(i_raw * 0.001, 3) if i_raw is not None else None
        slots.append({"voltage": voltage, "current": current, "complete": len(slot) == 5})
    return slots


def _select_latest_slot(slots: List[dict]) -> Optional[dict]:
    fallback = None
    for slot in reversed(slots):
        if slot["voltage"] is None:
            continue
        if slot["complete"] and slot["current"] is not None:
            return slot
        if fallback is None:
            fallback = slot
    return fallback


def parse_type7_response(data: bytes) -> dict:
    """
    Type 7 response contains live measurement + status of the L1060.

    Returns a dict: errflag, temperature, load_mode, load_active (current
    actually flowing, the confirm signal for set_load_on -- L1060's Type 7
    has no LoadEnabled bit, that's Type 10 only), input_voltage (USB/power
    rail, NOT load-terminal voltage), voltage, current.
    """
    assert data[0] == 7
    length = data[1]
    payload = data[2 : 2 + length]
    errflag = payload[0] if len(payload) >= 1 else 0
    temperature = _decode_bcd_tenths(payload[2:4]) if len(payload) >= 4 else None
    mode_code = payload[4] if len(payload) >= 5 else None
    load_mode = MODE_NAMES.get(mode_code, "Unknown")
    input_voltage = (
        _decode_digit_voltage(payload[6], payload[7]) if len(payload) >= 8 else None
    )
    slots = _parse_sample_slots(payload[8:]) if len(payload) >= 12 else []
    latest = _select_latest_slot(slots)
    voltage = latest["voltage"] if latest else None
    current = latest["current"] if latest else None
    load_active = current > 0.005 if current is not None else None
    return {
        "errflag": errflag,
        "temperature": temperature,
        "load_mode": load_mode,
        "load_active": load_active,
        "input_voltage": input_voltage,
        "voltage": voltage,
        "current": current,
    }


# ---------------------------------------------------------------------------
# Type 8: realtime readback, 9 samples of 12-bit (voltage_mV, current_mA)
# wrapped mod 4096. Unwrapping needs a reference near the true value -- the
# most recent Type-7 slot decode (exact/canonical). This parser stays pure:
# the caller supplies the anchor and owns its freshness.
# ---------------------------------------------------------------------------


@lru_cache()
def gen_get_type8(idcode: bytes, m01_channel: int = 0, blink: bool = True) -> bytes:
    return gen_packet(8, bytes.fromhex(_common_prefix(idcode, m01_channel, blink)))


def _unwrap(value: int, reference: Optional[int]) -> int:
    if reference is None:
        return value
    candidates = [value + TYPE8_WRAP * k for k in range(8)]
    return min(candidates, key=lambda c: abs(c - reference))


def parse_type8_response(
    data: bytes, anchor_voltage: Optional[float], anchor_current: Optional[float]
) -> Tuple[int, List[Tuple[float, float]]]:
    assert data[0] == 8
    errflag = data[2] if len(data) > 2 else 0
    ref_mv = None if anchor_voltage is None else int(round(anchor_voltage * 1000))
    ref_ma = None if anchor_current is None else int(round(anchor_current * 1000))
    values = []
    for i in range(0, data[1] - 1, 3):
        sd = data[2 + 1 + i : 2 + 1 + i + 3].hex()
        raw_v = int(sd[0:3], 16)
        raw_i = int(sd[3:6], 16)
        v_mv = _unwrap(raw_v, ref_mv)
        i_ma = _unwrap(raw_i, ref_ma)
        values.append((v_mv / 1000.0, i_ma / 1000.0))
    return errflag, values


# ---------------------------------------------------------------------------
# Type 9: LED-color write (request format shared with P906, see
# gen_set_led_color import above) / protection-latch get. Response payload is
# idcode(4) + word(2); word flips 0x0003 -> 0x0103 while ANY protection fault
# is latched -- a generic flag, it does not identify which fault (that's
# Type 10's job, see _classify_protection below).
# ---------------------------------------------------------------------------


def parse_type9_response(data: bytes) -> Tuple[bytes, Optional[bool]]:
    assert data[0] == 9
    assert data[1] == 6
    idcode = data[2:6]
    word = int.from_bytes(data[6:8], "big")
    if word == TYPE9_NORMAL_WORD:
        latched = False
    elif word == TYPE9_PROTECTION_LATCHED_WORD:
        latched = True
    else:
        latched = None
    return idcode, latched


# ---------------------------------------------------------------------------
# Type 10: mode select / load switch / set current, voltage, resistance &
# power / target-page get. Each get response carries one of four rotating
# target pages (CC->CR->CP->CV->CC...), self-reported via the trailing 5
# bytes (family(2) + value(3)), advancing per get the device receives (not
# per response reaching the host -- both directions can drop independently).
#
# gen_set_current/gen_set_voltage used to ride Type 7 instead (the live
# measurement channel), tagged with the same family codes used here. That
# didn't isolate CC/CV from the disturbance it was meant to avoid: writing
# the CV target register flips the device's reported LoadMode to CV even
# when sent as this passive Type-10 page write, with no gen_select_mode
# alongside it (hardware-confirmed). So the transient when editing CV while
# CC is active is inherent to writing that register at all, not a Type-7
# side effect -- moving to Type 10 here is for protocol consistency (one
# write path for all four targets), not a fix for that transient.
# ---------------------------------------------------------------------------


def gen_get_type10(idcode: bytes, m01_channel: int = 0, blink: bool = True) -> bytes:
    return gen_packet(10, bytes.fromhex(_common_prefix(idcode, m01_channel, blink)))


def gen_set_current(
    idcode: bytes, current: float, m01_channel: int = 0, blink: bool = True
) -> bytes:
    assert 0.0 < current < 10.0
    milliamps = round(current * 1000)
    d = _common_prefix(idcode, m01_channel, blink) + "0203"
    return gen_packet(10, bytes.fromhex(d) + bytes.fromhex("{:06d}".format(milliamps)))


def gen_set_voltage(
    idcode: bytes, voltage: float, m01_channel: int = 0, blink: bool = True
) -> bytes:
    assert 0.0 <= voltage <= 30.0
    millivolts = round(voltage * 1000)
    d = _common_prefix(idcode, m01_channel, blink) + "0303"
    return gen_packet(10, bytes.fromhex(d) + bytes.fromhex("{:06d}".format(millivolts)))


def gen_set_resistance(
    idcode: bytes, resistance: float, m01_channel: int = 0, blink: bool = True
) -> bytes:
    assert 0.01 <= resistance <= 999.999
    milliohms = round(resistance * 1000)
    d = _common_prefix(idcode, m01_channel, blink) + "0d03"
    return gen_packet(10, bytes.fromhex(d) + milliohms.to_bytes(3, "big"))


def gen_set_power(
    idcode: bytes, power: float, m01_channel: int = 0, blink: bool = True
) -> bytes:
    assert 0.0 <= power <= 999.999
    milliwatts = round(power * 1000)
    d = _common_prefix(idcode, m01_channel, blink) + "0e03"
    return gen_packet(10, bytes.fromhex(d) + bytes.fromhex("{:06d}".format(milliwatts)))


def gen_select_mode(
    idcode: bytes, mode: str, m01_channel: int = 0, blink: bool = True
) -> bytes:
    mode_code = MODE_CODES[mode.upper()]
    d = _common_prefix(idcode, m01_channel, blink) + "0b01{:02x}".format(mode_code)
    return gen_packet(10, bytes.fromhex(d))


def gen_set_load_switch(
    idcode: bytes, state: bool, m01_channel: int = 0, blink: bool = True
) -> bytes:
    d = _common_prefix(idcode, m01_channel, blink) + "0c01{:02x}".format(
        1 if state else 0
    )
    return gen_packet(10, bytes.fromhex(d))


def _classify_protection(status_data: bytes) -> str:
    """
    OVP and OCP are individually distinguishable; OPP and UVP are proven
    byte-identical on the wire (exhaustively verified, full payload) -- this
    reports the honest combined label rather than guessing between them.
    """
    sd8, sd10 = status_data[8], status_data[10]
    if sd10 != 0x06:
        return "OVP"
    if sd8 == 0x10:
        return "OCP"
    return "OPP_OR_UVP"


def parse_type10_response(data: bytes) -> Dict:
    assert data[0] == 10
    length = data[1]
    sd = data[2 : 2 + length]
    result: Dict = {
        "errflag": sd[0] if len(sd) >= 1 else None,
        "temperature": _decode_bcd_tenths(sd[2:4]) if len(sd) >= 4 else None,
        "load_mode": None,
        "load_enabled": None,
        "input_voltage": None,
        "page": None,
        "target_value": None,
        "protection": None,
    }
    if len(sd) >= 8:
        mode_code = sd[4] >> 4
        flags = sd[4] & 0x0F
        result["load_mode"] = MODE_NAMES.get(mode_code, "Unknown")
        result["load_enabled"] = bool(flags & 0x01)
        result["input_voltage"] = _decode_digit_voltage(sd[6], sd[7])
    if len(sd) >= 28:
        tail = sd[-5:]
        family = tail[:2].hex()
        page = TYPE10_PAGE_FAMILIES.get(family)
        if page is not None:
            if family == "0d03":
                value = int.from_bytes(tail[2:], "big") / 1000.0
            else:
                value = _decode_bcd_thousandths(tail[2:])
            result["page"] = page
            result["target_value"] = value
    if result["errflag"] and result["load_enabled"] is False and len(sd) >= 12:
        result["protection"] = _classify_protection(sd)
    return result
