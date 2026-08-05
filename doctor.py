"""doctor.py — проверка окружения перед запуском радара.

Нужен, потому что «бот не работает» — это десяток разных причин с одинаковым
симптомом: тишина в чате. Радар специально не падает на сетевых ошибках
(иначе он умирал бы от любого таймаута Helius), и из-за этого молчание бота
выглядит одинаково и при отозванном токене, и при неоткрытом диалоге,
и при второй запущенной копии.

Здесь наоборот: каждая проверка идёт отдельно и говорит, что именно
сломано и что с этим делать.

Запуск:
    python doctor.py
    python doctor.py --wallets wallets.txt
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re

import aiohttp

from radar_bot import TG, load_wallets
from wallet_analyzer import HELIUS_TX, _safe_proxy, load_config, make_session

YES, NO, HMM = "  ✔  ", "  ✘  ", "  !  "

_problems: list[str] = []
_report: list[str] = []


def out(line: str = "") -> None:
    """Печатаем и одновременно копим отчёт.

    Скрипт часто запускают двойным кликом: окно закрывается вместе с
    выводом, и показать результат становится нечем. Поэтому отчёт всегда
    ложится ещё и в файл.
    """
    print(line)
    _report.append(line)


def say(mark: str, line: str, fix: str = "") -> None:
    out(f"{mark}{line}")
    for row in fix.split("\n") if fix else ():
        out(f"       → {row}")
    # считаем провал провалом даже без готового рецепта: иначе неизвестная
    # ошибка тихо выпадала из итога, и отчёт врал, что всё почти хорошо
    if mark == NO:
        _problems.append(line)


async def _probe(session, method: str, url: str, **kw):
    """Возвращает (код, тело) или (None, причина).

    Тело Telegram отдаёт вместе с описанием ошибки, и именно описание
    отличает «неоткрытый диалог» от «неверного chat_id» — оба дают HTTP 400.
    """
    try:
        async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=20),
                                   **kw) as r:
            try:
                return r.status, await r.json(content_type=None)
            except (ValueError, aiohttp.ClientError):
                return r.status, (await r.text())[:200]
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return None, f"{type(e).__name__}: {e}"


def _desc(body) -> str:
    if isinstance(body, dict):
        return str(body.get("description") or body.get("error") or body)[:200]
    return str(body)[:200]


# --------------------------------------------------------------------------- #
async def check_proxy(proxy: str) -> bool:
    """Жив ли сам прокси.

    Без этой проверки мёртвый прокси выглядел как недоступный Telegram:
    соединение обрывается одинаково, и совет получался про блокировки,
    хотя чинить надо было адрес в HTTPS_PROXY. Особенно легко нарваться
    на 127.0.0.1 — клиент выключили, а строка осталась.
    """
    out("\n== Прокси ==")
    m = re.match(r"^\w+://(?:[^@/]+@)?\[?([^\]:/]+)\]?(?::(\d+))?", proxy)
    if not m:
        say(NO, f"не разбирается адрес {_safe_proxy(proxy)}",
            "Ожидается вид: http://адрес:порт (или socks5://адрес:порт)")
        return False

    host, port = m.group(1), int(m.group(2) or (443 if "https://" in proxy else 80))
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
        writer.close()
    except (OSError, asyncio.TimeoutError) as e:
        local = host in ("127.0.0.1", "localhost", "::1")
        say(NO, f"прокси {host}:{port} не отвечает ({type(e).__name__})",
            ("Порт на твоём же компьютере, значит программа-прокси не запущена.\n"
             "Запусти VPN-клиент, который его поднимает, — или убери\n"
             "строку HTTPS_PROXY из .env, если прокси больше нет."
             if local else
             "Проверь адрес и порт в HTTPS_PROXY, а также что прокси ещё жив."))
        return False

    say(YES, f"прокси {host}:{port} отвечает")
    return True


# --------------------------------------------------------------------------- #
async def check_telegram(session, token: str, chat_id: str) -> None:
    out("\n== Telegram ==")

    # ID бота — это часть токена до двоеточия. Если chat_id совпал с ним,
    # человек принял бота за адресата: сам себе бот написать не может.
    bot_id = token.split(":", 1)[0]
    if chat_id == bot_id:
        say(NO, f"chat_id {chat_id} — это ID самого бота, а не твой",
            "Свой ID узнай у @userinfobot и впиши в .env:\n"
            "TELEGRAM_CHAT_ID=<число из @userinfobot>")
        return

    code, body = await _probe(session, "GET", TG.format(token=token, method="getMe"))
    if code is None:
        # Отказ на уровне соединения, а не ответ Telegram. Если при этом
        # Helius отвечает, интернет цел и закрыт именно api.telegram.org —
        # у российских провайдеров это норма, а не поломка бота.
        say(NO, f"до api.telegram.org не достучались ({body})",
            "Похоже, провайдер закрыл доступ к Telegram API.\n"
            "Варианты, по возрастанию надёжности:\n"
            "1) включить VPN на всём компьютере и перезапустить радар;\n"
            "2) прописать прокси в .env — тогда VPN не нужен:\n"
            "     HTTPS_PROXY=http://логин:пароль@адрес:порт\n"
            "     socks5 тоже годится, но к нему: pip install aiohttp-socks\n"
            "3) арендовать VPS за границей и запускать радар там —\n"
            "   он всё равно должен работать круглосуточно.")
        return
    if code == 401:
        say(NO, "токен отклонён (401 Unauthorized)",
            "Токен неверный или отозван. Возьми новый у @BotFather:\n"
            "/mybots → выбрать бота → API Token → и вписать в .env")
        return
    if code != 200 or not isinstance(body, dict) or not body.get("ok"):
        say(NO, f"getMe вернул {code}: {_desc(body)}")
        return
    say(YES, f"токен принят, бот @{body['result'].get('username')}")

    # Webhook и getUpdates взаимоисключающи: при живом webhook опрос
    # молча возвращает пустоту, и бот не видит вообще ни одной команды.
    code, body = await _probe(session, "GET",
                              TG.format(token=token, method="getWebhookInfo"))
    hook = (body or {}).get("result", {}).get("url") if isinstance(body, dict) else None
    if hook:
        say(NO, f"установлен webhook: {hook}",
            "Радар читает команды через getUpdates, с webhook это несовместимо:\n"
            f"curl {TG.format(token='<ТОКЕН>', method='deleteWebhook')}")
    else:
        say(YES, "webhook не установлен, опрос свободен")

    code, body = await _probe(session, "GET", TG.format(token=token, method="getUpdates"),
                              params={"timeout": 0, "limit": 1})
    if code == 409:
        say(NO, "конфликт: бота уже опрашивает другой процесс",
            "Запущена вторая копия радара — останови лишнюю.\n"
            "Две копии на одном токене воруют друг у друга апдейты.")

    # Главная проверка: доходит ли сообщение. Бот не может написать первым,
    # пока человек не открыл диалог, — самая частая причина «бот молчит».
    code, body = await _probe(
        session, "POST", TG.format(token=token, method="sendMessage"),
        json={"chat_id": chat_id, "text": "🩺 doctor.py: канал до чата работает."})
    if code == 200:
        say(YES, f"тестовое сообщение доставлено в чат {chat_id}")
        return

    detail = _desc(body)
    if code == 400 and "chat not found" in detail.lower():
        say(NO, f"чат {chat_id} не найден",
            "Либо ID чужой, либо диалог с ботом не открыт.\n"
            "Открой бота в Telegram и нажми Start, потом запусти doctor.py снова.\n"
            "Для группы: добавь бота в неё, ID группы начинается с -100.")
    elif code == 403:
        say(NO, "бот заблокирован пользователем",
            "Разблокируй бота в чате и нажми Start.")
    else:
        say(NO, f"sendMessage вернул {code}: {detail}")


# --------------------------------------------------------------------------- #
async def check_helius(session, key: str, wallet: str | None) -> None:
    out("\n== Helius ==")
    probe = wallet or "So11111111111111111111111111111111111111112"
    code, body = await _probe(session, "GET", HELIUS_TX.format(addr=probe),
                              params={"api-key": key, "limit": 1, "type": "SWAP"})
    if code is None:
        say(NO, f"до api.helius.xyz не достучались ({body})",
            "Проверь интернет, прокси и файрвол.")
    elif code in (401, 403):
        say(NO, f"ключ отклонён ({code}): {_desc(body)}",
            "Возьми ключ на helius.dev → Dashboard → API Keys, впиши HELIUS_API_KEY в .env")
    elif code == 429:
        say(HMM, "лимит запросов исчерпан (429)",
            "Бесплатный тариф закончился на сегодня — радар будет работать рвано.")
    elif code == 200:
        n = len(body) if isinstance(body, list) else 0
        say(YES, f"ключ принят, свопов на пробном кошельке: {n}")
        if isinstance(body, dict):
            say(NO, f"Helius ответил ошибкой: {_desc(body)}")
    else:
        say(NO, f"Helius вернул {code}: {_desc(body)}")


# --------------------------------------------------------------------------- #
def check_config(cfg: dict, path: str) -> tuple[str, str, str]:
    out("== Конфиг ==")
    token = str(cfg.get("telegram", {}).get("bot_token") or "")
    chat = str(cfg.get("telegram", {}).get("chat_id") or "")
    key = str(cfg.get("rpc", {}).get("helius_api_key") or "")

    for name, value, env in (("bot_token", token, "TELEGRAM_BOT_TOKEN"),
                             ("chat_id", chat, "TELEGRAM_CHAT_ID"),
                             ("helius_api_key", key, "HELIUS_API_KEY")):
        if not value:
            say(NO, f"{name} не задан", f"Впиши {env} в .env рядом с {path}")
        elif value.startswith("${"):
            say(NO, f"{name}: переменная {value} не подставилась",
                f"Значение {env} пустое — проверь .env")
        else:
            say(YES, f"{name} задан")

    # Показываем явно: иначе непонятно, идут запросы напрямую или через прокси,
    # и почему настроенный в системе прокси мог не примениться.
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    say(YES, f"прокси: {_safe_proxy(proxy)}" if proxy else "прокси не задан, идём напрямую")
    return token, chat, key


def check_wallets(path: str) -> str | None:
    try:
        ws = load_wallets(path)
    except FileNotFoundError:
        say(NO, f"{path} не найден",
            "Список кошельков: python discover.py ... или свой .txt с адресами")
        return None
    except SystemExit as e:
        say(NO, str(e))
        return None
    if not ws:
        say(NO, f"{path} пуст", "Радар без кошельков не даст ни одного сигнала.")
        return None
    say(YES, f"кошельков в {path}: {len(ws)}")
    return ws[0]


async def run(args) -> int:
    cfg = load_config(args.config)
    token, chat, key = check_config(cfg, args.config)
    wallet = check_wallets(args.wallets)

    # Прокси проверяем первым: если мёртв он, все остальные диагнозы будут
    # враньём — они спишут его отказ на блокировки и неверные ключи.
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    if proxy and not await check_proxy(proxy):
        out()
        out("Дальше проверять нечего: весь трафик идёт через нерабочий прокси.")
        return 1

    async with make_session() as session:
        if token and not token.startswith("${"):
            await check_telegram(session, token, chat)
        if key and not key.startswith("${"):
            await check_helius(session, key, wallet)

    out()
    if _problems:
        out(f"Найдено проблем: {len(_problems)}. Пока они не устранены, "
              f"радар будет молчать.")
        for p in _problems:
            out(f"  · {p}")
        return 1
    out("Всё в порядке. Запуск:")
    out(f"    python wallet_analyzer.py --wallets {args.wallets} --out qualified.json")
    out("    python radar_bot.py --wallets qualified.json")
    return 0


def save_report(path: str) -> None:
    """Отчёт нужен ровно тогда, когда его не видно на экране."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(_report) + "\n")
        print(f"\nОтчёт сохранён: {path}")
    except OSError as e:
        print(f"\nОтчёт не удалось сохранить в {path}: {e}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--wallets", default="wallets.txt")
    p.add_argument("--out", default="doctor.log", help="куда сохранить отчёт")
    args = p.parse_args()
    try:
        code = asyncio.run(run(args))
    finally:
        # и при падении самой диагностики: пустой лог хуже частичного
        save_report(args.out)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
