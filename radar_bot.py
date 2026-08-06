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

Команды сгруппированы по назначению (полный список — в COMMANDS ниже):
  что происходит  — /status  /open  /watch
  результаты      — /stats   /history
  настройки       — /wallets /settings /pause /resume

Запуск:
    python radar_bot.py --wallets qualified.json
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import time
from collections import deque

import aiohttp

import token_safety as ts
from signal_journal import SignalJournal
from wallet_analyzer import (fetch_swaps, get_json, is_solana_address,
                             load_config, make_session, post_json,
                             require_config, setup_logging)

TG = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4096          # жёсткий лимит длины сообщения в Telegram

# Длинные списки режутся: в чате нужен обзор, а не выгрузка базы.
OPEN_LIMIT, WATCH_LIMIT, HISTORY_LIMIT, WALLET_LIMIT = 10, 10, 10, 20

# Команды сгруппированы по назначению: «что сейчас», «что уже случилось»,
# «как бот настроен». Раньше их было три штуки без всякого деления, и
# добрая половина состояния радара из чата просто не читалась.
COMMANDS = [
    ("status",   "жив ли радар и что он видит сейчас",  "Что происходит"),
    ("open",     "открытые сигналы с текущей ценой",    "Что происходит"),
    ("watch",    "токены на подходе к сигналу",         "Что происходит"),
    ("stats",    "замеренный winrate по условиям",      "Результаты"),
    ("history",  "последние закрытые сигналы",          "Результаты"),
    ("wallets",  "за какими кошельками слежу",          "Настройки"),
    ("addwallet", "добавить кошелёк: /addwallet адрес", "Настройки"),
    ("delwallet", "убрать кошелёк: /delwallet адрес",   "Настройки"),
    ("settings", "пороги входа, риска и безопасности",  "Настройки"),
    ("pause",    "перестать присылать сигналы",         "Настройки"),
    ("resume",   "вернуть сигналы",                     "Настройки"),
    ("help",     "этот список",                         "Справка"),
]

log = logging.getLogger("radar")


def help_text() -> str:
    """Справка собирается из COMMANDS, а не пишется отдельно.

    Раньше список в /help жил своей жизнью и отставал от кода: команда
    существовала, а в справке её не было.
    """
    lines: list[str] = []
    group = None
    for cmd, desc, grp in COMMANDS:
        if grp != group:
            lines.append(f"\n<b>{grp}</b>")
            group = grp
        lines.append(f"/{cmd} — {desc}")
    return "\n".join(lines).strip()


def esc(value) -> str:
    """Экранируем всё, что пришло извне.

    Символ токена берётся из Dexscreener, то есть его пишет автор монеты.
    Тикер вида `<b>` ломал разбор parse_mode=HTML, Telegram отвечал 400,
    и сигнал молча терялся.
    """
    return html.escape(str(value), quote=False)


# --------------------------------------------------------------------------- #
#  telegram
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self, session, token: str, chat_id: str):
        self.s, self.token, self.chat_id = session, token, chat_id
        self.offset = 0

    @staticmethod
    def _chunks(text: str) -> list[str]:
        """Режем по строкам, чтобы не рвать HTML-теги посреди сообщения."""
        if len(text) <= TG_LIMIT:
            return [text]
        out, cur = [], ""
        for line in text.split("\n"):
            line = line[:TG_LIMIT]
            if len(cur) + len(line) + 1 > TG_LIMIT:
                out.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            out.append(cur)
        return out

    async def send(self, text: str, chat_id: str | None = None) -> bool:
        """True, если Telegram принял всё сообщение целиком.

        Возврат нужен вызывающему: молчание в чате и молчание в логе
        выглядят одинаково, а причины у них разные.
        """
        ok = True
        for part in self._chunks(text):
            res = await post_json(
                self.s, TG.format(token=self.token, method="sendMessage"),
                {"chat_id": chat_id or self.chat_id, "text": part,
                 "parse_mode": "HTML", "disable_web_page_preview": True})
            if res is None:
                log.warning("Telegram не принял сообщение (%d символов)", len(part))
                ok = False
        return ok

    async def register_commands(self) -> bool:
        """Отдаём список команд в меню Telegram.

        Без этого кнопка меню в клиенте пустая, и команды знает только тот,
        кто читал README. Половина возможностей бота так и остаётся ненайденной.
        """
        res = await post_json(
            self.s, TG.format(token=self.token, method="setMyCommands"),
            {"commands": [{"command": c, "description": d} for c, d, _ in COMMANDS]})
        return res is not None

    async def poll(self) -> list[dict]:
        data = await get_json(self.s, TG.format(token=self.token, method="getUpdates"),
                              {"offset": self.offset, "timeout": 0}, retries=1)
        updates = (data or {}).get("result") if isinstance(data, dict) else None
        updates = [u for u in (updates or []) if isinstance(u, dict)]
        for u in updates:
            try:
                self.offset = max(self.offset, int(u["update_id"]) + 1)
            except (KeyError, TypeError, ValueError):
                continue
        return updates


