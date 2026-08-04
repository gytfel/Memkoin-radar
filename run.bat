@echo off
rem run.bat - запуск радара одной командой (Windows).
rem
rem Кнопка Start в Telegram только отправляет боту сообщение. Читать его
rem должен процесс radar_bot.py на компьютере - пока он не запущен, бот
rem молчит, и это не поломка.
rem
rem Запускать двойным кликом. Окно НЕ закроется само: в конце стоит pause,
rem иначе при любой ошибке текст исчезал бы вместе с окном.

chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Проверяем Python запуском, а не через where. Причина: в Windows 10/11
rem есть заглушка python.exe, которая существует в PATH, но вместо
rem интерпретатора открывает Microsoft Store. where её находит, запуск
rem проваливается - и человек видит магазин вместо бота.
rem Лаунчер py надёжнее, поэтому пробуем его первым.
set PY=
py -3 -c "import sys" >nul 2>&1 && set PY=py -3
if not defined PY (python -c "import sys" >nul 2>&1 && set PY=python)
if not defined PY (
    echo Рабочий Python не найден.
    echo.
    echo Поставь его с https://www.python.org/downloads/
    echo ВАЖНО: на первом экране установщика отметь галочку
    echo "Add python.exe to PATH", иначе запуск не увидит Python.
    echo После установки закрой это окно и запусти run.bat снова.
    goto :end
)
%PY% --version

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Создан файл .env - открой его Блокнотом и впиши три ключа:
    echo   HELIUS_API_KEY     - helius.dev
    echo   TELEGRAM_BOT_TOKEN - @BotFather
    echo   TELEGRAM_CHAT_ID   - @userinfobot
    echo.
    echo Потом запусти run.bat снова.
    goto :end
)

echo Ставлю зависимости...
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Не удалось установить зависимости. Проверь интернет.
    goto :end
)

echo.
echo === Диагностика ===
%PY% doctor.py
if errorlevel 1 (
    echo.
    echo Радар не запущен: сначала устрани проблемы выше.
    echo Отчёт целиком лежит в файле doctor.log
    goto :end
)

echo.
echo === Радар работает. Не закрывай это окно ===
echo === Остановить: Ctrl+C ===
%PY% radar_bot.py --wallets wallets.txt --log-file radar.log

:end
echo.
pause
