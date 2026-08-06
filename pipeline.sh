#!/usr/bin/env bash
# pipeline.sh — весь путь одной командой. Запускать из папки с проектом:
#
#     bash pipeline.sh
#
# По шагам: зависимости → проверка связи → поиск кандидатов по mints.txt
# → фильтр кошельков → выбор списка → запуск радара.
# Занимает 10–30 минут, дольше всего идёт поиск кандидатов.
#
# Работает и на сервере, и на своей машине: если рядом есть systemd-сервис
# radar, скрипт перезапустит его; если нет — просто запустит бота в этом окне.
#
# Прервать можно в любой момент (Ctrl+C): готовые файлы остаются, повторный
# запуск продолжит с того же места. Шаги пропускаются флагами:
#     --skip-discover   не искать кандидатов заново
#     --skip-analyze    не перепроверять кошельки

set -euo pipefail

# Папка проекта — та, где лежит сам скрипт. Так его можно звать откуда угодно
# и не привязываться к /opt/memkoin-radar.
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

ENV_FILE="$APP_DIR/.env"
SERVICE=radar

# Глубина листания истории на каждый минт, страниц по 1000 транзакций.
# 25 (умолчание discover.py) хватает только совсем свежим токенам: у ходовых
# история длиннее, до первых покупок не долистать, и «ранние покупатели»
# окажутся не ранними. Переопределяется: DISCOVER_PAGES=400 bash pipeline.sh
DISCOVER_PAGES="${DISCOVER_PAGES:-200}"

SKIP_DISCOVER=0
SKIP_ANALYZE=0
for a in "$@"; do
    case "$a" in
        --skip-discover) SKIP_DISCOVER=1 ;;
        --skip-analyze)  SKIP_ANALYZE=1 ;;
        *) echo "Неизвестный флаг: $a"; exit 1 ;;
    esac
done

step() { echo; echo "=============== $* ==============="; }
die()  { echo; echo "ОСТАНОВЛЕНО: $*"; exit 1; }

# --- чем запускать и от кого -------------------------------------------- #
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    PY="$APP_DIR/.venv/bin/python"
else
    PY="$(command -v python3 || command -v python || true)"
    [ -n "$PY" ] || die "Python не найден. Поставь его с python.org"
fi

# На сервере файлы принадлежат пользователю radar, и создавать их от root
# нельзя: сервис потом не сможет их перезаписать.
RUN=""
if [ "$(id -u)" -eq 0 ] && id -u radar >/dev/null 2>&1; then
    RUN="sudo -u radar"
fi

HAVE_SERVICE=0
if command -v systemctl >/dev/null 2>&1 \
   && systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1 \
   && [ -f "/etc/systemd/system/$SERVICE.service" ]; then
    HAVE_SERVICE=1
fi

# --- 1. зависимости ------------------------------------------------------ #
step "1/5  Зависимости"
if [ ! -f "$ENV_FILE" ]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    die "создан .env — впиши в него ключи и запусти снова:
    nano $ENV_FILE"
fi
grep -q "^TELEGRAM_BOT_TOKEN=." "$ENV_FILE" \
    || die "в $ENV_FILE не заполнен TELEGRAM_BOT_TOKEN. Открой: nano $ENV_FILE"
# Ставим от того же пользователя, что потом запускает бота: pip от root
# оставил бы в venv файлы с чужим владельцем.
$RUN "$PY" -m pip install -q -r requirements.txt || die "не установить зависимости"
echo "готово"

# --- 2. связь ------------------------------------------------------------ #
step "2/5  Проверяю связь"
$RUN "$PY" doctor.py || die "диагностика не прошла (подробности выше и в doctor.log).
Пока связи нет, остальные шаги смысла не имеют."

# --- 3. кандидаты -------------------------------------------------------- #
if [ "$SKIP_DISCOVER" -eq 0 ]; then
    step "3/5  Ищу ранних покупателей (глубина $DISCOVER_PAGES стр., это долго)"
    $RUN "$PY" discover.py --mints mints.txt --out candidates.txt --min-hits 2 \
        --max-pages "$DISCOVER_PAGES" \
        || die "discover.py не отработал. candidates.txt не изменён."
