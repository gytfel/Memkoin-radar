#!/usr/bin/env sh
# run.sh — запуск радара одной командой (Linux / macOS).
#
# Кнопка Start в Telegram только отправляет боту сообщение. Читать его
# должен процесс radar_bot.py, работающий на компьютере, — пока он не
# запущен, бот молчит, и это не поломка.
#
#     sh run.sh
#
# Скрипт проверяет Python, ставит зависимости, прогоняет диагностику и
# только потом поднимает радар. Остановка — Ctrl+C.

set -eu
cd "$(dirname "$0")"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python не найден. Поставь его с python.org и запусти снова."
    exit 1
fi
echo "Python: $("$PY" --version 2>&1)"

if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo "Создан .env — впиши в него три ключа и запусти скрипт снова:"
    echo "  HELIUS_API_KEY     — helius.dev"
    echo "  TELEGRAM_BOT_TOKEN — @BotFather"
    echo "  TELEGRAM_CHAT_ID   — @userinfobot"
    exit 1
fi

echo "Ставлю зависимости..."
"$PY" -m pip install -q -r requirements.txt

echo
echo "=== Диагностика ==="
if ! "$PY" doctor.py; then
    echo
    echo "Радар не запущен: сначала устрани проблемы выше."
    echo "Отчёт целиком лежит в doctor.log."
    exit 1
fi

echo
echo "=== Радар работает. Ctrl+C чтобы остановить ==="
exec "$PY" radar_bot.py --wallets wallets.txt --log-file radar.log
