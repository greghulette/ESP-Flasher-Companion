#!/bin/bash
# ============================================================
#  Build ESP Flasher Companion into a standalone macOS .app.
#  The result needs NO Python on the user's machine.
#
#  Run this ON A MAC — PyInstaller cannot cross-compile
#  (a Windows .exe must be built on Windows; see build-windows.bat).
#  First time only:  chmod +x scripts/build-macos.command
# ============================================================
set -e
# Work from the repo root (this script lives in scripts/).
cd "$(dirname "$0")/.."

PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[X] python3 not found. Install from https://www.python.org/downloads/"
  read -r -p "Press Enter to close..." _ ; exit 1
fi

echo "[*] Installing/updating build dependencies..."
"$PY" -m pip install --quiet --upgrade pyinstaller esptool pyserial

echo "[*] Building windowed .app bundle..."
"$PY" -m PyInstaller --noconfirm --clean \
  --windowed \
  --name "ESP Flasher Companion" \
  --collect-all esptool \
  --collect-all serial \
  src/esp_flasher_companion.py

APP="dist/ESP Flasher Companion.app"
BIN="$APP/Contents/MacOS/ESP Flasher Companion"

echo "[*] Self-test (verifies esptool + flasher stubs are bundled)..."
if "$BIN" --selftest; then
  echo "[OK] esptool + stubs bundled"
else
  echo "[X] Self-test failed (exit $?) - the bundle is missing esptool data."
  exit 1
fi

echo
echo "[DONE] $APP"
echo "       Zip it (keep symlinks: 'ditto -c -k --keepParent') and share it."
echo "       Unsigned app: first launch = right-click -> Open, or System"
echo "       Settings -> Privacy & Security -> Open Anyway."
