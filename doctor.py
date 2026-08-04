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

import aiohttp

from radar_bot import TG, load_wallets
from wallet_analyzer import HELIUS_TX, load_config

YES, NO, HMM = "  ✔  ", "  ✘  ", "  !  "

_problems: list[str] = []


def say(mark: str, line: str, fix: str = "") -> None:
    print(f"{mark}{line}")
    for row in fix.split("\n") if fix else ():
        print(f"       → {row}")
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
async def check_telegram(session, token: str, chat_id: str) -> None:
    print("\n== Telegram ==")

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
        say(NO, f"до api.telegram.org не достучались ({body})",
            "Проверь интернет, прокси и файрвол.\n"
            "В некоторых сетях Telegram API закрыт — нужен VPN или другой хост.")
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
    print("\n== Helius ==")
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
    print("== Конфиг ==")
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

    async with aiohttp.ClientSession() as session:
        if token and not token.startswith("${"):
            await check_telegram(session, token, chat)
        if key and not key.startswith("${"):
            await check_helius(session, key, wallet)

    print()
    if _problems:
        print(f"Найдено проблем: {len(_problems)}. Пока они не устранены, "
              f"радар будет молчать.")
        for p in _problems:
            print(f"  · {p}")
        return 1
    print("Всё в порядке. Запуск:")
    print(f"    python wallet_analyzer.py --wallets {args.wallets} --out qualified.json")
    print("    python radar_bot.py --wallets qualified.json")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--wallets", default="wallets.txt")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
