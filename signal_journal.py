"""
signal_journal.py — журнал сигналов и ЗАМЕРЕННЫЙ winrate.

Это ядро твоего требования «не присылать, если не 80%».
Гарантировать 80% заранее невозможно. Что можно — честно:
  * записать каждый сигнал вместе с параметрами (сколько кошельков зашло,
    ликвидность, возраст токена);
  * дождаться исхода (TP или SL);
  * считать реальный hit-rate по каждому «бакету» условий;
  * и глушить сигналы из тех бакетов, где замеренный winrate ниже цели.

Через 2–4 недели работы ты увидишь настоящие цифры своей стратегии,
а не обещанные.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    mint          TEXT NOT NULL,
    symbol        TEXT,
    bucket        TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    sl_price      REAL NOT NULL,
    tp_prices     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',   -- open|win|loss|expired
    peak_price    REAL DEFAULT 0,
    exit_price    REAL,
    exit_ts       INTEGER,
    r_multiple    REAL,
    delivered     INTEGER NOT NULL DEFAULT 1,     -- 0 = подавлен гейтом (shadow)
    meta          TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_bucket ON signals(bucket);
CREATE INDEX IF NOT EXISTS idx_mint_ts ON signals(mint, ts);
CREATE TABLE IF NOT EXISTS seen_sigs (sig TEXT PRIMARY KEY, ts INTEGER);
CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen_sigs(ts);
"""


@dataclass
class OpenSignal:
    id: int
    mint: str
    symbol: str
    entry_price: float
    sl_price: float
    tp_prices: list[float]
    peak_price: float
    ts: int
    delivered: bool


