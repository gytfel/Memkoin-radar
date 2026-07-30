"""
discover.py — откуда вообще брать кошельки.

Берём токены, которые уже сделали x10+, находим тех, кто покупал их
в первые минуты, и получаем список кандидатов. Дальше кандидатов
обязательно прогнать через wallet_analyzer.py — большинство из них
окажутся снайпер-ботами или разовыми везунчиками.

    python discover.py --mint <MINT1> --mint <MINT2> --out candidates.txt
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

import aiohttp

from wallet_analyzer import load_config, parse_swap, post_json

HELIUS_PARSE = "https://api.helius.xyz/v0/transactions"


async def oldest_signatures(session, rpc: str, mint: str,
                            max_pages: int = 25, page: int = 1000) -> list[str]:
    """Листаем историю адреса минта до самого начала, возвращаем самые старые подписи."""
    before, sigs = None, []
    for _ in range(max_pages):
        params: dict = {"limit": page}
        if before:
            params["before"] = before
        body = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [mint, params]}
        res = await post_json(session, rpc, body)
        batch = (res or {}).get("result") or []
        if not batch:
            break
        sigs.extend(s["signature"] for s in batch)
        before = batch[-1]["signature"]
        if len(batch) < page:
            break
    return sigs[::-1]  # от старых к новым


async def parse_batch(session, key: str, sigs: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(sigs), 100):
        res = await post_json(session, f"{HELIUS_PARSE}?api-key={key}",
                              {"transactions": sigs[i:i + 100]})
        if res:
            out.extend(res)
        await asyncio.sleep(0.2)
    return out


async def early_buyers(session, cfg: dict, mint: str, first_n: int = 400) -> list[str]:
    rpc = cfg["rpc"]["rpc_url"]
    key = cfg["rpc"]["helius_api_key"]

    sigs = await oldest_signatures(session, rpc, mint)
    if not sigs:
        print(f"  {mint[:8]}: история не получена")
        return []

    txs = await parse_batch(session, key, sigs[:first_n])
    buyers, seen = [], set()
    for tx in txs:
        payer = tx.get("feePayer")
        if not payer or payer in seen:
            continue
        swap = parse_swap(tx, payer, cfg["analyzer"]["sol_usd_fallback"])
        if swap and swap.mint == mint and swap.side == "buy":
            seen.add(payer)
            buyers.append(payer)
    print(f"  {mint[:8]}: {len(buyers)} ранних покупателей из {len(txs)} tx")
    return buyers


async def run(mints: list[str], cfg: dict, out: str, min_hits: int):
    counter: Counter[str] = Counter()
    async with aiohttp.ClientSession() as session:
        for mint in mints:
            for w in await early_buyers(session, cfg, mint):
                counter[w] += 1

    # кошельки, попавшие рано сразу в НЕСКОЛЬКО удачных токенов, ценнее всего:
    # один хит — это лотерея, три хита — уже похоже на систему
    picked = [w for w, c in counter.most_common() if c >= min_hits]
    with open(out, "w", encoding="utf-8") as f:
        f.write("# кандидаты. Обязательно прогнать через wallet_analyzer.py\n")
        for w in picked:
            f.write(f"{w}  # ранних входов: {counter[w]}\n")
    print(f"\n{len(picked)} кандидатов (>= {min_hits} попаданий) → {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mint", action="append", required=True,
                   help="минт токена, который уже отработал (можно несколько раз)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--out", default="candidates.txt")
    p.add_argument("--min-hits", type=int, default=2)
    a = p.parse_args()
    asyncio.run(run(a.mint, load_config(a.config), a.out, a.min_hits))
