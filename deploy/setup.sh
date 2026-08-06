#!/usr/bin/env bash
# setup.sh — установка радара на сервер (Ubuntu/Debian).
#
# Запускать на VPS от root:
#     bash setup.sh
#
# Скрипт ставит зависимости, кладёт код в /opt/memkoin-radar, заводит
# отдельного пользователя без прав входа и регистрирует systemd-сервис.
# Сам бот НЕ запускается: сначала нужно вписать ключи в .env.
#
# Повторный запуск безопасен — он обновляет код и оставляет .env как есть.

set -euo pipefail

APP_DIR=/opt/memkoin-radar
REPO=https://github.com/gytfel/Memkoin-radar.git
BRANCH=claude/telegram-bot-launch-usbos3
SERVICE=radar

if [ "$(id -u)" -ne 0 ]; then
    echo "Нужны права root. Запусти: sudo bash setup.sh"
    exit 1
fi

echo "1/5 Ставлю системные пакеты..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# tmux нужен не для красоты: pipeline.sh идёт десятки минут, а обрыв ssh
# убивает его вместе с сеансом — на середине поиска кандидатов это обидно.
apt-get install -y -qq python3 python3-venv python3-pip git tmux

echo "2/5 Забираю код в $APP_DIR..."
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
    git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "3/5 Собираю окружение Python..."
# venv, а не системный pip: системный python в Ubuntu ломать нельзя,
# да и apt его пакеты обновляет когда захочет.
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "4/5 Готовлю пользователя и .env..."
# Отдельный пользователь без shell: у бота нет причин уметь логиниться.
id -u radar >/dev/null 2>&1 || \
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin radar

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    NEED_KEYS=1
fi
chmod 600 "$APP_DIR/.env"          # в файле токен: чужим читать нечего
chown -R radar:radar "$APP_DIR"

echo "5/5 Регистрирую сервис..."
install -m 644 "$APP_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload

echo
echo "==========================================================="
if [ "${NEED_KEYS:-0}" = "1" ]; then
    echo "Осталось вписать ключи:"
    echo "    nano $APP_DIR/.env"
    echo
    echo "Затем проверить и запустить:"
else
    echo "Код обновлён, .env не тронут. Проверить и перезапустить:"
fi
echo "    cd $APP_DIR && sudo bash pipeline.sh"
echo
echo "pipeline.sh делает всё сам: проверку связи, поиск кандидатов,"
echo "фильтр кошельков и запуск сервиса."
echo
echo "Дальше пригодится:"
echo "    systemctl status $SERVICE      # работает ли"
echo "    journalctl -u $SERVICE -f      # живой лог"
echo "    systemctl restart $SERVICE     # после правки .env"
echo "==========================================================="
