@echo off
chcp 65001 >nul
title Обновление продакшна · Creative UGC CRM
cd /d "%~dp0"
".\venv\Scripts\python.exe" deploy_prod.py
echo.
pause
