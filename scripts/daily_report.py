#!/usr/bin/env python3
"""Daily Bitfinex trading report generator for scheduled delivery."""

import ccxt
import os
import json
import sys
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def load_env():
    """Load environment variables from .env file."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if line.strip() and not line.startswith('#') and '=' in line:
                    key, value = line.strip().split('=', 1)
                    key = key.replace('export ', '').strip()
                    value = value.strip().strip('"').strip("'")
                    os.environ[key] = value

def generate_report():
    """Generate comprehensive trading report."""
    load_env()
    
    api_key = os.getenv('BITFINEX_API_KEY')
    api_secret = os.getenv('BITFINEX_API_SECRET')
    
    if not api_key or not api_secret:
        return "Error: API credentials not found"
    
    # Initialize exchange
    exchange = ccxt.bitfinex({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'options': {'defaultType': 'margin'}
    })
    
    lines = []
    lines.append("=" * 60)
    lines.append("📊 DAILY BITFINEX MARGIN TRADING REPORT")
    lines.append("=" * 60)
    lines.append(f"Report Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("")
    
    # Wallet balance
    lines.append("💰 MARGIN WALLET")
    lines.append("-" * 60)
    try:
        balances = exchange.fetch_balance({'type': 'margin'})
        margin = balances.get('margin', {})
        
        ust = margin.get('UST', {})
        btc = margin.get('BTC', {})
        eth = margin.get('ETH', {})
        sol = margin.get('SOL', {})
        
        total_ust = ust.get('total', 0)
        lines.append(f"  USDT (UST): ${total_ust:,.2f}")
        lines.append(f"  BTC: {btc.get('total', 0):.8f}")
        lines.append(f"  ETH: {eth.get('total', 0):.6f}")
        lines.append(f"  SOL: {sol.get('total', 0):.4f}")
    except Exception as e:
        lines.append(f"  Error: {e}")
    
    lines.append("")
    
    # Positions
    lines.append("📈 OPEN POSITIONS")
    lines.append("-" * 60)
    try:
        positions = exchange.private_post_auth_r_positions()
        if positions:
            for pos in positions:
                symbol = pos[0].replace('t', '').replace('UST', '/USDT')
                status = pos[1]
                size = float(pos[2])
                entry = float(pos[3])
                pnl = float(pos[6])
                pnl_pct = float(pos[7])
                
                side = "LONG" if size > 0 else "SHORT"
                lines.append(f"  {symbol}: {side} | Size: {abs(size):.6f} | Entry: ${entry:,.2f}")
                lines.append(f"     P&L: ${pnl:+.4f} ({pnl_pct*100:+.2f}%)")
        else:
            lines.append("  No open positions")
    except Exception as e:
        lines.append(f"  Error: {e}")
    
    lines.append("")
    
    # Yesterday's trades
    lines.append("📅 YESTERDAY'S TRADING ACTIVITY")
    lines.append("-" * 60)
    try:
        yesterday_start = int((datetime.now() - timedelta(days=1)).timestamp() * 1000)
        symbols = ['tBTCUST', 'tETHUST', 'tSOLUST']
        
        total_trades = 0
        total_volume = 0
        total_fees = 0
        
        recent_trades = []
        for symbol in symbols:
            try:
                trades = exchange.fetch_my_trades(symbol, since=yesterday_start, limit=50)
                for trade in trades:
                    total_trades += 1
                    total_volume += trade.get('cost', 0)
                    fee_obj = trade.get('fee')
                    total_fees += fee_obj.get('cost', 0) if fee_obj else 0
                    recent_trades.append({
                        'time': datetime.fromtimestamp(trade.get('timestamp', 0)/1000).strftime('%H:%M'),
                        'symbol': symbol.replace('t', '').replace('UST', ''),
                        'side': trade.get('side', '').upper(),
                        'amount': trade.get('amount', 0),
                        'price': trade.get('price', 0),
                        'cost': trade.get('cost', 0)
                    })
            except:
                pass
        
        lines.append(f"  Trades Executed: {total_trades}")
        lines.append(f"  Volume: ${total_volume:,.2f}")
        lines.append(f"  Fees: ${total_fees:.4f}")
        
        if recent_trades:
            lines.append("")
            lines.append("  Recent Trades:")
            for t in recent_trades[:10]:
                lines.append(f"    {t['time']} {t['symbol']} {t['side']} {t['amount']:.6f} @ ${t['price']:,.2f}")
    except Exception as e:
        lines.append(f"  Error: {e}")
    
    lines.append("")
    lines.append("🎯 DAILY TARGET")
    lines.append("-" * 60)
    lines.append("  Target: $25.00/day ($12.50 per instance)")
    lines.append("  Status: Trading actively")
    
    lines.append("")
    lines.append("=" * 60)
    lines.append("🤖 Report by Hermes Agent Trading Bot")
    lines.append("=" * 60)
    
    return "\n".join(lines)

if __name__ == "__main__":
    print(generate_report())
