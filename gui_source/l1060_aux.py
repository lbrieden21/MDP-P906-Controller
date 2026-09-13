"""Qt-free helpers for the L1060 auxiliary workflows (Sweep/Sequence).

Kept free of PyQt and device-API imports so the state-machine/parsing logic
here can be exercised by plain unittest without a running Qt event loop or
hardware.
"""

import datetime
from typing import Dict, List, NamedTuple, Tuple, Union


def build_sweep_targets(
    start: float, stop: float, step: float, lo: float, hi: float
) -> List[float]:
    """Build an inclusive ascending or descending list of sweep targets.

    step is a positive magnitude (direction is inferred from start/stop).
    Raises ValueError for a non-positive step, equal endpoints, or an
    endpoint outside [lo, hi]. Target values are computed as start plus a
    multiple of step (not by repeated addition), so rounding error can't
    accumulate across a long sweep; the final value is always exactly stop.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    if start == stop:
        raise ValueError("start and stop must differ")
    if not (lo <= start <= hi):
        raise ValueError(f"start {start} is out of range [{lo}, {hi}]")
    if not (lo <= stop <= hi):
        raise ValueError(f"stop {stop} is out of range [{lo}, {hi}]")

    sign = 1.0 if stop > start else -1.0
    span = abs(stop - start)
    n = max(1, round(span / step))
    targets = [round(start + sign * step * i, 9) for i in range(n)]
    targets.append(round(stop, 9))
    return targets


######### Sequence actions #########

_WAIT_FORMAT = "%Y-%m-%d %H:%M:%S"


class DelayAction(NamedTuple):
    ms: int


class WaitAction(NamedTuple):
    at: datetime.datetime


class SetAction(NamedTuple):
    mode: str
    value: float


SequenceAction = Union[DelayAction, WaitAction, SetAction]

ModeRanges = Dict[str, Tuple[float, float, float]]
ModeUnits = Dict[str, str]


def format_delay_action(ms: int) -> str:
    return f"DELAY {ms} ms"


def format_wait_action(at: datetime.datetime) -> str:
    return f"WAIT {at.strftime(_WAIT_FORMAT)}"


def format_set_action(mode: str, value: float, unit: str) -> str:
    return f"SET-{mode} {value:.3f} {unit}"


def parse_sequence_line(
    line: str, mode_ranges: ModeRanges, mode_units: ModeUnits
) -> SequenceAction:
    """Strictly parse one saved/typed sequence line into a typed action.

    mode_ranges maps each CC/CV/CR/CP mode to (lo, hi, step); mode_units maps
    it to its expected unit suffix. Raises ValueError describing the problem
    for anything that doesn't match exactly: extra/missing tokens, an
    unrecognized action, a value outside its mode's range, or a mismatched
    unit suffix.
    """
    parts = line.split()
    if not parts:
        raise ValueError("empty line")
    action = parts[0]
    if action == "DELAY":
        if len(parts) != 3 or parts[2] != "ms":
            raise ValueError(f"malformed DELAY line: {line!r}")
        try:
            ms = int(parts[1])
        except ValueError:
            raise ValueError(f"malformed DELAY duration: {line!r}") from None
        if ms < 0:
            raise ValueError(f"DELAY duration must be non-negative: {line!r}")
        return DelayAction(ms)
    if action == "WAIT":
        if len(parts) != 3:
            raise ValueError(f"malformed WAIT line: {line!r}")
        try:
            at = datetime.datetime.strptime(f"{parts[1]} {parts[2]}", _WAIT_FORMAT)
        except ValueError:
            raise ValueError(f"malformed WAIT timestamp: {line!r}") from None
        return WaitAction(at)
    if action.startswith("SET-"):
        mode = action[len("SET-"):]
        if mode not in mode_ranges:
            raise ValueError(f"unknown SET mode: {line!r}")
        if len(parts) != 3:
            raise ValueError(f"malformed SET line: {line!r}")
        try:
            value = float(parts[1])
        except ValueError:
            raise ValueError(f"malformed SET value: {line!r}") from None
        if parts[2] != mode_units[mode]:
            raise ValueError(f"unit mismatch for {mode}: {line!r}")
        lo, hi, _ = mode_ranges[mode]
        if not (lo <= value <= hi):
            raise ValueError(
                f"{mode} value {value} out of range [{lo}, {hi}]: {line!r}"
            )
        return SetAction(mode, value)
    raise ValueError(f"unrecognized action: {line!r}")


def parse_sequence_lines(
    lines: List[str], mode_ranges: ModeRanges, mode_units: ModeUnits
) -> List[SequenceAction]:
    """Parse every line, raising ValueError on the first bad one (prefixed
    with its 1-based line number) so a caller can reject an entire loaded
    file before replacing a previously loaded, valid sequence."""
    actions = []
    for i, line in enumerate(lines):
        try:
            actions.append(parse_sequence_line(line, mode_ranges, mode_units))
        except ValueError as exc:
            raise ValueError(f"line {i + 1}: {exc}") from exc
    return actions
