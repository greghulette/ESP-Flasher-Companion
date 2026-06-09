# ESP Flasher Companion

A one-window desktop app for building and flashing **multiple ESP32 / ESP32-S3
Arduino projects** — no Arduino IDE round-trips, no command lines, no hunting
through file explorer for the right script.

Built with plain Python + tkinter. **No GUI framework dependencies.**

## What it does

- **A card per project/board.** Each card has its own remembered serial port —
  with several boards connected you set each port once and just push buttons,
  no switching a shared dropdown.
- **⚡ Build + Flash** — compiles the sketch with `arduino-cli` (verbose,
  IDE-style output streaming live) and flashes **only the app** at `0x10000`,
  leaving the bootloader, partition table, and NVS (saved settings) untouched.
- **Restore Bootloader** — re-flashes a custom bootloader at `0x0` *after* an
  Arduino IDE upload (the IDE overwrites the bootloader every time). **Guarded**:
  it reads the chip type and the flash chip's real size first and refuses to
  flash on any mismatch — a bootloader built for the wrong flash size silently
  corrupts NVS (settings stop persisting), so the guard makes that impossible.
- **Identify** — chip type, real flash size, PSRAM, MAC for whatever is on the
  selected port.
- **➕ Add Board / ✕ Remove / 📁 Change…** — manage projects entirely from the
  UI. Adding a board is: pick the sketch folder, tick the USB-CDC / OPI-PSRAM
  boxes if the project needs them, done. Everything persists in a JSON config.
- **Claude Usage tab** — if you use [Claude Code](https://claude.com/claude-code),
  shows your last 7 days of token usage per model, parsed from local session
  logs (exact token counts + rough cost estimate).

## The key trick: libraries resolve exactly like the IDE

The app runs `arduino-cli` with **the Arduino IDE's own config file**
(`~/.arduinoIDE/arduino-cli.yaml`), so the sketchbook and libraries resolve
*identically* to the IDE. If a sketch builds in your IDE, it builds here — no
duplicate library installs, no version drift.

## Requirements

- Python 3.8+ (tkinter included in the standard installer)
- `pip install pyserial esptool`
- `arduino-cli` on PATH (or the app finds a local copy in
  `%LOCALAPPDATA%\wcb-build-tools`) with your ESP32 core installed

## Run it

- **Windows:** double-click `esp_flasher_companion.bat`
- **macOS:** `chmod +x esp_flasher_companion.command` once, then double-click
- or just: `python esp_flasher_companion.py`

On first run it creates `esp_flasher_config.json` next to the script
(auto-detecting known projects if present). All paths/boards/ports live there;
the file is gitignored because it's per-machine.

## FQBN gotchas (read this if a board misbehaves after flashing)

A target's `fqbn` must mirror **every non-default setting in the IDE's Tools
menu** for that sketch, not just the board name. Two real-world examples that
cost us debugging time:

- A project using the S3's **native USB** for serial needs
  `USBMode=hwcdc,CDCOnBoot=cdc` — without it, `Serial` goes to UART pins and
  the board looks dead on USB.
- A project allocating big buffers in **PSRAM** needs `PSRAM=opi` (or `enabled`
  for quad) — without it `ps_malloc/ps_calloc` return null.

The ➕ Add Board dialog has checkboxes for both.

## Custom bootloader notes

The "Restore Bootloader" button exists because the Arduino IDE rewrites
address `0x0` on every upload, wiping any custom bootloader (e.g. one with a
shortened RTC watchdog for cold-boot auto-recovery). Point each target's
`bootloader` config entry at your `.bin`; `expect_chip` / `expect_size` are the
guard values — they must match the board's real chip + flash size.

**Hard-won warning:** a bootloader whose header declares the wrong flash size
(e.g. 4MB on a 16MB chip) will appear to work — the app boots — but runtime
NVS reads silently fail and *no setting ever persists across reboot*. Build
custom bootloaders from your Arduino core's own sdkconfig and verify with
`esptool image-info` before deploying.

## License

MIT — see [LICENSE](LICENSE).
