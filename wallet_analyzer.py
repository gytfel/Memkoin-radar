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
import logging
import os
import re
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field

import aiohttp
import yaml

log = logging.getLogger("radar")

HELIUS_TX = "https://api.helius.xyz/v0/addresses/{addr}/transactions"


class FetchError(RuntimeError):
    """Данные не получены — это не то же самое, что «данных нет».

    Разница принципиальная: по «нет данных» выносится вердикт, по «не
    получили» выносить нечего. Раньше оба случая выглядели пустым списком.
    """

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
def setup_logging(verbose: bool = False, log_file: str | None = None) -> None:
    """Единая настройка логов. Раньше сетевые ошибки глотались молча.

    log_file дублирует всё в файл. Нужен, когда консоли фактически нет:
    скрипт запустили двойным кликом и окно закрылось, или он крутится
    в фоне. Без файла разбираться потом не с чем.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", handlers=handlers)
    if log_file:
        log.info("Лог пишется в %s", os.path.abspath(log_file))


def load_dotenv(path: str) -> None:
    """Подтягиваем ключи из .env.

    Раньше файл читал только шелл: без `set -a && source .env && set +a` токен
    не доезжал до процесса, и запуск падал на require_config, хотя значения
    были вписаны. Уже заданное окружение приоритетнее файла — переменную,
    выставленную в шелле или в systemd-юните, молча перетирать нельзя.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return                      # .env необязателен: ключи можно задать и извне

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # хвостовой комментарий: "KEY=значение  # пояснение".
            # Только у неэкранированного значения — внутри кавычек # легален.
            value = value.split(" #", 1)[0].rstrip()
        # не setdefault: `cp .env.example .env` + source экспортирует пустые
        # строки, и они бы навсегда заслонили реальные значения из файла
        if not os.environ.get(key):
            os.environ[key] = value


def _deep_merge(base: dict, over: dict) -> dict:
    """Накладываем профиль на базовые значения, не теряя незаданные ключи."""
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def apply_profile(cfg: dict, path: str = "config.yaml") -> dict:
    """Профиль — набор порогов «строгий/обычный» поверх базовых.

    Смысл в том, что строгость это один осознанный выбор, а не десяток
    разрозненных чисел: править их по одному — верный способ получить
    несогласованный конфиг вроде «трое кошельков, но ликвидность 15k».
    """
    profiles = cfg.pop("profiles", None) or {}
    name = str(cfg.get("profile") or "").strip()
    if not name:
        return cfg
    if name not in profiles:
        raise SystemExit(f"{path}: профиль {name!r} не описан. "
                         f"Доступны: {', '.join(profiles) or '—'}")
    over = profiles[name]
    if not isinstance(over, dict):
        raise SystemExit(f"{path}: профиль {name!r} должен быть словарём")
    return _deep_merge(cfg, over)


def load_config(path: str = "config.yaml") -> dict:
    # .env ищем и рядом с конфигом, и в рабочем каталоге: конфиг могут вынести
    # в /etc или передать через --config, а ключи оставить там, откуда
    # запускают. Первый найденный выигрывает — load_dotenv не перетирает
    # уже заданное.
    for candidate in (os.path.join(os.path.dirname(os.path.abspath(path)), ".env"),
                      os.path.join(os.getcwd(), ".env")):
        load_dotenv(candidate)

    with open(path, "r", encoding="utf-8") as f:
        raw = os.path.expandvars(f.read())
    cfg = yaml.safe_load(raw)
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path}: ожидался YAML-словарь, получено {type(cfg).__name__}")
    return apply_profile(cfg, path)


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

ENV_HINT = ("\nКлючи берутся из .env рядом с config.yaml (или из окружения):\n"
            "    cp .env.example .env   # вписать значения и запускать как обычно")


def _dig(cfg: dict, dotted: str):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def require_config(cfg: dict, *dotted_paths: str) -> None:
    """Падаем сразу и понятно, если ключей нет.

    Без этой проверки `${HELIUS_API_KEY}` уезжал в URL как литерал, все запросы
    возвращали 4xx, а бот молча крутился вхолостую.
    """
    problems: list[str] = []
    for path in dotted_paths:
        value = _dig(cfg, path)
        if value is None or value == "":
            problems.append(f"{path}: не задан в config.yaml")
            continue
        for var in _PLACEHOLDER.findall(str(value)):
            problems.append(f"{path}: переменная окружения {var} не установлена")
    if problems:
        raise SystemExit("Конфиг не готов:\n  " + "\n  ".join(problems) + "\n" + ENV_HINT)


