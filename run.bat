@echo off
REM ===========================================================================
REM  AI Autopilot - Windows launcher
REM    run.bat           : install deps on first run if needed, then start
REM    run.bat install   : (re)create the venv and install dependencies only
REM ===========================================================================
setlocal enableextensions
cd /d "%~dp0"

REM --- locate a Python launcher (prefer the 'py' launcher) -------------------
set "PYLAUNCH="
py -3 --version >nul 2>&1 && set "PYLAUNCH=py -3"
if defined PYLAUNCH goto have_python
python --version >nul 2>&1 && set "PYLAUNCH=python"
:have_python
if not defined PYLAUNCH goto no_python

set "VENV_PY=.venv\Scripts\python.exe"

REM --- decide whether to (re)install ----------------------------------------
set "DO_SETUP="
if /i "%~1"=="install" set "DO_SETUP=1"
if not exist "%VENV_PY%" set "DO_SETUP=1"
if not defined DO_SETUP goto after_setup

echo [setup] Creating virtual environment in .venv ...
%PYLAUNCH% -m venv .venv
if errorlevel 1 goto fail_venv
echo [setup] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip
echo [setup] Installing dependencies - this can take a minute ...
"%VENV_PY%" -m pip install -e ".[dev]"
if errorlevel 1 goto fail_pip
echo [setup] Dependencies installed.
:after_setup

REM --- ensure a config.yaml exists ------------------------------------------
if exist "config.yaml" goto have_config
if not exist "config.example.yaml" goto have_config
echo [setup] Creating config.yaml from config.example.yaml ...
copy /y "config.example.yaml" "config.yaml" >nul
echo [setup] Edit config.yaml before running for real.
:have_config

REM --- environment checks ----------------------------------------------------
if not "%ANTHROPIC_API_KEY%"=="" goto have_key
echo [warn] ANTHROPIC_API_KEY is not set - Claude calls will fail.
echo        Set it with:  setx ANTHROPIC_API_KEY "sk-ant-..."  then reopen the terminal
:have_key
where claude >nul 2>&1 && goto have_claude
echo [warn] 'claude' CLI not found. Install with: npm install -g @anthropic-ai/claude-code
:have_claude

REM --- if this was an install-only run, stop here ---------------------------
if /i not "%~1"=="install" goto run
echo [done] Install complete. Run run.bat to start the service.
goto end

:run
echo.
echo [run] Starting AI Autopilot - dashboard at http://localhost:5080/dashboard
echo [run] Press Ctrl+C to stop.
echo.
"%VENV_PY%" -m ai_autopilot
goto end

:no_python
echo [ERROR] Python 3.11+ not found. Install it from https://www.python.org/downloads/
exit /b 1

:fail_venv
echo [ERROR] Failed to create the virtual environment.
exit /b 1

:fail_pip
echo [ERROR] Dependency install failed. See the messages above.
exit /b 1

:end
endlocal
