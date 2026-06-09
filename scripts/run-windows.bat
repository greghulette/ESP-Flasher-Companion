@echo off
REM Launch ESP Flasher Companion FROM SOURCE (no console window if pythonw exists).
REM Needs Python 3 + "pip install pyserial esptool". For a build that needs no
REM Python at all, use scripts\build-windows.bat instead.
set "SRC=%~dp0..\src\esp_flasher_companion.py"
where pythonw >nul 2>&1
if not errorlevel 1 (
  start "" pythonw "%SRC%"
  exit /b
)
where python >nul 2>&1
if not errorlevel 1 (
  start "" python "%SRC%"
  exit /b
)
echo [X] Python not found. Install Python 3 from https://www.python.org/downloads/
pause
