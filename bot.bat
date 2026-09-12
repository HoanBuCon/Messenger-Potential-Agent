@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Messenger AI Agent - Kuchiba Chisa Dashboard
echo [*] Dang khoi dong Giao Dien Messenger AI Agent...
"%~dp0venv\Scripts\python.exe" "%~dp0gui_app.py"
pause
