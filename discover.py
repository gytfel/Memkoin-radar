"""
discover.py — откуда вообще брать кошельки.

Берём токены, которые уже сделали x10+, находим тех, кто покупал их
в первые минуты, и получаем список кандидатов. Дальше кандидатов
обязательно прогнать через wallet_analyzer.py — большинство из них
окажутся снайпер-ботами или разовыми везунчиками.

    python discover.py --mints mints.txt --out candidates.txt
    python discover.py --mint <MINT1> --mint <MINT2> --out candidates.txt
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter

from wallet_analyzer import (FetchError, is_solana_address, load_config,
                             make_session, parse_swap, post_json,
                             require_config, setup_logging)

HELIUS_PARSE = "https://api.helius.xyz/v0/transactions"

log = logging.getLogger("radar")


async def oldest_signatures(session, rpc: str, mint: str, max_pages: int = 25,
                            page: int = 1000) -> tuple[list[str], bool]:
    """Листаем историю минта до начала. Возвращаем (подписи, дошли ли до конца).

    Флаг важнее самих подписей: RPC умеет листать только назад от свежих,
    поэтому если страниц не хватило, «самые старые» из полученных — это
    просто граница, до которой успели дойти. Ранними покупателями такие
    адреса не являются, и молча считать их за таковых нельзя.
    """
    before, sigs = None, []
    exhausted = False
    for attempt in range(max_pages):
        params: dict = {"limit": page}
        if before:
            params["before"] = before
        body = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [mint, params]}
        res = await post_json(session, rpc, body)

        # Первая страница особая: без неё про минт не известно ничего, и
        # «ранних покупателей не нашлось» было бы выводом из пустоты.
        if attempt == 0 and res is None:
            raise FetchError(f"RPC не ответил по минту {mint[:8]}")
        if attempt == 0 and isinstance(res, dict) and res.get("error"):
            raise FetchError(f"RPC отклонил запрос по {mint[:8]}: {res['error']}")

        if isinstance(res, dict) and res.get("error"):
            log.warning("RPC вернул ошибку для %s: %s", mint[:8], res["error"])
            break
        batch = (res or {}).get("result") if isinstance(res, dict) else None
        batch = [s for s in (batch or []) if isinstance(s, dict) and s.get("signature")]
        if not batch:
            exhausted = True
            break
        sigs.extend(s["signature"] for s in batch)
        before = batch[-1]["signature"]
        if len(batch) < page:
            exhausted = True
            break
        await asyncio.sleep(0.2)          # RPC-лимиты: 25 страниц подряд ловят 429
    if not exhausted:
        log.warning("%s: история длиннее %d стр. — до первых покупок не долистали. "
                    "Нужен --max-pages больше %d либо токен посвежее",
                    mint[:8], max_pages, max_pages)
    return sigs[::-1], exhausted  # от старых к новым


async def parse_batch(session, key: str, sigs: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(sigs), 100):     # Helius принимает не больше 100 за раз
        res = await post_json(session, f"{HELIUS_PARSE}?api-key={key}",
                              {"transactions": sigs[i:i + 100]})
        if isinstance(res, list):
            out.extend(tx for tx in res if isinstance(tx, dict))
        elif isinstance(res, dict) and res.get("error"):
            log.warning("Helius не разобрал пачку транзакций: %s", res["error"])
        await asyncio.sleep(0.2)
    return out


async def early_buyers(session, cfg: dict, mint: str, first_n: int = 400,
                       max_pages: int = 25) -> tuple[list[str], bool]:
    rpc = cfg["rpc"]["rpc_url"]
    key = cfg["rpc"]["helius_api_key"]

    sigs, complete = await oldest_signatures(session, rpc, mint, max_pages)
    if not sigs:
        print(f"  {mint[:8]}: история не получена")
        return [], complete

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
    mark = "" if complete else "  (НЕ РАННИЕ: до начала истории не дошли)"
    print(f"  {mint[:8]}: {len(buyers)} покупателей из {len(txs)} tx{mark}")
    return buyers, complete


async def run(mints: list[str], cfg: dict, out: str, min_hits: int,
              max_pages: int = 25, allow_incomplete: bool = False):
    counter: Counter[str] = Counter()
    done, failed, partial = 0, [], []
    async with make_session() as session:
        for mint in mints:
            try:
                buyers, complete = await early_buyers(session, cfg, mint,
                                                      max_pages=max_pages)
            except FetchError as e:
                failed.append(mint)
                print(f"  ⚠️  {mint[:8]}: НЕ ОБРАБОТАН — {e}")
                continue

            if not complete:
                partial.append(mint)
                if not allow_incomplete:
                    # Считать их за ранних — значит наполнить список кандидатов
                    # случайными трейдерами и потом гонять их через анализатор
                    # как будто это находка. --allow-incomplete снимает запрет.
                    continue
            for w in buyers:
                counter[w] += 1
            done += 1

    # Пустой файл поверх готового списка кандидатов — то же самое, что было
    # в анализаторе: обрыв связи выглядит как «никого не нашлось».
    if not done:
        if partial and not failed:
            raise SystemExit(
                f"\nНи по одному минту не дошли до начала истории "
                f"({len(partial)} из {len(mints)}).\n"
                f"Найденные адреса ранними покупателями не являются, поэтому "
                f"в зачёт не пошли и {out} не тронут.\n\n"
                f"Что делать:\n"
                f"  1) взять токены посвежее — у них история короче;\n"
                f"  2) листать глубже: --max-pages {max_pages * 8} "
                f"(дольше в {max_pages * 8 // max_pages} раз);\n"
                f"  3) --allow-incomplete — считать что есть, понимая, что это "
                f"не ранние покупатели.")
        raise SystemExit(
            f"\nНи один минт не обработан — данные не получены "
            f"({len(failed)} из {len(mints)}).\n"
            f"{out} не тронут. Причину покажет: python doctor.py")

    # кошельки, попавшие рано сразу в НЕСКОЛЬКО удачных токенов, ценнее всего:
    # один хит — это лотерея, три хита — уже похоже на систему
    picked = [w for w, c in counter.most_common() if c >= min_hits]
    with open(out, "w", encoding="utf-8") as f:
        f.write("# кандидаты. Обязательно прогнать через wallet_analyzer.py\n")
        for w in picked:
            f.write(f"{w}  # ранних входов: {counter[w]}\n")
    print(f"\n{len(picked)} кандидатов (>= {min_hits} попаданий) из {done} минтов → {out}")
    if failed:
        print(f"⚠️  Не обработано минтов: {len(failed)} — данные не получены. "
              f"Это не значит, что там нет ранних покупателей.")
    if partial:
        state = "учтены как есть" if allow_incomplete else "в зачёт не пошли"
        print(f"⚠️  Не долистали до начала истории: {len(partial)} из {len(mints)} "
              f"({state}). Помогут --max-pages больше {max_pages} или токены "
              f"посвежее.")
    if min_hits > done:
        # порог выше числа успешно обработанных минтов физически недостижим
        print(f"⚠️  --min-hits {min_hits} больше, чем обработано минтов ({done}): "
              f"пройти этот порог не может никто. Добавь минтов или снизь порог.")


def load_mints(path: str) -> list[str]:
    """Список минтов из файла: по одному в строке, # — комментарий."""
    with open(path, encoding="utf-8") as f:
        return [m for m in (ln.split("#", 1)[0].strip() for ln in f) if m]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mint", action="append", default=[],
                   help="минт токена, который уже отработал (можно несколько раз)")
    p.add_argument("--mints", help="файл со списком минтов, по одному в строке")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--out", default="candidates.txt")
    p.add_argument("--min-hits", type=int, default=2)
    p.add_argument("--max-pages", type=int, default=25,
                   help="сколько страниц истории листать на минт (1000 tx на "
                        "страницу). Больше — дольше, но добирается до начала")
    p.add_argument("--allow-incomplete", action="store_true",
                   help="считать покупателей и там, где до начала истории не "
                        "дошли (это НЕ ранние покупатели)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    setup_logging(a.verbose)

    mints = list(a.mint)
    if a.mints:
        try:
            mints += load_mints(a.mints)
        except OSError as e:
            raise SystemExit(f"Не прочитать {a.mints}: {e}") from e
    if not mints:
        raise SystemExit("Нужны минты: --mints mints.txt или --mint <адрес>")

    # Опечатка в минте не ошибка для RPC — он просто вернёт пустую историю,
    # и токен молча выпадет из подсчёта попаданий.
    bad = [m for m in mints if not is_solana_address(m)]
    if bad:
        raise SystemExit("Это не адреса Solana:\n  " + "\n  ".join(bad))

    seen: dict[str, None] = dict.fromkeys(mints)   # дубли ломают счёт попаданий
    if len(seen) < len(mints):
        log.warning("Повторов в списке минтов: %d — считаю по уникальным",
                    len(mints) - len(seen))
    mints = list(seen)

    cfg = load_config(a.config)
    require_config(cfg, "rpc.helius_api_key", "rpc.rpc_url")
    print(f"Минтов на разбор: {len(mints)}, порог попаданий: {a.min_hits}, "
          f"глубина: {a.max_pages} стр.")
    asyncio.run(run(mints, cfg, a.out, a.min_hits, a.max_pages, a.allow_incomplete))


if __name__ == "__main__":
    main()
