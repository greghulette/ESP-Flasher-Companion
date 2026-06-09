#!/bin/bash
# Launch ESP Flasher Companion (macOS).
# FIRST TIME: chmod +x esp_flasher_companion.command
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then exec python3 esp_flasher_companion.py
elif command -v python >/dev/null 2>&1; then exec python esp_flasher_companion.py
else echo "[X] Python 3 not found."; read -r -p "Press Enter to close..." _; fi