B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_solana_address(value: str) -> bool:
    """Адрес Solana — ровно 32 байта в base58.

    Проверка нужна на входе от человека: опечатка в адресе не даёт ошибки,
    Helius на неё просто отвечает пустым списком. Кошелёк молча не следится,
    и понять это можно только по отсутствию сигналов.
    """
    if not 32 <= len(value) <= 44:
        return False
    num = 0
    for ch in value:
        idx = B58_ALPHABET.find(ch)
        if idx < 0:
            return False
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, "big")
    pad = len(value) - len(value.lstrip("1"))
    return len(body) + pad == 32


def _safe_proxy(proxy: str) -> str:
    """Прячем логин и пароль: прокси часто выдают с ними в адресе."""
    return re.sub(r"://[^@/]+@", "://***@", proxy)


def make_session(**kw) -> aiohttp.ClientSession:
    """Сессия, умеющая ходить через прокси.

    Обычный aiohttp.ClientSession игнорирует HTTPS_PROXY: переменную нужно
    разрешить явным trust_env. Из-за этого в сетях, где api.telegram.org
    закрыт провайдером, бот молчал даже с настроенным системным прокси —
    Helius при этом отвечал, и выглядело это как поломка именно бота.

    Адрес берётся из HTTPS_PROXY, её можно положить прямо в .env.
    """
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    if not proxy:
        return aiohttp.ClientSession(trust_env=True, **kw)

    if proxy.lower().startswith("socks"):
        # aiohttp сам socks не умеет — нужен отдельный коннектор
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            raise SystemExit(
                f"HTTPS_PROXY={_safe_proxy(proxy)} — это socks-прокси, "
                f"для него нужен дополнительный пакет:\n"
                f"    pip install aiohttp-socks") from None
        log.info("Работаю через socks-прокси %s", _safe_proxy(proxy))
        return aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy), **kw)

    log.info("Работаю через прокси %s", _safe_proxy(proxy))
    return aiohttp.ClientSession(trust_env=True, **kw)


def _safe_url(url: str) -> str:
    """Прячем секреты: они попадают и в api-key, и в путь телеграм-бота."""
    url = re.sub(r"(api-key=)[^&]+", r"\1***", url)
    return re.sub(r"/bot[^/]+/", "/bot***/", url)


