"""Token Factory list prices for the Nemotron models Moonwright routes to.

One table, ``model id -> (input, output)`` in USD per million tokens, copied from the price
comment in ``backend/.env.example`` (checked against the Token Factory catalogue on
2026-10-22). It feeds the ``cost_usd`` column of ``moonwright eval``, the replan evaluation and
the call-log report, so every dollar figure in the results tables comes from the same source.

The ids are case-exact catalogue ids, but :func:`price_for` also accepts a different casing
(a response's ``model`` field echoes the requested id, which is what the call log records) and
the ``served_as`` spelling. A model that is not in the table prices as ``None`` rather than
zero: an unknown model is "not priced", never "free". ``nvidia/Nemotron-3.5-Super-VL`` is served
by the catalogue but carries no price in ``.env.example``, so it stays unpriced until measured.
"""

from __future__ import annotations

from typing import Final

Price = tuple[float, float]   # (input, output) USD per 1M tokens

PRICES_USD_PER_M: Final[dict[str, Price]] = {
    "nvidia/Nemotron-3_5-Lightning": (0.06, 0.24),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06, 0.24),
}

_BY_LOWER: Final[dict[str, Price]] = {k.lower(): v for k, v in PRICES_USD_PER_M.items()}
TOKENS_PER_UNIT: Final = 1_000_000


def price_for(model: str | None) -> Price | None:
    """``(input, output)`` USD per 1M tokens for ``model``, or None when it is not priced."""
    if not model:
        return None
    exact = PRICES_USD_PER_M.get(model)
    if exact is not None:
        return exact
    return _BY_LOWER.get(model.strip().lower())


def cost_usd(model: str | None, input_tokens: int | None, output_tokens: int | None,
             *, decimals: int | None = 6) -> float | None:
    """What one call (or a sum of calls) of ``model`` cost in USD, ``None`` for an unpriced
    model. Missing token counts count as zero (a failed request reports no usage).
    ``decimals=None`` leaves the figure unrounded, for callers that sum many calls and round
    the total once."""
    price = price_for(model)
    if price is None:
        return None
    per_in, per_out = price
    total = ((input_tokens or 0) * per_in + (output_tokens or 0) * per_out) / TOKENS_PER_UNIT
    return total if decimals is None else round(total, decimals)


def format_usd(value: float | None, *, decimals: int = 4) -> str:
    """``$0.0312`` for a known cost, ``-`` for an unpriced one (tables never print a zero for
    "we do not know")."""
    if value is None:
        return "-"
    return f"${value:.{decimals}f}"
