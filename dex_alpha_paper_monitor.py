#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*")
warnings.filterwarnings("ignore", module=r"urllib3.*")

import requests
from dotenv import dotenv_values


WORKSPACE = Path(__file__).resolve().parents[1]
STATE_PATH = WORKSPACE / "memory" / "dex-alpha-paper-state.json"

DEX_TOKEN_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKEN_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_TOKEN_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_TOKEN_PAIRS = "https://api.dexscreener.com/latest/dex/tokens/{token}"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"
NANSEN_TOKEN_SCREENER = "https://api.nansen.ai/api/v1/token-screener"
NANSEN_SMART_MONEY_FLOW = "https://api.nansen.ai/api/v1/token-god-mode/smart-money-flow"
NANSEN_PROFILER = "https://api.nansen.ai/api/v1/profiler"

ALLOWED_CHAINS = {"base", "ethereum"}
EXCLUDED_SYMBOLS = {
    "USDC", "USDT", "DAI", "USDE", "EUSD", "WETH", "WBTC", "STETH", "WSTETH", "CBETH", "MSETH",
}


@dataclass
class Candidate:
    chain: str
    symbol: str
    token_address: str
    pair_address: str
    dex_id: str
    mcap: float
    liquidity_usd: float
    volume_24h: float
    price_usd: float
    txns_24h: int
    buys_24h: int
    sells_24h: int
    twitter: str | None
    score: float
    fail_reasons: list[str]


