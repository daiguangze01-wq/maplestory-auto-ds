@echo off
rem ============================================================
rem MapleStory Auto DS launcher
rem
rem Usage:
rem   run.bat                 start normally
rem   run.bat --debug         verbose logging
rem   run.bat --disable_viz   no detection preview window
rem
rem Prerequisite: the game is already running and in a map, and the
rem window title matches config/config_custom.yaml -> game_window.title
rem ============================================================
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [WARN] .venv not found, falling back to system Python.
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" src\main.py %*
pause
