@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem facefind portable launcher (ASCII only, no chcp).
rem First run: create .venv, install deps, download models, create config.yaml.
cd /d "%~dp0"
title facefind
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\.venv\Scripts\python.exe"
set "BASE_PY="

echo Project: %ROOT%
echo.

rem ---- 1) locate Python 3.10+ ----
py -3 -c "import sys; assert sys.version_info[:2] >= (3,10)" >nul 2>nul
if not errorlevel 1 set "BASE_PY=py -3"
if not defined BASE_PY (
  python -c "import sys; assert sys.version_info[:2] >= (3,10)" >nul 2>nul
  if not errorlevel 1 set "BASE_PY=python"
)
if not defined BASE_PY (
  echo [ERROR] Python 3.10+ not found.
  echo   Install Python 3.11 from https://www.python.org/downloads/
  echo   and enable "Add python.exe to PATH", or run: py -3 -m venv .venv
  pause
  exit /b 1
)

rem ---- 2) create venv on first run ----
if not exist "%VENV%" (
  echo [setup] creating .venv ...
  %BASE_PY% -m venv "%ROOT%\.venv"
  if errorlevel 1 goto :fail
  echo [setup] installing dependencies ...
  "%VENV%" -m pip install --upgrade pip
  "%VENV%" -m pip install -r "%ROOT%\requirements.txt"
  if errorlevel 1 goto :fail
  where nvidia-smi >nul 2>nul
  if not errorlevel 1 (
    rem choose cu124 / cu128 by the CUDA version reported by nvidia-smi
    set "GPU_REQ=%ROOT%\requirements-gpu-cu124.txt"
    set "CUDAVLINE="
    for /f "tokens=*" %%L in ('nvidia-smi 2^>nul ^| findstr /C:"CUDA Version"') do set "CUDAVLINE=%%L"
    if defined CUDAVLINE (
      set "CUDAV=!CUDAVLINE:*CUDA Version:=!"
      for /f "tokens=1" %%v in ("!CUDAV!") do set "CUDAV=%%v"
      for /f "tokens=1,2 delims=." %%a in ("!CUDAV!") do (
        set "CUMAJ=%%a"
        set "CUMIN=%%b"
      )
      if not defined CUMIN set "CUMIN=0"
      if defined CUMAJ if !CUMAJ! GEQ 13 set "GPU_REQ=%ROOT%\requirements-gpu-cu128.txt"
      if defined CUMAJ if !CUMAJ! EQU 12 if !CUMIN! GEQ 8 set "GPU_REQ=%ROOT%\requirements-gpu-cu128.txt"
      echo [setup] CUDA Version !CUDAV! detected.
    )
    echo [setup] NVIDIA GPU detected, installing GPU extras from "!GPU_REQ!" ...
    "%VENV%" -m pip uninstall -y onnxruntime >nul 2>nul
    "%VENV%" -m pip install -r "!GPU_REQ!"
    if errorlevel 1 echo [WARN] GPU extras failed. Face detection will fall back to CPU.
  )
)

rem ---- 3) config.yaml ----
if not exist "%ROOT%\config.yaml" (
  copy "%ROOT%\config.example.yaml" "%ROOT%\config.yaml" >nul
  echo [setup] config.yaml created from config.example.yaml.
)

rem ---- 4) models ----
if not exist "%ROOT%\models\insightface\models\buffalo_l" (
  echo [setup] downloading InsightFace buffalo_l ...
  "%VENV%" "%ROOT%\scripts\fetch_models.py" --insightface buffalo_l --sr base
  if errorlevel 1 echo [WARN] model download failed. Run scripts\fetch_models.py manually.
)

echo.
echo Starting facefind (tray host). URL: http://127.0.0.1:8765/
echo Close from the tray menu, or run with --no-tray to use the old mode.
echo.
"%VENV%" -m facefind.supervisor %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
echo.
echo [ERROR] startup failed, exit code %RC%
echo   Check deps: "%VENV%" -m pip install -r "%ROOT%\requirements.txt"
pause
exit /b %RC%

:fail
echo.
echo [ERROR] setup failed.
echo   Try manually: py -3 -m venv .venv
echo                 .venv\Scripts\python -m pip install -r requirements.txt
pause
exit /b 1
