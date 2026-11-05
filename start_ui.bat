@echo off
setlocal EnableExtensions
rem facefind UI launcher. ASCII only, CRLF only. Do not add chcp here.
cd /d "%~dp0"
title facefind UI
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
echo Starting facefind local UI ...
echo.
set "PY_EXE="
set "PY_ARG="
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY_EXE=%ROOT%\.venv\Scripts\python.exe"
if defined PY_EXE goto run_ui
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto try_where
set "PY_EXE=py"
set "PY_ARG=-3"
goto run_ui
:try_where
for /f "delims=" %%I in ('where python 2^>nul') do if not defined PY_EXE set "PY_EXE=%%I"
if defined PY_EXE goto run_ui
echo.
echo [ERROR] Python 3 not found.
echo   Option 1: install Python 3.10+ and enable "Add python.exe to PATH".
echo   Option 2: run  py -3 -m venv .venv  in this folder.
echo   Then install deps:  python -m pip install -r requirements.txt
echo.
pause
exit /b 1
:run_ui
echo Python: "%PY_EXE%" %PY_ARG%
echo URL:    http://127.0.0.1:8765/
echo A tray icon appears after start: open / restart / quit the service from it.
echo The browser opens automatically when the server is ready.
echo Use  --no-tray  to run without the tray host (old mode).
echo.
"%PY_EXE%" %PY_ARG% -m facefind.supervisor %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto done
echo.
echo [ERROR] startup failed, exit code %RC%
echo   Check deps: "%PY_EXE%" %PY_ARG% -m pip install -r requirements.txt
echo.
pause
exit /b %RC%
:done
exit /b 0
