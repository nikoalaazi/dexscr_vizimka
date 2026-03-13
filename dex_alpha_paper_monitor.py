#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


WORKSPACE = Path(__file__).resolve().parents[1]
STATE_PATH = WORKSPACE / "memory" / "dex-alpha-paper-state.json"

DEX_TOKEN_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKEN_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_TOKEN_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_TOKEN_PAIRS = "https://api.dexscreener.com/latest/dex/tokens/{token}"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"

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


def _num(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="DexScreener alpha scout (Base+ETH) with paper trades")
    ap.add_argument("--capital-usd", type=float, default=1000.0)
    ap.add_argument("--ticket-share", type=float, default=0.1, help="paper position size = capital * share")
    ap.add_argument("--min-liquidity", type=float, default=50_000)
    ap.add_argument("--min-volume-24h", type=float, default=50_000)
    ap.add_argument("--min-mcap", type=float, default=200_000)
    ap.add_argument("--max-mcap", type=float, default=10_000_000)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--entry-score", type=float, default=7.0)
    ap.add_argument("--tp", type=float, default=0.4, help="take profit in fraction")
    ap.add_argument("--sl", type=float, default=0.2, help="stop loss in fraction")
    ap.add_argument("--max-hold-hours", type=int, default=48)
    ap.add_argument("--dry-run", action="store_true")
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


def build_candidates(args: argparse.Namespace) -> list[Candidate]:
    profiles = fetch_profiles()
    boosts = fetch_boosts()
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
    for q in ("base", "ethereum"):
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
        if not (args.min_mcap <= mcap <= args.max_mcap):
            continue
        if liq < args.min_liquidity or vol24 < args.min_volume_24h:
            continue

        tx24 = (p.get("txns") or {}).get("h24") or {}
        sc = score_pair(p)
        tw = twitter_by_token.get(token_addr.lower())
        if tw:
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
        )

        old = best_by_symbol.get(symbol)
        if old is None or (c.score, c.volume_24h, c.liquidity_usd) > (old.score, old.volume_24h, old.liquidity_usd):
            best_by_symbol[symbol] = c

    out = list(best_by_symbol.values())
    out.sort(key=lambda c: (c.score, c.volume_24h, c.liquidity_usd), reverse=True)
    return out[: args.top]


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

    try:
        cands = build_candidates(args)
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
    lines.append("Топ-кандидаты:")
    if cands:
        for i, c in enumerate(cands, 1):
            lines.append(
                f"{i}) {c.symbol} ({c.chain}) — score {c.score:.1f}, mcap ${c.mcap:,.0f}, liq ${c.liquidity_usd:,.0f}, vol24h ${c.volume_24h:,.0f}, smart proxy tx {c.txns_24h}, twitter {'yes' if c.twitter else 'no'}"
            )
    else:
        lines.append("1) Нет данных")

    lines.append("")
    lines.append("Риски:")
    lines.append("- Ранняя стадия (200k–500k mcap) = высокий риск манипуляций/illiquidity.")
    lines.append("- Social score здесь базовый (наличие Twitter), не quality-influencer граф.")

    lines.append("")
    lines.append("Следующий шаг:")
    if actions:
        lines.append("- Действия paper-mode: " + ", ".join([f"{a['action']} {a['symbol']}" for a in actions[:6]]))
    else:
        lines.append("- Новых paper-сделок нет; продолжаю мониторинг каждые 3 часа.")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
