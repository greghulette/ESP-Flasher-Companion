#!/bin/bash
# Launch ESP Flasher Companion FROM SOURCE (macOS).
# Needs Python 3 + "pip install pyserial esptool". For a build that needs no
# Python at all, use scripts/build-macos.command instead.
# FIRST TIME ONLY:  chmod +x scripts/run-macos.command
cd "$(dirname "$0")/../src"
if command -v python3 >/dev/null 2>&1; then exec python3 esp_flasher_companion.py
elif command -v python >/dev/null 2>&1; then exec python esp_flasher_companion.py
else echo "[X] Python 3 not found."; read -r -p "Press Enter to close..." _; fi
