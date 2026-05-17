#!/usr/bin/env python3
"""
Telegram notifications for the trading bot.
Sends trade alerts, daily summaries, and error notifications.
"""

import os
import logging
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
        """Send a compact cycle summary for all pairs."""
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
