#!/usr/bin/env python3
"""
Technical Analysis Engine
Computes indicators, detects patterns, generates trading signals.
"""

import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple


class TechnicalAnalyzer:
    """Comprehensive technical analysis with indicator computation and signal generation."""

    def __init__(self, config: dict):
        self.config = config
        self.log = logging.getLogger("TechnicalAnalyzer")
        self.log.info("Technical Analyzer initialized")

    def analyze(self, df: pd.DataFrame, timeframe: str = "1h") -> Dict:
        """
        Full technical analysis on a price DataFrame.
        Returns dict with signal, confidence, indicators, and patterns.
        """
        if df is None or len(df) < 50:
            return {"signal": "HOLD", "confidence": 0.0, "current_price": 0.0}

        df = df.copy()
        current_price = float(df['close'].iloc[-1])

        # Compute all indicators
        indicators = self.compute_all_indicators(df)

        # Detect patterns
        patterns = self.detect_patterns(df)

        # Find support/resistance
        sr = self.find_support_resistance(df)

        # Get trend info
        trend_info = self.get_trend_strength(df)

        # Generate signal
        signal, confidence = self._generate_signal(indicators, patterns, trend_info, timeframe)

        result = {
            'signal': signal,
            'confidence': confidence,
            'current_price': current_price,
            'trend': trend_info.get('trend', 'neutral'),
            'rsi': indicators.get('rsi', 50),
            'macd': {
                'value': indicators.get('macd', 0),
                'signal': indicators.get('macd_signal', 0),
                'histogram': indicators.get('macd_histogram', 0),
            },
            'bollinger': {
                'upper': indicators.get('bb_upper', current_price),
                'middle': indicators.get('bb_middle', current_price),
                'lower': indicators.get('bb_lower', current_price),
                'position': self._bb_position(current_price, indicators),
            },
            'ema_9': indicators.get('ema_9', current_price),
            'ema_21': indicators.get('ema_21', current_price),
            'volume_profile': {
                'current': indicators.get('volume', 0),
                'sma': indicators.get('volume_sma', 1),
                'ratio': indicators.get('volume', 1) / max(indicators.get('volume_sma', 1), 0.001),
            },
            'support_resistance': sr,
            'patterns': patterns,
            'indicators': indicators,
            'volatility': indicators.get('volatility', 0),
            'atr': indicators.get('atr', 0),
        }

        return result

    def compute_all_indicators(self, df: pd.DataFrame) -> Dict:
        """Compute all technical indicators."""
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        volume = df['volume'].values
        length = len(df)

        indicators = {}

        # --- RSI (14) ---
        indicators['rsi'] = self._compute_rsi(close, 14)

        # --- MACD (12, 26, 9) ---
        macd_line, macd_signal, macd_hist = self._compute_macd(close, 12, 26, 9)
        indicators['macd'] = macd_line
        indicators['macd_signal'] = macd_signal
        indicators['macd_histogram'] = macd_hist

        # --- EMAs ---
        indicators['ema_9'] = self._compute_ema(close, 9)
        indicators['ema_21'] = self._compute_ema(close, 21)
        indicators['ema_50'] = self._compute_ema(close, 50)
        indicators['ema_200'] = self._compute_ema(close, 200)

        # --- SMAs ---
        indicators['sma_20'] = self._compute_sma(close, 20)
        indicators['sma_50'] = self._compute_sma(close, 50)

        # --- Bollinger Bands (20, 2) ---
        bb_mid = self._compute_sma(close, 20)
        bb_std = self._compute_rolling_std(close, 20)
        indicators['bb_middle'] = bb_mid
        indicators['bb_upper'] = bb_mid + 2 * bb_std
        indicators['bb_lower'] = bb_mid - 2 * bb_std

        # --- ATR (14) ---
        indicators['atr'] = self._compute_atr(high, low, close, 14)

        # --- Stochastic (14, 3, 3) ---
        stoch_k, stoch_d = self._compute_stochastic(high, low, close, 14, 3)
        indicators['stoch_k'] = stoch_k
        indicators['stoch_d'] = stoch_d

        # --- Volume SMA ---
        indicators['volume'] = float(volume[-1]) if len(volume) > 0 else 0
        indicators['volume_sma'] = self._compute_sma(volume, 20)

        # --- OBV ---
        indicators['obv'] = self._compute_obv(close, volume)

        # --- Money Flow Index ---
        indicators['mfi'] = self._compute_mfi(high, low, close, volume, 14)

        # --- Ichimoku ---
        ichimoku = self._compute_ichimoku(high, low, close)
        indicators.update(ichimoku)

        # --- Volatility ---
        if indicators.get('atr', 0) > 0 and close[-1] > 0:
            indicators['volatility'] = float(indicators['atr'] / close[-1] * 100)
        else:
            indicators['volatility'] = 0.0

        # --- Price changes ---
        if length >= 25:
            indicators['price_change_1h'] = self._price_change_pct(close, 1)
            indicators['price_change_4h'] = self._price_change_pct(close, 4)
            indicators['price_change_24h'] = self._price_change_pct(close, 24)
        else:
            indicators['price_change_1h'] = 0
            indicators['price_change_4h'] = 0
            indicators['price_change_24h'] = 0

        # --- ADX ---
        indicators['adx'] = self._compute_adx(high, low, close, 14)

        return indicators

    def _compute_rsi(self, close: np.ndarray, period: int = 14) -> float:
        if len(close) < period + 1:
            return 50.0
        deltas = np.diff(close)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    def _compute_ema(self, data: np.ndarray, period: int) -> float:
        if len(data) < period:
            return float(data[-1])
        alpha = 2.0 / (period + 1)
        ema = float(data[0])
        for price in data[1:]:
            ema = alpha * price + (1 - alpha) * ema
        return ema

    def _compute_sma(self, data: np.ndarray, period: int) -> float:
        if len(data) < period:
            return float(np.mean(data))
        return float(np.mean(data[-period:]))

    def _compute_rolling_std(self, data: np.ndarray, period: int) -> float:
        if len(data) < period:
            return float(np.std(data))
        return float(np.std(data[-period:]))

    def _compute_macd(self, close: np.ndarray, fast: int, slow: int, signal: int) -> Tuple[float, float, float]:
        if len(close) < slow + signal:
            return 0.0, 0.0, 0.0
        # Use EMA approximation
        ema_fast = self._compute_ema(close, fast)
        ema_slow = self._compute_ema(close, slow)
        macd_line = ema_fast - ema_slow

        # Signal line: we need multiple MACD values; approximate
        # For a single data point, compute from a small EMA of recent diffs
        recent_macds = []
        for i in range(max(0, len(close) - signal - 5), len(close)):
            ef = self._compute_ema(close[:i+1], fast) if i >= fast else close[i]
            es = self._compute_ema(close[:i+1], slow) if i >= slow else close[i]
            recent_macds.append(ef - es)

        if len(recent_macds) >= signal:
            macd_signal = self._compute_ema(np.array(recent_macds), signal)
        else:
            macd_signal = recent_macds[-1] if recent_macds else 0.0

        macd_hist = macd_line - macd_signal
        return float(macd_line), float(macd_signal), float(macd_hist)

    def _compute_atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
        if len(close) < 2:
            return 0.0
        trs = []
        for i in range(1, len(close)):
            hl = high[i] - low[i]
            hc = abs(high[i] - close[i-1])
            lc = abs(low[i] - close[i-1])
            trs.append(max(hl, hc, lc))
        if not trs:
            return 0.0
        return float(np.mean(trs[-period:]) if len(trs) >= period else np.mean(trs))

    def _compute_stochastic(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, k_period: int = 14, d_period: int = 3) -> Tuple[float, float]:
        if len(close) < k_period + d_period:
            return 50.0, 50.0
        recent_high = float(np.max(high[-k_period:]))
        recent_low = float(np.min(low[-k_period:]))
        if recent_high == recent_low:
            return 50.0, 50.0
        stoch_k = float((close[-1] - recent_low) / (recent_high - recent_low) * 100)
        stoch_d = stoch_k  # Simplified: single value
        return stoch_k, stoch_d

    def _compute_obv(self, close: np.ndarray, volume: np.ndarray) -> float:
        if len(close) < 2:
            return float(volume[-1]) if len(volume) > 0 else 0
        obv = 0.0
        for i in range(1, len(close)):
            if close[i] > close[i-1]:
                obv += volume[i]
            elif close[i] < close[i-1]:
                obv -= volume[i]
        return float(obv)

    def _compute_mfi(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, period: int = 14) -> float:
        if len(close) < period + 1:
            return 50.0
        typical_price = (high + low + close) / 3
        money_flow = typical_price * volume
        pos_flow = 0.0
        neg_flow = 0.0
        for i in range(-period, 0):
            if typical_price[i] > typical_price[i-1]:
                pos_flow += money_flow[i]
            else:
                neg_flow += money_flow[i]
        if neg_flow == 0:
            return 100.0
        mfr = pos_flow / neg_flow
        return float(100.0 - (100.0 / (1.0 + mfr)))

    def _compute_ichimoku(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> Dict:
        result = {}
        if len(close) < 52:
            return result
        tenkan = (float(np.max(high[-9:])) + float(np.min(low[-9:]))) / 2
        kijun = (float(np.max(high[-26:])) + float(np.min(low[-26:]))) / 2
        senkou_a = (tenkan + kijun) / 2
        senkou_b = (float(np.max(high[-52:])) + float(np.min(low[-52:]))) / 2
        chikou = float(close[-26]) if len(close) > 26 else float(close[-1])

        result['ichimoku_tenkan'] = tenkan
        result['ichimoku_kijun'] = kijun
        result['ichimoku_senkou_a'] = senkou_a
        result['ichimoku_senkou_b'] = senkou_b
        result['ichimoku_chikou'] = chikou
        result['ichimoku_cloud'] = senkou_a - senkou_b  # positive = bullish cloud
        return result

    def _compute_adx(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
        if len(close) < period + 2:
            return 25.0  # Neutral ADX
        trs = []
        plus_dm = []
        minus_dm = []
        for i in range(1, len(close)):
            tr = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
            trs.append(tr)
            up_move = high[i] - high[i-1]
            down_move = low[i-1] - low[i]
            if up_move > down_move and up_move > 0:
                plus_dm.append(up_move)
            else:
                plus_dm.append(0)
            if down_move > up_move and down_move > 0:
                minus_dm.append(down_move)
            else:
                minus_dm.append(0)

        if not trs or sum(trs[-period:]) == 0:
            return 25.0

        atr_period = sum(trs[-period:]) / period
        plus_di = (sum(plus_dm[-period:]) / period) / atr_period * 100
        minus_di = (sum(minus_dm[-period:]) / period) / atr_period * 100
        dx = abs(plus_di - minus_di) / max(plus_di + minus_di, 0.001) * 100
        return float(dx)

    def _price_change_pct(self, data: np.ndarray, periods_back: int) -> float:
        if len(data) <= periods_back:
            return 0.0
        return float((data[-1] - data[-periods_back - 1]) / data[-periods_back - 1] * 100)

    def _bb_position(self, price: float, indicators: Dict) -> float:
        """Return 0-1 value: 0 = at lower band, 1 = at upper band."""
        upper = indicators.get('bb_upper', price)
        lower = indicators.get('bb_lower', price)
        if upper == lower:
            return 0.5
        return float((price - lower) / (upper - lower))

    def detect_patterns(self, df: pd.DataFrame) -> List[Dict]:
        """Detect candlestick patterns."""
        patterns = []
        if len(df) < 5:
            return patterns

        open_p = df['open'].values
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values

        # --- Doji (last candle) ---
        body = abs(close[-1] - open_p[-1])
        total_range = high[-1] - low[-1]
        if total_range > 0 and body / total_range < 0.1:
            patterns.append({
                'name': 'doji',
                'position': 'last',
                'direction': 'neutral',
                'strength': 'medium'
            })

        # --- Hammer / Shooting Star ---
        if total_range > 0:
            lower_wick = min(open_p[-1], close[-1]) - low[-1]
            upper_wick = high[-1] - max(open_p[-1], close[-1])
            if lower_wick > 2 * body and upper_wick < 0.3 * body:
                patterns.append({
                    'name': 'hammer',
                    'position': 'last',
                    'direction': 'bullish',
                    'strength': 'strong'
                })
            elif upper_wick > 2 * body and lower_wick < 0.3 * body:
                patterns.append({
                    'name': 'shooting_star',
                    'position': 'last',
                    'direction': 'bearish',
                    'strength': 'strong'
                })

        # --- Engulfing ---
        if len(df) >= 2:
            prev_body = abs(close[-2] - open_p[-2])
            curr_body = body
            prev_bullish = close[-2] > open_p[-2]
            prev_bearish = close[-2] < open_p[-2]
            curr_bullish = close[-1] > open_p[-1]
            curr_bearish = close[-1] < open_p[-1]

            if prev_bearish and curr_bullish and curr_body > prev_body:
                if open_p[-1] < close[-2] and close[-1] > open_p[-2]:
                    patterns.append({
                        'name': 'bullish_engulfing',
                        'position': 'last',
                        'direction': 'bullish',
                        'strength': 'strong'
                    })
            elif prev_bullish and curr_bearish and curr_body > prev_body:
                if open_p[-1] > close[-2] and close[-1] < open_p[-2]:
                    patterns.append({
                        'name': 'bearish_engulfing',
                        'position': 'last',
                        'direction': 'bearish',
                        'strength': 'strong'
                    })

        # --- Morning / Evening Star (3 candle) ---
        if len(df) >= 3:
            c1_bearish = close[-3] < open_p[-3]
            c3_bullish = close[-1] > open_p[-1]
            c2_body = abs(close[-2] - open_p[-2])
            c1_body = abs(close[-3] - open_p[-3])
            c3_body = abs(close[-1] - open_p[-1])

            if c1_bearish and c2_body < c1_body * 0.3 and c3_bullish:
                if close[-1] > (open_p[-3] + close[-3]) / 2:
                    patterns.append({
                        'name': 'morning_star',
                        'position': 'last',
                        'direction': 'bullish',
                        'strength': 'very_strong'
                    })

            c1_bullish = close[-3] > open_p[-3]
            c3_bearish = close[-1] < open_p[-1]
            if c1_bullish and c2_body < c1_body * 0.3 and c3_bearish:
                if close[-1] < (open_p[-3] + close[-3]) / 2:
                    patterns.append({
                        'name': 'evening_star',
                        'position': 'last',
                        'direction': 'bearish',
                        'strength': 'very_strong'
                    })

        return patterns

    def find_support_resistance(self, df: pd.DataFrame, window: int = 5) -> Dict:
        """Find support and resistance levels using pivot points."""
        if len(df) < window * 3:
            return {'support': [], 'resistance': []}

        highs = df['high'].values
        lows = df['low'].values
        supports = []
        resistances = []

        for i in range(window, len(df) - window):
            # Pivot high
            if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
                resistances.append(float(highs[i]))
            # Pivot low
            if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
                supports.append(float(lows[i]))

        # Cluster nearby levels
        def cluster_levels(levels: List[float], tolerance: float = 0.005) -> List[float]:
            if not levels:
                return []
            levels = sorted(levels)
            clustered = [levels[0]]
            for lvl in levels[1:]:
                if abs(lvl - clustered[-1]) / clustered[-1] > tolerance:
                    clustered.append(lvl)
            return clustered[:5]  # Top 5 levels

        current_price = float(df['close'].iloc[-1])

        return {
            'support': [l for l in cluster_levels(supports) if l < current_price],
            'resistance': [l for l in cluster_levels(resistances) if l > current_price],
            'nearest_support': max([l for l in supports if l < current_price], default=None),
            'nearest_resistance': min([l for l in resistances if l > current_price], default=None),
        }

    def get_trend_strength(self, df: pd.DataFrame) -> Dict:
        """Determine trend direction and strength."""
        if len(df) < 50:
            return {'trend': 'neutral', 'strength': 0, 'adx': 25}

        close = df['close'].values
        high = df['high'].values
        low = df['low'].values

        ema_9 = self._compute_ema(close, 9)
        ema_21 = self._compute_ema(close, 21)
        ema_50 = self._compute_ema(close, 50)
        ema_200 = self._compute_ema(close, 200)

        current_price = float(close[-1])
        adx = self._compute_adx(high, low, close)

        # Trend direction based on EMA alignment
        bullish = ema_9 > ema_21 > ema_50
        bearish = ema_9 < ema_21 < ema_50

        # Price relative to EMAs
        above_emas = current_price > ema_9 > ema_21 > ema_50 > ema_200
        below_emas = current_price < ema_9 < ema_21 < ema_50 < ema_200

        # Strength
        if adx >= 25:
            strength = min(1.0, (adx - 25) / 50)
        else:
            strength = max(0.0, adx / 25 * 0.4)

        if bullish or above_emas:
            return {'trend': 'bullish', 'strength': strength, 'adx': adx}
        elif bearish or below_emas:
            return {'trend': 'bearish', 'strength': strength, 'adx': adx}
        else:
            return {'trend': 'neutral', 'strength': strength, 'adx': adx}

    def get_volatility(self, df: pd.DataFrame) -> float:
        """Return volatility as ATR/close percentage."""
        if len(df) < 15:
            return 1.0
        indicators = self.compute_all_indicators(df)
        return indicators.get('volatility', 1.0)

    def _generate_signal(self, indicators: Dict, patterns: List[Dict],
                         trend_info: Dict, timeframe: str) -> Tuple[str, float]:
        """Generate trading signal from all indicators."""
        buy_score = 0.0
        sell_score = 0.0
        total_score = 0.0

        rsi = indicators.get('rsi', 50)
        macd_hist = indicators.get('macd_histogram', 0)
        bb_pos = self._bb_position(indicators.get('bb_middle', 50), indicators)
        ema_9 = indicators.get('ema_9', 0)
        ema_21 = indicators.get('ema_21', 0)
        volume_ratio = indicators.get('volume', 1) / max(indicators.get('volume_sma', 1), 0.001)
        trend = trend_info.get('trend', 'neutral')
        trend_strength = trend_info.get('strength', 0)
        adx = trend_info.get('adx', 25)

        # 1. RSI signal (weight: 1.5)
        if rsi < 30:  # Oversold
            buy_score += 1.5 * (1 - rsi / 30)
        elif rsi > 70:  # Overbought
            sell_score += 1.5 * (rsi - 70) / 30

        # 2. MACD histogram (weight: 1.5)
        if macd_hist > 0:
            buy_score += 1.5 * min(1.0, abs(macd_hist) / 50)
        elif macd_hist < 0:
            sell_score += 1.5 * min(1.0, abs(macd_hist) / 50)

        # 3. Bollinger Bands position (weight: 1.0)
        if bb_pos < 0.2:  # Near lower band
            buy_score += 1.0 * (1 - bb_pos / 0.2)
        elif bb_pos > 0.8:  # Near upper band
            sell_score += 1.0 * (bb_pos - 0.8) / 0.2

        # 4. EMA crossover (weight: 1.5)
        if ema_9 > ema_21:
            buy_score += 1.5 * min(1.0, abs(ema_9 - ema_21) / ema_21 * 100)
        elif ema_21 > ema_9:
            sell_score += 1.5 * min(1.0, abs(ema_9 - ema_21) / ema_21 * 100)

        # 5. Volume confirmation (weight: 0.5)
        if volume_ratio > 1.2:
            # Amplify existing bias
            bias = buy_score - sell_score
            if bias > 0:
                buy_score += 0.5 * min(1.0, (volume_ratio - 1.2) / 2)
            elif bias < 0:
                sell_score += 0.5 * min(1.0, (volume_ratio - 1.2) / 2)

        # 6. Trend alignment (weight: 1.0)
        if trend == 'bullish':
            buy_score += 1.0 * trend_strength
        elif trend == 'bearish':
            sell_score += 1.0 * trend_strength

        # 7. Pattern signals (weight: 1.0 each)
        for p in patterns:
            strength_map = {'weak': 0.3, 'medium': 0.6, 'strong': 0.8, 'very_strong': 1.0}
            s = strength_map.get(p.get('strength', 'medium'), 0.5)
            if p.get('direction') == 'bullish':
                buy_score += 1.0 * s
            elif p.get('direction') == 'bearish':
                sell_score += 1.0 * s

        # 8. ADX trend strength (weight: 0.5)
        if adx > 30 and trend == 'bullish':
            buy_score += 0.5 * min(1.0, (adx - 30) / 30)
        elif adx > 30 and trend == 'bearish':
            sell_score += 0.5 * min(1.0, (adx - 30) / 30)

        total_score = buy_score + sell_score
        if total_score == 0:
            return 'HOLD', 0.0

        buy_ratio = buy_score / total_score
        sell_ratio = sell_score / total_score

        # Thresholds for signal
        if buy_ratio > 0.62:  # Stronger threshold for signal
            confidence = buy_ratio
            return 'BUY', min(1.0, confidence)
        elif sell_ratio > 0.62:
            confidence = sell_ratio
            return 'SELL', min(1.0, confidence)
        else:
            return 'HOLD', max(buy_ratio, sell_ratio)
