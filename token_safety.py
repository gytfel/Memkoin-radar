"""
token_safety.py — фильтр «это не откровенный скам».

Проверяет: ликвидность, возраст пары, FDV, mint/freeze authority,
концентрацию топ-10 холдеров, оценку rugcheck.xyz, соотношение buy/sell.

Ни один фильтр не даёт гарантии. Он лишь отсекает самые дешёвые схемы:
токены с несожжённой LP, живой mint authority и одним кошельком на 60% сапплая.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

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


def _num(value, default: float = 0.0) -> float:
    """Dexscreener отдаёт числа строками, а иногда null или мусор."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def dexscreener(session, mint: str) -> dict | None:
    data = await get_json(session, DEXSCREENER.format(mint=mint))
    pairs = (data or {}).get("pairs") if isinstance(data, dict) else None
    pairs = [p for p in (pairs or []) if isinstance(p, dict)]
    if not pairs:
        return None
    # берём пару с максимальной ликвидностью
    return max(pairs, key=lambda p: _num((p.get("liquidity") or {}).get("usd")))


async def mint_authorities(session, rpc: str, mint: str) -> tuple[str | None, str | None, float]:
    """Возвращает (mintAuthority, freezeAuthority, supply)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}]}
    res = await post_json(session, rpc, body)
    try:
        info = res["result"]["value"]["data"]["parsed"]["info"]
    except (TypeError, KeyError, IndexError):
        return None, None, 0.0
    if not isinstance(info, dict):
        return None, None, 0.0
    dec = int(_num(info.get("decimals")))
    supply = _num(info.get("supply"))
    return info.get("mintAuthority"), info.get("freezeAuthority"), supply / (10 ** dec)


async def top10_share(session, rpc: str, mint: str, supply: float) -> float:
    if supply <= 0:
        return 0.0
    body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
            "params": [mint]}
    res = await post_json(session, rpc, body)
    try:
        accounts = res["result"]["value"]
    except (TypeError, KeyError, IndexError):
        return 0.0
    if not isinstance(accounts, list):
        return 0.0
    total = sum(_num((acc or {}).get("uiAmount")) for acc in accounts[:10]
                if isinstance(acc, dict))
    # доля не может быть больше 100%: supply и балансы приходят из разных
    # запросов и на свежих токенах успевают разъехаться
    return round(min(total / supply * 100, 100.0), 2)


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
    s.price_usd = _num(pair.get("priceUsd"))
    s.liquidity_usd = _num((pair.get("liquidity") or {}).get("usd"))
    s.fdv_usd = _num(pair.get("fdv")) or _num(pair.get("marketCap"))
    s.vol_h1 = _num((pair.get("volume") or {}).get("h1"))
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    s.buys_m5 = int(_num(txns_m5.get("buys")))
    s.sells_m5 = int(_num(txns_m5.get("sells")))
    s.pair_url = pair.get("url") or f"https://dexscreener.com/solana/{mint}"
    created_ms = _num(pair.get("pairCreatedAt"))
    if created_ms > 0:
        s.age_min = round(max(0.0, time.time() - created_ms / 1000) / 60, 1)

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
    max_age = (cfg.get("radar") or {}).get("max_token_age_min")
    if max_age and s.age_min > max_age:
        s.reasons.append(f"токену {s.age_min/60:.1f} ч — вне окна радара")

    s.ok = not s.reasons
    return s


async def price_usd(session, mint: str) -> float:
    pair = await dexscreener(session, mint)
    return _num(pair.get("priceUsd")) if pair else 0.0