else
    step "3/5  Поиск кандидатов пропущен"
fi
# Считаем адреса, а не размер файла: discover.py всегда пишет строку-заголовок,
# поэтому файл без единого кандидата всё равно непустой, проверка проходила,
# и спотыкался уже анализатор — с сообщением не про то.
CANDS=$(grep -c '^[^#[:space:]]' candidates.txt 2>/dev/null || echo 0)
if [ "$CANDS" -eq 0 ]; then
    die "кандидатов не найдено.

Смотри предупреждения discover.py выше. Если там «история длиннее N стр.» —
до первых покупок он не долистал, и найденные адреса ранними не являются.
Что делать, по возрастанию усилий:
  1) взять токены посвежее (дни, а не месяцы) — у них история короче;
  2) добавить минтов в mints.txt: чем больше, тем выше шанс пересечений;
  3) листать глубже — медленнее, но добирается:
       DISCOVER_PAGES=$((DISCOVER_PAGES * 3)) bash pipeline.sh"
fi
echo "кандидатов: $CANDS"

# --- 4. фильтр ----------------------------------------------------------- #
if [ "$SKIP_ANALYZE" -eq 0 ]; then
    step "4/5  Фильтрую кошельки"
    $RUN "$PY" wallet_analyzer.py --wallets candidates.txt --out qualified.json \
        || die "wallet_analyzer.py не отработал. qualified.json не изменён."
else
    step "4/5  Фильтрация пропущена"
fi

GOOD=$("$PY" - "$APP_DIR/qualified.json" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(len(json.load(f).get("qualified") or []))
except Exception:
    print(0)
PY
)
echo "прошли фильтр: $GOOD"

set_env() {
    "$PY" - "$ENV_FILE" "$1" "$2" <<'PY'
import pathlib, re, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
text = p.read_text(encoding="utf-8")
if re.search(rf"(?m)^{re.escape(key)}=", text):
    text = re.sub(rf"(?m)^{re.escape(key)}=.*$", f"{key}={val}", text)
else:
    text = text.rstrip("\n") + f"\n{key}={val}\n"
p.write_text(text, encoding="utf-8")
PY
    if [ -n "$RUN" ]; then
        chown radar:radar "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"
}

if [ "$GOOD" -gt 0 ]; then
    set_env WALLETS qualified.json
    echo "радар переключён на qualified.json"
else
    # Пустой список — рабочее состояние фильтра, а не сбой. Но стартовать с
    # ним нельзя: радар откажется, и это выглядело бы поломкой.
    set_env WALLETS wallets.txt
    echo "фильтр не пропустил никого — остаёмся на wallets.txt (непроверенный список)."
    echo "Чтобы это изменить: добавь минтов в mints.txt и прогони снова."
fi

# --- 5. запуск ----------------------------------------------------------- #
step "5/5  Запускаю радар"
if [ "$HAVE_SERVICE" -eq 1 ]; then
    systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    systemctl restart "$SERVICE"
    sleep 5
    systemctl is-active --quiet "$SERVICE" \
        || die "сервис не поднялся. Смотри: journalctl -u $SERVICE -n 50"
    echo
    echo "Радар работает и поднимется сам после перезагрузки сервера."
    echo "  systemctl status $SERVICE     # состояние"
    echo "  journalctl -u $SERVICE -f     # живой лог, выйти Ctrl+C"
    echo
    journalctl -u "$SERVICE" -n 15 --no-pager
else
    echo "systemd-сервиса нет — запускаю в этом окне."
    echo "Радар работает, пока окно открыто. Остановить: Ctrl+C"
    echo "Для круглосуточной работы: sudo bash deploy/setup.sh на сервере."
    echo
    exec "$PY" radar_bot.py --log-file radar.log
fi