# --------------------------------------------------------------------------- #
#  радар
# --------------------------------------------------------------------------- #
class Radar:
    def __init__(self, cfg: dict, wallets: list[str], journal: SignalJournal,
                 wallets_path: str | None = None):
        self.cfg = cfg
        self.wallets = wallets
        # Путь нужен, чтобы /addwallet пережил перезапуск: правка только в
        # памяти выглядит как работающая, а после рестарта кошелёк исчезает.
        self.wallets_path = wallets_path
        self.j = journal
        # mint -> deque[(ts, wallet, sol)]. Обычный dict, а не defaultdict:
        # чтение конфлюэнса не должно само плодить пустые ключи.
        self.buys: dict[str, deque] = {}
        self.started = int(time.time())
        self.checked = 0
        # /pause глушит только доставку. Радар продолжает вести журнал,
        # иначе пауза рвала бы замер winrate — ровно то, ради чего он есть.
        self.muted = False

    # ---------------------------------------------------------------- #
    def _prune(self, mint: str) -> deque:
        """Оставляем только покупки внутри окна конфлюэнса.

        Фильтруем всю очередь, а не срезаем голову: кошельки опрашиваются
        по очереди, поэтому свопы попадают сюда не в хронологическом порядке
        и старые записи прятались за более свежими — окно «протекало»,
        а конфлюэнс считался по покупкам многочасовой давности.
        """
        dq = self.buys.get(mint)
        if dq is None:
            return deque()
        cutoff = int(time.time()) - self.cfg["radar"]["confluence_window_min"] * 60
        fresh = [item for item in dq if item[0] >= cutoff]
        if not fresh:
            self.buys.pop(mint, None)      # иначе словарь растёт бесконечно
            return deque()
        dq.clear()
        dq.extend(fresh)
        return dq

    def sweep(self) -> None:
        """Периодическая уборка: минты, по которым давно не было покупок."""
        for mint in list(self.buys):
            self._prune(mint)

    def _remember_buy(self, mint: str, wallet: str, sol: float, when: int) -> None:
        self.buys.setdefault(mint, deque()).append((when, wallet, sol))
        self._prune(mint)

    def _confluence(self, mint: str) -> tuple[int, float, list[str]]:
        uniq: dict[str, float] = {}
        for _, w, sol in self._prune(mint):
            uniq[w] = uniq.get(w, 0.0) + sol
        return len(uniq), sum(uniq.values()), list(uniq)

    # ---------------------------------------------------------------- #
    async def scan_wallets(self, session, tg: Telegram) -> None:
        """Один проход по всем кошелькам: ищем свежие покупки и продажи."""
        r = self.cfg["radar"]
        fresh_window = int(time.time()) - r["confluence_window_min"] * 60

        for w in self.wallets:
            try:
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
            except asyncio.CancelledError:
                raise
            except Exception as e:                              # noqa: BLE001
                # один нерабочий кошелёк не должен обрывать весь проход
                log.warning("Кошелёк %s пропущен: %s: %s", w[:8], type(e).__name__, e)
                log.debug("traceback", exc_info=True)
            await asyncio.sleep(0.15)  # щадим rate limit

    # ---------------------------------------------------------------- #
    async def maybe_signal(self, session, tg: Telegram, mint: str) -> None:
        r, rk = self.cfg["radar"], self.cfg["risk"]
        n, total_sol, who = self._confluence(mint)

        if n < r["confluence_wallets"] or total_sol < r["min_combined_buy_sol"]:
            return
        if self.j.is_on_cooldown(mint, r["cooldown_min"]):
            return

        stop_pct = float(rk["stop_loss_pct"])
        ladder = [float(p) for p in rk["take_profit_ladder_pct"]]
        if not 0 < stop_pct < 100 or not ladder:
            log.error("risk.stop_loss_pct должен быть в (0, 100), "
                      "take_profit_ladder_pct — непустым. Сигнал пропущен.")
            return

        self.checked += 1
        safety = await ts.check_token(session, mint, self.cfg)
        if not safety.ok or safety.price_usd <= 0:
            return  # молча: скам-токены в чат не идут

        entry = safety.price_usd
        sl = entry * (1 - stop_pct / 100)
        tps = sorted(entry * (1 + p / 100) for p in ladder)

        bucket = self.j.make_bucket(n, safety.liquidity_usd, safety.age_min)
        allowed, gate_note = self.j.gate(bucket, rk["target_hit_rate"],
                                         rk["min_sample_for_gate"])
        hard = self.cfg["risk"]["gate_mode"] == "hard"
        deliver = (allowed or not hard) and not self.muted

        sid = self.j.open_signal(
            mint, safety.symbol, bucket, entry, sl, tps, deliver,
            {"wallets": who, "sol": round(total_sol, 2),
             "liq": safety.liquidity_usd, "age_min": safety.age_min,
             "gate": gate_note})

        if not deliver:
            return  # hard-режим: тишина, но сделка пишется в журнал для замера

        risk_sol = rk["account_size_sol"] * rk["risk_per_trade_pct"] / 100
        pos_sol = round(risk_sol / (stop_pct / 100), 3)

        msg = (
            f"🟢 <b>ВХОД #{sid} · {esc(safety.symbol)}</b>\n"
            f"<code>{esc(mint)}</code>\n\n"
            f"Купили <b>{n}</b> отслеживаемых кошелька на <b>{total_sol:.2f} SOL</b>\n"
            f"Цена: <b>${entry:.8f}</b>\n"
            f"Ликвидность: ${safety.liquidity_usd:,.0f} · FDV ${safety.fdv_usd:,.0f}\n"
            f"Возраст: {safety.age_min:.0f} мин · топ-10: {safety.top10_pct}%\n"
            f"Rugcheck: {safety.rugcheck_score if safety.rugcheck_score is not None else '—'}\n\n"
            f"🛑 Стоп: <b>${sl:.8f}</b> (−{stop_pct:g}%)\n"
            "🎯 Тейки: " + " / ".join(f"${p:.8f}" for p in tps) + "\n"
            "   (" + " / ".join(f"+{p:g}%" for p in sorted(ladder)) + ")\n"
            f"💰 Размер позиции: ~{pos_sol} SOL "
            f"(риск {rk['risk_per_trade_pct']}% от {rk['account_size_sol']} SOL)\n\n"
            f"📊 {esc(gate_note)}\n"
            f"<a href='{esc(safety.pair_url)}'>график</a>"
        )
        if not allowed:
            msg = "⚠️ <i>ниже целевого winrate, shadow-режим</i>\n\n" + msg
        if await tg.send(msg):
            log.info("Сигнал #%s %s отправлен: %d кошелька, %.2f SOL",
                     sid, safety.symbol, n, total_sol)

    # ---------------------------------------------------------------- #
    async def notify_wallet_exit(self, tg: Telegram, mint: str, wallet: str) -> None:
        for sig in self.j.list_open():
            if sig.mint == mint and sig.delivered:
                await tg.send(
                    f"🟡 <b>#{sig.id} {esc(sig.symbol)}</b>: отслеживаемый кошелёк "
                    f"<code>{esc(wallet[:6])}..{esc(wallet[-4:])}</code> начал продавать.\n"
                    f"Умные деньги выходят — обычно это сигнал сокращать позицию.")
                return

    # ---------------------------------------------------------------- #
    async def monitor_open(self, session, tg: Telegram) -> None:
        """Ведём открытые сигналы: тейки, стоп, протухание."""
        for sig in self.j.list_open():
            await self._track(session, tg, sig)
            # пауза вынесена из тела: раньше она стояла после серии continue
            # и при закрытии нескольких сигналов подряд не срабатывала вовсе
            await asyncio.sleep(0.2)

    async def _track(self, session, tg: Telegram, sig) -> None:
        name = esc(sig.symbol)
        price = await ts.price_usd(session, sig.mint)
        if price <= 0:
            # ликвидность исчезла = раг
            self.j.resolve(sig.id, "loss", 0.0)
            if sig.delivered:
                await tg.send(f"💀 <b>#{sig.id} {name}</b>: ликвидность "
                              f"пропала (rug). Сигнал закрыт как убыточный.")
            return

        self.j.update_peak(sig.id, price)
        if sig.entry_price <= 0:                       # битая строка в старой БД
            log.warning("Сигнал #%s без цены входа — закрываю.", sig.id)
            self.j.resolve(sig.id, "loss", price)
            return
        chg = (price / sig.entry_price - 1) * 100

        if price <= sig.sl_price:
            self.j.resolve(sig.id, "loss", price)
            if sig.delivered:
                await tg.send(f"🔴 <b>СТОП #{sig.id} {name}</b>\n"
                              f"${price:.8f} ({chg:+.1f}%) — выходим, "
                              f"не усредняемся.")
            return

        # tp_prices пуст, если в конфиге пустая лестница или строка битая:
        # раньше здесь был IndexError, ронявший весь цикл мониторинга
        if sig.tp_prices and price >= sig.tp_prices[0]:
            # первый тейк = сигнал считается отработавшим в плюс
            self.j.resolve(sig.id, "win", price)
            if sig.delivered:
                hit = sum(1 for p in sig.tp_prices if price >= p)
                await tg.send(
                    f"✅ <b>ТЕЙК {hit} #{sig.id} {name}</b>\n"
                    f"${price:.8f} ({chg:+.1f}%)\n"
                    f"Фиксируй часть, стоп переставь в безубыток.")
            return

        max_age_h = self.cfg["radar"]["max_signal_age_h"]
        if (time.time() - sig.ts) / 3600 > max_age_h:
            self.j.resolve(sig.id, "win" if chg > 0 else "loss", price)
            if sig.delivered:
                await tg.send(f"⏳ <b>#{sig.id} {name}</b>: {max_age_h:g} ч без "
                              f"движения к цели ({chg:+.1f}%). Закрываю по времени.")

    # ---------------------------------------------------------------- #
    #  команды
    # ---------------------------------------------------------------- #
    async def handle_commands(self, session, tg: Telegram) -> None:
        router = {
            "start": self.cmd_start, "help": self.cmd_help,
            "status": self.cmd_status, "open": self.cmd_open,
            "watch": self.cmd_watch, "stats": self.cmd_stats,
            "history": self.cmd_history, "wallets": self.cmd_wallets,
            "settings": self.cmd_settings,
            "addwallet": self.cmd_addwallet,
            "delwallet": self.cmd_delwallet,
            "pause": self.cmd_pause, "resume": self.cmd_resume,
        }
        for u in await tg.poll():
            msg = u.get("message") or u.get("channel_post") or {}
            text = (msg.get("text") or "").strip()
            chat = str((msg.get("chat") or {}).get("id") or "")
            if not text.startswith("/"):
                continue
            # Имя бота публично, и написать ему может кто угодно. Без этой
            # проверки любой посторонний вычитывал /stats и /open — то есть
            # позиции и статистику владельца.
            if chat != tg.chat_id:
                log.warning("Команда %r из чужого чата %s — игнорирую", text, chat)
                continue

            # "/open@my_bot что-то" -> "open". В группах Telegram сам дописывает
            # имя бота, и сравнение по целой строке такую команду не узнавало.
            head, _, arg = text.partition(" ")
            cmd = head[1:].split("@")[0].lower()
            handler = router.get(cmd)
            if handler is None:
                await tg.send(f"Не знаю команду /{esc(cmd)}.\n\n" + help_text(), chat)
                continue
            try:
                await handler(session, tg, chat, arg)
            except asyncio.CancelledError:
                raise
            except Exception as e:                              # noqa: BLE001
                # Одна кривая команда не должна ронять итерацию радара:
                # иначе из-за /open с битой строкой в БД встал бы весь скан.
                log.error("Команда /%s не отработала: %s: %s", cmd, type(e).__name__, e)
                log.debug("traceback", exc_info=True)
                await tg.send(f"Команда /{esc(cmd)} сорвалась: "
                              f"{esc(type(e).__name__)}. Подробности в логе.", chat)

    # ---- справка ---------------------------------------------------- #
    async def cmd_start(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        r = self.cfg["radar"]
        await tg.send(
            "🛰 <b>Memkoin Radar</b>\n\n"
            "Слежу за кошельками в Solana и пишу, когда несколько из них "
            "заходят в один токен одновременно.\n\n"
            f"Под наблюдением <b>{len(self.wallets)}</b> кошельков, "
            f"проверка каждые {r['poll_interval_sec']} с.\n\n"
            "Это следование за умными деньгами, а не предсказание роста: "
            "сигнал приходит <i>после</i> того, как они зашли. Решение за тобой.\n\n"
            + help_text(), chat)

    async def cmd_help(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        await tg.send(help_text(), chat)

    # ---- что происходит --------------------------------------------- #
    async def cmd_status(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        up = (time.time() - self.started) / 3600
        wr, n = self.j.hit_rate()
        rk = self.cfg["risk"]
        await tg.send(
            "📡 <b>Состояние радара</b>\n\n"
            f"Аптайм: <b>{up:.1f} ч</b>\n"
            f"Кошельков: <b>{len(self.wallets)}</b>\n"
            f"Проверено кандидатов: <b>{self.checked}</b>\n"
            f"Токенов копится в окне: <b>{len(self.buys)}</b>\n"
            f"Открытых сигналов: <b>{len(self.j.list_open())}</b>\n"
            f"Закрытых исходов: <b>{n}</b>"
            + (f" · winrate {wr*100:.0f}%" if n else " — статистики ещё нет") + "\n"
            f"Гейт: <b>{esc(rk['gate_mode'])}</b>, цель {rk['target_hit_rate']*100:.0f}%\n"
            f"Сигналы: <b>{'приглушены — /resume' if self.muted else 'включены'}</b>",
            chat)

    async def cmd_open(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        rows = self.j.list_open()
        if not rows:
            await tg.send("Открытых сигналов нет.", chat)
            return
        lines = [f"📂 <b>Открытых сигналов: {len(rows)}</b>"]
        for s in rows[:OPEN_LIMIT]:
            price = await ts.price_usd(session, s.mint)
            age_h = (time.time() - s.ts) / 3600
            if price > 0 and s.entry_price > 0:
                now = f"${price:.8f} ({(price / s.entry_price - 1) * 100:+.1f}%)"
            else:
                now = "цена недоступна"
            peak = (s.peak_price / s.entry_price - 1) * 100 if s.entry_price > 0 else 0.0
            lines.append(
                f"\n<b>#{s.id} {esc(s.symbol)}</b> · {age_h:.1f} ч в позиции\n"
                f"вход ${s.entry_price:.8f} → {now}\n"
                f"пик {peak:+.0f}% · стоп ${s.sl_price:.8f}")
        if len(rows) > OPEN_LIMIT:
            lines.append(f"\n…и ещё {len(rows) - OPEN_LIMIT}")
        await tg.send("\n".join(lines), chat)

    async def cmd_watch(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        """Что набирает конфлюэнс, но сигналом ещё не стало.

        Самая частая претензия к такому боту — «он молчит, он сломан».
        Здесь видно, что он считает прямо сейчас.
        """
        r = self.cfg["radar"]
        need_n, need_sol = r["confluence_wallets"], r["min_combined_buy_sol"]
        rows = []
        for mint in list(self.buys):
            n, sol, _ = self._confluence(mint)
            if n:
                rows.append((n, sol, mint))
        if not rows:
            await tg.send(
                "Пока пусто: ни один токен не набирает конфлюэнс.\n\n"
                f"Для сигнала нужно <b>{need_n}+</b> разных кошельков и "
                f"<b>{need_sol}+</b> SOL за {r['confluence_window_min']} мин.\n"
                "Это нормальное состояние: такие совпадения редки.", chat)
            return
        rows.sort(reverse=True)
        lines = [f"👀 <b>На подходе</b> (нужно {need_n} кошельков и {need_sol} SOL)"]
        for n, sol, mint in rows[:WATCH_LIMIT]:
            mark = "🔥" if n >= need_n and sol >= need_sol else "·"
            lines.append(f"{mark} <code>{esc(mint[:8])}…{esc(mint[-4:])}</code> — "
                         f"{n} кош. · {sol:.2f} SOL")
        if len(rows) > WATCH_LIMIT:
            lines.append(f"…и ещё {len(rows) - WATCH_LIMIT}")
        await tg.send("\n".join(lines), chat)

    # ---- результаты -------------------------------------------------- #
    async def cmd_stats(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        await tg.send("📊 <b>Замеренная статистика</b>\n<pre>"
                      + esc(self.j.summary()) + "</pre>", chat)

    async def cmd_history(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        rows = self.j.list_closed(HISTORY_LIMIT)
        if not rows:
            await tg.send("Закрытых сигналов пока нет.", chat)
            return
        lines = [f"📜 <b>Последние закрытые: {len(rows)}</b>"]
        for r in rows:
            icon = "✅" if r["status"] == "win" else "🔴"
            rm = f"{r['r_multiple']:+.2f}R" if r["r_multiple"] is not None else "—"
            when = time.strftime("%d.%m %H:%M", time.localtime(r["exit_ts"] or 0))
            lines.append(f"{icon} <b>#{r['id']} {esc(r['symbol'] or '?')}</b> · "
                         f"{rm} · {when}")
        lines.append("\nR — прибыль в размерах риска. +2R значит вдвое больше, "
                     "чем стояло на стопе.")
        await tg.send("\n".join(lines), chat)

    # ---- настройки ---------------------------------------------------- #
    async def cmd_wallets(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        lines = [f"👛 <b>Отслеживаю {len(self.wallets)} кошельков</b>\n"]
        lines += [f"<code>{esc(w)}</code>" for w in self.wallets[:WALLET_LIMIT]]
        if len(self.wallets) > WALLET_LIMIT:
            lines.append(f"…и ещё {len(self.wallets) - WALLET_LIMIT}")
        await tg.send("\n".join(lines), chat)

    async def cmd_addwallet(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        addr = arg.strip().split()[0] if arg.strip() else ""
        if not addr:
            await tg.send("Нужен адрес: <code>/addwallet 6S8Gez…ajKC</code>", chat)
            return
        if not is_solana_address(addr):
            # Опечатку Helius не считает ошибкой — просто вернёт пустой список.
            # Кошелёк молча не следился бы, и понять это было бы нельзя.
            await tg.send(f"<code>{esc(addr)}</code> не похож на адрес Solana "
                          f"(нужны 32 байта в base58). Проверь, не потерялся ли символ.",
                          chat)
            return
        if addr in self.wallets:
            await tg.send("Такой кошелёк уже отслеживается.", chat)
            return

        ok, note = self._persist_wallet(addr, add=True)
        if not ok:
            await tg.send(note, chat)
            return
        self.wallets.append(addr)
        await tg.send(f"✅ Добавлен.\n<code>{esc(addr)}</code>\n"
                      f"Теперь отслеживаю <b>{len(self.wallets)}</b>.\n\n{note}", chat)

    async def cmd_delwallet(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        addr = arg.strip().split()[0] if arg.strip() else ""
        if not addr:
            await tg.send("Нужен адрес: <code>/delwallet 6S8Gez…ajKC</code>\n"
                          "Список — /wallets", chat)
            return
        if addr not in self.wallets:
            await tg.send("Такого кошелька в списке нет. Проверь /wallets", chat)
            return

        ok, note = self._persist_wallet(addr, add=False)
        if not ok:
            await tg.send(note, chat)
            return
        self.wallets.remove(addr)
        await tg.send(f"🗑 Убран.\n<code>{esc(addr)}</code>\n"
                      f"Осталось <b>{len(self.wallets)}</b>.\n\n{note}", chat)

    def _persist_wallet(self, addr: str, add: bool) -> tuple[bool, str]:
        """Правим файл со списком. Возвращаем (получилось, что сказать человеку)."""
        path = self.wallets_path
        if not path:
            return True, "⚠️ Только до перезапуска: файл со списком неизвестен."
        if path.endswith(".json"):
            # qualified.json собирает анализатор, руками его править бессмысленно:
            # следующий прогон wallet_analyzer всё перезапишет.
            return False, ("Список собран анализатором в qualified.json — "
                           "правка вручную пропадёт при следующем прогоне.\n"
                           "Добавляй адреса в wallets.txt и запускай "
                           "wallet_analyzer.py.")
        try:
            if add:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{addr}\n")
            else:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                with open(path, "w", encoding="utf-8") as f:
                    # сверяем адрес до комментария: discover.py пишет
                    # "<адрес>  # ранних входов: N", и строка не равна адресу
                    f.writelines(ln for ln in lines
                                 if ln.split("#", 1)[0].strip() != addr)
        except OSError as e:
            return False, f"Не смог записать {esc(path)}: {esc(e)}"
        return True, f"Записано в {esc(path)} — переживёт перезапуск."

    async def cmd_settings(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        r, s, rk = self.cfg["radar"], self.cfg["safety"], self.cfg["risk"]
        ladder = " / ".join(f"+{p:g}%" for p in rk["take_profit_ladder_pct"])
        await tg.send(
            f"⚙️ <b>Текущие пороги</b> · профиль "
            f"<b>{esc(self.cfg.get('profile') or 'без профиля')}</b>\n\n"
            "<b>Условие сигнала</b>\n"
            f"кошельков: {r['confluence_wallets']}+ за {r['confluence_window_min']} мин\n"
            f"объём покупок: {r['min_combined_buy_sol']}+ SOL\n"
            f"повтор по токену: не чаще {r['cooldown_min']} мин\n\n"
            "<b>Проверка токена</b>\n"
            f"ликвидность: от ${s['min_liquidity_usd']:,}\n"
            f"FDV: до ${s['max_fdv_usd']:,}\n"
            f"топ-10 холдеров: до {s['max_top10_pct']}%\n\n"
            "<b>Риск</b>\n"
            f"стоп: −{rk['stop_loss_pct']:g}%\n"
            f"тейки: {ladder}\n"
            f"риск на сделку: {rk['risk_per_trade_pct']:g}% "
            f"от {rk['account_size_sol']:g} SOL\n"
            f"гейт: {esc(rk['gate_mode'])}, цель {rk['target_hit_rate']*100:.0f}%\n\n"
            "Правятся в config.yaml, после правки нужен перезапуск.\n"
            "Строже — profile: strict в начале файла.", chat)

    async def cmd_pause(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        self.muted = True
        await tg.send("🔇 Сигналы приглушены.\n\n"
                      "Радар продолжает работать и писать сигналы в журнал — "
                      "замер winrate не прервётся, вы просто их не увидите.\n"
                      "Вернуть: /resume", chat)

    async def cmd_resume(self, session, tg: Telegram, chat: str, arg: str = "") -> None:
        self.muted = False
        await tg.send("🔔 Сигналы снова приходят.", chat)


# --------------------------------------------------------------------------- #
async def main_loop(cfg: dict, wallets: list[str], db: str,
                    wallets_path: str | None = None):
    journal = SignalJournal(db)
    radar = Radar(cfg, wallets, journal, wallets_path)
    tick = 0

    try:
        async with make_session() as session:
            tg = Telegram(session, cfg["telegram"]["bot_token"],
                          str(cfg["telegram"]["chat_id"]))
            # Без этой строки здоровый радар не печатал вообще ничего: все
            # логи были уровня WARNING, а сетевая ошибка всплывает только
            # через четыре ретрая с бэкоффом. Со стороны — намертво зависший
            # процесс, хотя он просто ждёт ответа.
            log.info("Радар запущен: кошельков %d, опрос каждые %d с, чат %s",
                     len(wallets), cfg["radar"]["poll_interval_sec"], tg.chat_id)

            if not await tg.register_commands():
                log.warning("Меню команд не зарегистрировалось — команды всё равно "
                            "работают, просто их не будет в списке у кнопки меню.")

            if await tg.send(
                    f"🛰 Радар запущен.\nКошельков: {len(wallets)}\n"
                    f"Условие сигнала: {cfg['radar']['confluence_wallets']}+ кошелька, "
                    f"{cfg['radar']['min_combined_buy_sol']}+ SOL за "
                    f"{cfg['radar']['confluence_window_min']} мин\n"
                    f"Гейт: {cfg['risk']['gate_mode']}, цель "
                    f"{cfg['risk']['target_hit_rate']*100:.0f}%"):
                log.info("Приветствие доставлено — канал до чата работает.")
            else:
                log.error("Приветствие НЕ доставлено в чат %s. Сигналы тоже не "
                          "дойдут. Причину покажет: python doctor.py", tg.chat_id)

            # «Жив» примерно раз в 10 минут — независимо от того, какой
            # интервал опроса стоит в конфиге.
            beat = max(1, 600 // max(1, int(cfg["radar"]["poll_interval_sec"])))

            while True:
                try:
                    await radar.handle_commands(session, tg)
                    await radar.scan_wallets(session, tg)
                    await radar.monitor_open(session, tg)
                    tick += 1
                    if tick % beat == 0:
                        log.info("Жив: циклов %d, открытых сигналов %d, "
                                 "проверено кандидатов %d",
                                 tick, len(journal.list_open()), radar.checked)
                    if tick % 200 == 0:
                        journal.prune_seen()
                        radar.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception as e:                          # noqa: BLE001
                    log.error("Сбой итерации: %s: %s", type(e).__name__, e)
                    log.debug("traceback", exc_info=True)
                await asyncio.sleep(cfg["radar"]["poll_interval_sec"])
    finally:
        journal.close()


def load_wallets(path: str) -> list[str]:
    """Понимает и qualified.json от анализатора, и обычный список адресов."""
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            data = json.load(f)
            if not isinstance(data, dict) or "qualified" not in data:
                raise SystemExit(
                    f"{path}: ожидался вывод wallet_analyzer.py с ключом "
                    f'"qualified". Для простого списка адресов используй .txt')
            return [m["wallet"] for m in data["qualified"]
                    if isinstance(m, dict) and m.get("wallet")]
        # адрес идёт до комментария: discover.py пишет "<адрес>  # ранних входов: N"
        return [w for w in (ln.split("#", 1)[0].strip() for ln in f) if w]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--wallets", default="qualified.json")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--db", default="signals.db")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", help="дублировать лог в файл (когда консоли не видно)")
    args = p.parse_args()

    setup_logging(args.verbose, args.log_file)

    # Всё, что до этого падало через SystemExit, писало причину в stderr мимо
    # логов: в --log-file не попадала как раз самая нужная строка — почему
    # радар не взлетел. Для запуска двойным кликом это означало пустой файл.
    try:
        conf = load_config(args.config)
        require_config(conf, "telegram.bot_token", "telegram.chat_id",
                       "rpc.helius_api_key")
        ws = load_wallets(args.wallets)
        if not ws:
            raise SystemExit("Список кошельков пуст. Сначала прогони "
                             "wallet_analyzer.py — радар без проверенных "
                             "кошельков бесполезен.")
    except SystemExit as e:
        log.error("Запуск прерван. %s", e)
        raise
    except OSError as e:
        log.error("Запуск прерван: %s", e)
        raise SystemExit(1) from e

    try:
        asyncio.run(main_loop(conf, ws, args.db, args.wallets))
    except KeyboardInterrupt:
        log.info("Остановлено вручную.")


if __name__ == "__main__":
    main()
