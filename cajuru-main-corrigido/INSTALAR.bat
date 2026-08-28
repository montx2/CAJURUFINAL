@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Cajuru A1 - instalacao
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1

echo.
echo  ============================================
echo   Cajuru A1 v3 - instalacao verificada
echo   O Dropbox NAO sera alterado.
echo  ============================================
echo.

if not exist "cajuru_a1\__init__.py" (
  echo [ERRO] Esta pasta nao contem a versao v3 completa.
  echo Falta: cajuru_a1\__init__.py
  echo Baixe uma copia limpa do repositorio atualizado.
  pause
  exit /b 1
)
if not exist "pyproject.toml" (
  echo [ERRO] pyproject.toml ausente. Pacote incompleto.
  pause
  exit /b 1
)

where py >nul 2>&1
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if %errorlevel%==0 (
    set "PY=python"
  ) else (
    echo [ERRO] Python 3 nao encontrado. Instale em https://www.python.org/downloads/
    echo Marque a opcao Add Python to PATH.
    pause
    exit /b 1
  )
)

echo [1/4] Criando ou atualizando ambiente .venv ...
%PY% -m venv .venv
if errorlevel 1 goto :venv_error
set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :venv_error
"%VENV_PY%" --version
if errorlevel 1 goto :venv_error

echo [2/4] Instalando bibliotecas e o pacote Cajuru A1 ...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :pip_error
"%VENV_PY%" -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto :pip_error
"%VENV_PY%" -m pip install --editable . --no-deps
if errorlevel 1 goto :package_error
"%VENV_PY%" -c "import pathlib, cajuru_a1; p=pathlib.Path(cajuru_a1.__file__).resolve(); print('Pacote:', p); assert cajuru_a1.__version__ == '3.2.3'"
if errorlevel 1 goto :package_error

echo [3/4] Instalando Chromium do Playwright (Jettax) ...
"%VENV_PY%" -m playwright install chromium
if errorlevel 1 (
  echo [ERRO] Nao foi possivel instalar o Chromium do Playwright.
  pause
  exit /b 1
)

echo [4/4] Verificando dependencias ...
"%VENV_PY%" -m pip check
if errorlevel 1 (
  echo [ERRO] Existem dependencias incompativeis no ambiente.
  pause
  exit /b 1
)

if not exist config.yaml copy config.example.yaml config.yaml >nul
echo.
echo  ============================================
echo   Instalacao v3 OK.
echo  ============================================
echo  Selecione diretamente CERTIFICADOS ou CERTIFICADOS A1 no config.yaml.
echo  A raiz do Dropbox nao e aceita.
echo  Depois execute INICIAR.bat ou INICIAR_POWERSHELL.ps1.
echo.
pause
exit /b 0

:venv_error
echo [ERRO] Falha ao criar o ambiente .venv.
pause
exit /b 1
:pip_error
echo [ERRO] Falha ao instalar as bibliotecas de requirements.txt.
pause
exit /b 1
:package_error
echo [ERRO] O pacote cajuru_a1 v3 nao pode ser instalado/importado.
pause
exit /b 1
