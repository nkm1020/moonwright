"""The figures of every sentence a person reads.

The solver's hints, the validator's verdicts, the relaxation advice and the simulator's events
all render their numbers here, so one screen never shows "5,000 Wh" next to "5000 Wh": whole
Wh and h with thousands separators and a spaced unit, decimals only where a sentence needs
them, real plurals. (:func:`moonwright.schemas.fmt_wh` is the exact counterpart: a Wh figure
to the milli-Wh, for the verdicts that compare figures exactly.)
"""

from __future__ import annotations

from collections.abc import Sequence

from moonwright.schemas import MWH_PER_WH


def whole_wh(mwh: int) -> int:
    """Milli-Wh as whole Wh, halves rounded up (``round`` would make 500 mWh "0 Wh")."""
    return (mwh + MWH_PER_WH // 2) // MWH_PER_WH


def wh(mwh: int, decimals: int = 0) -> str:
    """'9,930 Wh': milli-Wh as a rounded Wh figure with thousands separators and the unit.
    ``decimals`` (0 to 3) keeps that many decimals, rounded half up on the integer milli-Wh
    exactly as :func:`moonwright.pipeline.schedule._decimals_apart` counts them (the binary
    float of the quotient would print 1,000.005 as "1,000" at two decimals and undo the
    decimals that were chosen to keep two figures apart); trailing zeros are dropped:
    ``wh(1784385, 2)`` is '1,784.39 Wh', ``wh(1000005, 2)`` is '1,000.01 Wh'."""
    decimals = max(0, min(decimals, 3))
    unit = MWH_PER_WH // 10 ** decimals
    whole, frac = divmod((mwh + unit // 2) // unit, 10 ** decimals)
    text = f"{whole:,}"
    if frac:
        text += f".{frac:0{decimals}d}".rstrip("0")
    return f"{text} Wh"


def h(hours: int) -> str:
    """'62 h', '1,200 h'."""
    return f"{hours:,} h"


def plural(count: int, noun: str) -> str:
    """'1 excavator', '2 excavators'."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def an(noun: str) -> str:
    """'an excavator', 'a hauler'."""
    return f"an {noun}" if noun[:1].lower() in "aeiou" else f"a {noun}"


def join_and(parts: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]
