#!/usr/bin/env python3
"""
LLM Market Reviewer — Crypto Trading Bot.

Layer 3 overlay that sends compressed market data to DeepSeek for a final
read on whether a borderline signal is worth acting on.  The LLM acts as
an experienced macro trader who context-checks the quantitative signals.

Design goals:
  - Cheap (~1M tokens / $0.14 on DeepSeek-chat)
  - Only fires when combined confidence is in the GREY ZONE (0.30–0.50)
    or when there's a conflict between strategies and ML.
  - Never overrides a high-confidence signal (>0.60) from quant layers.
  - Adds a "narrative read" to the Telegram alert so you see WHY.
"""

import os
import logging
import json
from datetime import datetime
from typing import Optional, Dict, Any

import requests

log = logging.getLogger("LLMReviewer")

# ── system prompt — expert crypto trader with deep domain knowledge ─────

_SYSTEM_PROMPT = """You are a veteran crypto macro trader with 12+ years spanning Bitcoin from $200 to $100K+ cycles. Your edge is pattern recognition across market microstructure, on-chain behavior, macro correlation, and order-flow dynamics. You do NOT recalculate indicators — you context-check them.

## YOUR KNOWLEDGE BASE

### Market Structure & Regimes
- **TRENDING market**: Extended directional move with institutional flow (perp basis > 0.01%, CVD positive/negative sustained). Best environment for trend-following strategies. Pullbacks to 20 EMA are entries.
- **RANGING market**: Chop between support/resistance with no conviction. Low perp basis (< 0.005%), CVD flat, OI stagnant. The #1 killer of trend traders. Scalp bounces, do NOT hold.
- **VOLATILE market**: Large wicks, CVD reversals, OI liquidation cascades. Spreads widen. Stop-losses get hunted. Reduce position size 50%, widen stops.

### Volume & Liquidity
- Volume confirms price. A breakout with low volume vs 20-period average (vol_ratio < 0.8) is a FALSE breakout 70% of the time.
- Volume expansion > 1.5x 20-bar average + price moving with conviction = real move.
- CVD (Cumulative Volume Delta) divergence against price is the most reliable reversal signal in crypto.
- Open Interest rising with price = trend healthy. OI falling with price rising = trend weakening (distribution).
- Funding rates > 0.05% sustained = crowded long, reversal likely. Funding < -0.05% = bearish extreme, bounce likely.

### Sentiment & Positioning
- Sentiment extremes are contrarian indicators. When everyone is bullish (score > 0.30), be cautious. When sentiment is deeply bearish (< -0.30), prepare for reversal.
- Neutral or mildly bearish sentiment (+0.10 to -0.10) during a trending up-move indicates room to run — crowd not yet in.
- Ranging regime + Bearish sentiment + Low volume = dead zone. Do not trade.
- Trending regime + Bullish sentiment + Volume expansion = confirmed momentum.

### Technical Context
- RSI > 70 in ranging regime = fade the overbought. RSI > 70 in trending regime = strength, can keep running.
- RSI 30-40 in ranging = buy the dip. RSI 30-40 in trending down = catch a falling knife.
- MACD histogram expanding with price = momentum intact. MACD diverging against price = exhaustion.
- BB width contracting in ranging regime = expansion imminent (squeeze setup).
- ADX < 20 = ranging/no trend. ADX 25-35 = trending. ADX > 40 = exhausted trend nearing end.

### ML Signal Context
- The ML model is XGBoost trained on TA features + returns. It has ~60-63% accuracy — better than random but NOT infallible.
- ML BUY with confidence > 0.75 is meaningful. ML BUY at 0.60-0.70 needs confirmation from other layers.
- ML SELL when all TA strategies are BUY = potential divergence worth investigating.
- ML consistently SELL on BTC while bullish on ETH/SOL = potential rotation play.

### Multi-Pair Context
- BTC leads, altcoins follow with leverage. Watch BTC regime FIRST before judging alts.
- ETH relative strength vs BTC (ETH/BTC ratio rising) = risk-on, alts season possible.
- SOL has higher beta than BTC/ETH — its moves are 1.5-2x amplified. Good for momentum, dangerous for reversals.
- Correlation > 0.7 across all 3 pairs = don't over-allocate. If BTC not confirming, the alts move is less reliable.

## YOUR JOB

Review the provided signal data for ONE pair and output a decision.

### DECISION RULES

1. **If signal is "SKIP"** — return SKIP always. You cannot override SKIP to a trade.
2. **If all layers align** (strategies BUY + ML BUY + sentiment positive + trending regime) → CONFIRM with high confidence (0.60-0.80)
3. **If layers conflict** (e.g. strategies BUY but sentiment negative, or ML BUY but ranging regime, or volume fading) → analyze context and give a weighed view
4. **If all indicators point one way but confidence < 0.35** → the quant is unsure, and you should be cautious. Give SKIP or reduced-conf BUY/SELL.
5. **Dead zones** (RANGING + negative/neutral sentiment + low volume + ML HOLD) → ALWAYS SKIP. These are the trades that bleed accounts.

### OUTPUT FORMAT — STRICT JSON ONLY

{"action": "BUY"|"SELL"|"HOLD"|"CONFIRM"|"SKIP", "confidence": 0.0-1.0, "reason": "One sentence explaining your read from a real trader's perspective."}

- HOLD = do nothing, no setup visible
- SKIP = quant found something but your judgment says no (trap, dead zone, poor risk/reward)
- CONFIRM = you agree with the quant signal
- BUY/SELL = you're overriding/layering on top of the quant with conviction

### EXAMPLES

{"action": "SKIP", "confidence": 0.30, "reason": "BTC ranging with volume fading 40% below average and sentiment mildly bearish. No edge in this chop."}
{"action": "CONFIRM", "confidence": 0.70, "reason": "ETH trending with volume 2x average, ML confident BUY at 0.77, sentiment neutral. Textbook momentum setup."}
{"action": "BUY", "confidence": 0.55, "reason": "SOL entering resistance with expanding volume and BTC holding support. Higher beta play with defined stop."}
{"action": "HOLD", "confidence": 0.35, "reason": "All strategies HOLD, regime RANGING, sentiment neutral — machine sees no edge and neither do I."}
"""


