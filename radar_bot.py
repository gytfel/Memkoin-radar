"""
radar_bot.py — телеграм-радар.

Логика:
  1. Следит за кошельками, прошедшими фильтр wallet_analyzer.
  2. Ждёт «конфлюэнс»: N разных годных кошельков купили один токен
     за окно T минут на суммарно >= X SOL.
  3. Гоняет токен через token_safety (ликвидность, authority, холдеры).
  4. Спрашивает у signal_journal: какой РЕАЛЬНЫЙ winrate у сигналов
     с такими же условиями? Если ниже цели — не отправляет.
  5. Отправленные сигналы ведёт до конца: TP-лестница, стоп, выход
     tracked-кошельков — всё приходит в тот же чат.

Команды в боте: /stats  /open  /wallets  /help

Запуск:
    python radar_bot.py --wallets qualified.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import defaultdict, deque

import aiohttp

import token_safety as ts
from signal_journal import SignalJournal
from wallet_analyzer import fetch_swaps, get_json, load_config, post_json

TG = "https://api.telegram.org/bot{token}/{method}"


# --------------------------------------------------------------------------- #
#  telegram
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self, session, token: str, chat_id: str):
        self.s, self.token, self.chat_id = session, token, chat_id
        self.offset = 0

    async def send(self, text: str, chat_id: str | None = None) -> None:
        await post_json(self.s, TG.format(token=self.token, method="sendMessage"),
                        {"chat_id": chat_id or self.chat_id, "text": text,
                         "parse_mode": "HTML", "disable_web_page_preview": True})

    async def poll(self) -> list[dict]:
        data = await get_json(self.s, TG.format(token=self.token, method="getUpdates"),
                              {"offset": self.offset, "timeout": 0}, retries=1)
        updates = (data or {}).get("result") or []
        for u in updates:
            self.offset = max(self.offset, u["update_id"] + 1)
        return updates


# --------------------------------------------------------------------------- #
#  радар
# --------------------------------------------------------------------------- #
class Radar:
    def __init__(self, cfg: dict, wallets: list[str], journal: SignalJournal):
        self.cfg = cfg
        self.wallets = wallets
        self.j = journal
        # mint -> deque[(ts, wallet, sol)]
        self.buys: dict[str, deque] = defaultdict(deque)
        self.started = int(time.time())
        self.checked = 0

    # ---------------------------------------------------------------- #
    def _remember_buy(self, mint: str, wallet: str, sol: float, when: int) -> None:
        window = self.cfg["radar"]["confluence_window_min"] * 60
        dq = self.buys[mint]
        dq.append((when, wallet, sol))
        cutoff = int(time.time()) - window
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _confluence(self, mint: str) -> tuple[int, float, list[str]]:
        dq = self.buys[mint]
        uniq: dict[str, float] = {}
        for _, w, sol in dq:
            uniq[w] = uniq.get(w, 0.0) + sol
        return len(uniq), sum(uniq.values()), list(uniq)

    # ---------------------------------------------------------------- #
    async def scan_wallets(self, session, tg: Telegram) -> None:
        """Один проход по всем кошелькам: ищем свежие покупки и продажи."""
        r = self.cfg["radar"]
        fresh_window = int(time.time()) - r["confluence_window_min"] * 60

        for w in self.wallets:
            swaps = await fetch_swaps(session, w, self.cfg, pages=1,
                                      until_ts=fresh_window)
            for s in swaps:
                if s.ts < fresh_window or self.j.already_seen(s.sig):
                    continue
                if s.side == "buy":
                    self._remember_buy(s.mint, w, s.quote_sol, s.ts)
                    await self.maybe_signal(session, tg, s.mint)
                else:
                    await self.notify_wallet_exit(tg, s.mint, w)
            await asyncio.sleep(0.15)  # щадим rate limit

    # ---------------------------------------------------------------- #
    async def maybe_signal(self, session, tg: Telegram, mint: str) -> None:
        r, rk = self.cfg["radar"], self.cfg["risk"]
        n, total_sol, who = self._confluence(mint)

        if n < r["confluence_wallets"] or total_sol < r["min_combined_buy_sol"]:
            return
        if self.j.is_on_cooldown(mint, r["cooldown_min"]):
            return

        self.checked += 1
        safety = await ts.check_token(session, mint, self.cfg)
        if not safety.ok or safety.price_usd <= 0:
            return  # молча: скам-токены в чат не идут

        entry = safety.price_usd
        sl = entry * (1 - rk["stop_loss_pct"] / 100)
        tps = [entry * (1 + p / 100) for p in rk["take_profit_ladder_pct"]]

        bucket = self.j.make_bucket(n, safety.liquidity_usd, safety.age_min)
        allowed, gate_note = self.j.gate(bucket, rk["target_hit_rate"],
                                         rk["min_sample_for_gate"])
        hard = self.cfg["risk"]["gate_mode"] == "hard"
        deliver = allowed or not hard

        sid = self.j.open_signal(
            mint, safety.symbol, bucket, entry, sl, tps, deliver,
            {"wallets": who, "sol": round(total_sol, 2),
             "liq": safety.liquidity_usd, "age_min": safety.age_min,
             "gate": gate_note})

        if not deliver:
            return  # hard-режим: тишина, но сделка пишется в журнал для замера

        risk_sol = rk["account_size_sol"] * rk["risk_per_trade_pct"] / 100
        pos_sol = round(risk_sol / (rk["stop_loss_pct"] / 100), 3)

        msg = (
            f"🟢 <b>ВХОД #{sid} · {safety.symbol}</b>\n"
            f"<code>{mint}</code>\n\n"
            f"Купили <b>{n}</b> отслеживаемых кошелька на <b>{total_sol:.2f} SOL</b>\n"
            f"Цена: <b>${entry:.8f}</b>\n"
            f"Ликвидность: ${safety.liquidity_usd:,.0f} · FDV ${safety.fdv_usd:,.0f}\n"
            f"Возраст: {safety.age_min:.0f} мин · топ-10: {safety.top10_pct}%\n"
            f"Rugcheck: {safety.rugcheck_score if safety.rugcheck_score is not None else '—'}\n\n"
            f"🛑 Стоп: <b>${sl:.8f}</b> (−{rk['stop_loss_pct']}%)\n"
            f"🎯 Тейки: " + " / ".join(f"${p:.8f}" for p in tps) + "\n"
            f"   (+" + "% / +".join(str(p) for p in rk["take_profit_ladder_pct"]) + "%)\n"
            f"💰 Размер позиции: ~{pos_sol} SOL "
            f"(риск {rk['risk_per_trade_pct']}% от {rk['account_size_sol']} SOL)\n\n"
            f"📊 {gate_note}\n"
            f"<a href='{safety.pair_url}'>график</a>"
        )
        if not allowed:
            msg = "⚠️ <i>ниже целевого winrate, shadow-режим</i>\n\n" + msg
        await tg.send(msg)

    # ---------------------------------------------------------------- #
    async def notify_wallet_exit(self, tg: Telegram, mint: str, wallet: str) -> None:
        for sig in self.j.list_open():
            if sig.mint == mint and sig.delivered:
                await tg.send(
                    f"🟡 <b>#{sig.id} {sig.symbol}</b>: отслеживаемый кошелёк "
                    f"<code>{wallet[:6]}..{wallet[-4:]}</code> начал продавать.\n"
                    f"Умные деньги выходят — обычно это сигнал сокращать позицию.")
                return

    # ---------------------------------------------------------------- #
    async def monitor_open(self, session, tg: Telegram) -> None:
        """Ведём открытые сигналы: тейки, стоп, протухание."""
        for sig in self.j.list_open():
            price = await ts.price_usd(session, sig.mint)
            if price <= 0:
                # ликвидность исчезла = раг
                self.j.resolve(sig.id, "loss", 0.0)
                if sig.delivered:
                    await tg.send(f"💀 <b>#{sig.id} {sig.symbol}</b>: ликвидность "
                                  f"пропала (rug). Сигнал закрыт как убыточный.")
                continue

            self.j.update_peak(sig.id, price)
            chg = (price / sig.entry_price - 1) * 100

            if price <= sig.sl_price:
                self.j.resolve(sig.id, "loss", price)
                if sig.delivered:
                    await tg.send(f"🔴 <b>СТОП #{sig.id} {sig.symbol}</b>\n"
                                  f"${price:.8f} ({chg:+.1f}%) — выходим, "
                                  f"не усредняемся.")
                continue

            if price >= sig.tp_prices[0]:
                # первый тейк = сигнал считается отработавшим в плюс
                self.j.resolve(sig.id, "win", price)
                if sig.delivered:
                    hit = max(i + 1 for i, p in enumerate(sig.tp_prices) if price >= p)
                    await tg.send(
                        f"✅ <b>ТЕЙК {hit} #{sig.id} {sig.symbol}</b>\n"
                        f"${price:.8f} ({chg:+.1f}%)\n"
                        f"Фиксируй часть, стоп переставь в безубыток.")
                continue

            age_h = (time.time() - sig.ts) / 3600
            if age_h > 24:
                self.j.resolve(sig.id, "win" if chg > 0 else "loss", price)
                if sig.delivered:
                    await tg.send(f"⏳ <b>#{sig.id} {sig.symbol}</b>: 24 ч без "
                                  f"движения к цели ({chg:+.1f}%). Закрываю по времени.")
            await asyncio.sleep(0.2)

    # ---------------------------------------------------------------- #
    async def handle_commands(self, tg: Telegram) -> None:
        for u in await tg.poll():
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip().lower()
            chat = str((msg.get("chat") or {}).get("id") or "")
            if not text.startswith("/"):
                continue

            if text.startswith("/stats"):
                await tg.send("📊 <b>Реальная статистика радара</b>\n<pre>"
                              + self.j.summary() + "</pre>", chat)
            elif text.startswith("/open"):
                rows = self.j.list_open()
                if not rows:
                    await tg.send("Открытых сигналов нет.", chat)
                else:
                    await tg.send("\n".join(
                        f"#{s.id} {s.symbol} вход ${s.entry_price:.8f} "
                        f"пик ${s.peak_price:.8f}" for s in rows), chat)
            elif text.startswith("/wallets"):
                up = (time.time() - self.started) / 3600
                await tg.send(f"Отслеживаю {len(self.wallets)} кошельков.\n"
                              f"Аптайм: {up:.1f} ч · проверено кандидатов: {self.checked}",
                              chat)
            else:
                await tg.send("/stats — замеренный winrate\n/open — открытые сигналы\n"
                              "/wallets — что отслеживаю", chat)


# --------------------------------------------------------------------------- #
async def main_loop(cfg: dict, wallets: list[str], db: str):
    journal = SignalJournal(db)
    radar = Radar(cfg, wallets, journal)
    tick = 0

    async with aiohttp.ClientSession() as session:
        tg = Telegram(session, cfg["telegram"]["bot_token"],
                      str(cfg["telegram"]["chat_id"]))
        await tg.send(
            f"🛰 Радар запущен.\nКошельков: {len(wallets)}\n"
            f"Условие сигнала: {cfg['radar']['confluence_wallets']}+ кошелька, "
            f"{cfg['radar']['min_combined_buy_sol']}+ SOL за "
            f"{cfg['radar']['confluence_window_min']} мин\n"
            f"Гейт: {cfg['risk']['gate_mode']}, цель "
            f"{cfg['risk']['target_hit_rate']*100:.0f}%")

        while True:
            try:
                await radar.handle_commands(tg)
                await radar.scan_wallets(session, tg)
                await radar.monitor_open(session, tg)
                tick += 1
                if tick % 200 == 0:
                    journal.prune_seen()
            except Exception as e:                              # noqa: BLE001
                print(f"[loop error] {type(e).__name__}: {e}")
            await asyncio.sleep(cfg["radar"]["poll_interval_sec"])


def load_wallets(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            data = json.load(f)
            return [m["wallet"] for m in data.get("qualified", [])]
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--wallets", default="qualified.json")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--db", default="signals.db")
    args = p.parse_args()

    conf = load_config(args.config)
    ws = load_wallets(args.wallets)
    if not ws:
        raise SystemExit("Список кошельков пуст. Сначала прогони wallet_analyzer.py — "
                         "радар без проверенных кошельков бесполезен.")
    asyncio.run(main_loop(conf, ws, args.db))