class SignalJournal:
    def __init__(self, path: str = "signals.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "SignalJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def already_seen(self, sig: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM seen_sigs WHERE sig=?", (sig,))
        if cur.fetchone():
            return True
        self.db.execute("INSERT OR IGNORE INTO seen_sigs VALUES (?,?)",
                        (sig, int(time.time())))
        self.db.commit()
        return False

    def prune_seen(self, older_than_sec: int = 3 * 86400) -> None:
        self.db.execute("DELETE FROM seen_sigs WHERE ts < ?",
                        (int(time.time()) - older_than_sec,))
        self.db.commit()

    # ------------------------------------------------------------------ #
    @staticmethod
    def make_bucket(n_wallets: int, liq_usd: float, age_min: float) -> str:
        """Огрубляем условия входа, чтобы по бакету набиралась статистика."""
        w = "w2" if n_wallets <= 2 else ("w3" if n_wallets == 3 else "w4+")
        liq = "liqL" if liq_usd < 40_000 else ("liqM" if liq_usd < 150_000 else "liqH")
        age = "new" if age_min < 120 else ("mid" if age_min < 1440 else "old")
        return f"{w}|{liq}|{age}"

    def open_signal(self, mint: str, symbol: str, bucket: str, entry: float,
                    sl: float, tps: list[float], delivered: bool,
                    meta: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO signals (ts,mint,symbol,bucket,entry_price,sl_price,"
            "tp_prices,peak_price,delivered,meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), mint, symbol, bucket, entry, sl,
             json.dumps(tps), entry, int(delivered),
             json.dumps(meta, ensure_ascii=False)))
        self.db.commit()
        return int(cur.lastrowid)

    def list_open(self) -> list[OpenSignal]:
        rows = self.db.execute(
            "SELECT * FROM signals WHERE status='open' ORDER BY ts").fetchall()
        out: list[OpenSignal] = []
        for r in rows:
            try:
                tps = [float(p) for p in json.loads(r["tp_prices"])]
            except (TypeError, ValueError):
                tps = []
            out.append(OpenSignal(r["id"], r["mint"], r["symbol"] or "?",
                                  r["entry_price"], r["sl_price"], tps,
                                  r["peak_price"] or 0.0, r["ts"],
                                  bool(r["delivered"])))
        return out

    def list_closed(self, limit: int = 10) -> list[sqlite3.Row]:
        """Последние отработавшие сигналы — для /history.

        Только доставленные: подавленные гейтом человек не видел, и в
        истории они выглядели бы сделками, которых у него не было.
        """
        return self.db.execute(
            "SELECT id, symbol, status, r_multiple, exit_ts, entry_price, exit_price "
            "FROM signals WHERE status IN ('win','loss') AND delivered=1 "
            "ORDER BY COALESCE(exit_ts, ts) DESC LIMIT ?", (limit,)).fetchall()

    def update_peak(self, sid: int, price: float) -> None:
        # COALESCE: sqlite-шный MAX(a,b) возвращает NULL, если любой аргумент NULL
        self.db.execute(
            "UPDATE signals SET peak_price=MAX(COALESCE(peak_price,0),?) WHERE id=?",
            (price, sid))
        self.db.commit()

    def resolve(self, sid: int, status: str, exit_price: float) -> None:
        row = self.db.execute("SELECT entry_price, sl_price FROM signals WHERE id=?",
                             (sid,)).fetchone()
        r_mult = None
        if row:
            risk = (row["entry_price"] or 0) - (row["sl_price"] or 0)
            if risk > 0:
                r_mult = round((exit_price - row["entry_price"]) / risk, 2)
        # status='open' в условии: сигнал нельзя закрыть дважды и переписать исход
        self.db.execute(
            "UPDATE signals SET status=?, exit_price=?, exit_ts=?, r_multiple=? "
            "WHERE id=? AND status='open'",
            (status, exit_price, int(time.time()), r_mult, sid))
        self.db.commit()

    def is_on_cooldown(self, mint: str, minutes: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM signals WHERE mint=? AND ts > ? LIMIT 1",
            (mint, int(time.time()) - minutes * 60)).fetchone()
        return bool(row)

    # ------------------------------------------------------------------ #
    #  замеренный winrate — тот самый «гейт 80%»
    # ------------------------------------------------------------------ #
    def hit_rate(self, bucket: str | None = None) -> tuple[float, int]:
        q = "SELECT status FROM signals WHERE status IN ('win','loss')"
        args: tuple = ()
        if bucket:
            q += " AND bucket=?"
            args = (bucket,)
        rows = self.db.execute(q, args).fetchall()
        if not rows:
            return 0.0, 0
        wins = sum(1 for r in rows if r["status"] == "win")
        return wins / len(rows), len(rows)

    def gate(self, bucket: str, target: float, min_sample: int) -> tuple[bool, str]:
        """True = сигнал можно отправлять."""
        wr, n = self.hit_rate(bucket)
        if n < min_sample:
            wr_all, n_all = self.hit_rate()
            return True, (f"КАЛИБРОВКА: по условиям {bucket} набрано {n}/{min_sample} "
                          f"исходов. Общий замер: {wr_all*100:.0f}% на {n_all} сигналах")
        if wr < target:
            return False, (f"подавлен: замеренный winrate {wr*100:.0f}% "
                           f"на {n} сигналах < цели {target*100:.0f}%")
        return True, f"замеренный winrate {wr*100:.0f}% на {n} сигналах"

    def summary(self) -> str:
        wr, n = self.hit_rate()
        rows = self.db.execute(
            "SELECT r_multiple FROM signals WHERE r_multiple IS NOT NULL").fetchall()
        rs = [r["r_multiple"] for r in rows]
        avg_r = sum(rs) / len(rs) if rs else 0.0
        open_n = len(self.list_open())
        supp = self.db.execute(
            "SELECT COUNT(*) c FROM signals WHERE delivered=0").fetchone()["c"]

        lines = [f"Исходов: {n}   Winrate: {wr*100:.1f}%   Средний R: {avg_r:+.2f}",
                 f"Открытых: {open_n}   Подавлено гейтом: {supp}",
                 f"Мат. ожидание на сделку: {avg_r:+.2f}R "
                 f"({'плюс' if avg_r > 0 else 'минус'})", "", "По бакетам:"]
        buckets = self.db.execute(
            "SELECT bucket, COUNT(*) n, "
            "SUM(CASE WHEN status='win' THEN 1 ELSE 0 END) w "
            "FROM signals WHERE status IN ('win','loss') "
            "GROUP BY bucket ORDER BY n DESC").fetchall()
        if not buckets:
            lines.append("  пока пусто")
        for b in buckets:
            lines.append(f"  {b['bucket']:<16} {b['w']}/{b['n']} "
                         f"= {b['w']/b['n']*100:.0f}%")
        return "\n".join(lines)
