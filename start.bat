@echo off
chcp 65001 >nul
echo ========================================================
echo  Запуск Creative UGC CRM (Бот + Mini App)
echo ========================================================
echo.

IF NOT EXIST "venv\Scripts\activate.bat" (
    echo [1/3] Создание виртуального окружения...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo [2/3] Установка зависимостей...
    pip install -r requirements.txt
) ELSE (
    call venv\Scripts\activate.bat
)

echo [3/3] Запуск сервера и Telegram-бота...
echo.
echo Веб-панель доступна по адресу: http://localhost:8000
echo.
echo Для остановки нажмите Ctrl + C
echo ========================================================
python main.py
pause
