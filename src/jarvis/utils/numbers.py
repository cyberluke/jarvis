"""Locale-aware number and duration formatting (Czech/European style).

The UI is Czech-first: thousands are separated by a narrow space, the
decimal separator is a comma. A single helper set stops the drift between
"1500ms", "1,000s" (US comma) and "1 500 ms" (Czech) that previously
depended on whichever f-string produced the value.

Examples::

    format_int(1500)        -> "1 500"
    format_decimal(1500.5)  -> "1 500,50"
    format_ms(1500)         -> "1 500 ms"
    format_seconds(1000)    -> "1 000 s"
"""

from __future__ import annotations

from typing import Optional, Union

_NBSP = " "  # plain space thousands separator (Czech style, copy-paste safe)


def format_int(value: Union[int, float, None]) -> str:
    """Integer with a space thousands separator (Czech style)."""
    if value is None:
        return "0"
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return "0"
    sign = "-" if n < 0 else ""
    digits = str(abs(n))
    groups = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    return sign + _NBSP.join(groups)


def format_decimal(value: Union[int, float, None], decimals: int = 2) -> str:
    """Decimal number: space thousands separator, comma decimal point."""
    if value is None:
        value = 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = 0.0
    neg = "-" if f < 0 else ""
    f = abs(f)
    whole = int(f)
    frac = round(f - whole, decimals)
    # Guard against rounding carrying into the whole part (0.999 -> 1.00).
    if frac >= 1.0:
        whole += 1
        frac = 0.0
    whole_part = format_int(whole)
    if decimals <= 0:
        return f"{neg}{whole_part}"
    digits = f"{frac:.{decimals}f}".split(".", 1)[1]
    return f"{neg}{whole_part},{digits}"


def format_ms(ms: Optional[Union[int, float]], decimals: int = 0) -> str:
    """Milliseconds as ``1 500 ms`` (Czech style, space separator)."""
    if ms is None:
        return "–"
    return f"{format_decimal(ms, decimals)}{_NBSP}ms"


def format_seconds(sec: Optional[Union[int, float]], decimals: int = 0) -> str:
    """Seconds as ``1 000 s`` (Czech style, space separator)."""
    if sec is None:
        return "–"
    return f"{format_decimal(sec, decimals)}{_NBSP}s"