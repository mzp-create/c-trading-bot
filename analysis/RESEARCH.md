# Sentiment Analysis Research — May 2026

## Ranked Recommendation

### #1: RSS Feeds + Lexicon Scoring ← RECOMMENDED (zero cost, already built)
- **Cost:** $0, unlimited, no API keys needed
- **Sources:** CoinDesk RSS (verified working) + CoinTelegraph RSS (verified working)
- **Quality:** Top 2 crypto news sources. ~40 headlines per fetch, covering BTC/ETH/SOL
- **Implementation:** Already built in analysis/sentiment.py — `SentimentAnalyzer` class
- **Limitation:** Lexicon misses sarcasm/context (fine for confirmation filter)

### #2: NewsAPI.org free tier
- **Cost:** $0 — 100 requests/day
- **Signup:** https://newsapi.org/register (free key)
- **Quality:** Broader coverage (Bloomberg, Reuters, Yahoo Finance)
- **Integration:** Already built in the module — set NEWSAPI_KEY env var

### #3: DeepSeek LLM scoring (optional)
- **Cost:** ~$0.0001 per 20-headline batch = $0.03/day at 288 calls
- **Quality:** Better than lexicon — understands context, nuance, sarcasm
- **Integration:** Already built — set LLM_API_KEY env var
- **Use when:** You want maximum accuracy for a few cents/month

### #4: Reddit OAuth (free, need app registration)
- **Cost:** $0 — 600 requests/min
- **Setup:** Register app at reddit.com/prefs/apps, set REDDIT_CLIENT_ID/SECRET
- **Value:** Retail sentiment, contrarian indicator
- **Integration:** Already built in the module

### #5: Google Trends (fragile, not recommended)
- Google blocks most cloud IPs (429 errors)
- Works from residential IPs only
- Use only as bonus signal from home connection

### #6: LunarCrush / Santiment
- Paywalled for API access (Santiment $99+/month)
- Free tiers limited to basic price data, not sentiment
- Not recommended for low-cost approach

## What's Already Built
- File: analysis/sentiment.py (807 lines)
- CLI: python3 analysis/sentiment.py [BTC|ETH|SOL]
- Integration: analysis/sentiment_integration.md

## Integration Steps (main.py)
1. Import: `from analysis.sentiment import SentimentAnalyzer`
2. Init: `self.sentiment = SentimentAnalyzer(self.config)` in __init__
3. Filter: call `self.sentiment.get_signal_filter(symbol, signal, confidence)`
4. Config: add `sentiment: {enabled: true, cache_ttl: 300}`
