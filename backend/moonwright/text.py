"""The figures of every sentence a person reads.

The solver's hints, the validator's verdicts, the relaxation advice and the simulator's events
all render their numbers here, so one screen never shows "5,000 Wh" next to "5000 Wh": whole
Wh and h with thousands separators and a spaced unit, decimals only where a sentence needs
them, real plurals. (:func:`moonwright.schemas.fmt_wh` is the exact counterpart: a Wh figure
to the milli-Wh, for the verdicts that compare figures exactly.) The volume verdicts and
advisories (``volume:<structure>``) quote their m3, m2 and metres through :func:`qty` and
their shares through :func:`pct`; the failure copy of a rejected model answer names the model
through :func:`model_name`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

from moonwright.schemas import MWH_PER_WH

# "Nemotron-3-Ultra-550b-a55b", "nemotron-3-super-120b-a12b", "Nemotron-3_5-Lightning",
# "NVIDIA-Nemotron-3-Nano-30B-A3B": the family, the version (an underscore or a dot between
# major and minor) and the variant, whatever the vendor prefix and the size suffix
_NEMOTRON_RE = re.compile(r"nemotron-(\d+)(?:[_.](\d+))?-(ultra|super|lightning|nano)",
                          re.IGNORECASE)
_LEAD_FIGURE_RE = re.compile(r"\d[\d,]*")   # '1,300' of '1,300 m3 berm', '11' of '11.5 m3'


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


def qty(x: float) -> str:
    """A volume, area or length without its unit: 1300.0 -> '1,300', 125.66 -> '125.7',
    56.55 -> '56.5', 0.15 -> '0.15': whole figures with thousands separators, one decimal from
    10 up, two below it, trailing zeros dropped (0.5 x 1.2 x 3 x 60 is 107.99999999999999 in
    floats and prints '108')."""
    if float(x).is_integer():
        return f"{int(x):,}"
    if abs(x) >= 10:
        return f"{x:,.1f}".removesuffix(".0")
    return f"{x:.2f}".rstrip("0").rstrip(".")


def pct(share: float, *, floor: bool = False) -> str:
    """A share as a whole percentage with a spaced sign: 0.333 -> '33 %', 1.99 -> '199 %'.
    ``floor`` rounds down instead (a shortfall's share: 0.6996 of a "below 70 %" verdict
    must read '69 %', never the threshold itself); a float a hair under a whole percentage
    (0.3 x 100 = 29.999999999999996) still reads '30 %'."""
    if floor:
        return f"{math.floor(share * 100 + 1e-9):.0f} %"
    return f"{share * 100:.0f} %"


def plural(count: int, noun: str) -> str:
    """'1 excavator', '2 excavators'."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def an(noun: str) -> str:
    """'an excavator', 'a hauler'; a phrase that opens with a figure takes the article of the
    figure read aloud (the footprint note's 'an 8 m by 8 m footprint', 'a 12 m by 12 m
    footprint'; 'an 80 m3 berm', 'an 11 m3 trench', 'an 18,000 m3 pit', 'a 180 m3 pit')."""
    figure = _LEAD_FIGURE_RE.match(noun)
    if figure is not None:
        head = figure.group().split(",")[0]
        vowel = head.startswith("8") or head in ("11", "18")
    else:
        vowel = noun[:1].lower() in "aeiou"
    return f"an {noun}" if vowel else f"a {noun}"


def join_and(parts: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def model_name(model_id: str) -> str:
    """The short name of a Token Factory model id, for the sentences a person reads:
    'Nemotron 3 Ultra' for 'nvidia/Nemotron-3-Ultra-550b-a55b', 'Nemotron 3 Super',
    'Nemotron 3.5 Lightning' for 'nvidia/Nemotron-3_5-Lightning', 'Nemotron 3 Nano' for
    'nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B'. Any other id (a fixture name, a test stub, an
    unknown model) is returned as given, so a sentence never loses the model."""
    m = _NEMOTRON_RE.search(model_id)
    if m is None:
        return model_id
    major, minor, variant = m.groups()
    version = f"{major}.{minor}" if minor else major
    return f"Nemotron {version} {variant.capitalize()}"
