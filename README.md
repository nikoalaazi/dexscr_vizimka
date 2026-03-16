# dexscr_vizimka

Version: **0.3**

DexScreener-based alpha monitor for Base + Ethereum in paper-trades mode.

## What it does (v0.3)
- scans DexScreener candidates
- filters by mcap/liquidity/volume
- excludes stables/wrapped majors
- computes simple score
- **Nansen smart money integration** — shows smart wallet count and netflow
- **Twitter profile analysis** — followers, posts, engagement
- **Nansen holders → Twitter following** — follow top token holders with Twitter
- **Obsidian export** — saves holder profiles to markdown
- simulates paper buy/sell with TP/SL/timeout

## Run basic
```bash
python3 dex_alpha_paper_monitor.py --capital-usd 1000 --ticket-share 0.1
```

## Run with all features
```bash
python3 dex_alpha_paper_monitor.py \
  --capital-usd 1000 \
  --ticket-share 0.1 \
  --follow-token-holders \
  --max-holders-per-token 5 \
  --enable-influencer-tracking
```

## Features

### Nansen Smart Money
Shows smart wallet activity for each candidate token.

### Twitter Analysis
Scrapes Twitter profiles for:
- Follower count
- Recent posts
- Engagement metrics

### Holder Following (Nansen)
Finds top token holders via Nansen API who have Twitter accounts and automatically follows them.

### Obsidian Export
Saves followed holders to `memory/obsidian/crypto_holders/` as markdown notes with tags.

## Notes
- State file: `memory/dex-alpha-paper-state.json`
- Obsidian vault: `memory/obsidian/crypto_holders/`
- Randomized delays to avoid Twitter rate limits
- Intended for monitoring/research, not financial advice.
