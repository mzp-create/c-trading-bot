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

        # Instance identification for dual-bot setup
        self.instance = config.get('trading', {}).get('instance', 'default')
        self.trade_direction = config.get('trading', {}).get('trade_direction', 'both')
        self.instance_prefix = config.get('monitoring', {}).get('instance_prefix', '')
        
        # Build header prefix for messages
        if self.instance_prefix:
            self.msg_prefix = f"{self.instance_prefix} "
        elif self.instance == 'long':
            self.msg_prefix = "🟢 **LONG** | "
        elif self.instance == 'short':
            self.msg_prefix = "🔴 **SHORT** | "
        else:
            self.msg_prefix = ""

        self._last_update_id = 0
        self._current_chat_id = None
        self._last_command_time = 0.0
        self._last_command_text = ""

        if self.enabled:
            self.log.info(f"Telegram notifications enabled (instance={self.instance})")
        else:
            self.log.info("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars")

    def send(self, message: str) -> bool:
        """Send a message to Telegram."""
        if not self.enabled:
            self.log.debug(f"[Telegram disabled] Would send: {message[:100]}...")
            return False

        try:
            # Add instance prefix to message
            prefixed_message = self.msg_prefix + message if self.msg_prefix else message
            
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    'chat_id': self.chat_id,
                    'text': prefixed_message,
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

    def send_startup(self, mode: str, capital: float, symbols: list):
        """Send startup notification."""
        # Instance-specific header
        if self.instance == 'long':
            header = "🟢 **LONG Bot Started**"
            direction_note = "\n📍 Only BUY signals (long positions)"
        elif self.instance == 'short':
            header = "🔴 **SHORT Bot Started**"
            direction_note = "\n📍 Only SELL signals (short positions)"
        else:
            header = "🤖 **Bot Started**"
            direction_note = ""
        
        symbols_str = ', '.join(symbols) if isinstance(symbols, list) else symbols
        
        msg = (
            f"{header}{direction_note}\n"
            f"Mode: {mode.upper()}\n"
            f"Capital: ${capital:.2f}\n"
            f"Daily Target: $25.00\n"
            f"Symbols: {symbols_str}\n"
            f"Time: {self._now_str()}"
        )
        return self.send(msg)

    def send_cycle_summary(self, results: list[dict], daily_pnl: float,
                           open_positions: int, trade_count: int,
                           market_data: dict = None, regime: str = "",
                           daily_target: float = 10.0):
        """Send an enhanced cycle summary with market overview and signal details."""
        
        # Instance-specific header
        if self.instance == 'long':
            header = "🟢 **LONG Bot | Market Overview**"
        elif self.instance == 'short':
            header = "🔴 **SHORT Bot | Market Overview**"
        else:
            header = "📊 **Market Overview**"
        
        lines = [header]

        # Market regime indicator
        regime_emoji = {"TRENDING": "📈", "RANGING": "📊", "VOLATILE": "🌪️"}.get(regime, "⚪")
        if regime:
            lines.append(f"{regime_emoji} Regime: *{regime}*")
            lines.append("")

        # Per-pair detailed signals
        for r in results:
            sym = r.get("symbol_name", r.get("symbol", "?"))
            sig = r.get("signal", "?")
            conf = r.get("confidence", 0)
            price = r.get("current_price", 0)
            reason = r.get("reason", "")

            # Signal emoji and strength bar
            if sig == "BUY":
                emoji = "🟢"
                strength = "▰" * int(conf * 5) + "▱" * (5 - int(conf * 5))
            elif sig == "SELL":
                emoji = "🔴"
                strength = "▰" * int(conf * 5) + "▱" * (5 - int(conf * 5))
            else:
                emoji = "⚪"
                strength = "▱▱▱▱▱"

            # Format price based on value
            if price > 1000:
                price_str = f"${price:,.0f}"
            elif price > 100:
                price_str = f"${price:.1f}"
            else:
                price_str = f"${price:.2f}"

            lines.append(f"{emoji} *{sym}* — {price_str}")
            lines.append(f"   Signal: `{sig}` {strength} ({conf:.0%})")

            # Add LLM override note if present
            if "LLM" in reason or "overrode" in reason.lower():
                lines.append(f"   🧠 LLM filtered")

        lines.append("")

        # Performance summary
        pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
        target = daily_target
        progress = min(100, abs(daily_pnl) / target * 100) if target > 0 else 0
        progress_bar = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))

        lines.append(f"💰 *Performance*")
        lines.append(f"   Daily PnL: {pnl_emoji} ${daily_pnl:+.2f}")
        lines.append(f"   Progress:  [{progress_bar}] {progress:.0f}% of ${target:.0f}")
        lines.append(f"   📊 Positions: {open_positions} | 🔁 Trades: {trade_count}")

        return self.send("\n".join(lines))

    def send_portfolio_summary(self, balance: dict, positions: list,
                                daily_pnl: float, total_trades: int,
                                market_prices: dict = None):
        """Send a comprehensive portfolio summary with allocation breakdown.

        Parameters
        ----------
        balance : dict
            Balance dict with 'free', 'used', 'total' per currency
        positions : list
            Open positions with unrealized PnL
        daily_pnl : float
            Today's realized PnL
        total_trades : int
            Number of trades today
        market_prices : dict
            Current market prices for valuation (optional)
        """
        lines = ["💼 **Portfolio Summary**"]
        lines.append("")

        # Wallet breakdown
        free = balance.get("free", {})
        used = balance.get("used", {})
        total = balance.get("total", {})

        usdt_free = float(free.get("USDT", 0))
        usdt_used = float(used.get("USDT", 0))
        usdt_total = float(total.get("USDT", 0))

        lines.append("💵 *Margin Wallet*")
        lines.append(f"   Available: ${usdt_free:,.2f}")
        lines.append(f"   In Positions: ${usdt_used:,.2f}")
        lines.append(f"   Total: ${usdt_total:,.2f}")
        lines.append("")

        # Asset holdings with valuations
        lines.append("🏦 *Holdings*")
        assets = ["BTC", "ETH", "SOL"]
        total_value = usdt_total

        for asset in assets:
            qty = float(total.get(asset, 0))
            if qty > 0:
                price = market_prices.get(f"{asset}/USDT", 0) if market_prices else 0
                value = qty * price
                total_value += value
                if price > 0:
                    lines.append(f"   {asset}: {qty:.6f} ≈ ${value:,.2f}")
                else:
                    lines.append(f"   {asset}: {qty:.6f}")

        lines.append("")

        # Open positions with PnL
        if positions:
            lines.append("📈 *Open Positions*")
            total_unrealized = 0.0

            for pos in positions:
                sym = pos.get("symbol", "?")
                side = pos.get("side", "buy").upper()
                qty = float(pos.get("amount", 0))
                entry = float(pos.get("entry_price", 0))
                unrealized = float(pos.get("unrealized_pnl", 0))
                total_unrealized += unrealized

                emoji = "🟢" if unrealized >= 0 else "🔴"
                side_emoji = "📗" if side == "BUY" else "📕"

                lines.append(f"   {side_emoji} {sym} {side}")
                lines.append(f"      Size: {qty:.4f} @ ${entry:,.2f}")
                lines.append(f"      PnL: {emoji} ${unrealized:+.2f}")

            lines.append("")
            pnl_emoji = "🟢" if total_unrealized >= 0 else "🔴"
            lines.append(f"   *Total Unrealized:* {pnl_emoji} ${total_unrealized:+.2f}")
        else:
            lines.append("📭 *No Open Positions*")

        lines.append("")

        # Daily performance
        lines.append("📊 *Today's Performance*")
        pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"
        lines.append(f"   Realized PnL: {pnl_emoji} ${daily_pnl:+.2f}")
        lines.append(f"   Trades: {total_trades}")

        if daily_pnl != 0 and total_trades > 0:
            avg_trade = daily_pnl / total_trades
            lines.append(f"   Avg/Trade: ${avg_trade:+.2f}")

        return self.send("\n".join(lines))

    # ── Command handling (getUpdates polling) ─────────────────────────────

    def poll_commands(self) -> Optional[dict]:
        """Quick poll for a single Telegram command (non-blocking).

        Returns the first pending command as a dict:
            {'cmd': '/status', 'args': '', 'chat_id': '123'}
        or None if no commands found.

        Only processes commands from the configured chat_id (P2-11 whitelist).
        """
        if not self.enabled:
            return None

        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self._last_update_id + 1,
                    "timeout": 0,  # short poll — check once, don't long-wait
                    "allowed_updates": ["message"],
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
                # Whitelist check: only process commands from configured chat_id (P2-11)
                if chat_id != self.chat_id:
                    self.log.debug(f"Ignoring command from unauthorized chat: {chat_id}")
                    continue
                # Deduplication: skip if same command text processed within 5 seconds
                import time as _time
                now = _time.time()
                if text == self._last_command_text and (now - self._last_command_time) < 5.0:
                    self.log.debug(f"Skipping duplicate command: {text}")
                    continue
                self._last_command_text = text
                self._last_command_time = now
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
            "/portfolio": self._cmd_portfolio,
            "/pause":    self._cmd_pause,
            "/resume":   self._cmd_resume,
            "/positions": self._cmd_positions,
            "/close":    self._cmd_close,
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
            "/portfolio — Full portfolio summary\n"
            "/positions — Open position details\n"
            "/close <symbol> — Close a position (e.g. /close BTC/USDT)\n"
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
            wallets = bot.collector.client.fetch_balance()
            total = {w.currency: w.balance for w in wallets}
            free = {w.currency: w.available for w in wallets}
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
    def _cmd_portfolio(_args: str, bot: "TradingBot") -> str:  # noqa: F821
        """Show comprehensive portfolio summary."""
        try:
            # Fetch balance
            _wallets = bot.collector.client.fetch_balance()
            total = {w.currency: w.balance for w in _wallets}
            free = {w.currency: w.available for w in _wallets}
            used = {w.currency: w.balance - w.available for w in _wallets}

            # Get current market prices for valuation
            prices = {}
            for s in bot.symbols:
                try:
                    sym = s["name"]
                    price = bot.collector.get_current_price(sym)
                    if price:
                        prices[sym] = price
                except Exception:
                    pass

            # Build portfolio lines
            lines = ["💼 **Portfolio Summary**"]
            lines.append("")

            # USDT Margin breakdown
            usdt_free = float(free.get("USDT", 0))
            usdt_used = float(used.get("USDT", 0))
            usdt_total = float(total.get("USDT", 0))

            lines.append("💵 *Margin Wallet*")
            lines.append(f"   Available: ${usdt_free:,.2f}")
            lines.append(f"   In Positions: ${usdt_used:,.2f}")
            lines.append(f"   Total: ${usdt_total:,.2f}")
            lines.append("")

            # Crypto holdings with valuations
            lines.append("🏦 *Asset Holdings*")
            crypto_value = 0.0
            for asset in ["BTC", "ETH", "SOL"]:
                qty = float(total.get(asset, 0))
                if qty > 0:
                    price = prices.get(f"{asset}/USDT", 0)
                    value = qty * price
                    crypto_value += value
                    # Asset emojis
                    if asset == "BTC":
                        emoji = "₿"
                    elif asset == "ETH":
                        emoji = "Ξ"
                    else:
                        emoji = "◎"
                    lines.append(f"   {emoji} {asset}: {qty:.6f} ≈ ${value:,.2f}")

            total_portfolio = usdt_total + crypto_value
            lines.append(f"   ")
            lines.append(f"   *Total Portfolio:* ${total_portfolio:,.2f}")
            lines.append("")

            # Open positions with PnL
            positions = bot.executor.open_positions
            if positions:
                lines.append("📈 *Open Positions*")
                total_unrealized = 0.0

                for pos in positions:
                    sym = pos.get("symbol", "?")
                    side = pos.get("side", "buy").upper()
                    qty = float(pos.get("amount", 0))
                    entry = float(pos.get("entry_price", 0))
                    unrealized = float(pos.get("unrealized_pnl", 0) or 0)
                    total_unrealized += unrealized

                    emoji = "🟢" if unrealized >= 0 else "🔴"
                    side_emoji = "📗" if side == "BUY" else "📕"

                    lines.append(f"   {side_emoji} {sym} {side}")
                    lines.append(f"      Size: {qty:.4f} @ ${entry:,.2f}")
                    lines.append(f"      PnL: {emoji} ${unrealized:+.2f}")

                lines.append("")
                pnl_emoji = "🟢" if total_unrealized >= 0 else "🔴"
                lines.append(f"   *Total Unrealized:* {pnl_emoji} ${total_unrealized:+.2f}")
            else:
                lines.append("📭 *No Open Positions*")

            lines.append("")

            # Today's performance
            lines.append("📊 *Today's Performance*")
            pnl_emoji = "🟢" if bot.daily_pnl >= 0 else "🔴"
            lines.append(f"   Realized PnL: {pnl_emoji} ${bot.daily_pnl:+.2f}")
            lines.append(f"   Trades: {bot.trade_count}")

            if bot.trade_count > 0:
                avg = bot.daily_pnl / bot.trade_count
                lines.append(f"   Avg per Trade: ${avg:+.2f}")

            return "\n".join(lines)

        except Exception as e:
            return f"⚠️ Portfolio fetch failed: {e}"

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
            qty = p.get("amount", p.get("quantity", 0))
            entry = p.get("entry_price", 0)
            side_raw = p.get("side", "long")
            direction = "🟢" if side_raw.lower() in ("buy", "long") else "🔴"
            pnl = p.get("unrealized_pnl", 0)
            # SL/TP may not be in live position data from exchange
            sl = p.get("stop_loss")
            tp = p.get("take_profit")
            current_price = 0
            try:
                current_price = bot.collector.get_current_price(sym)
            except Exception:
                pass

            lines.append(
                f"\n{direction} **{sym}**\n"
                f"  Side: {side_raw.upper()}\n"
                f"  Qty: {qty:.4f} @ ${entry:.2f}\n"
            )
            if sl:
                lines.append(f"  SL: ${float(sl):.2f}")
            if tp:
                lines.append(f"  TP: ${float(tp):.2f}")
            if current_price and entry > 0:
                if side_raw.lower() in ("buy", "long"):
                    pnl_val = (current_price - entry) * qty
                else:
                    pnl_val = (entry - current_price) * qty
                lines[-1] += f" | Current: ${current_price:.2f}"
                lines.append(f"  PnL: ${pnl_val:+.2f}")
            elif pnl != 0:
                lines.append(f"  PnL: ${float(pnl):+.2f}")

        return "\n".join(lines)

    @staticmethod
    def _cmd_close(args: str, bot: "TradingBot") -> str:  # noqa: F821
        """Close a specific position."""
        if not args:
            return "⚠️ Usage: `/close <symbol>` (e.g. `/close BTC/USDT`)"

        symbol = args.strip()
        # Normalize symbol format
        if "/" not in symbol:
            # Try to add /USDT if missing
            symbol = f"{symbol}/USDT"

        # Check if position exists
        positions = bot.executor.open_positions
        target = None
        for p in positions:
            if p.get("symbol") == symbol:
                target = p
                break

        if not target:
            return f"📭 No open position for `{symbol}`"

        # Execute close
        result = bot.executor.close_position(symbol, reason="manual")
        if result.get('success'):
            pnl = result.get('pnl', 0)
            emoji = "🟢" if pnl >= 0 else "🔴"
            return (
                f"{emoji} **Position Closed**\n"
                f"Symbol: `{symbol}`\n"
                f"PnL: ${pnl:+.2f}\n"
                f"Price: ${result.get('price', 0):.2f}"
            )
        else:
            return f"⚠️ Failed to close `{symbol}`: {result.get('error', 'Unknown error')}"

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
        lines = ["📊 **Market Overview**"]

        # --- Prices for each symbol ---
        for s in bot.symbols:
            sym = s["name"]
            try:
                price = bot.collector.get_current_price(sym)
                if price:
                    short = sym.split("/")[0]
                    lines.append(f"\n{short}: **${price:,.0f}**")
            except Exception:
                pass

        # --- Regime ---
        if hasattr(bot, "regime_detector") and bot.regime_detector:
            try:
                reg = bot.regime_detector.current_regime
                if reg:
                    emoji = {"TRENDING": "📈", "RANGING": "📊", "VOLATILE": "🌪️"}.get(reg.name, "❓")
                    lines.append(f"\nRegime: {emoji} {reg.name}")
            except Exception:
                pass

        # --- Sentiment ---
        try:
            result = sent.analyze("BTC")  # fetches all headlines, returns full sentiment
            score = result.score
            conf = result.confidence
            headlines = result.sample_headlines or []
            # Determine label from score
            if score > 0.1:
                label = "Bullish"
            elif score < -0.1:
                label = "Bearish"
            else:
                label = "Neutral"
        except Exception:
            score, label, conf, headlines = 0.0, "Neutral", 0.0, []

        lines.append(f"\n{'🟢' if score > 0.1 else '🔴' if score < -0.1 else '⚪'} **Sentiment**: {score:+.3f} ({label}) — conf: {conf:.2f}")

        if headlines:
            lines.append(f"\nLatest headlines ({len(headlines)} total):")
            for h in headlines[:5]:
                lines.append(f"  • {h[:100]}")
        else:
            if not headlines:
                lines.append("\nNo recent news headlines.")

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
