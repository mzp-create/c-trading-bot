#!/usr/bin/env python3
"""
Telegram notifications for the trading bot.
Sends trade alerts, daily summaries, and error notifications.
"""

import os
import logging
import json
import requests
from typing import Optional


class TelegramNotifier:
    """Send trading alerts via Telegram bot."""

    def __init__(self, config: dict):
        self.log = logging.getLogger("Telegram")

        # Try env vars first, then config
        self.bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '') or \
                        config.get('monitoring', {}).get('telegram_bot_token', '')
        self.chat_id = os.environ.get('TELEGRAM_CHAT_ID', '') or \
                      config.get('monitoring', {}).get('telegram_chat_id', '')

        self.enabled = bool(self.bot_token and self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.enabled else ""

        self._last_update_id = 0
        self._current_chat_id = None

        if self.enabled:
            self.log.info("Telegram notifications enabled")
        else:
            self.log.info("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars")

    def send(self, message: str) -> bool:
        """Send a message to Telegram."""
        if not self.enabled:
            self.log.debug(f"[Telegram disabled] Would send: {message[:100]}...")
            return False

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    'chat_id': self.chat_id,
                    'text': message,
                    'parse_mode': 'Markdown',
                    'disable_web_page_preview': True,
                },
                timeout=10
            )
            if resp.status_code == 200:
                self.log.debug("Telegram message sent")
                return True
            else:
                self.log.warning(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
                return False
        except requests.exceptions.RequestException as e:
            self.log.warning(f"Telegram connection error: {e}")
            return False

    def send_trade_alert(self, action: str, symbol: str, amount: float,
                         price: float, pnl: Optional[float] = None,
                         reason: str = ""):
        """Send a trade alert."""
        emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "⚪"
        msg = f"{emoji} **{action}** {symbol}\n"
        msg += f"Amount: {amount:.6f}\n"
        msg += f"Price: ${price:.2f}\n"
        if pnl is not None:
            msg += f"PnL: ${pnl:+.2f}\n"
        if reason:
            msg += f"Reason: {reason}"

        return self.send(msg)

    def send_daily_summary(self, pnl: float, trades: int, win_rate: float,
                           capital: float, best_trade: float, worst_trade: float):
        """Send daily performance summary."""
        emoji = "📈" if pnl >= 0 else "📉"
        target = 100.0
        pct_to_target = (pnl / target * 100) if target > 0 else 0

        msg = (
            f"{emoji} **Daily Summary**\n"
            f"PnL: **${pnl:+.2f}** ({pct_to_target:.0f}% of ${target:.0f} target)\n"
            f"Trades: {trades} | Win Rate: {win_rate:.1f}%\n"
            f"Capital: ${capital:.2f}\n"
            f"Best: ${best_trade:+.2f} | Worst: ${worst_trade:+.2f}\n"
        )

        if pnl >= target:
            msg += "\n🎯 **Target ACHIEVED!**"
        elif pnl >= target * 0.5:
            msg += "\n✅ Halfway to target!"

        return self.send(msg)

    def send_error(self, error_msg: str):
        """Send error alert."""
        return self.send(f"🚨 **Error**\n```{error_msg[:500]}```")

    def send_startup(self, mode: str, capital: float, symbol: str):
        """Send startup notification."""
        msg = (
            f"🤖 **Bot Started**\n"
            f"Mode: {mode.upper()}\n"
            f"Capital: ${capital:.2f}\n"
            f"Target: $100/day\n"
            f"Symbol: {symbol}\n"
            f"Time: {self._now_str()}"
        )
        return self.send(msg)

    def send_cycle_summary(self, results: list[dict], daily_pnl: float,
                           open_positions: int, trade_count: int):
        """Send a compact cycle summary for all pairs. Only sends if there are open positions or trades today."""
        # Quiet mode — only alert if something changed
        if open_positions == 0 and trade_count == 0:
            return True
        lines = ["🔄 **Bot Cycle**"]
        for r in results:
            sym = r.get("symbol_name", r.get("symbol", "?"))
            sig = r.get("signal", "?")
            conf = r.get("confidence", 0)
            price = r.get("current_price", 0)
            reason = r.get("reason", "")
            emoji = "🟢" if sig == "BUY" else "🔴" if sig == "SELL" else "⚪"
            lines.append(
                f"{emoji} {sym}: **{sig}** ({conf:.0%}) @ ${price:.0f}"
            )
        lines.append(f"")
        lines.append(f"💰 Daily PnL: ${daily_pnl:+.2f}")
        lines.append(f"📊 Positions: {open_positions} | Trades: {trade_count}")
        return self.send("\n".join(lines))

    # ── Command handling (getUpdates polling) ─────────────────────────────

    def poll_commands(self) -> Optional[dict]:
        """Quick poll for a single Telegram command (non-blocking).

        Returns the first pending command as a dict:
            {'cmd': '/status', 'args': '', 'chat_id': '123'}
        or None if no commands found.
        """
        if not self.enabled:
            return None

        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self._last_update_id + 1,
                    "timeout": 5,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            self.log.debug(f"getUpdates poll failed: {exc}")
            return None

        if resp.status_code != 200:
            return None

        data = resp.json()
        if not data.get("ok") or not data.get("result"):
            return None

        for update in data["result"]:
            self._last_update_id = max(
                self._last_update_id, update.get("update_id", 0)
            )
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if text.startswith("/"):
                parts = text.split(maxsplit=1)
                cmd = parts[0].lower()
                args = parts[1] if len(parts) > 1 else ""
                return {"cmd": cmd, "args": args, "chat_id": chat_id}

        return None

    def execute_command(
        self,
        cmd: str,
        args: str,
        chat_id: str,
        bot: "TradingBot",  # noqa: F821
    ) -> Optional[str]:
        """Execute a Telegram slash command and return a response string.

        Known commands (caller passes *bot* for live state access):
          /status     — bot mode, uptime, PnL, positions
          /balance    — wallet breakdown per symbol
          /pause      — pause trading
          /resume     — resume trading
          /positions  — open positions detail
          /config     — key config values
          /help       — list all commands
          /sentiment  — latest news sentiment scores
          /retrain    — force ML retrain
        """
        import time as _time

        cmd_map = {
            "/start":    self._cmd_help,
            "/help":     self._cmd_help,
            "/status":   self._cmd_status,
            "/balance":  self._cmd_balance,
            "/pause":    self._cmd_pause,
            "/resume":   self._cmd_resume,
            "/positions": self._cmd_positions,
            "/config":   self._cmd_config,
            "/sentiment": self._cmd_sentiment,
            "/retrain":  self._cmd_retrain,
        }

        handler = cmd_map.get(cmd)
        if handler is None:
            return (
                f"Unknown command `{cmd}`. Use /help to see available commands."
            )

        self._current_chat_id = chat_id
        try:
            return handler(args, bot)
        except Exception as exc:
            self.log.error(f"Command handler {cmd} failed: {exc}")
            return f"⚠️ Error running `{cmd}`: {exc}"
        finally:
            self._current_chat_id = None

    # ── stored for reply routing ──────────────────────────────────────────

    def reply(self, text: str) -> bool:
        """Send a reply back to the chat that issued the last command."""
        cid = self._current_chat_id
        if not cid or not self.enabled:
            return False
        return self._send_to_chat(cid, text)

    def _send_to_chat(self, chat_id: str, text: str) -> bool:
        """Send a message to a specific chat_id."""
        if not self.enabled:
            return False
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False

    # ── command implementations ───────────────────────────────────────────

    @staticmethod
    def _cmd_help(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        return (
            "🤖 **Crypto Trader Commands**\n\n"
            "/status — Bot status, PnL, positions\n"
            "/balance — Wallet balance per pair\n"
            "/positions — Open position details\n"
            "/pause — Pause trading\n"
            "/resume — Resume trading\n"
            "/config — Current configuration\n"
            "/sentiment — Latest news sentiment scores\n"
            "/retrain — Force retrain ML models\n"
            "/help — This message"
        )

    @staticmethod
    def _cmd_status(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        from datetime import datetime
        uptime = ""
        if bot.start_time:
            elapsed = int(
                (datetime.now() - bot.start_time).total_seconds()
            )
            h, r = divmod(elapsed, 3600)
            m, s = divmod(r, 60)
            uptime = f"{h}h {m}m {s}s"

        mode_icon = "🔴" if bot.mode == "live" else "🟡"
        pause = " ⏸️ PAUSED" if bot.paused else ""

        # Count open positions
        pos_count = len(bot.executor.open_positions)

        # Regime
        regime_str = "N/A"
        if hasattr(bot, "regime_detector") and bot.regime_detector:
            try:
                reg = bot.regime_detector.current_regime
                regime_str = f"{reg.name}" if reg else "N/A"
            except Exception:
                regime_str = "N/A"

        return (
            f"🤖 **Bot Status**{pause}\n\n"
            f"Mode: {mode_icon} {bot.mode.upper()}\n"
            f"Uptime: {uptime}\n"
            f"Capital: ${bot.initial_capital:.2f}\n"
            f"Daily PnL: **${bot.daily_pnl:+.2f}**\n"
            f"Trades Today: {bot.trade_count}\n"
            f"Open Positions: {pos_count}\n"
            f"Regime: {regime_str}\n"
            f"Symbols: {', '.join(s['name'] for s in bot.symbols)}"
        )

    @staticmethod
    def _cmd_balance(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        try:
            raw = bot.collector.client.fetch_balance()
            free = raw.get("free", {})
            total = raw.get("total", {})
            lines = ["💰 **Wallet Balance (Margin)**"]
            for cur in ["USDT", "BTC", "ETH", "SOL"]:
                t = float(total.get(cur, 0))
                f = float(free.get(cur, 0))
                if t != 0 or f != 0:
                    lines.append(f"  {cur}: {t:.4f} total / {f:.4f} free")
            lines.append(f"\nTotal USDT: ${float(total.get('USDT', 0)):.2f}")
            return "\n".join(lines)
        except Exception as e:
            return f"⚠️ Balance fetch failed: {e}"

    @staticmethod
    def _cmd_pause(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        bot.paused = True
        bot.log.info("Trading paused via Telegram command")
        return "⏸️ **Trading Paused** — no new trades until /resume"

    @staticmethod
    def _cmd_resume(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        bot.paused = False
        bot.log.info("Trading resumed via Telegram command")
        return "▶️ **Trading Resumed**"

    @staticmethod
    def _cmd_positions(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        positions = bot.executor.open_positions
        if not positions:
            return "📭 No open positions"

        lines = ["📊 **Open Positions**"]
        for p in positions:
            sym = p.get("symbol", "?")
            qty = p.get("quantity", 0)
            entry = p.get("entry_price", 0)
            sl = p.get("stop_loss", "N/A")
            tp = p.get("take_profit", "N/A")
            pnl = p.get("unrealized_pnl", 0)
            side = p.get("side", "LONG")
            direction = "🟢" if side.upper() == "BUY" else "🔴"

            lines.append(
                f"\n{direction} **{sym}**\n"
                f"  Side: {side}\n"
                f"  Qty: {qty:.4f} @ ${entry:.2f}\n"
                f"  SL: ${sl:.2f} | TP: ${tp:.2f}\n"
                f"  PnL: ${pnl:+.2f}"
            )
        return "\n".join(lines)

    @staticmethod
    def _cmd_config(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        cfg = bot.config
        tc = cfg.get("trading", {})
        ml = cfg.get("ml", {})
        risk = cfg.get("risk", {})
        sent = cfg.get("sentiment", {})

        symbols = ", ".join(s["name"] for s in bot.symbols) or tc.get(
            "symbol", "BTC/USDT"
        )
        lines = [
            "⚙️ **Bot Configuration**",
            f"\nTrading:",
            f"  Mode: {bot.mode}",
            f"  Symbols: {symbols}",
            f"  Capital: ${tc.get('initial_capital', 0)}",
            f"  Max Positions: {tc.get('max_open_positions', 6)}",
            f"  Target: ${tc.get('daily_target', 100)}/day",
            f"\nML:",
            f"  Enabled: {ml.get('enabled', True)}",
            f"  Retrain Interval: {ml.get('retrain_interval', 24)}h",
            f"\nRisk:",
            f"  Max Daily Loss: ${risk.get('max_daily_loss', 50)}",
            f"  Max Drawdown: {risk.get('max_drawdown', 20)}%",
            f"  Kelly Fraction: {risk.get('kelly_fraction', 0.25)}",
            f"\nSentiment:",
            f"  Enabled: {sent.get('enabled', False)}",
            f"  Filter Strength: {sent.get('filter_strength', 0.30)}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _cmd_sentiment(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        if not hasattr(bot, "sentiment") or bot.sentiment is None:
            return "⚠️ Sentiment analyzer not available"

        sent = bot.sentiment
        # Force a fresh fetch
        try:
            result = sent.get_signal_filter(0.0, 1.0, 0.5)
        except Exception:
            result = {"score": 0, "label": "Neutral", "headlines": []}

        score = result.get("score", 0)
        label = result.get("label", "Neutral")
        headlines = result.get("headlines", [])

        emoji = "🟢" if score > 0.1 else "🔴" if score < -0.1 else "⚪"
        lines = [
            f"{emoji} **Sentiment Overview**",
            f"Score: {score:+.3f} ({label})",
        ]
        if headlines:
            lines.append(f"\nLatest headlines ({len(headlines)} total):")
            for h in headlines[:5]:
                lines.append(f"  • {h[:80]}")
        return "\n".join(lines)

    @staticmethod
    def _cmd_retrain(_args: str, bot: "TradingBot") -> str:  # noqa: F821

        results = []

        for s in bot.symbols:
            sym = s["name"]
            try:
                df = bot.collector.get_ohlcv(
                    sym,
                    timeframe="1h",
                    limit=bot.config.get("ml", {}).get(
                        "min_train_samples", 500
                    ),
                )
                if df is not None:
                    r = bot.ml_predictor.train(df, symbol=sym)
                    acc = r.get("accuracy", 0)
                    results.append(f"  {sym}: {acc:.1%} accuracy ({r.get('samples', 0)} samples)")
                else:
                    results.append(f"  {sym}: no data")
            except Exception as e:
                results.append(f"  {sym}: {e}")

        bot.log.info("ML models retrained via Telegram command")
        return "🔄 **ML Retrain Complete**\n" + "\n".join(results)

    def send_shutdown(self, pnl: float, trades: int, duration: str):
        """Send shutdown notification."""
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} **Bot Stopped**\n"
            f"Session PnL: ${pnl:+.2f}\n"
            f"Trades: {trades}\n"
            f"Duration: {duration}"
        )
        return self.send(msg)

    def _now_str(self) -> str:
        from datetime import datetime
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
