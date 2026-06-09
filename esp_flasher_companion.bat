@echo off
REM Launch ESP Flasher Companion (no console window if pythonw exists).
set "HERE=%~dp0"
where pythonw >nul 2>&1
if not errorlevel 1 (
  start "" pythonw "%HERE%esp_flasher_companion.py"
  exit /b
)
where python >nul 2>&1
if not errorlevel 1 (
  start "" python "%HERE%esp_flasher_companion.py"
  exit /b
)
echo [X] Python not found. Install Python 3 from https://www.python.org/downloads/
pause