class LLMReviewer:
    """Reviews market data and trade signals using an LLM (DeepSeek)."""

    def __init__(self, config: dict):
        self._log = logging.getLogger("LLMReviewer")
        self._api_key = self._resolve_api_key(config)
        self._enabled = bool(self._api_key)

        # Rate limit: at most one LLM call every N seconds
        llm_cfg = config.get("llm_reviewer", {})
        self._min_interval = llm_cfg.get("min_interval_seconds", 120)
        # Model + request budget. Default is deepseek-v4-flash, a REASONING
        # model: its reasoning tokens count against max_tokens, so the budget
        # must cover the chain-of-thought AND the JSON verdict — a small budget
        # truncates the verdict (empty content) and silently degrades every
        # review to HOLD. timeout is larger for the same reason (slower calls).
        self._model = llm_cfg.get("model", "deepseek-v4-flash")
        self._max_tokens = int(llm_cfg.get("max_tokens", 2048))
        self._timeout = int(llm_cfg.get("timeout_seconds", 30))
        self._last_call: Optional[datetime] = None

        if self._enabled:
            self._log.info(
                "LLM Reviewer enabled (min_interval=%ds)", self._min_interval
            )
        else:
            self._log.info(
                "LLM Reviewer disabled — set DEEPSEEK_API_KEY env var"
            )

    # ── public API ──────────────────────────────────────────────────────

    def review_signal(
        self,
        symbol: str,
        combined_signal: str,
        combined_confidence: float,
        strategies: list,
        ml_signal: dict,
        sentiment_score: float,
        sentiment_label: str,
        regime: str,
        price: float,
        daily_pnl: float,
        price_change_24h: float = 0.0,
        volume_ratio: float = 0.0,
        rsi: float = 50.0,
        adx: float = 20.0,
        macd_hist: float = 0.0,
    ) -> Dict[str, Any]:
        """Review a trading signal and return an LLM recommendation.

        Only fires when conditions are interesting (grey-zone confidence
        or conflicting signals).  High-confidence signals pass through.
        """
        if not self._enabled:
            return self._passthrough(combined_signal, combined_confidence)

        # Rate limit
        if self._last_call is not None:
            elapsed = (datetime.now() - self._last_call).total_seconds()
            if elapsed < self._min_interval:
                return self._passthrough(combined_signal, combined_confidence)

        # Only review if confidence is below 0.55 or there's conflict
        high_conf_threshold = 0.55
        if combined_confidence >= high_conf_threshold:
            return {
                "action": combined_signal,
                "confidence": combined_confidence,
                "reason": "High-confidence quant signal — LLM review skipped",
                "llm_called": False,
            }

        # Build prompt with rich market data
        strat_lines = []
        for s in strategies:
            strat_lines.append(
                f"  {s.get('name', '?')}: {s.get('signal', '?')} "
                f"(conf={s.get('confidence', 0):.2f})"
            )

        prompt = (
            f"## Market Data for {symbol}\n\n"
            f"**Price**: ${price:.2f}  |  24h Change: {price_change_24h:+.2f}%\n"
            f"**Regime**: {regime}\n"
            f"**Daily PnL**: ${daily_pnl:+.2f}\n\n"
            f"**Combined Signal**: {combined_signal} (confidence={combined_confidence:.2f})\n\n"
            f"**Strategies**:\n"
            + "\n".join(strat_lines)
            + f"\n\n**ML Model**: {ml_signal.get('signal', '?')} "
            f"(confidence={ml_signal.get('confidence', 0):.2f})\n"
            f"**Sentiment**: Score={sentiment_score:+.3f} ({sentiment_label})\n\n"
            f"**Technical Details**:\n"
            f"  RSI(14): {rsi:.1f}\n"
            f"  ADX(14): {adx:.1f}\n"
            f"  MACD Histogram: {macd_hist:+.4f}\n"
            f"  Volume Ratio (5/20): {volume_ratio:.2f}x\n"
        )

        try:
            result = self._call_llm(prompt)
            self._last_call = datetime.now()
            result["llm_called"] = True
            return result
        except Exception as e:
            self._log.warning(f"LLM review failed for {symbol}: {e}")
            return {
                **self._passthrough(combined_signal, combined_confidence),
                "llm_called": False,
                "reason": f"LLM error: {e}",
            }

    # ── internal ────────────────────────────────────────────────────────

    def _call_llm(self, prompt: str) -> Dict[str, Any]:
        """Call DeepSeek chat and parse the JSON response."""
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": self._max_tokens,
                "temperature": 0.3,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()

        # Parse JSON — try direct first, then extract from markdown
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON block
            if "```" in raw:
                json_block = raw.split("```")[1]
                if json_block.startswith("json"):
                    json_block = json_block[4:]
                parsed = json.loads(json_block.strip())
            else:
                return self._fallback_review(raw)

        return {
            "action": parsed.get("action", "HOLD"),
            "confidence": float(parsed.get("confidence", 0.3)),
            "reason": parsed.get("reason", "LLM review"),
            "llm_called": True,
        }

    def _fallback_review(self, raw_text: str) -> Dict[str, Any]:
        """Parse plain-text LLM response when JSON parsing fails."""
        text = raw_text.lower()
        if "buy" in text or "long" in text:
            action = "BUY"
        elif "sell" in text or "short" in text:
            action = "SELL"
        elif "skip" in text or "wait" in text:
            action = "SKIP"
        else:
            action = "HOLD"

        return {
            "action": action,
            "confidence": 0.35,
            "reason": raw_text[:150],
            "llm_called": True,
        }

    def _passthrough(
        self, signal: str, confidence: float
    ) -> Dict[str, Any]:
        return {
            "action": signal,
            "confidence": confidence,
            "reason": "LLM not called",
            "llm_called": False,
        }

    def _resolve_api_key(self, config: dict) -> str:
        """Try env var, then config block."""
        return (
            os.environ.get("DEEPSEEK_API_KEY", "")
            or config.get("llm_reviewer", {}).get("api_key", "")
        )
