@echo off
REM ============================================================================
REM  AI Autopilot - Windows launcher
REM    run.bat            : install deps on first run (if needed), then start
REM    run.bat install    : (re)create the venv and install dependencies only
REM ============================================================================
setlocal enableextensions
cd /d "%~dp0"

REM --- locate a Python launcher (prefer the 'py' launcher) -------------------
set "PYLAUNCH="
py -3 --version >nul 2>&1 && set "PYLAUNCH=py -3"
if not defined PYLAUNCH (
    python --version >nul 2>&1 && set "PYLAUNCH=python"
)
if not defined PYLAUNCH (
    echo [ERROR] Python 3.11+ not found. Install it from https://www.python.org/downloads/
    exit /b 1
)

set "VENV_PY=.venv\Scripts\python.exe"

REM --- decide whether to (re)install ----------------------------------------
set "DO_SETUP="
if /i "%~1"=="install" set "DO_SETUP=1"
if not exist "%VENV_PY%" set "DO_SETUP=1"

if defined DO_SETUP (
    echo [setup] Creating virtual environment in .venv ...
    %PYLAUNCH% -m venv .venv || ( echo [ERROR] Failed to create venv & exit /b 1 )
    echo [setup] Upgrading pip ...
    "%VENV_PY%" -m pip install --upgrade pip
    echo [setup] Installing dependencies (this can take a minute) ...
    "%VENV_PY%" -m pip install -e ".[dev]" || ( echo [ERROR] Dependency install failed & exit /b 1 )
    echo [setup] Dependencies installed.
)

REM --- ensure a config.yaml exists ------------------------------------------
if not exist "config.yaml" (
    if exist "config.example.yaml" (
        echo [setup] Creating config.yaml from config.example.yaml ...
        copy /y "config.example.yaml" "config.yaml" >nul
        echo [setup] Edit config.yaml before running for real.
    )
)

REM --- environment checks ----------------------------------------------------
if "%ANTHROPIC_API_KEY%"=="" (
    echo [warn] ANTHROPIC_API_KEY is not set - Claude calls will fail.
    echo        Set it with:  setx ANTHROPIC_API_KEY "sk-ant-..."  (then reopen the terminal)
)
where claude >nul 2>&1 || echo [warn] 'claude' CLI not found. Install with: npm install -g @anthropic-ai/claude-code

REM --- if this was an install-only run, stop here ---------------------------
if /i "%~1"=="install" (
    echo [done] Install complete. Run "run.bat" to start the service.
    exit /b 0
)

echo.
echo [run] Starting AI Autopilot  ^|  dashboard: http://localhost:5080/dashboard
echo [run] Press Ctrl+C to stop.
echo.
"%VENV_PY%" -m ai_autopilot

endlocal
