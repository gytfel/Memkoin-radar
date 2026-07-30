"""
token_safety.py — фильтр «это не откровенный скам».

Проверяет: ликвидность, возраст пары, FDV, mint/freeze authority,
концентрацию топ-10 холдеров, оценку rugcheck.xyz, соотношение buy/sell.

Ни один фильтр не даёт гарантии. Он лишь отсекает самые дешёвые схемы:
токены с несожжённой LP, живой mint authority и одним кошельком на 60% сапплая.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp

from wallet_analyzer import get_json, post_json

DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"


@dataclass
class TokenSafety:
    mint: str
    ok: bool = False
    reasons: list[str] = field(default_factory=list)
    symbol: str = "?"
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    fdv_usd: float = 0.0
    age_min: float = 0.0
    vol_h1: float = 0.0
    buys_m5: int = 0
    sells_m5: int = 0
    top10_pct: float = 0.0
    mint_authority: str | None = None
    freeze_authority: str | None = None
    rugcheck_score: int | None = None
    pair_url: str = ""


async def dexscreener(session, mint: str) -> dict | None:
    data = await get_json(session, DEXSCREENER.format(mint=mint))
    pairs = (data or {}).get("pairs") or []
    if not pairs:
        return None
    # берём пару с максимальной ликвидностью
    return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


async def mint_authorities(session, rpc: str, mint: str) -> tuple[str | None, str | None, float]:
    """Возвращает (mintAuthority, freezeAuthority, supply)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}]}
    res = await post_json(session, rpc, body)
    try:
        info = res["result"]["value"]["data"]["parsed"]["info"]
    except (TypeError, KeyError):
        return None, None, 0.0
    dec = int(info.get("decimals") or 0)
    supply = float(info.get("supply") or 0) / (10 ** dec) if dec else float(info.get("supply") or 0)
    return info.get("mintAuthority"), info.get("freezeAuthority"), supply


async def top10_share(session, rpc: str, mint: str, supply: float) -> float:
    if supply <= 0:
        return 0.0
    body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
            "params": [mint]}
    res = await post_json(session, rpc, body)
    try:
        accounts = res["result"]["value"]
    except (TypeError, KeyError):
        return 0.0
    total = 0.0
    for acc in accounts[:10]:
        try:
            total += float(acc.get("uiAmount") or 0)
        except (TypeError, ValueError):
            continue
    return round(total / supply * 100, 2)


async def rugcheck(session, mint: str) -> int | None:
    data = await get_json(session, RUGCHECK.format(mint=mint), timeout=15)
    if not isinstance(data, dict):
        return None
    score = data.get("score_normalised", data.get("score"))
    try:
        return int(score)
    except (TypeError, ValueError):
        return None


async def check_token(session, mint: str, cfg: dict) -> TokenSafety:
    s = TokenSafety(mint=mint)
    rpc = cfg["rpc"]["rpc_url"]
    sf = cfg["safety"]

    pair = await dexscreener(session, mint)
    if not pair:
        s.reasons.append("нет пары на dexscreener (нет ликвидности)")
        return s

    s.symbol = (pair.get("baseToken") or {}).get("symbol") or "?"
    s.price_usd = float(pair.get("priceUsd") or 0)
    s.liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0)
    s.fdv_usd = float(pair.get("fdv") or pair.get("marketCap") or 0)
    s.vol_h1 = float((pair.get("volume") or {}).get("h1") or 0)
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    s.buys_m5 = int(txns_m5.get("buys") or 0)
    s.sells_m5 = int(txns_m5.get("sells") or 0)
    s.pair_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"
    created_ms = pair.get("pairCreatedAt")
    if created_ms:
        import time as _t
        s.age_min = round((_t.time() - created_ms / 1000) / 60, 1)

    s.mint_authority, s.freeze_authority, supply = await mint_authorities(session, rpc, mint)
    s.top10_pct = await top10_share(session, rpc, mint, supply)
    s.rugcheck_score = await rugcheck(session, mint)

    # ---------------- правила ----------------
    if s.liquidity_usd < sf["min_liquidity_usd"]:
        s.reasons.append(f"ликвидность ${s.liquidity_usd:,.0f} < ${sf['min_liquidity_usd']:,}")
    if sf["max_fdv_usd"] and s.fdv_usd > sf["max_fdv_usd"]:
        s.reasons.append(f"FDV ${s.fdv_usd:,.0f} — вход уже поздний")
    if s.top10_pct > sf["max_top10_pct"]:
        s.reasons.append(f"топ-10 держат {s.top10_pct}%")
    if sf["require_mint_authority_revoked"] and s.mint_authority:
        s.reasons.append("mint authority активен (могут допечатать)")
    if sf["require_freeze_authority_revoked"] and s.freeze_authority:
        s.reasons.append("freeze authority активен (могут заморозить продажу)")
    if s.rugcheck_score is not None and s.rugcheck_score > sf["max_rugcheck_score"]:
        s.reasons.append(f"rugcheck score {s.rugcheck_score}")
    if s.sells_m5 and s.buys_m5 / max(s.sells_m5, 1) < sf["min_buys_sells_ratio"]:
        s.reasons.append("продавцов больше, чем покупателей")
    if cfg["radar"]["max_token_age_min"] and s.age_min > cfg["radar"]["max_token_age_min"]:
        s.reasons.append(f"токену {s.age_min/60:.1f} ч — вне окна радара")

    s.ok = not s.reasons
    return s


async def price_usd(session, mint: str) -> float:
    pair = await dexscreener(session, mint)
    return float(pair.get("priceUsd") or 0) if pair else 0.0