async def _request(session: aiohttp.ClientSession, method: str, url: str, *,
                   params=None, payload=None, retries: int = 4, timeout: int = 30):
    """HTTP с бэкоффом. Возвращает None вместо исключения — радар не должен падать."""
    last = "неизвестно"
    for attempt in range(retries):
        try:
            async with session.request(
                    method, url, params=params, json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 429:
                    last = "HTTP 429 (rate limit)"
                    if attempt < retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue
                if r.status >= 400:
                    body = (await r.text())[:200]
                    log.warning("%s %s → HTTP %s %s", method, _safe_url(url), r.status, body)
                    return None
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < retries - 1:
                await asyncio.sleep(1.5 ** attempt)
    log.warning("%s %s не удался за %d попыток (%s)",
                method, _safe_url(url), retries, last)
    return None


async def get_json(session: aiohttp.ClientSession, url: str, params=None,
                   retries: int = 4, timeout: int = 30):
    return await _request(session, "GET", url, params=params,
                          retries=retries, timeout=timeout)


async def post_json(session: aiohttp.ClientSession, url: str, payload: dict,
                    retries: int = 4, timeout: int = 30):
    return await _request(session, "POST", url, payload=payload,
                          retries=retries, timeout=timeout)


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
    pages = cfg["analyzer"]["max_tx_pages"] if pages is None else pages

    url = HELIUS_TX.format(addr=wallet)
    before, out = None, []

    for page in range(max(1, pages)):
        params = {"api-key": key, "limit": limit, "type": "SWAP"}
        if before:
            params["before"] = before
        data = await get_json(session, url, params)

        # Первая страница особая: если её не получили, про кошелёк не известно
        # НИЧЕГО. Раньше здесь возвращался пустой список — неотличимый от
        # «кошелёк не торговал», и анализатор выносил уверенный вердикт
        # «сделок 0, отклонён» по нулю данных. Неверный ключ Helius давал
        # ровно ту же картину.
        if page == 0 and data is None:
            raise FetchError(f"Helius не ответил по {wallet[:8]}")
        if page == 0 and isinstance(data, dict):
            raise FetchError(f"Helius отклонил запрос по {wallet[:8]}: "
                             f"{data.get('error') or data}")

        # Обрыв на середине истории — не то же самое: часть данных уже есть,
        # дальше просто нечего листать.
        if isinstance(data, dict):
            log.warning("Helius вернул ошибку для %s: %s",
                        wallet[:8], data.get("error") or data)
            break
        if not isinstance(data, list) or not data:
            break

        for tx in data:
            if not isinstance(tx, dict):
                continue
            s = parse_swap(tx, wallet, sol_usd)
            if s:
                out.append(s)

        last = data[-1] if isinstance(data[-1], dict) else {}
        before = last.get("signature")
        oldest = int(last.get("timestamp") or 0)
        if not before or len(data) < limit or (until_ts and oldest < until_ts):
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
    unchecked: list[str] = []

    async with make_session() as session:
        async def worker(w: str):
            async with sem:
                try:
                    m = await analyze_wallet(session, w, cfg)
                except FetchError as e:
                    # Вердикт по кошельку, о котором ничего не известно, —
                    # это дезинформация: выглядит как «плохой кошелёк».
                    unchecked.append(w)
                    log.warning("Кошелёк %s не проверен: %s", w[:8], e)
                    print(f"⚠️  {w[:6]}..{w[-4:]}  НЕ ПРОВЕРЕН: {e}")
                    return
                except Exception as e:                      # noqa: BLE001
                    log.warning("Кошелёк %s не проанализирован: %s: %s",
                                w[:8], type(e).__name__, e)
                    log.debug("traceback", exc_info=True)
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

    # Пустой результат по нулю данных нельзя записывать в файл: он затрёт
    # рабочий qualified.json, и радар останется без кошельков из-за обрыва
    # связи, а не из-за качества кошельков.
    if not results:
        raise SystemExit(
            f"\nНи один кошелёк не проверен — данные не получены "
            f"({len(unchecked)} из {len(wallets)}).\n"
            f"{out_path} не тронут: пустой список затёр бы рабочий.\n"
            f"Причину покажет: python doctor.py")

    results.sort(key=lambda m: m.score, reverse=True)
    good = [m for m in results if qualifies(m, cfg["analyzer"])[0]]

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": int(time.time()),
                   "qualified": [asdict(m) for m in good],
                   "all": [asdict(m) for m in results],
                   "unchecked": unchecked}, f, indent=2, ensure_ascii=False)

    print(f"\nГодных кошельков: {len(good)} из {len(results)} проверенных → {out_path}")
    if unchecked:
        print(f"⚠️  Не проверено {len(unchecked)} из {len(wallets)}: данные не "
              f"получены. Это не вердикт — прогони заново, когда сеть вернётся.")
    if not good:
        print("Это нормальный результат. Большинство «топовых» кошельков "
              "не проходят фильтр на expectancy и на скрытые мешки.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wallets", help="файл с адресами, по одному в строке")
    p.add_argument("--wallet", action="append", default=[], help="адрес (можно несколько)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--out", default="qualified.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    require_config(cfg, "rpc.helius_api_key")

    addrs = list(args.wallet)
    if args.wallets:
        with open(args.wallets, encoding="utf-8") as f:
            # у кандидатов из discover.py адрес идёт до комментария "# ранних входов: N"
            addrs += [ln.split("#", 1)[0].strip() for ln in f
                      if ln.strip() and not ln.lstrip().startswith("#")]
    addrs = [a for a in dict.fromkeys(addrs) if a]
    if not addrs:
        sys.exit("Нужен --wallet или --wallets")

    asyncio.run(run(addrs, cfg, args.out))


if __name__ == "__main__":
    main()
