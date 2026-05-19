"""
Shared utility functions for the trading bot.

Currently provides ``_flatten_ta()`` to normalize nested TA dicts
for strategy consumption, extracting from ``indicators``, ``bollinger``,
``macd``, and ``volume_profile`` sub-dicts.
"""


def flatten_ta(ta_dict: dict) -> dict:
    """Flatten nested TA dict into flat keys that strategies expect."""
    flat = {
        "current_price": ta_dict.get("current_price", 0),
        "close": ta_dict.get("current_price", 0),
        "signal": ta_dict.get("signal", "HOLD"),
        "confidence": ta_dict.get("confidence", 0.0),
        "trend": ta_dict.get("trend", "neutral"),
    }
    # Indicators sub-dict (most important)
    ind = ta_dict.get("indicators", {})
    for k, v in ind.items():
        flat[k] = v
    # Top-level TA values
    for k in ["rsi", "ema_9", "ema_21"]:
        if k in ta_dict:
            flat[k] = ta_dict[k]
    # Volume
    vp = ta_dict.get("volume_profile", {})
    flat["volume"] = vp.get("current", ind.get("volume", 0))
    flat["volume_sma"] = vp.get("sma", ind.get("volume_sma", 0))
    flat["volume_avg"] = flat["volume_sma"]
    # Bollinger
    bb = ta_dict.get("bollinger", {})
    flat["bb_upper"] = bb.get("upper", ind.get("bb_upper", 0))
    flat["bb_lower"] = bb.get("lower", ind.get("bb_lower", 0))
    flat["bb_mid"] = bb.get("middle", ind.get("bb_middle", 0))
    flat["bb_ma"] = flat["bb_mid"]
    flat["bb_position"] = bb.get("position", 0)
    # MACD
    macd = ta_dict.get("macd", {})
    flat["macd"] = macd.get("value", ind.get("macd", 0))
    flat["macd_histogram"] = macd.get("histogram", ind.get("macd_histogram", 0))
    flat["macd_hist"] = flat["macd_histogram"]
    flat["macd_histogram_prev"] = macd.get("histogram_prev", ind.get("macd_histogram_prev", flat["macd_hist"]))
    flat["macd_hist_prev"] = flat["macd_histogram_prev"]
    flat["macd_signal"] = macd.get("signal", ind.get("macd_signal", 0))
    # ATR / vol
    flat["atr"] = ind.get("atr", 0)
    bb_u = flat.get("bb_upper", 0)
    bb_l = flat.get("bb_lower", 0)
    flat["bb_width"] = bb_u - bb_l if bb_u and bb_l else 0
    # bb_prev_width — estimate from second-to-last candle if possible
    bb_prev_u = ind.get("bb_upper_prev", 0) or flat["bb_upper"]
    bb_prev_l = ind.get("bb_lower_prev", 0) or flat["bb_lower"]
    flat["bb_prev_width"] = bb_prev_u - bb_prev_l if bb_prev_u and bb_prev_l else flat["bb_width"]
    return flat