def _num(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def reason_priority(reason: str) -> int:
    # lower is more critical
    order = {
        "liquidity": 1,
        "volume": 2,
        "mcap": 3,
    }
    return order.get(reason, 99)


def risk_label(c: Candidate, strict: bool) -> str:
    if not strict:
        return "🟠 near-miss"
    if c.liquidity_usd < 100_000:
        return "🟠 повышенный"
    if c.txns_24h < 80:
        return "🟠 повышенный"
    return "🟢 норм"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="DexScreener alpha scout v2 (Base+ETH) with paper trades")
    ap.add_argument("--capital-usd", type=float, default=1000.0)
    ap.add_argument("--ticket-share", type=float, default=0.1, help="paper position size = capital * share")
    ap.add_argument("--min-liquidity", type=float, default=50_000)
    ap.add_argument("--min-volume-24h", type=float, default=50_000)
    ap.add_argument("--min-mcap", type=float, default=200_000)
    ap.add_argument("--max-mcap", type=float, default=10_000_000)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--top-per-chain", type=int, default=3)
    ap.add_argument("--entry-score", type=float, default=7.0)
    ap.add_argument("--tp", type=float, default=0.4, help="take profit in fraction")
    ap.add_argument("--sl", type=float, default=0.2, help="stop loss in fraction")
    ap.add_argument("--max-hold-hours", type=int, default=48)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watchlist", default="AERO,DEGEN,BRETT,TOSHI,VIRTUAL,MORPHO,JUNO,DIEM")
    ap.add_argument("--nansen-per-page", type=int, default=80)
    ap.add_argument("--enable-influencer-tracking", action="store_true", help="Enable following and monitoring crypto influencers")
    ap.add_argument("--max-follow-per-run", type=int, default=5, help="Max influencers to follow per run (avoid rate limits)")
    ap.add_argument("--follow-token-holders", action="store_true", help="Follow top token holders who have Twitter (via Nansen)")
    ap.add_argument("--max-holders-per-token", type=int, default=5, help="Max holders to follow per token")
    return ap.parse_args()


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"positions": {}, "last_report_hash": "", "updated_at": 0}


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = int(time.time())
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_nansen_smart_money(chain: str, token_address: str) -> dict | None:
    """Fetch smart money data for a specific token from Nansen API."""
    # Check env var first (like base_digest_autobuy), then .env file
    key = os.getenv("NANSEN_API_KEY")
    if not key:
        env = dotenv_values(str(WORKSPACE / ".env"))
        key = env.get("NANSEN_API_KEY")
    if not key:
        return None
    
    # Use proxy if configured
    proxy = os.getenv("NANSEN_PROXY") or dotenv_values(str(WORKSPACE / ".env")).get("NANSEN_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    headers = {"Content-Type": "application/json", "apikey": key}
    chain_id = "bnb" if chain == "bsc" else chain
    
    payload = {
        "chains": [chain_id],
        "timeframe": "24h",
        "pagination": {"page": 1, "per_page": 200},
        "filters": {},
        "order_by": [{"field": "netflow", "direction": "DESC"}],
    }
    
    try:
        r = requests.post(NANSEN_TOKEN_SCREENER, json=payload, headers=headers, proxies=proxies, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", []):
            if item.get("token_address", "").lower() == token_address.lower():
                return {
                    "smart_wallets": item.get("nof_traders", 0),
                    "netflow": item.get("netflow", 0),
                    "liquidity": item.get("liquidity", 0),
                }
        return None
    except Exception as e:
        return None


def scrape_twitter_profile(page, twitter_url: str) -> dict | None:
    """Scrape Twitter/X profile for followers, engagement metrics."""
    if not twitter_url or "x.com" not in twitter_url and "twitter.com" not in twitter_url:
        return None
    
    try:
        # Clean up URL - ensure we're going to the profile page
        profile_url = twitter_url.strip()
        if "?" in profile_url:
            profile_url = profile_url.split("?")[0]
        
        page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2500)
        
        # Check if redirected to login
        if "flow/login" in page.url or "login" in page.url:
            return {"error": "X login required", "followers": None}
        
        result = {"followers": None, "following": None, "recent_posts": 0, "engagement": 0}
        
        # Try to extract followers count from various selectors
        try:
            # Modern X layout - look for followers in aria-label or text
            body_text = page.locator("body").inner_text()
            
            # Look for "Followers" pattern
            import re
            followers_match = re.search(r'(\d+[.,]?\d*)\s*[KkMm]?\s*[Ff]ollowers', body_text)
            if followers_match:
                followers_str = followers_match.group(1).replace(',', '').replace('.', '')
                multiplier = 1
                if 'K' in followers_match.group(0).upper():
                    multiplier = 1000
                elif 'M' in followers_match.group(0).upper():
                    multiplier = 1000000
                result["followers"] = int(float(followers_str) * multiplier)
            
            # Count recent posts (tweets visible on page)
            tweets = page.locator("article[data-testid='tweet']").count()
            result["recent_posts"] = tweets
            
            # Calculate engagement proxy (likes + replies visible)
            try:
                likes = page.locator("button[data-testid='like']").count()
                replies = page.locator("button[data-testid='reply']").count()
                result["engagement"] = likes + replies
            except:
                pass
                
        except Exception as e:
            result["error"] = f"Parse error: {str(e)[:50]}"
        
        return result
    except Exception as e:
        return {"error": str(e)[:50], "followers": None}


def enrich_candidates_with_twitter(ctx, candidates: list[Candidate]) -> dict[str, dict]:
    """Enrich candidates with Twitter profile data via browser scrape."""
    twitter_data = {}
    page = None
    
    try:
        page = ctx.new_page()
        
        for c in candidates:
            if c.twitter:
                data = scrape_twitter_profile(page, c.twitter)
                if data:
                    twitter_data[c.symbol] = data
                    # Small delay to avoid rate limiting
                    time.sleep(0.5)
    finally:
        if page:
            try:
                page.close()
            except:
                pass
    
    return twitter_data


def enrich_candidates_with_nansen(candidates: list[Candidate]) -> dict[str, dict]:
    """Enrich candidates with Nansen smart money data."""
    nansen_data = {}
    for c in candidates:
        if c.token_address:
            sm_data = fetch_nansen_smart_money(c.chain, c.token_address)
            if sm_data:
                nansen_data[c.symbol] = {
                    "smart_wallets": sm_data.get("smartWallets", 0),
                    "netflow": sm_data.get("netflow", 0),
                    "inflow": sm_data.get("inflow", 0),
                    "outflow": sm_data.get("outflow", 0),
                }
    return nansen_data


def fetch_profiles() -> list[dict]:
    r = requests.get(DEX_TOKEN_PROFILES, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def fetch_boosts() -> list[dict]:
    out: list[dict] = []
    for url in (DEX_TOKEN_BOOSTS, DEX_TOKEN_BOOSTS_TOP):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                out.extend(data)
        except Exception:
            continue
    return out


def fetch_pairs(token_address: str) -> list[dict]:
    r = requests.get(DEX_TOKEN_PAIRS.format(token=token_address), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("pairs", []) if isinstance(data, dict) else []


def fetch_search_pairs(query: str) -> list[dict]:
    r = requests.get(DEX_SEARCH.format(q=query), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("pairs", []) if isinstance(data, dict) else []


def _get_nansen_key() -> str | None:
    env = dotenv_values(str(WORKSPACE / ".env"))
    return env.get("NANSEN_API_KEY")


def fetch_nansen_candidates(min_liquidity: float, max_mcap: float, per_page: int = 80) -> list[dict]:
    key = _get_nansen_key()
    if not key:
        return []
    out: list[dict] = []
    headers = {"Content-Type": "application/json", "apikey": key}
    for chain in ("base", "ethereum"):
        body = {
            "chains": [chain],
            "timeframe": "24h",
            "pagination": {"page": 1, "per_page": per_page},
            "filters": {"liquidity": {"min": min_liquidity}},
            "order_by": [{"field": "netflow", "direction": "DESC"}],
        }
        try:
            r = requests.post(NANSEN_TOKEN_SCREENER, json=body, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json().get("data", [])
        except Exception:
            data = []

        for it in data:
            sym = str(it.get("token_symbol") or "").upper().strip()
            addr = str(it.get("token_address") or "")
            mcap = _num(it.get("market_cap_usd"))
            liq = _num(it.get("liquidity"))
            traders = int(_num(it.get("nof_traders")))
            if not sym or not addr:
                continue
            if sym in EXCLUDED_SYMBOLS or any(x in sym for x in ("USD", "WETH", "WBTC", "STETH", "BTCB")):
                continue
            if not (200_000 <= mcap <= max_mcap):
                continue
            if liq < min_liquidity or traders <= 0:
                continue
            out.append({"chain": chain, "symbol": sym, "tokenAddress": addr, "traders": traders})
    return out


def score_pair(p: dict) -> float:
    liq = _num((p.get("liquidity") or {}).get("usd"))
    vol = _num((p.get("volume") or {}).get("h24"))
    tx = p.get("txns") or {}
    t24 = tx.get("h24") or {}
    buys = _num(t24.get("buys"))
    sells = _num(t24.get("sells"))
    ratio = (buys + 1.0) / (sells + 1.0)

    score = 0.0
    if liq >= 150_000:
        score += 2
    if liq >= 500_000:
        score += 1
    if vol >= 200_000:
        score += 2
    if vol >= 1_000_000:
        score += 1
    if ratio >= 1.1:
        score += 1
    if ratio >= 1.5:
        score += 1
    return score


def build_candidates(args: argparse.Namespace) -> tuple[list[Candidate], dict[str, list[Candidate]]]:
    profiles = fetch_profiles()
    boosts = fetch_boosts()
    nansen = fetch_nansen_candidates(args.min_liquidity, args.max_mcap, args.nansen_per_page)
    out: list[Candidate] = []

    twitter_by_token: dict[str, str] = {}
    for src in (profiles, boosts):
        for it in src:
            t = str(it.get("tokenAddress") or "")
            if not t:
                continue
            for s in it.get("links", []) or []:
                if isinstance(s, dict) and (s.get("type") == "twitter" or "twitter.com" in str(s.get("url", ""))):
                    twitter_by_token[t.lower()] = str(s.get("url") or "")
                    break

    pair_pool: list[dict] = []
    watch_syms = [x.strip().upper() for x in str(args.watchlist).split(",") if x.strip()]
    query_seeds = ["base", "ethereum", *watch_syms]
    for q in query_seeds:
        try:
            pair_pool.extend(fetch_search_pairs(q))
        except Exception:
            pass

    # Extra discovery from profiles/boosts token addresses.
    seeds: list[dict] = []
    seeds.extend(profiles[:120])
    seeds.extend(boosts[:120])
    seen_tokens = set()
    for pr in seeds:
        token = pr.get("tokenAddress") or ""
        chain_hint = (pr.get("chainId") or "").lower()
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        if chain_hint and chain_hint not in ALLOWED_CHAINS:
            continue
        try:
            pair_pool.extend(fetch_pairs(token)[:5])
        except Exception:
            continue

    # Stronger source: seed from Nansen top smart-money flow (Base + ETH).
    for it in nansen:
        token = str(it.get("tokenAddress") or "")
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        try:
            pair_pool.extend(fetch_pairs(token)[:8])
        except Exception:
            continue

    nansen_traders_by_token = {str(x.get("tokenAddress") or "").lower(): int(x.get("traders") or 0) for x in nansen}

    best_by_symbol: dict[str, Candidate] = {}
    for p in pair_pool:
        chain = (p.get("chainId") or "").lower()
        if chain not in ALLOWED_CHAINS:
            continue

        base = p.get("baseToken") or {}
        symbol = str(base.get("symbol") or "").upper().strip()
        token_addr = str(base.get("address") or "")
        if not symbol or not token_addr:
            continue
        if symbol in EXCLUDED_SYMBOLS or any(x in symbol for x in ("USD", "WETH", "WBTC", "STETH", "BTCB")):
            continue

        mcap = _num(p.get("marketCap")) or _num(p.get("fdv"))
        liq = _num((p.get("liquidity") or {}).get("usd"))
        vol24 = _num((p.get("volume") or {}).get("h24"))

        fail_reasons: list[str] = []
        if not (args.min_mcap <= mcap <= args.max_mcap):
            fail_reasons.append("mcap")
        if liq < args.min_liquidity:
            fail_reasons.append("liquidity")
        if vol24 < args.min_volume_24h:
            fail_reasons.append("volume")

        tx24 = (p.get("txns") or {}).get("h24") or {}
        sc = score_pair(p)
        tw = twitter_by_token.get(token_addr.lower())
        if not tw:
            info = p.get("info") or {}
            socials = info.get("socials") or []
            for s in socials:
                if isinstance(s, dict) and (s.get("type") == "twitter" or "twitter.com" in str(s.get("url", ""))):
                    tw = str(s.get("url") or "")
                    break
            if not tw:
                for s in (info.get("websites") or []):
                    if isinstance(s, dict) and "twitter.com" in str(s.get("url", "")):
                        tw = str(s.get("url") or "")
                        break

        if tw:
            sc += 1.0
        n_traders = int(nansen_traders_by_token.get(token_addr.lower(), 0))
        if n_traders >= 10:
            sc += 1.0
        if n_traders >= 50:
            sc += 1.0

        c = Candidate(
            chain=chain,
            symbol=symbol,
            token_address=token_addr,
            pair_address=str(p.get("pairAddress") or ""),
            dex_id=str(p.get("dexId") or ""),
            mcap=mcap,
            liquidity_usd=liq,
            volume_24h=vol24,
            price_usd=_num(p.get("priceUsd")),
            txns_24h=int(_num(tx24.get("buys")) + _num(tx24.get("sells"))),
            buys_24h=int(_num(tx24.get("buys"))),
            sells_24h=int(_num(tx24.get("sells"))),
            twitter=tw,
            score=sc,
            fail_reasons=fail_reasons,
        )

        old = best_by_symbol.get(symbol)
        if old is None or (c.score, c.volume_24h, c.liquidity_usd) > (old.score, old.volume_24h, old.liquidity_usd):
            best_by_symbol[symbol] = c

    all_cands = list(best_by_symbol.values())
    all_cands.sort(key=lambda c: (c.score, c.volume_24h, c.liquidity_usd), reverse=True)

    # top-N per chain strict + near-miss fillers (failed exactly one criterion)
    picked: list[Candidate] = []
    near_by_chain: dict[str, list[Candidate]] = {"base": [], "ethereum": []}
    for chain in ("base", "ethereum"):
        chain_cands = [c for c in all_cands if c.chain == chain]
        strict = [c for c in chain_cands if not c.fail_reasons]
        near = [c for c in chain_cands if len(c.fail_reasons) == 1]
        picked.extend(strict[: args.top_per_chain])
        near_by_chain[chain] = near

    picked.sort(key=lambda c: (c.chain, -c.score, -c.volume_24h, -c.liquidity_usd))
    return picked[: max(args.top, args.top_per_chain * 2)], near_by_chain


def apply_paper_trades(args: argparse.Namespace, st: dict, cands: list[Candidate]) -> list[dict]:
    positions = st.get("positions", {}) if isinstance(st.get("positions"), dict) else {}
    actions: list[dict] = []
    now = int(time.time())
    ticket = round(args.capital_usd * args.ticket_share, 2)

    by_symbol = {c.symbol: c for c in cands}

    # update existing positions
    for sym, pos in list(positions.items()):
        if pos.get("closed"):
            continue
        c = by_symbol.get(sym)
        if not c:
            # no market data now: keep position open
            continue
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0 or c.price_usd <= 0:
            continue
        pnl = (c.price_usd / entry) - 1.0
        hold_hours = (now - int(pos.get("opened_at", now))) / 3600.0

        reason = None
        if pnl >= args.tp:
            reason = "tp"
        elif pnl <= -args.sl:
            reason = "sl"
        elif hold_hours >= args.max_hold_hours:
            reason = "timeout"

        if reason:
            pos["closed"] = True
            pos["closed_at"] = now
            pos["exit_price"] = c.price_usd
            pos["pnl_pct"] = round(pnl * 100, 2)
            pos["close_reason"] = reason
            actions.append({"symbol": sym, "action": "paper_sell", "reason": reason, "pnl_pct": round(pnl * 100, 2)})

    # open new positions
    for c in cands:
        if c.score < args.entry_score:
            continue
        pos = positions.get(c.symbol)
        if pos and not pos.get("closed"):
            continue
        if c.price_usd <= 0:
            continue
        qty = round(ticket / c.price_usd, 8)
        positions[c.symbol] = {
            "chain": c.chain,
            "token": c.token_address,
            "entry_price": c.price_usd,
            "qty": qty,
            "notional": ticket,
            "opened_at": now,
            "score": c.score,
            "closed": False,
        }
        actions.append({"symbol": c.symbol, "action": "paper_buy", "price": c.price_usd, "score": c.score})

    st["positions"] = positions
    return actions


def main() -> int:
    args = parse_args()
    st = load_state()
    
    # Import playwright here for Twitter scraping
    from playwright.sync_api import sync_playwright

    try:
        cands, near_by_chain = build_candidates(args)
        # Enrich with Nansen smart money data
        nansen_enrichment = enrich_candidates_with_nansen(cands)
        
        # Enrich with Twitter data via browser scrape
        twitter_enrichment: dict[str, dict] = {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                twitter_enrichment = enrich_candidates_with_twitter(ctx, cands)
                try:
                    ctx.close()
                except:
                    pass
        except Exception as e:
            twitter_enrichment = {"_error": str(e)[:50]}
    except Exception as e:
        print("Сделано:")
        print("- Dex alpha scan (Base+ETH) не завершён.")
        print("")
        print("Риски:")
        print(f"- Источник данных недоступен: {e}")
        print("")
        print("Следующий шаг:")
        print("- Повторю цикл автоматически через 3 часа.")
        return 0

    actions = apply_paper_trades(args, st, cands)

    if not args.dry_run:
        save_state(st)

    lines = []
    lines.append("Сделано:")
    lines.append("- Dex alpha scout: Base + Ethereum")
    lines.append(f"- Кандидатов после фильтров: {len(cands)}")
    lines.append(f"- Режим: paper trades (ticket ${args.capital_usd * args.ticket_share:.2f})")
    lines.append("")
    lines.append("Топ-кандидаты (top-3 per chain):")
    if cands:
        for chain in ("base", "ethereum"):
            chain_cands = [c for c in cands if c.chain == chain]
            lines.append(f"- {chain.upper()}:")
            shown = 0
            for i, c in enumerate(chain_cands[: args.top_per_chain], 1):
                shown += 1
                # Get Nansen data for this candidate
                ns = nansen_enrichment.get(c.symbol, {})
                ns_info = ""
                if ns:
                    ns_info = f" | Nansen: смартов {ns.get('smart_wallets', 'N/A')}, netflow ${ns.get('netflow', 0):,.0f}"
                # Get Twitter data
                tw = twitter_enrichment.get(c.symbol, {})
                tw_info = ""
                if tw and "error" not in tw:
                    followers = tw.get('followers')
                    posts = tw.get('recent_posts', 0)
                    if followers:
                        tw_info = f" | Twitter: {followers:,} followers, {posts} posts"
                    else:
                        tw_info = " | Twitter: данные недоступны"
                elif tw and "error" in tw:
                    tw_info = f" | Twitter: {tw['error']}"
                else:
                    tw_info = " | Twitter: не найден"
                lines.append(
                    f"  {i}) {c.symbol} — {risk_label(c, strict=True)}, score {c.score:.1f}, mcap ${c.mcap:,.0f}, liq ${c.liquidity_usd:,.0f}, vol24h ${c.volume_24h:,.0f}, smart proxy tx {c.txns_24h}{ns_info}{tw_info}"
                )

            if shown < args.top_per_chain:
                fillers = near_by_chain.get(chain, [])
                fillers = sorted(
                    fillers,
                    key=lambda x: (reason_priority(x.fail_reasons[0] if x.fail_reasons else "unknown"), -x.score, -x.volume_24h),
                )
                idx = shown + 1
                for f in fillers[: max(0, args.top_per_chain - shown)]:
                    reason = f.fail_reasons[0] if f.fail_reasons else "unknown"
                    ns = nansen_enrichment.get(f.symbol, {})
                    ns_info = ""
                    if ns:
                        ns_info = f" | Nansen: смартов {ns.get('smart_wallets', 'N/A')}, netflow ${ns.get('netflow', 0):,.0f}"
                    tw = twitter_enrichment.get(f.symbol, {})
                    tw_info = ""
                    if tw and "error" not in tw:
                        followers = tw.get('followers')
                        posts = tw.get('recent_posts', 0)
                        if followers:
                            tw_info = f" | Twitter: {followers:,} followers, {posts} posts"
                        else:
                            tw_info = " | Twitter: данные недоступны"
                    elif tw and "error" in tw:
                        tw_info = f" | Twitter: {tw['error']}"
                    else:
                        tw_info = " | Twitter: не найден"
                    lines.append(
                        f"  {idx}) {f.symbol} — 🟠 near-miss ({reason}), score {f.score:.1f}, mcap ${f.mcap:,.0f}, liq ${f.liquidity_usd:,.0f}, vol24h ${f.volume_24h:,.0f}{ns_info}{tw_info}"
                    )
                    idx += 1

                while idx <= args.top_per_chain:
                    lines.append(f"  {idx}) Нет данных (рынок узкий под фильтры)")
                    idx += 1
    else:
        lines.append("1) Нет данных")

    # Add Nansen Smart Money detailed section
    if nansen_enrichment and "_error" not in nansen_enrichment:
        lines.append("")
        lines.append("Nansen Smart Money (24h):")
        for symbol, data in nansen_enrichment.items():
            if data and isinstance(data, dict):
                lines.append(f"- {symbol}: смарт-кошельков: {data.get('smart_wallets', 'N/A')}, "
                           f"netflow: ${data.get('netflow', 0):,.0f}")

    # Add Twitter Analysis detailed section
    if twitter_enrichment and "_error" not in twitter_enrichment:
        lines.append("")
        lines.append("Twitter Анализ:")
        for symbol, data in twitter_enrichment.items():
            if data and isinstance(data, dict):
                if "error" in data:
                    lines.append(f"- {symbol}: ошибка — {data['error']}")
                elif data.get('followers'):
                    lines.append(f"- {symbol}: {data.get('followers', 0):,} подписчиков, "
                               f"{data.get('recent_posts', 0)} постов, "
                               f"engagement: {data.get('engagement', 0)}")
                else:
                    lines.append(f"- {symbol}: данные недоступны (возможно требуется логин)")
    elif "_error" in twitter_enrichment:
        lines.append("")
        lines.append(f"Twitter Анализ: ошибка подключения ({twitter_enrichment['_error']})")

    lines.append("")
    lines.append("Риски:")
    lines.append("- Легенда: 🟢 норм / 🟠 повышенный / 🟠 near-miss (рядом с фильтром, не вход).")
    lines.append("- Ранняя стадия (200k–500k mcap) = высокий риск манипуляций/illiquidity.")
    lines.append("- Twitter данные могут быть недоступны если X требует повторного логина.")

    lines.append("")
    lines.append("Следующий шаг:")
    if actions:
        lines.append("- Действия paper-mode: " + ", ".join([f"{a['action']} {a['symbol']}" for a in actions[:6]]))
    else:
        lines.append("- Новых paper-сделок нет; продолжаю мониторинг каждые 3 часа.")

    # Influencer tracking section
    if args.enable_influencer_tracking:
        lines.append("")
        lines.append("Инфлюенсер Трекинг:")
        try:
            inf_results = manage_influencers_interactive(args.max_follow_per_run)
            if inf_results.get("status") == "no_influencers_configured":
                lines.append(f"- {inf_results['message']}")
            else:
                if inf_results.get("followed"):
                    lines.append(f"- Подписались: {', '.join(inf_results['followed'])}")
                if inf_results.get("new_posts"):
                    lines.append(f"- Новых постов: {len(inf_results['new_posts'])}")
                    for post in inf_results["new_posts"][:3]:  # Show first 3
                        lines.append(f"  • @{post['handle']}: {post['text'][:60]}...")
                if inf_results.get("mentions"):
                    lines.append(f"- Упоминания токенов: {len(inf_results['mentions'])}")
                    for m in inf_results["mentions"][:3]:
                        lines.append(f"  • @{m['handle']} → ${m['symbol']}")
                if inf_results.get("errors"):
                    lines.append(f"- Ошибки: {len(inf_results['errors'])}")
        except Exception as e:
            lines.append(f"- Ошибка трекинга: {str(e)[:50]}")

    # Nansen Holders → Twitter Following section
    if args.follow_token_holders:
        lines.append("")
        lines.append("Nansen Холдеры → Подписки:")
        try:
            holder_results = process_top_tokens_holders(cands, args.max_holders_per_token)
            for result in holder_results:
                token = result.get("token", "Unknown")
                lines.append(f"- {token}: найдено {result.get('holders_found', 0)} холдеров с Twitter")
                if result.get("followed"):
                    lines.append(f"  Подписались: {', '.join(result['followed'])}")
                if result.get("saved_to_obsidian"):
                    lines.append(f"  Сохранено в Obsidian: {', '.join(result['saved_to_obsidian'])}")
                if result.get("errors"):
                    lines.append(f"  Ошибки: {len(result['errors'])}")
        except Exception as e:
            lines.append(f"- Ошибка: {str(e)[:50]}")

    print("\n".join(lines))
    return 0


# =============================================================================
# INFLUENCER TRACKING FUNCTIONS
# =============================================================================

INFLUENCER_STATE_PATH = WORKSPACE / "memory" / "influencer-state.json"

# Default influencer watchlist - can be customized
DEFAULT_INFLUENCERS = [
    # Tier 1: Major crypto influencers
    # "elonmusk",           # Elon Musk
    # "VitalikButerin",     # Ethereum founder
    # "saylor",             # Michael Saylor
    # "cz_binance",         # CZ Binance
    # Tier 2: Crypto analysts and traders
    # "SmartContracter",    # Smart Contracter
    # "CryptoCobain",       # Crypto Cobain
    # "Route2FI",           # Route 2 FI
    # Tier 3: Base ecosystem influencers
    # "jessepollak",        # Base lead
    # "jmtreg",             # JMTreg
]


def load_influencer_state() -> dict:
    """Load influencer tracking state."""
    if INFLUENCER_STATE_PATH.exists():
        try:
            return json.loads(INFLUENCER_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "following": [],  # List of handles we're following
        "monitored_posts": {},  # handle -> list of post IDs we've seen
        "last_follow_check": 0,
        "influencer_scores": {},  # handle -> engagement/follower metrics
    }


def save_influencer_state(st: dict) -> None:
    """Save influencer tracking state."""
    INFLUENCER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = int(time.time())
    INFLUENCER_STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> None:
    """Random delay to avoid detection."""
    import random
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)


def follow_influencer(page, handle: str) -> bool:
    """Follow a single influencer with randomization."""
    try:
        # Navigate to profile with random delay
        random_delay(2, 5)
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=30_000)
        random_delay(1, 3)
        
        # Look for follow button
        follow_button = page.locator("button[data-testid='FollowButton']")
        if follow_button.count() == 0:
            # Already following or button not found
            return False
        
        # Random mouse movement before click
        random_delay(0.5, 2)
        follow_button.first.click()
        random_delay(1, 2)
        
        return True
    except Exception as e:
        return False


def follow_influencers_batch(page, handles: list[str], max_follow: int = 5) -> dict:
    """Follow up to max influencers with randomization."""
    results = {"followed": [], "failed": [], "skipped": []}
    
    import random
    # Shuffle order for randomization
    shuffled = handles.copy()
    random.shuffle(shuffled)
    
    for handle in shuffled[:max_follow]:
        if follow_influencer(page, handle):
            results["followed"].append(handle)
            # Random delay between follows (30-90 seconds to avoid rate limits)
            random_delay(30, 90)
        else:
            results["failed"].append(handle)
    
    return results


def check_influencer_posts(page, handle: str, state: dict) -> list[dict]:
    """Check for new posts from an influencer."""
    try:
        random_delay(2, 4)
        page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=30_000)
        random_delay(2, 4)
        
        # Collect posts (articles/tweets)
        posts = page.locator("article[data-testid='tweet']")
        count = posts.count()
        
        new_posts = []
        seen_posts = state.get("monitored_posts", {}).get(handle, [])
        
        for i in range(min(count, 10)):  # Check up to 10 recent posts
            try:
                post = posts.nth(i)
                # Get post ID/link
                links = post.locator("a[href*='/status/']")
                if links.count() > 0:
                    href = links.first.get_attribute("href")
                    if href:
                        post_id = href.split("/status/")[-1].split("?")[0]
                        if post_id not in seen_posts:
                            # Extract post text
                            text_elem = post.locator("div[data-testid='tweetText']")
                            text = text_elem.inner_text() if text_elem.count() > 0 else ""
                            
                            new_posts.append({
                                "id": post_id,
                                "handle": handle,
                                "text": text[:200],  # First 200 chars
                                "url": f"https://x.com{href}",
                            })
            except:
                continue
        
        return new_posts
    except Exception as e:
        return []


def scan_influencers_for_tokens(page, handles: list[str], token_symbols: list[str]) -> list[dict]:
    """Scan influencer posts for mentions of specific tokens."""
    mentions = []
    
    for handle in handles:
        try:
            random_delay(3, 6)
            posts = check_influencer_posts(page, handle, {"monitored_posts": {}})
            
            for post in posts:
                text_lower = post["text"].lower()
                for symbol in token_symbols:
                    if f"${symbol.lower()}" in text_lower or f"#{symbol.lower()}" in text_lower:
                        mentions.append({
                            "handle": handle,
                            "symbol": symbol,
                            "post_text": post["text"],
                            "post_url": post["url"],
                        })
            
            # Delay between influencers
            random_delay(5, 10)
        except:
            continue
    
    return mentions


def manage_influencers_interactive(max_follow_per_run: int = 5) -> dict:
    """Interactive influencer management - follow and monitor."""
    import random
    from playwright.sync_api import sync_playwright
    
    state = load_influencer_state()
    results = {
        "followed": [],
        "new_posts": [],
        "mentions": [],
        "errors": [],
    }
    
    # Get list of influencers not yet followed
    following = set(state.get("following", []))
    to_follow = [h for h in DEFAULT_INFLUENCERS if h not in following]
    
    if not to_follow and not following:
        return {"status": "no_influencers_configured", "message": "Add influencers to DEFAULT_INFLUENCERS list"}
    
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        
        try:
            # Follow new influencers (up to max)
            if to_follow:
                random.shuffle(to_follow)
                follow_results = follow_influencers_batch(page, to_follow, max_follow_per_run)
                results["followed"] = follow_results["followed"]
                state["following"].extend(follow_results["followed"])
            
            # Check posts from existing follows
            for handle in state["following"][:20]:  # Check up to 20 influencers
                try:
                    posts = check_influencer_posts(page, handle, state)
                    if posts:
                        results["new_posts"].extend(posts)
                        # Update state with seen posts
                        if handle not in state["monitored_posts"]:
                            state["monitored_posts"][handle] = []
                        state["monitored_posts"][handle].extend([p["id"] for p in posts])
                        # Keep only last 100 post IDs
                        state["monitored_posts"][handle] = state["monitored_posts"][handle][-100:]
                    
                    random_delay(3, 7)
                except Exception as e:
                    results["errors"].append(f"{handle}: {str(e)[:50]}")
            
            save_influencer_state(state)
            
        finally:
            try:
                page.close()
                ctx.close()
            except:
                pass
    
    return results


# =============================================================================
# NANsen HOLDERS → TWITTER FOLLOWING
# =============================================================================

def get_nansen_holders_with_twitter(chain: str, token_address: str, limit: int = 5) -> list[dict]:
    """Query Nansen for top holders who have Twitter accounts."""
    key = os.getenv("NANSEN_API_KEY")
    if not key:
        env = dotenv_values(str(WORKSPACE / ".env"))
        key = env.get("NANSEN_API_KEY")
    if not key:
        return []
    
    proxy = os.getenv("NANSEN_PROXY") or dotenv_values(str(WORKSPACE / ".env")).get("NANSEN_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    headers = {"Content-Type": "application/json", "apikey": key}
    chain_id = "bnb" if chain == "bsc" else chain
    
    # Try to get holders from Nansen profiler endpoint
    holders = []
    
    # Endpoint for token holders with social data
    url = f"{NANSEN_PROFILER}/token/{chain_id}/{token_address}/holders"
    
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=30)
        if r.status_code == 200:
            data = r.json()
            for holder in data.get("holders", [])[:limit]:
                twitter = holder.get("twitter") or holder.get("social", {}).get("twitter")
                if twitter:
                    holders.append({
                        "address": holder.get("address"),
                        "twitter": twitter,
                        "balance": holder.get("balance", 0),
                        "balance_usd": holder.get("balanceUsd", 0),
                        "rank": holder.get("rank"),
                    })
    except Exception as e:
        print(f"Nansen holders API error: {e}")
    
    # Alternative: try smart-money-flow endpoint which might have holder info
    if not holders:
        try:
            url = NANSEN_SMART_MONEY_FLOW
            payload = {
                "chain": chain_id,
                "tokenAddress": token_address,
                "timeframe": "24h"
            }
            r = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=30)
            if r.status_code == 200:
                data = r.json()
                # Extract unique wallets with activity
                wallets = set()
                for activity in data.get("activities", []):
                    wallet = activity.get("wallet")
                    if wallet and wallet not in wallets:
                        wallets.add(wallet)
                        # Try to get profile for this wallet
                        profile_url = f"{NANSEN_PROFILER}/wallet/{chain_id}/{wallet}"
                        try:
                            profile_r = requests.get(profile_url, headers=headers, proxies=proxies, timeout=10)
                            if profile_r.status_code == 200:
                                profile = profile_r.json()
                                twitter = profile.get("twitter") or profile.get("social", {}).get("twitter")
                                if twitter:
                                    holders.append({
                                        "address": wallet,
                                        "twitter": twitter,
                                        "balance": profile.get("currentBalance", 0),
                                        "balance_usd": profile.get("currentBalanceUsd", 0),
                                        "rank": len(holders) + 1,
                                    })
                                    if len(holders) >= limit:
                                        break
                        except:
                            continue
        except Exception as e:
            print(f"Nansen smart money holders error: {e}")
    
    return holders


def save_holder_to_obsidian(token_symbol: str, holder: dict) -> None:
    """Save holder profile to Obsidian vault."""
    obsidian_path = WORKSPACE / "memory" / "obsidian" / "crypto_holders"
    obsidian_path.mkdir(parents=True, exist_ok=True)
    
    filename = f"{token_symbol}_{holder['twitter']}.md"
    filepath = obsidian_path / filename
    
    content = f"""# {holder['twitter']}

## Token: ${token_symbol}

- **Address**: `{holder['address']}`
- **Twitter**: [@{holder['twitter']}](https://x.com/{holder['twitter']})
- **Balance**: {holder.get('balance', 'N/A')} tokens
- **Balance USD**: ${holder.get('balance_usd', 0):,.2f}
- **Rank**: #{holder.get('rank', 'N/A')}

## Notes

- Followed on: {time.strftime('%Y-%m-%d %H:%M')}
- Source: Nansen API

## Tags

#holder #{token_symbol.lower()} #crypto
"""
    
    filepath.write_text(content, encoding="utf-8")


def follow_token_holders(ctx, token_symbol: str, token_address: str, chain: str, max_follow: int = 5) -> dict:
    """Find and follow top holders of a token who have Twitter."""
    results = {
        "token": token_symbol,
        "holders_found": 0,
        "followed": [],
        "skipped": [],
        "saved_to_obsidian": [],
        "errors": [],
    }
    
    # Get holders with Twitter from Nansen
    holders = get_nansen_holders_with_twitter(chain, token_address, limit=max_follow + 2)
    results["holders_found"] = len(holders)
    
    if not holders:
        return results
    
    page = None
    try:
        page = ctx.new_page()
        
        for holder in holders[:max_follow]:
            twitter_handle = holder.get("twitter", "").strip()
            if not twitter_handle:
                continue
            
            # Remove @ if present
            if twitter_handle.startswith("@"):
                twitter_handle = twitter_handle[1:]
            
            # Try to follow
            try:
                if follow_influencer(page, twitter_handle):
                    results["followed"].append(twitter_handle)
                    # Save to Obsidian
                    save_holder_to_obsidian(token_symbol, holder)
                    results["saved_to_obsidian"].append(twitter_handle)
                    # Random delay between follows
                    random_delay(30, 90)
                else:
                    results["skipped"].append(twitter_handle)
            except Exception as e:
                results["errors"].append(f"{twitter_handle}: {str(e)[:50]}")
                
    finally:
        if page:
            try:
                page.close()
            except:
                pass
    
    return results


def process_top_tokens_holders(candidates: list[Candidate], max_per_token: int = 5) -> list[dict]:
    """Process top tokens and follow their holders."""
    from playwright.sync_api import sync_playwright
    
    all_results = []
    
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        
        try:
            for c in candidates[:3]:  # Top 3 tokens
                if c.token_address and c.twitter:  # Only if token has Twitter
                    print(f"Processing holders for {c.symbol}...")
                    result = follow_token_holders(ctx, c.symbol, c.token_address, c.chain, max_per_token)
                    all_results.append(result)
                    # Delay between tokens
                    random_delay(10, 20)
        finally:
            try:
                ctx.close()
            except:
                pass
    
    return all_results


if __name__ == "__main__":
    raise SystemExit(main())
