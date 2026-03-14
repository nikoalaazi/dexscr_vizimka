#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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

    try:
        cands, near_by_chain = build_candidates(args)
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
                lines.append(
                    f"  {i}) {c.symbol} — {risk_label(c, strict=True)}, score {c.score:.1f}, mcap ${c.mcap:,.0f}, liq ${c.liquidity_usd:,.0f}, vol24h ${c.volume_24h:,.0f}, smart proxy tx {c.txns_24h}, twitter: {'найден' if c.twitter else 'не найден'}"
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
                    lines.append(
                        f"  {idx}) {f.symbol} — 🟠 near-miss ({reason}), score {f.score:.1f}, mcap ${f.mcap:,.0f}, liq ${f.liquidity_usd:,.0f}, vol24h ${f.volume_24h:,.0f}, twitter: {'найден' if f.twitter else 'не найден'}"
                    )
                    idx += 1

                while idx <= args.top_per_chain:
                    lines.append(f"  {idx}) Нет данных (рынок узкий под фильтры)")
                    idx += 1
    else:
        lines.append("1) Нет данных")

    lines.append("")
    lines.append("Риски:")
    lines.append("- Легенда: 🟢 норм / 🟠 повышенный / 🟠 near-miss (рядом с фильтром, не вход).")
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
