#!/usr/bin/env python3
"""
Live mode setup — configure Bitfinex API keys and Telegram.
Run this when you have your credentials ready.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent

def main():
    print("=" * 50)
    print("🔐 Hermes Trading Bot — Live Mode Setup")
    print("=" * 50)
    print()
    print("I'll create a .env file with your credentials.")
    print("This file is for your eyes only — keep it safe!")
    print()
    
    env_path = ROOT / '.env'
    
    # Don't overwrite existing
    if env_path.exists():
        overwrite = input(".env already exists. Overwrite? (y/N): ").lower()
        if overwrite != 'y':
            print("Keeping existing .env")
            print()
            print("Make sure it has these variables:")
            print("  BITFINEX_API_KEY=your_key_here")
            print("  BITFINEX_API_SECRET=your_secret_here")
            print("  TELEGRAM_BOT_TOKEN=your_token_here")
            print("  TELEGRAM_CHAT_ID=your_chat_id_here")
            return
    
    print("Enter your credentials (paste and press Enter):")
    print()
    
    bitfinex_key = input("Bitfinex API Key: ").strip()
    bitfinex_secret = input("Bitfinex API Secret: ").strip()
    tg_token = input("Telegram Bot Token (optional — press Enter to skip): ").strip()
    tg_chat = input("Telegram Chat ID (optional): ").strip()
    
    env_content = f"""# Hermes Trading Bot — Credentials
# NEVER share this file or commit it to git!
BITFINEX_API_KEY={bitfinex_key}
BITFINEX_API_SECRET={bitfinex_secret}
"""
    if tg_token:
        env_content += f"TELEGRAM_BOT_TOKEN={tg_token}\n"
    if tg_chat:
        env_content += f"TELEGRAM_CHAT_ID={tg_chat}\n"
    
    # Set restrictive permissions
    with open(env_path, 'w') as f:
        f.write(env_content)
    os.chmod(env_path, 0o600)
    
    print()
    print("✅ Credentials saved to .env (permissions: 600)")
    print()
    print("To start the LIVE bot:")
    print("  source venv/bin/activate")
    print("  cd trading-bot")
    print("  source .env")
    print("  python main.py --mode live")
    print()
    print("To test connectivity first:")
    print("  python setup.py")


if __name__ == "__main__":
    main()
