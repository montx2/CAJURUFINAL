@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Ambiente virtual nao encontrado. Execute INSTALAR.bat primeiro.
    pause
    exit /b 1
)
.venv\Scripts\python.exe run.py gui
if errorlevel 1 pause
