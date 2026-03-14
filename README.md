# dexscr_vizimka

Version: **0.2**

DexScreener-based alpha monitor for Base + Ethereum in paper-trades mode.

## What it does
- scans DexScreener candidates
- filters by mcap/liquidity/volume
- excludes stables/wrapped majors
- computes simple score
- simulates paper buy/sell with TP/SL/timeout

## Run
```bash
python3 dex_alpha_paper_monitor.py --capital-usd 1000 --ticket-share 0.1
```

## Notes
- State file: `memory/dex-alpha-paper-state.json` (relative to workspace usage)
- Intended for monitoring/research, not financial advice.
