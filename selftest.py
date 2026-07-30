"""
selftest.py — проверка логики на синтетических данных, без сети и без API-ключей.

    python selftest.py

Показывает главное: два кошелька с ОДИНАКОВЫМ winrate 80% могут быть
прибыльным и убыточным. Winrate сам по себе не говорит ни о чём.
"""

from __future__ import annotations

import os
import tempfile

from signal_journal import SignalJournal
from wallet_analyzer import (Swap, compute_metrics, load_config, parse_swap,
                             qualifies, reconstruct)

CFG = load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))
A = CFG["analyzer"]
T0 = 1_700_000_000


def mk(mint: str, i: int, cost: float, ret: float, hold: int = 3600,
       space: int = 86_400) -> list[Swap]:
    """Одна сделка: вход на cost SOL, выход на cost*ret."""
    return [Swap(T0 + i * space, f"b{i}", mint, "buy", 1_000_000, cost),
            Swap(T0 + i * space + hold, f"s{i}", mint, "sell", 1_000_000, cost * ret)]


def case(name: str, swaps: list[Swap]) -> None:
    trades = reconstruct(swaps, A["dust_ratio"])
    m = compute_metrics(name, swaps, trades, A)
    ok, why = qualifies(m, A)
    print(f"\n{'='*74}\n{name}\n{'-'*74}")
    print(f"  закрытых сделок : {m.closed_trades}   открытых: {m.open_trades}")
    print(f"  winrate         : {m.win_rate*100:.1f}%")
    print(f"  winrate (мешки=0): {m.win_rate_worst_case*100:.1f}%   <-- честная цифра")
    print(f"  realized PnL    : {m.realized_pnl_sol:+.2f} SOL")
    print(f"  PnL с мешками   : {m.pnl_worst_case_sol:+.2f} SOL")
    print(f"  expectancy      : {m.expectancy_sol:+.3f} SOL/сделка")
    print(f"  profit factor   : {m.profit_factor}")
    print(f"  флаги           : {', '.join(m.flags) or '—'}")
    print(f"  ВЕРДИКТ         : {'ГОДЕН' if ok else 'ОТКЛОНЁН — ' + '; '.join(why)}")


# --- 1. классика: 80% winrate и минус по деньгам -------------------------- #
s1: list[Swap] = []
for i in range(40):
    s1 += mk(f"WIN{i}", i, 2.0, 1.10) if i % 5 else mk(f"RUG{i}", i, 2.0, 0.05)
case("Кошелёк A: winrate 80%, но убыточный", s1)

# --- 2. 40% winrate и хороший плюс --------------------------------------- #
s2: list[Swap] = []
for i in range(40):
    s2 += mk(f"T{i}", i, 2.0, 3.2) if i % 5 < 2 else mk(f"T{i}", i, 2.0, 0.75)
case("Кошелёк B: winrate 40%, прибыльный", s2)

# --- 3. спрятанные мешки: продал только удачное --------------------------- #
s3: list[Swap] = []
for i in range(35):
    s3 += mk(f"G{i}", i, 1.5, 1.6)
for i in range(35):                        # 35 позиций куплено и НЕ продано
    s3.append(Swap(T0 + i * 86_400 + 100, f"h{i}", f"BAG{i}", "buy", 500_000, 1.5))
case("Кошелёк C: 100% winrate за счёт непроданных мешков", s3)

# --- 4. фарм статистики микро-суммами ------------------------------------ #
s4: list[Swap] = []
for i in range(120):
    s4 += mk(f"F{i}", i, 0.02, 1.05, hold=25, space=21_600)
case("Кошелёк D: бот-фарм (0.02 SOL, 25 сек)", s4)


# --- 5. парсер Helius ----------------------------------------------------- #
print(f"\n{'='*74}\nПарсер транзакций Helius\n{'-'*74}")
W = "WalletAAA"
tx_buy = {"timestamp": T0, "signature": "sig1", "events": {"swap": {
    "nativeInput": {"account": W, "amount": "2500000000"},
    "tokenOutputs": [{"userAccount": W, "mint": "MEME1",
                      "rawTokenAmount": {"tokenAmount": "1500000000000", "decimals": 6}}]}}}
tx_sell = {"timestamp": T0 + 600, "signature": "sig2", "events": {"swap": {
    "nativeOutput": {"account": W, "amount": "6000000000"},
    "tokenInputs": [{"userAccount": W, "mint": "MEME1",
                     "rawTokenAmount": {"tokenAmount": "1500000000000", "decimals": 6}}]}}}
for tx in (tx_buy, tx_sell):
    print("  ", parse_swap(tx, W, 150.0))
tr = reconstruct([parse_swap(tx_buy, W, 150.0), parse_swap(tx_sell, W, 150.0)],
                 A["dust_ratio"])[0]
print(f"   сделка: вложено {tr.cost_sol} SOL → получено {tr.proceeds_sol} SOL, "
      f"PnL {tr.pnl_realized:+.2f} SOL, {tr.multiple:.2f}x, закрыта={tr.is_closed}")
assert abs(tr.pnl_realized - 3.5) < 1e-6 and tr.is_closed

# --- 6. гейт по замеренному winrate -------------------------------------- #
print(f"\n{'='*74}\nГейт «не присылать, если ниже цели»\n{'-'*74}")
path = os.path.join(tempfile.mkdtemp(), "t.db")
j = SignalJournal(path)
bucket = j.make_bucket(2, 30_000, 45)
print("   бакет условий:", bucket)
for i in range(30):
    sid = j.open_signal("M", "TEST", bucket, 1.0, 0.75, [1.4], True, {})
    j.resolve(sid, "win" if i % 10 < 5 else "loss", 1.4 if i % 10 < 5 else 0.75)
allowed, note = j.gate(bucket, CFG["risk"]["target_hit_rate"],
                       CFG["risk"]["min_sample_for_gate"])
print(f"   сигналы разрешены: {allowed}\n   {note}")
assert allowed is False, "гейт обязан глушить бакет с winrate ниже цели"
print("\n" + j.summary())
os.remove(path)

print(f"\n{'='*74}\nВсе проверки логики пройдены.\n{'='*74}")
