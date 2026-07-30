"""
wallet_analyzer.py — считает РЕАЛЬНУЮ статистику кошелька по on-chain данным.

Что важно и чем это отличается от красивых дашбордов:
  1. Позиции восстанавливаются по FIFO, а не «сумма продаж минус сумма покупок».
  2. Незакрытые позиции (мешки) считаются отдельно и в худшем случае по нулю.
     Именно их сокрытие превращает реальные 45% winrate в рекламные 90%.
  3. Winrate сам по себе не значит ничего. Главные метрики — expectancy
     (средний профит на сделку) и profit factor.

Запуск:
    python wallet_analyzer.py --wallets wallets.txt --out qualified.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field

import aiohttp
import yaml

HELIUS_TX = "https://api.helius.xyz/v0/addresses/{addr}/transactions"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {WSOL, USDC, USDT}
STABLES = {USDC, USDT}
LAMPORTS = 1_000_000_000
DAY = 86_400


# --------------------------------------------------------------------------- #
#  утилиты
# --------------------------------------------------------------------------- #
def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = os.path.expandvars(f.read())
    return yaml.safe_load(raw)


async def get_json(session: aiohttp.ClientSession, url: str, params=None,
                   retries: int = 4, timeout: int = 30):
    """GET с бэкоффом. Возвращает None вместо исключения — радар не должен падать."""
    for attempt in range(retries):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if r.status >= 400:
                    return None
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1.5 ** attempt)
    return None


async def post_json(session: aiohttp.ClientSession, url: str, payload: dict,
                    retries: int = 4, timeout: int = 30):
    for attempt in range(retries):
        try:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if r.status >= 400:
                    return None
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1.5 ** attempt)
    return None


# --------------------------------------------------------------------------- #
#  парсинг свопов из Helius Enhanced Transactions
# --------------------------------------------------------------------------- #
@dataclass
class Swap:
    ts: int
    sig: str
    mint: str
    side: str        # "buy" | "sell"
    tokens: float    # количество мем-токена
    quote_sol: float # сколько SOL (или SOL-эквивалента) потрачено/получено


def _amount(entry: dict) -> float:
    raw = entry.get("rawTokenAmount") or {}
    try:
        amt = float(raw.get("tokenAmount") or 0)
        dec = int(raw.get("decimals") or 0)
    except (TypeError, ValueError):
        return 0.0
    return abs(amt) / (10 ** dec) if dec else abs(amt)


def _mine(entries: list, wallet: str) -> list:
    """Оставляем только то, что относится к нашему кошельку (если поле есть)."""
    if not entries:
        return []
    own = [e for e in entries if e.get("userAccount") == wallet]
    return own or entries


def parse_swap(tx: dict, wallet: str, sol_usd: float) -> Swap | None:
    ev = (tx.get("events") or {}).get("swap")
    if not ev:
        return None

    ts = int(tx.get("timestamp") or 0)
    sig = tx.get("signature") or ""

    def _native(key: str) -> float:
        node = ev.get(key) or {}
        try:
            return abs(float(node.get("amount") or 0)) / LAMPORTS
        except (TypeError, ValueError):
            return 0.0

    quote_in = _native("nativeInput")
    quote_out = _native("nativeOutput")

    tok_in = _mine(ev.get("tokenInputs") or [], wallet)
    tok_out = _mine(ev.get("tokenOutputs") or [], wallet)

    other_in, other_out = [], []

    for e in tok_in:
        mint = e.get("mint")
        amt = _amount(e)
        if mint == WSOL:
            quote_in += amt
        elif mint in STABLES:
            quote_in += amt / max(sol_usd, 1e-9)
        elif mint and amt > 0:
            other_in.append((mint, amt))

    for e in tok_out:
        mint = e.get("mint")
        amt = _amount(e)
        if mint == WSOL:
            quote_out += amt
        elif mint in STABLES:
            quote_out += amt / max(sol_usd, 1e-9)
        elif mint and amt > 0:
            other_out.append((mint, amt))

    # покупка мемкоина: отдали квоту, получили токен
    if other_out and quote_in > 0 and not other_in:
        mint, amt = max(other_out, key=lambda x: x[1])
        return Swap(ts, sig, mint, "buy", amt, quote_in)

    # продажа: отдали токен, получили квоту
    if other_in and quote_out > 0 and not other_out:
        mint, amt = max(other_in, key=lambda x: x[1])
        return Swap(ts, sig, mint, "sell", amt, quote_out)

    return None  # token->token свопы игнорируем, они ломают учёт цены входа


async def fetch_swaps(session, wallet: str, cfg: dict, pages: int | None = None,
                      until_ts: int | None = None) -> list[Swap]:
    key = cfg["rpc"]["helius_api_key"]
    limit = cfg["analyzer"]["page_limit"]
    sol_usd = cfg["analyzer"]["sol_usd_fallback"]
    pages = pages or cfg["analyzer"]["max_tx_pages"]

    url = HELIUS_TX.format(addr=wallet)
    before, out = None, []

    for _ in range(pages):
        params = {"api-key": key, "limit": limit, "type": "SWAP"}
        if before:
            params["before"] = before
        data = await get_json(session, url, params)
        if not data:
            break

        for tx in data:
            s = parse_swap(tx, wallet, sol_usd)
            if s:
                out.append(s)

        before = data[-1].get("signature")
        oldest = int(data[-1].get("timestamp") or 0)
        if len(data) < limit or (until_ts and oldest < until_ts):
            break

    out.sort(key=lambda s: s.ts)
    return out


# --------------------------------------------------------------------------- #
#  восстановление сделок (FIFO)
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    mint: str
    opened: int
    closed: int | None
    cost_sol: float          # вложено всего
    cost_realized_sol: float # вложено в проданную часть
    proceeds_sol: float
    open_tokens: float
    bought_tokens: float
    is_closed: bool

    @property
    def pnl_realized(self) -> float:
        return self.proceeds_sol - self.cost_realized_sol

    @property
    def pnl_worst_case(self) -> float:
        """Незакрытый остаток оценён в НОЛЬ. Так считают честно."""
        return self.proceeds_sol - self.cost_sol

    @property
    def multiple(self) -> float:
        base = self.cost_realized_sol if self.is_closed else self.cost_sol
        return self.proceeds_sol / base if base > 1e-12 else 0.0

    @property
    def hold_sec(self) -> int:
        return max(0, (self.closed or self.opened) - self.opened)


def _trades_for_mint(mint: str, swaps: list[Swap], dust_ratio: float) -> list[Trade]:
    lots: deque[list[float]] = deque()  # [остаток_токенов, цена_за_токен_в_SOL]
    cur: dict | None = None
    trades: list[Trade] = []

    def finish(closed_ts: int | None, is_closed: bool) -> Trade:
        return Trade(
            mint=mint, opened=cur["opened"], closed=closed_ts,
            cost_sol=cur["cost"], cost_realized_sol=cur["cost_real"],
            proceeds_sol=cur["proceeds"], open_tokens=sum(l[0] for l in lots),
            bought_tokens=cur["bought"], is_closed=is_closed,
        )

    for s in swaps:
        if s.side == "buy":
            if s.tokens <= 0 or s.quote_sol <= 0:
                continue
            if cur is None:
                cur = dict(opened=s.ts, bought=0.0, cost=0.0,
                           cost_real=0.0, proceeds=0.0)
            lots.append([s.tokens, s.quote_sol / s.tokens])
            cur["bought"] += s.tokens
            cur["cost"] += s.quote_sol
        else:
            if cur is None:
                continue  # продажа без покупки: аирдроп/перевод — не наша сделка
            remaining, matched = s.tokens, 0.0
            while remaining > 1e-12 and lots:
                lot = lots[0]
                take = min(lot[0], remaining)
                lot[0] -= take
                remaining -= take
                matched += take
                cur["cost_real"] += take * lot[1]
                if lot[0] <= 1e-12:
                    lots.popleft()
            if matched <= 0:
                continue
            frac = matched / s.tokens if s.tokens > 0 else 1.0
            cur["proceeds"] += s.quote_sol * frac

            if sum(l[0] for l in lots) <= cur["bought"] * dust_ratio:
                trades.append(finish(s.ts, True))
                lots.clear()
                cur = None

    if cur is not None:
        trades.append(finish(None, False))
    return trades


def reconstruct(swaps: list[Swap], dust_ratio: float) -> list[Trade]:
    by_mint: dict[str, list[Swap]] = defaultdict(list)
    for s in swaps:
        by_mint[s.mint].append(s)
    trades: list[Trade] = []
    for mint, group in by_mint.items():
        trades.extend(_trades_for_mint(mint, group, dust_ratio))
    trades.sort(key=lambda t: t.opened)
    return trades


# --------------------------------------------------------------------------- #
#  метрики
# --------------------------------------------------------------------------- #
@dataclass
class WalletMetrics:
    wallet: str
    swaps: int = 0
    tokens_traded: int = 0
    closed_trades: int = 0
    open_trades: int = 0
    win_rate: float = 0.0              # только по закрытым
    win_rate_worst_case: float = 0.0   # мешки = -100%
    realized_pnl_sol: float = 0.0
    pnl_worst_case_sol: float = 0.0
    avg_win_sol: float = 0.0
    avg_loss_sol: float = 0.0
    expectancy_sol: float = 0.0
    profit_factor: float = 0.0
    median_buy_sol: float = 0.0
    median_hold_sec: float = 0.0
    trades_per_day: float = 0.0
    best_multiple: float = 0.0
    history_days: float = 0.0
    dist: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    score: float = 0.0


def _pct(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def compute_metrics(wallet: str, swaps: list[Swap], trades: list[Trade],
                    a: dict) -> WalletMetrics:
    m = WalletMetrics(wallet=wallet)
    if not trades:
        m.flags.append("no_trades")
        return m

    closed = [t for t in trades if t.is_closed]
    open_ = [t for t in trades if not t.is_closed]

    m.swaps = len(swaps)
    m.tokens_traded = len({t.mint for t in trades})
    m.closed_trades = len(closed)
    m.open_trades = len(open_)

    wins = [t.pnl_realized for t in closed if t.pnl_realized > 0]
    losses = [t.pnl_realized for t in closed if t.pnl_realized <= 0]

    m.win_rate = _pct(len(wins), len(closed))
    worst_wins = sum(1 for t in trades if t.pnl_worst_case > 0)
    m.win_rate_worst_case = _pct(worst_wins, len(trades))

    m.realized_pnl_sol = round(sum(t.pnl_realized for t in closed), 3)
    m.pnl_worst_case_sol = round(sum(t.pnl_worst_case for t in trades), 3)
    m.avg_win_sol = round(statistics.fmean(wins), 3) if wins else 0.0
    m.avg_loss_sol = round(statistics.fmean(losses), 3) if losses else 0.0
    m.expectancy_sol = round(m.realized_pnl_sol / len(closed), 4) if closed else 0.0

    gross_loss = abs(sum(losses))
    m.profit_factor = round(sum(wins) / gross_loss, 2) if gross_loss > 1e-9 \
        else (999.0 if wins else 0.0)

    buys = [s.quote_sol for s in swaps if s.side == "buy" and s.quote_sol > 0]
    m.median_buy_sol = round(statistics.median(buys), 3) if buys else 0.0
    holds = [t.hold_sec for t in closed if t.hold_sec > 0]
    m.median_hold_sec = round(statistics.median(holds), 1) if holds else 0.0

    span = max(1.0, (swaps[-1].ts - swaps[0].ts) / DAY) if swaps else 1.0
    m.history_days = round(span, 1)
    m.trades_per_day = round(len(trades) / span, 2)
    m.best_multiple = round(max((t.multiple for t in closed), default=0.0), 2)

    buckets = {"rug(<0.5x)": 0, "loss(0.5-1x)": 0, "1-2x": 0, "2-5x": 0, "5x+": 0}
    for t in closed:
        mult = t.multiple
        if mult < 0.5:
            buckets["rug(<0.5x)"] += 1
        elif mult < 1:
            buckets["loss(0.5-1x)"] += 1
        elif mult < 2:
            buckets["1-2x"] += 1
        elif mult < 5:
            buckets["2-5x"] += 1
        else:
            buckets["5x+"] += 1
    m.dist = buckets

    # ---- красные флаги ----
    if m.closed_trades < a["min_closed_trades"]:
        m.flags.append("too_few_trades")
    if m.history_days < a["min_history_days"]:
        m.flags.append("short_history")
    if m.median_buy_sol < a["min_median_buy_sol"]:
        m.flags.append("farm_like_size")       # микро-суммы = фарм статистики
    if m.trades_per_day > a["max_trades_per_day"]:
        m.flags.append("hft_bot")
    if m.median_hold_sec and m.median_hold_sec < a["min_median_hold_sec"]:
        m.flags.append("sniper_bot")           # руками за ним не успеть
    if m.win_rate > a["max_win_rate"]:
        m.flags.append("suspicious_win_rate")  # 90%+ на мемах не бывает
    if trades and m.open_trades / len(trades) > a["max_open_loss_share"]:
        m.flags.append("bagholder")            # прячет убытки в незакрытых позициях
    if m.expectancy_sol <= 0:
        m.flags.append("negative_expectancy")

    m.score = round(
        min(m.profit_factor, 5) * 20
        + min(m.expectancy_sol * 10, 30)
        + min(m.closed_trades / 10, 15)
        - 12 * len(m.flags), 1)
    return m


def qualifies(m: WalletMetrics, a: dict) -> tuple[bool, list[str]]:
    reasons = []
    if m.closed_trades < a["min_closed_trades"]:
        reasons.append(f"сделок {m.closed_trades} < {a['min_closed_trades']}")
    if m.expectancy_sol < a["min_expectancy_sol"]:
        reasons.append(f"expectancy {m.expectancy_sol} < {a['min_expectancy_sol']}")
    if m.profit_factor < a["min_profit_factor"]:
        reasons.append(f"PF {m.profit_factor} < {a['min_profit_factor']}")
    if not (a["min_win_rate"] <= m.win_rate <= a["max_win_rate"]):
        reasons.append(f"winrate {m.win_rate} вне [{a['min_win_rate']}, {a['max_win_rate']}]")
    for bad in ("farm_like_size", "hft_bot", "sniper_bot", "bagholder", "short_history"):
        if bad in m.flags:
            reasons.append(bad)
    return (not reasons), reasons


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
async def analyze_wallet(session, wallet: str, cfg: dict) -> WalletMetrics:
    swaps = await fetch_swaps(session, wallet, cfg)
    trades = reconstruct(swaps, cfg["analyzer"]["dust_ratio"])
    return compute_metrics(wallet, swaps, trades, cfg["analyzer"])


async def run(wallets: list[str], cfg: dict, out_path: str):
    sem = asyncio.Semaphore(cfg["analyzer"]["concurrency"])
    results: list[WalletMetrics] = []

    async with aiohttp.ClientSession() as session:
        async def worker(w: str):
            async with sem:
                try:
                    m = await analyze_wallet(session, w, cfg)
                except Exception as e:                      # noqa: BLE001
                    m = WalletMetrics(wallet=w, flags=[f"error:{type(e).__name__}"])
                results.append(m)
                ok, why = qualifies(m, cfg["analyzer"])
                mark = "✅" if ok else "❌"
                print(f"{mark} {w[:6]}..{w[-4:]}  trades={m.closed_trades:<4} "
                      f"WR={m.win_rate*100:>5.1f}%  WR(мешки=0)={m.win_rate_worst_case*100:>5.1f}%  "
                      f"E={m.expectancy_sol:>7.3f} SOL  PF={m.profit_factor:<6} "
                      f"PnL={m.realized_pnl_sol:>9.2f}  {','.join(m.flags) or ''}"
                      + ("" if ok else f"  ← {'; '.join(why)}"))

        await asyncio.gather(*(worker(w) for w in wallets))

    results.sort(key=lambda m: m.score, reverse=True)
    good = [m for m in results if qualifies(m, cfg["analyzer"])[0]]

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": int(time.time()),
                   "qualified": [asdict(m) for m in good],
                   "all": [asdict(m) for m in results]}, f, indent=2, ensure_ascii=False)

    print(f"\nГодных кошельков: {len(good)} из {len(results)} → {out_path}")
    if not good:
        print("Это нормальный результат. Большинство «топовых» кошельков "
              "не проходят фильтр на expectancy и на скрытые мешки.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wallets", help="файл со адресами, по одному в строке")
    p.add_argument("--wallet", action="append", default=[], help="адрес (можно несколько)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--out", default="qualified.json")
    args = p.parse_args()

    cfg = load_config(args.config)
    addrs = list(args.wallet)
    if args.wallets:
        with open(args.wallets, encoding="utf-8") as f:
            addrs += [ln.strip() for ln in f
                      if ln.strip() and not ln.startswith("#")]
    addrs = list(dict.fromkeys(addrs))
    if not addrs:
        sys.exit("Нужен --wallet или --wallets")

    asyncio.run(run(addrs, cfg, args.out))


if __name__ == "__main__":
    main()
