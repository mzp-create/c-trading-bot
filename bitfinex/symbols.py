"""Single source of truth for symbol conversion (spot-margin).

display form:  BTC/USDT   (base/quote, what the bot and dashboard use)
bitfinex form: tBTCUST    (t-prefixed; USDT is "UST" on Bitfinex)
"""

# display quote -> bitfinex quote, and the inverse.
_DISPLAY_TO_BFX_QUOTE = {"USDT": "UST", "USD": "USD"}
_BFX_TO_DISPLAY_QUOTE = {"UST": "USDT", "USD": "USD"}


def to_bitfinex(symbol: str) -> str:
    """'BTC/USDT' -> 'tBTCUST'. Pass-through if already bitfinex form."""
    if symbol.startswith("t") and "/" not in symbol:
        return symbol
    if "/" not in symbol:
        raise ValueError(f"not a display symbol: {symbol!r}")
    base, quote = symbol.split("/", 1)
    quote = quote.split(":", 1)[0]            # drop any :USDT margin suffix
    if quote not in _DISPLAY_TO_BFX_QUOTE:
        raise ValueError(f"unsupported quote in {symbol!r}")
    return f"t{base}{_DISPLAY_TO_BFX_QUOTE[quote]}"


def to_display(symbol: str) -> str:
    """'tBTCUST' -> 'BTC/USDT'. Tolerates the 'tBTCF0:USTF0' derivative form.
    Pass-through if already display form."""
    if "/" in symbol:
        return symbol.split(":", 1)[0]
    if not symbol.startswith("t"):
        raise ValueError(f"not a bitfinex symbol: {symbol!r}")
    body = symbol[1:]
    # Derivative form, e.g. BTCF0:USTF0 -> base BTC, quote UST.
    if ":" in body:
        left = body.split(":", 1)[0]          # 'BTCF0'
        base = left.replace("F0", "")
        right = body.split(":", 1)[1]         # 'USTF0'
        bfx_quote = right.replace("F0", "")
    else:
        bfx_quote = None
        for q in _BFX_TO_DISPLAY_QUOTE:
            if body.endswith(q):
                bfx_quote = q
                base = body[: -len(q)]
                break
        if bfx_quote is None:
            raise ValueError(f"cannot parse bitfinex symbol {symbol!r}")
    if bfx_quote not in _BFX_TO_DISPLAY_QUOTE:
        raise ValueError(f"unsupported quote in {symbol!r}")
    return f"{base}/{_BFX_TO_DISPLAY_QUOTE[bfx_quote]}"
