@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === Cajuru A1 - Instalador ===
where py >nul 2>nul
if errorlevel 1 (
    echo Python 3 nao encontrado. Instale o Python 3.11+ marcando "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Criando ambiente virtual...
    py -3 -m venv .venv
)
echo Instalando dependencias...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .

echo.
echo Instalacao concluida. Execute INICIAR.bat
pause
