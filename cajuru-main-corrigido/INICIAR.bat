@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Cajuru A1 v3
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1

if not exist "cajuru_a1\__init__.py" (
  echo [ERRO] Versao v3 incompleta: falta cajuru_a1\__init__.py.
  echo Baixe uma copia limpa do repositorio atualizado.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Rode INSTALAR.bat ou INSTALAR_POWERSHELL.ps1 primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import cajuru_a1; assert cajuru_a1.__version__ == '3.2.1'"
if errorlevel 1 (
  echo [ERRO] O pacote Cajuru A1 v3 nao esta instalado corretamente.
  echo Execute o instalador novamente.
  pause
  exit /b 1
)
echo Escopo protegido: somente a pasta CERTIFICADOS selecionada sera lida.
".venv\Scripts\python.exe" -m cajuru_a1 --gui
if errorlevel 1 pause
