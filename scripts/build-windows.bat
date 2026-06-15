@echo off
REM ============================================================
REM  Build ESP Flasher Companion into a standalone Windows .exe.
REM  The result needs NO Python on the user's machine.
REM
REM  Run this ON WINDOWS — PyInstaller cannot cross-compile
REM  (a macOS app must be built on a Mac; see build-macos.command).
REM ============================================================
setlocal
REM Work from the repo root (this script lives in scripts\).
cd /d "%~dp0.."

where python >nul 2>&1
if errorlevel 1 (
  echo [X] Python 3 not found. Install from https://www.python.org/downloads/
  pause & exit /b 1
)

echo [*] Installing/updating build dependencies...
python -m pip install --quiet --upgrade pyinstaller esptool pyserial
if errorlevel 1 ( echo [X] pip install failed & pause & exit /b 1 )

echo [*] Building single-file, windowed executable...
python -m PyInstaller --noconfirm --clean ^
  --onefile --windowed ^
  --name "ESP-Flasher-Companion" ^
  --icon src\WCB.ico ^
  --add-data "src\WCB.ico;." ^
  --add-data "src\WCB.png;." ^
  --distpath . --workpath build ^
  --collect-all esptool ^
  --collect-all serial ^
  src\esp_flasher_companion.py
if errorlevel 1 ( echo [X] PyInstaller build failed & pause & exit /b 1 )

echo [*] Self-test (verifies esptool + flasher stubs are bundled)...
start /wait "" "ESP-Flasher-Companion.exe" --selftest
if errorlevel 1 (
  echo [X] Self-test failed ^(exit %errorlevel%^) - the bundle is missing esptool data.
  pause & exit /b 1
)

echo.
echo [DONE] ESP-Flasher-Companion.exe  (in the repo root)
echo        Hand that single file to users - no Python required.
echo        ^(arduino-cli + the ESP32 core are still needed for the Build button.^)
pause
