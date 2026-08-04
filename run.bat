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
cd /d "%~dp0"

set PY=
where python >nul 2>&1 && set PY=python
if "%PY%"=="" (where py >nul 2>&1 && set PY=py)
if "%PY%"=="" (
    echo Python не найден. Поставь его с python.org
    echo ВАЖНО: при установке отметь галочку "Add Python to PATH".
    goto :end
)
%PY% --version

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Создан .env - впиши в него три ключа и запусти снова:
    echo   HELIUS_API_KEY     - helius.dev
    echo   TELEGRAM_BOT_TOKEN - @BotFather
    echo   TELEGRAM_CHAT_ID   - @userinfobot
    goto :end
)

echo Ставлю зависимости...
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Не удалось установить зависимости.
    goto :end
)

echo.
echo === Диагностика ===
%PY% doctor.py
if errorlevel 1 (
    echo.
    echo Радар не запущен: сначала устрани проблемы выше.
    echo Отчёт целиком лежит в doctor.log
    goto :end
)

echo.
echo === Радар работает. Ctrl+C чтобы остановить ===
%PY% radar_bot.py --wallets wallets.txt --log-file radar.log

:end
echo.
pause
