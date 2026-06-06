"""
Market Data Collector — Hermes Crypto Trading Bot.

Fetches and caches market data from Bitfinex via the BitfinexClient.
"""

import os
import time
import logging
import random
from pathlib import Path
from typing import Optional, Dict, List, Any

import pandas as pd

from bitfinex import BitfinexClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Collector
# ---------------------------------------------------------------------------

class MarketDataCollector:
    """Fetches and caches OHLCV / ticker / orderbook data from Bitfinex.

    Parameters
    ----------
    config : dict
        Full bot configuration.  Expects ``config["data"]`` for cache paths
        and ``config["exchange"]`` for exchange settings.
    """

    def __init__(self, config: dict, mode: Optional[str] = None):
        self.config = config
        self._log = logging.getLogger(f"{__name__}.MarketDataCollector")

        # Resolve exchange mode. The caller's explicit `mode` (the authoritative
        # CLI --mode, threaded from the bot) takes precedence; we only derive it
        # from config when not given. Deriving from `exchange.testnet` alone is
        # unsafe: configs ship testnet=false for live, so a paper run would
        # otherwise build a LIVE authenticated client (and claim the live API key
        # in the registry) — a paper/live isolation leak.
        if mode is None:
            mode = "live"
            if config.get("exchange", {}).get("testnet", True):
                mode = "paper"
            mode = config.get("trading", {}).get("mode", mode)

        self._client: BitfinexClient = BitfinexClient(
            config, mode=mode, instance=config.get("instance", "default")
        )

        # Cache directory
        self._cache_dir = Path(
            config.get("data", {}).get("ohlcv_dir", "data/ohlcv/")
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # Timeframe → seconds mapping for cache expiry checks
        self._TIMEFRAME_SECONDS = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "6h": 21600,
            "12h": 43200,
            "1d": 86400,
            "1w": 604800,
        }

        self._log.info(
            "MarketDataCollector ready (mode=%s, cache=%s)", mode, self._cache_dir
        )

    # ── properties ───────────────────────────────────────────────────────

    @property
    def client(self) -> BitfinexClient:
        """Expose the underlying client for direct access."""
        return self._client

    # ── public methods ───────────────────────────────────────────────────

    def get_ohlcv(self, symbol: str, timeframe: str = "1h",
                  limit: int = 200) -> Optional[pd.DataFrame]:
        """Fetch OHLCV data with caching.

        Returns a cached CSV if it is recent enough (less than **2×** the
        timeframe duration old).  Otherwise fetches fresh data from Bitfinex
        and updates the cache.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT"``.
        timeframe : str
            Candle duration, e.g. ``"1h"``, ``"5m"``.
        limit : int
            Number of candles to request.

        Returns
        -------
        pd.DataFrame or None
            Columns: timestamp (ms index), open, high, low, close, volume.
        """
        cache_key = f"{symbol}_{timeframe}"
        cache_path = self._cache_dir / self._safe_filename(cache_key)

        # Check cache freshness
        df = self._load_from_cache(cache_path, timeframe)
        if df is not None:
            self._log.debug("Cache HIT for %s", cache_key)
            return df

        self._log.info("Cache MISS for %s — fetching from Bitfinex", cache_key)

        # For Bitfinex 1h+ candles, pass a `since` so the window covers the most
        # recent `limit` candles: start `limit` periods ago and let the API fill
        # forward to *now*. Bitfinex returns candles ascending from `since`, so
        # `since = now - limit*tf` ends at the current candle. (The old code used
        # `* 2`, which started 2x too far back and therefore returned candles
        # ending ~limit periods — e.g. ~8 days for 1h — in the PAST. That fed
        # stale prices into TA/ML and tripped the divergence guard on every 1h
        # entry: the 2026-06-05 stale-price incident.)
        since = None
        tf_seconds = self._TIMEFRAME_SECONDS.get(timeframe, 3600)
        if tf_seconds >= 3600:  # 1h and above
            since = int((time.time() - limit * tf_seconds) * 1000)
            self._log.debug("Using since=%d for timeframe=%s", since, timeframe)

        # Fetch from exchange with retries
        df = self._fetch_with_retry(symbol, timeframe, limit, since=since)
        if df is None:
            return None

        # Save to cache
        self._save_cache(df, cache_path)
        self._log.info("Cached %d rows to %s", len(df), cache_path)
        return df

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Return the current mid‑price (average of bid and ask).

        Returns None if ticker data is unavailable.
        """
        t = self._client.fetch_ticker(symbol)
        if t.bid > 0 and t.ask > 0:
            return (t.bid + t.ask) / 2.0
        return t.last if t.last > 0 else None

    def get_multiple_timeframes(
        self,
        symbol: str,
        timeframes: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV for several timeframes at once.

        Parameters
        ----------
        symbol : str
            Trading pair.
        timeframes : list of str, optional
            Defaults to ``["5m", "15m", "1h", "4h"]``.

        Returns
        -------
        dict
            Mapping ``{timeframe: DataFrame}``.  Missing timeframes are
            omitted from the dict.
        """
        if timeframes is None:
            timeframes = ["5m", "15m", "1h", "4h"]

        result: Dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            try:
                df = self.get_ohlcv(symbol, timeframe=tf, limit=200)
                if df is not None:
                    result[tf] = df
            except Exception as exc:
                self._log.warning("Failed to fetch %s %s: %s", symbol, tf, exc)
        return result

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int = 1000,
    ) -> Optional[pd.DataFrame]:
        """Fetch historical OHLCV data since a given timestamp.

        Parameters
        ----------
        symbol : str
            Trading pair.
        timeframe : str
            Candle duration.
        since : int
            Unix timestamp **in milliseconds**.
        limit : int
            Maximum candles to fetch (ccxt default: 1000).

        Returns
        -------
        pd.DataFrame or None
        """
        self._log.info("Fetching historical %s %s since %d", symbol, timeframe, since)
        try:
            df = self._client.get_ohlcv(
                symbol, timeframe=timeframe, since=since, limit=limit
            )
        except Exception as exc:
            self._log.error("get_historical_data failed: %s", exc)
            return None

        if df is None or df.empty:
            return None

        self._log.info("Got %d rows of historical %s %s", len(df), symbol, timeframe)
        return df

    def refresh_cache(self):
        """Clear the local OHLCV cache directory and refetch all symbols.

        This removes all CSV files under the cache dir and then re‑fetches
        the primary trading symbol from config for the main timeframes.
        """
        self._log.info("Refreshing OHLCV cache…")

        # Clear cache files
        for f in self._cache_dir.glob("*.csv"):
            f.unlink()
            self._log.debug("Removed cache file: %s", f)

        # Refetch primary symbol
        symbol = self.config.get("trading", {}).get("symbol", "BTC/USDT")
        for tf in ("5m", "15m", "1h", "4h"):
            self.get_ohlcv(symbol, timeframe=tf, limit=200)

        self._log.info("Cache refresh complete")

    # ── internal helpers ─────────────────────────────────────────────────

    def _safe_filename(self, name: str) -> str:
        """Convert a symbol+timeframe string into a safe file name."""
        return name.replace("/", "_").replace(" ", "_") + ".csv"

    def _load_from_cache(self, path: Path,
                         timeframe: str) -> Optional[pd.DataFrame]:
        """Load a cached CSV if it is recent enough.

        "Recent enough" means the file's modification time is less than
        **2 × the timeframe duration** in seconds.
        """
        if not path.exists():
            return None

        cache_age_s = time.time() - path.stat().st_mtime
        tf_seconds = self._TIMEFRAME_SECONDS.get(timeframe, 3600)
        max_age = tf_seconds * 2

        if cache_age_s > max_age:
            self._log.debug(
                "Cache stale for %s (age=%.0fs, max=%.0fs)",
                path.name, cache_age_s, max_age,
            )
            return None

        try:
            df = pd.read_csv(path)
            if "timestamp" in df.columns:
                df.set_index("timestamp", inplace=True)
            # Ensure proper dtypes
            for col in ("open", "high", "low", "close", "volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            # Reject if the newest CANDLE is itself stale. File mtime alone is
            # not enough: a re-written file can still hold days-old candles,
            # which would feed a phantom entry/SL price (root cause of the
            # 2026-06-05 stale-position incident). The index is epoch-ms.
            if df.index.size:
                try:
                    last_ts_ms = int(pd.to_numeric(df.index, errors="coerce").max())
                    candle_age_s = time.time() - last_ts_ms / 1000.0
                    if candle_age_s > max_age:
                        self._log.warning(
                            "Cache candle stale for %s (newest candle age=%.0fs,"
                            " max=%.0fs) — forcing refresh",
                            path.name, candle_age_s, max_age,
                        )
                        return None
                except (ValueError, TypeError) as exc:
                    # Index wasn't epoch-ms numeric (unexpected cache format) —
                    # skip the candle-age check rather than crash. Log so a silent
                    # format drift doesn't quietly disable this staleness guard.
                    self._log.debug(
                        "Candle-age staleness check skipped for %s: %s",
                        path.name, exc,
                    )
            self._log.debug("Loaded cached %s (age=%.0fs)", path.name, cache_age_s)
            return df
        except (pd.errors.EmptyDataError, ValueError, KeyError) as exc:
            self._log.warning("Corrupt cache file %s: %s", path, exc)
            return None

    def _save_cache(self, df: pd.DataFrame, path: Path):
        """Write a DataFrame to a CSV cache file."""
        df_out = df.copy()
        if df_out.index.name == "timestamp" or "timestamp" in df_out.index.name:
            df_out.reset_index(inplace=True)
        df_out.to_csv(path, index=False)

    def _fetch_with_retry(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        since: Optional[int] = None,
        retries: int = 3,
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV with exponential backoff retry logic."""
        base_delay = 2.0  # seconds

        for attempt in range(1, retries + 1):
            try:
                df = self._client.get_ohlcv(symbol, timeframe, limit, since=since)
                if df is not None and not df.empty:
                    return df

                self._log.warning(
                    "get_ohlcv returned empty (attempt %d/%d)", attempt, retries
                )
            except Exception as exc:
                self._log.error(
                    "get_ohlcv attempt %d/%d failed: %s", attempt, retries, exc
                )

            if attempt < retries:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                self._log.info("Retrying in %.1f seconds…", delay)
                time.sleep(delay)

        self._log.error(
            "All %d retries exhausted for %s %s", retries, symbol, timeframe
        )
        return None

    def __repr__(self) -> str:
        return (
            f"<MarketDataCollector client={self._client!r} "
            f"cache={self._cache_dir}>"
        )
