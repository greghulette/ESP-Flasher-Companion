#!/usr/bin/env python3
"""
============================================================
 ESP Flasher Companion — build / flash control panel
============================================================
One window, no file-explorer hunting:

  • Pick a serial port from a dropdown (refreshable).
  • Per-board buttons (boards defined in esp_flasher_config.json):
       Build + Flash   — arduino-cli compile (verbose) + app-only flash
                         at 0x10000 (custom bootloader / NVS untouched)
       Restore Bootloader — GUARDED: detects chip + real flash size via
                         esptool flash_id and REFUSES unless it matches
                         the bootloader (prevents the 4MB/16MB NVS trap).
                         Run after any Arduino IDE upload (the IDE wipes
                         the custom bootloader at 0x0 every time).
       Identify        — show chip type / flash size / MAC.

LIBRARIES MATCH THE ARDUINO IDE: compiles run with the IDE's own
arduino-cli config file (~/.arduinoIDE/arduino-cli.yaml), so the
sketchbook + libraries resolve EXACTLY as they do in the IDE.

PATHS ARE CONFIGURABLE: on first run an esp_flasher_config.json is
created next to this script with auto-detected defaults. Use the
"Edit Config" button to change sketch paths / FQBNs / boards, then
"Reload Config". Delete the json to regenerate fresh defaults.

Requires: python3 + tkinter (bundled), pyserial, esptool, arduino-cli.
============================================================
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# esptool 5.x emits ANSI colour codes only to a TTY. Force them off — we capture
# its output into the GUI log, and a --windowed frozen build has no real stdout.
os.environ.setdefault("NO_COLOR", "1")

# ----------------------------------------------------------------------------
# Theme palette (Tokyo-Night-ish). All styling is plain ttk — no extra deps.
# ----------------------------------------------------------------------------
BG      = "#1a1b26"   # window background
CARD    = "#24283b"   # card / panel background
FIELD   = "#16161e"   # entry/list field background
BTN     = "#2f3450"   # normal button
BTN_HI  = "#3d4466"   # hovered button
FG      = "#c0caf5"   # main text
SUB     = "#9099c0"   # secondary text
ACCENT  = "#7aa2f7"   # primary action (blue)
GREEN   = "#9ece6a"
RED     = "#f7768e"
YELLOW  = "#e0af68"

# When frozen by PyInstaller, __file__ lives in a temp unpack dir; anchor to the
# executable instead so esp_flasher_config.json sits next to the app and persists.
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "esp_flasher_config.json"
LOG_DIR     = SCRIPT_DIR / "logs"
BUILD_ROOT  = Path(os.environ.get("TEMP", "/tmp")) / "esp_flasher_build"


def resource_path(name):
    """Locate a bundled READ-ONLY resource (the app icon, etc.).

    PyInstaller unpacks --add-data files into a temp dir exposed as
    sys._MEIPASS at runtime; in a normal (dev) run they sit next to this
    source file. This is deliberately separate from SCRIPT_DIR, which points
    at the executable's own folder (so the user-editable config/logs persist),
    NOT at the throw-away unpack dir."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / name

# Colours cycled per concurrent output source (Board@PORT) so interleaved
# build/flash streams stay visually distinct in the log.
SOURCE_PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff",
                  "#f7768e", "#73daca", "#ff9e64", "#c0caf5", "#b4f9f8"]


# ----------------------------------------------------------------------------
# Config — auto-generated on first run, then user-editable JSON.
# ----------------------------------------------------------------------------
def default_config():
    """Auto-detected defaults. Known boards are only added if their sketch
    folder exists on this machine; everyone else starts empty and uses the
    app's '+ Add Board' button (no code edits needed)."""
    # Walk up from the app to find the folder holding the sibling Arduino repos
    # so known boards can be pre-filled. Works whether we run from src/, from a
    # frozen exe, or anywhere else; harmless if nothing matches (the user just
    # adds boards from the UI).
    github_dir = next(
        (d for d in (SCRIPT_DIR, *SCRIPT_DIR.parents)
         if (d / "Wireless_Communication_Board-WCB").is_dir()),
        SCRIPT_DIR.parent)
    wcb_bin    = github_dir / "Wireless_Communication_Board-WCB" / "Code" / "bin"
    s3_boot16  = wcb_bin / "WCB_S3_custom_bootloader_16MB_wdt3s.bin"
    s3_boot8   = wcb_bin / "WCB_S3_custom_bootloader_8MB_wdt3s.bin"
    ide_cfg    = Path.home() / ".arduinoIDE" / "arduino-cli.yaml"

    def s3_target(name, sketch, fqbn):
        # Both custom bootloaders keyed by flash size. Restore Bootloader reads
        # the connected board's real size and auto-picks the matching one, so a
        # single card serves both the 8MB (3.1) and 16MB (3.2) S3 dev kits.
        bls = {}
        if s3_boot8.is_file():
            bls["8MB"] = str(s3_boot8)
        if s3_boot16.is_file():
            bls["16MB"] = str(s3_boot16)
        return {
            "name":        name,
            "sketch":      str(sketch),
            "fqbn":        fqbn,
            "chip":        "esp32s3",
            "app_addr":    "0x10000",
            "bootloaders": bls,
            "expect_chip": "ESP32-S3",
            "ports":       [],
        }

    candidates = [
        s3_target("WCB  (ESP32-S3 3.1/3.2)",
                  github_dir / "Wireless_Communication_Board-WCB" / "Code" / "WCB",
                  "esp32:esp32:esp32s3:PartitionScheme=min_spiffs"),
        # NaviCore REQUIRES native-USB serial (CDCOnBoot=cdc) AND OPI PSRAM —
        # rcConfig is ps_calloc'd in PSRAM; without PSRAM=opi it halts red.
        s3_target("NaviCore  (ESP32-S3)",
                  github_dir / "NaviCore",
                  "esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,"
                  "PartitionScheme=min_spiffs,PSRAM=opi"),
        s3_target("SBUS Controller  (ESP32-S3)",
                  github_dir / "Arduino-Code" / "SBUSController",
                  "esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,"
                  "PartitionScheme=min_spiffs"),
    ]
    targets = [t for t in candidates if Path(t["sketch"]).is_dir()]

    return {
        "_help": [
            "Edit paths/boards here, or use the app's '+ Add Board' /",
            "'Change...' / 'x' buttons — they read and write this file.",
            "arduino_cli_config_file: the Arduino IDE's own cli config — using it",
            "  makes library resolution IDENTICAL to the IDE (sketchbook etc).",
            "  Set to null to use arduino-cli defaults instead.",
            "Per target: sketch = folder containing the .ino;  fqbn = board +",
            "  options exactly as the IDE's Tools menu uses;  bootloaders is a",
            "  {flash-size: path} map and Restore Bootloader auto-detects the",
            "  board's real flash size and flashes the matching one (refusing if",
            "  there is no bootloader for that size);",
            "  ports = the COM ports for this board — each gets its own row of",
            "  Build+Flash / Restore Bootloader / Identify buttons in the app.",
            "Delete this file to regenerate fresh defaults.",
        ],
        "arduino_cli_config_file": str(ide_cfg) if ide_cfg.is_file() else None,
        "targets": targets,
    }


def load_config():
    if not CONFIG_PATH.is_file():
        cfg = default_config()
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return cfg, "Created default config: %s" % CONFIG_PATH
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")), None
    except Exception as e:
        return default_config(), "Config unreadable (%s) — using defaults. Fix or delete %s" % (e, CONFIG_PATH)


# ----------------------------------------------------------------------------
# Tool discovery
# ----------------------------------------------------------------------------
def find_arduino_cli():
    from shutil import which
    p = which("arduino-cli")
    if p:
        return p
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "wcb-build-tools" / "arduino-cli.exe"
    if local.is_file():
        return str(local)
    return None


def esptool_cmd(*args):
    """Build an argv that runs esptool as a SEPARATE PROCESS, so several flashes
    can run concurrently (each with its own stdout) and STOP can kill them. A
    frozen build can't spawn 'python -m esptool' (sys.executable is this app, not
    Python), so it re-runs ITSELF with a hidden --run-esptool flag that hands off
    to esptool.main() — see the __main__ block at the bottom."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-esptool", *[str(a) for a in args]]
    return [sys.executable, "-m", "esptool", *[str(a) for a in args]]


# ----------------------------------------------------------------------------
# GUI app
# ----------------------------------------------------------------------------
class CompanionApp(tk.Tk):
    def _set_window_icon(self):
        """Use the WCB (R2) logo for the title bar / taskbar instead of Tk's
        default feather. On Windows the .ico drives both the title-bar glyph
        and the taskbar button; iconphoto(PNG) covers macOS/Linux and any
        toplevel that ignores the .ico. Both are bundled via PyInstaller
        --add-data and resolved through resource_path(). Best-effort: a missing
        or unreadable icon must never stop the app from launching."""
        try:
            ico = resource_path("WCB.ico")
            if os.name == "nt" and ico.exists():
                self.iconbitmap(default=str(ico))
        except Exception:
            pass
        try:
            png = resource_path("WCB.png")
            if png.exists():
                # Keep a reference — Tk does not retain PhotoImage objects, and
                # a GC'd image silently reverts the icon.
                self._icon_img = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def __init__(self):
        super().__init__()
        self.title("ESP Flasher Companion — Build / Flash")
        self._set_window_icon()
        self.geometry("1020x800")
        self.minsize(860, 580)
        self.configure(bg=BG)
        self._setup_style()

        self.log_q = queue.Queue()
        self.config_data, cfg_note = load_config()
        self._migrate_ports()

        # Concurrency: several build/flash jobs may run at once, each esptool
        # call in its own subprocess so they don't collide and STOP can kill them.
        self._procs = set()              # live subprocesses (for STOP)
        self._procs_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._active = 0                 # running jobs (drives STOP enabled state)
        self._busy_ports = set()         # ports with an action in flight
        self._row_combos = {}            # target name -> [port comboboxes]
        self._port_row_frames = {}       # target name -> rows container frame
        self._detected_ports = []        # current serial ports (full label strings)
        self._build_cache = {}           # target name -> (sketch_sig, appbin path)
        self._build_locks = {}           # target name -> compile lock
        self._src_colors = {}            # source label -> Text tag name

        # Per-session timestamped log file mirroring everything shown in the log.
        self._logf = None
        self._logfile_path = None
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._logfile_path = LOG_DIR / ("esp_flasher_%s.log"
                                            % datetime.now().strftime("%Y%m%d-%H%M%S"))
            self._logf = open(self._logfile_path, "a", encoding="utf-8")
        except Exception:
            pass

        # Header bar
        head = ttk.Frame(self, style="Head.TFrame", padding=(14, 10))
        head.pack(fill="x")
        ttk.Label(head, text="ESP Flasher Companion", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="   build · flash · monitor",
                  style="Sub.TLabel").pack(side="left", pady=(4, 0))

        self.flash_tab = ttk.Frame(self, style="TFrame")
        self.flash_tab.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._build_flash_tab()
        self.after(80, self._drain_log)
        self.refresh_ports()
        self._startup_checks(cfg_note)

    def _migrate_ports(self):
        """Normalise every target to a 'ports' list (legacy configs had a single
        'port' string). Each port becomes its own button row in the UI."""
        for t in self.config_data.get("targets", []):
            if not isinstance(t.get("ports"), list):
                p = (t.get("port") or "").strip()
                t["ports"] = [p] if p else []
            t.pop("port", None)

    def _mono_family(self):
        from tkinter import font as tkfont
        fams = set(tkfont.families())
        for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono"):
            if f in fams:
                return f
        return "Courier"

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, bordercolor=CARD,
                    lightcolor=CARD, darkcolor=CARD, focuscolor=ACCENT,
                    font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Head.TFrame", background=FIELD)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Title.TLabel", background=FIELD, foreground=ACCENT,
                    font=("Segoe UI Semibold", 16))
        s.configure("Sub.TLabel", background=FIELD, foreground=SUB,
                    font=("Segoe UI", 10))
        s.configure("CardName.TLabel", background=CARD, foreground=FG,
                    font=("Segoe UI Semibold", 11))
        s.configure("CardPath.TLabel", background=CARD, foreground=SUB,
                    font=("Segoe UI", 9))
        s.configure("CardErr.TLabel", background=CARD, foreground=RED,
                    font=("Segoe UI", 9))
        s.configure("Card.TLabel", background=CARD, foreground=FG)
        # Buttons
        s.configure("TButton", background=BTN, foreground=FG, borderwidth=0,
                    padding=(10, 5))
        s.map("TButton", background=[("active", BTN_HI), ("disabled", "#20243a")],
              foreground=[("disabled", "#565f89")])
        s.configure("Accent.TButton", background=ACCENT, foreground="#16161e",
                    font=("Segoe UI Semibold", 10))
        s.map("Accent.TButton", background=[("active", "#92b6ff"),
                                            ("disabled", "#3b4261")],
              foreground=[("disabled", "#9099c0")])
        s.configure("Danger.TButton", background="#5c2a35", foreground=FG)
        s.map("Danger.TButton", background=[("active", RED)],
              foreground=[("active", "#16161e")])
        # Combobox / fields
        s.configure("TCombobox", fieldbackground=FIELD, background=BTN,
                    foreground=FG, arrowcolor=FG, borderwidth=0,
                    selectbackground=FIELD, selectforeground=FG)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#16161e")
        s.configure("TEntry", fieldbackground=FIELD, foreground=FG,
                    insertcolor=FG, borderwidth=0)
        s.configure("Card.TCheckbutton", background=CARD, foreground=FG,
                    focuscolor=CARD)
        s.map("Card.TCheckbutton", background=[("active", CARD)])
        # Notebook
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(8, 6, 8, 0))
        s.configure("TNotebook.Tab", background=BG, foreground=SUB,
                    padding=(14, 6), font=("Segoe UI", 10))
        s.map("TNotebook.Tab", background=[("selected", CARD)],
              foreground=[("selected", ACCENT)])
        # Scrollbars / labelframe
        s.configure("Vertical.TScrollbar", background=BTN, troughcolor=BG,
                    borderwidth=0, arrowcolor=FG)
        s.configure("TLabelframe", background=BG, bordercolor=CARD)
        s.configure("TLabelframe.Label", background=BG, foreground=SUB)

    # ---------------- Flash tab ----------------
    def _build_flash_tab(self):
        top = ttk.Frame(self.flash_tab, padding=8)
        top.pack(fill="x")

        ttk.Button(top, text="🔌 Refresh Ports",
                   command=self.refresh_ports).pack(side="left")
        ttk.Button(top, text="➕ Add Board",
                   command=self.add_board_dialog).pack(side="left", padx=(12, 2))
        ttk.Button(top, text="Edit Config (advanced)",
                   command=self.edit_config).pack(side="left", padx=(2, 2))
        ttk.Button(top, text="Reload Config",
                   command=self.reload_config).pack(side="left")
        self.stop_btn = ttk.Button(top, text="◼ STOP", style="Danger.TButton",
                                   command=self.stop_task, state="disabled")
        self.stop_btn.pack(side="right")
        ttk.Button(top, text="📂 Open Logs",
                   command=self.open_logs).pack(side="right", padx=(2, 8))
        ttk.Button(top, text="📋 Copy Output",
                   command=self.copy_output).pack(side="right", padx=2)

        self.boards_frame = ttk.Frame(self.flash_tab, padding=(8, 0))
        self.boards_frame.pack(fill="x")
        self._build_board_cards()

        logf = ttk.LabelFrame(self.flash_tab, text="Output", padding=4)
        logf.pack(fill="both", expand=True, padx=8, pady=8)
        self.log = tk.Text(logf, wrap="none", font=(self._mono_family(), 9),
                           bg=FIELD, fg=FG, insertbackground=FG,
                           relief="flat", highlightthickness=0,
                           state="disabled")
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground=RED)
        self.log.tag_configure("ok", foreground=GREEN)
        self.log.tag_configure("hdr", foreground=ACCENT)
        self.log.tag_configure("ts", foreground=SUB)

    def _build_board_cards(self):
        for w in self.boards_frame.winfo_children():
            w.destroy()
        self._row_combos = {}
        self._port_row_frames = {}
        for t in self.config_data.get("targets", []):
            self._build_locks.setdefault(t["name"], threading.Lock())
            self._board_card(self.boards_frame, t)
        self.refresh_ports()

    def _board_card(self, parent, t):
        f = ttk.Frame(parent, style="Card.TFrame", padding=(12, 8))
        f.pack(fill="x", pady=4)
        missing = not Path(t["sketch"]).is_dir()

        # Row 1 — board name, sketch path, folder picker, remove
        r1 = ttk.Frame(f, style="Card.TFrame")
        r1.pack(fill="x")
        ttk.Label(r1, text=t["name"], style="CardName.TLabel").pack(side="left")
        ttk.Button(r1, text="✕", width=3, style="Danger.TButton",
                   command=lambda t=t: self._remove_board(t)).pack(side="right",
                                                                   padx=(6, 0))
        ttk.Button(r1, text="📁 Change…", width=11,
                   command=lambda t=t: self._browse_sketch(t)).pack(side="right")
        path_style = "CardErr.TLabel" if missing else "CardPath.TLabel"
        path_text = ("not found:  %s" % t["sketch"]) if missing else t["sketch"]
        ttk.Label(r1, text=path_text, style=path_style).pack(side="right", padx=10)

        # Port rows — one per assigned COM port, each with its own action buttons.
        rows = ttk.Frame(f, style="Card.TFrame")
        rows.pack(fill="x", pady=(6, 0))
        self._port_row_frames[t["name"]] = rows
        self._row_combos[t["name"]] = []
        for port in self._target_ports(t):
            self._port_row(t, port)

        addbar = ttk.Frame(f, style="Card.TFrame")
        addbar.pack(fill="x", pady=(4, 0))
        ttk.Button(addbar, text="➕ Add port",
                   command=lambda t=t: self._add_port_row(t)).pack(side="left")
        if missing:
            ttk.Label(addbar, text="(sketch folder missing — Build disabled)",
                      style="CardErr.TLabel").pack(side="left", padx=10)

    def _browse_sketch(self, t):
        start = t["sketch"] if Path(t["sketch"]).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(
            title="Choose the sketch folder for %s (contains the .ino)" % t["name"],
            initialdir=start)
        if not chosen:
            return
        chosen = str(Path(chosen))
        inos = list(Path(chosen).glob("*.ino"))
        if not inos:
            self.log_line("[!] No .ino in %s — folder NOT changed. Pick the "
                          "folder that contains the sketch's .ino file." % chosen,
                          "err")
            return
        t["sketch"] = chosen
        self._write_config()
        self._build_board_cards()
        self.log_line("[OK] %s sketch -> %s  (%s)"
                      % (t["name"], chosen, inos[0].name), "ok")

    def _write_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps(self.config_data, indent=2),
                                   encoding="utf-8")
        except OSError as e:
            self.log_line("[!] Could not save config: %s" % e, "err")

    def _remove_board(self, t):
        if not messagebox.askyesno(
                "Remove board",
                "Remove '%s' from the app?\n\n(Only this app's config entry is "
                "removed — nothing on disk is deleted.)" % t["name"]):
            return
        self.config_data["targets"] = [
            x for x in self.config_data.get("targets", []) if x is not t]
        self._write_config()
        self._build_board_cards()
        self.log_line("[OK] Removed board: %s" % t["name"], "ok")

    def add_board_dialog(self):
        d = tk.Toplevel(self)
        d.title("Add Board")
        d.configure(bg=BG)
        d.transient(self)
        d.grab_set()
        d.resizable(False, False)
        frm = ttk.Frame(d, style="Card.TFrame", padding=16)
        frm.pack(fill="both", expand=True, padx=12, pady=12)

        name_v   = tk.StringVar()
        sketch_v = tk.StringVar()
        chip_v   = tk.StringVar(value="esp32s3")
        cdc_v    = tk.BooleanVar(value=False)
        psram_v  = tk.BooleanVar(value=False)
        fqbn_v   = tk.StringVar()
        fqbn_edited = [False]   # once the user hand-edits FQBN, stop regenerating

        def gen_fqbn(*_):
            if fqbn_edited[0]:
                return
            chip = chip_v.get()
            opts = []
            if chip == "esp32s3":
                if cdc_v.get():
                    opts.append("USBMode=hwcdc")
                    opts.append("CDCOnBoot=cdc")
                if psram_v.get():
                    opts.append("PSRAM=opi")
            opts.append("PartitionScheme=min_spiffs")
            fqbn_v.set("esp32:esp32:%s:%s" % (chip, ",".join(opts)))
        gen_fqbn()

        def browse(*_):
            chosen = filedialog.askdirectory(parent=d,
                title="Choose the sketch folder (contains the .ino)")
            if chosen:
                sketch_v.set(str(Path(chosen)))
                if not name_v.get():
                    name_v.set(Path(chosen).name)

        def lab(row, text):
            ttk.Label(frm, text=text, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=4, padx=(0, 10))

        lab(0, "Name")
        ttk.Entry(frm, textvariable=name_v, width=42).grid(
            row=0, column=1, columnspan=2, sticky="we", pady=4)
        lab(1, "Sketch folder")
        ttk.Entry(frm, textvariable=sketch_v, width=34).grid(
            row=1, column=1, sticky="we", pady=4)
        ttk.Button(frm, text="📁 Browse…", command=browse).grid(
            row=1, column=2, padx=(6, 0), pady=4)
        lab(2, "Chip")
        chip_cb = ttk.Combobox(frm, textvariable=chip_v, state="readonly",
                               values=("esp32s3", "esp32"), width=12)
        chip_cb.grid(row=2, column=1, sticky="w", pady=4)
        chip_cb.bind("<<ComboboxSelected>>", gen_fqbn)
        opt = ttk.Frame(frm, style="Card.TFrame")
        opt.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(opt, text="USB CDC on boot (native-USB serial)",
                        variable=cdc_v, command=gen_fqbn,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Checkbutton(opt, text="OPI PSRAM", variable=psram_v,
                        command=gen_fqbn,
                        style="Card.TCheckbutton").pack(side="left", padx=12)
        lab(4, "FQBN")
        fq = ttk.Entry(frm, textvariable=fqbn_v, width=52)
        fq.grid(row=4, column=1, columnspan=2, sticky="we", pady=4)
        fq.bind("<Key>", lambda e: fqbn_edited.__setitem__(0, True))
        ttk.Label(frm, style="CardPath.TLabel", text=(
            "FQBN must mirror the sketch's Arduino IDE Tools-menu settings.\n"
            "The checkboxes cover the common ones; hand-edit for anything else."
        )).grid(row=5, column=1, columnspan=2, sticky="w", pady=(0, 6))

        def on_ok():
            name = name_v.get().strip()
            sketch = sketch_v.get().strip()
            if not name or not sketch:
                messagebox.showerror("Add Board", "Name and sketch folder are "
                                     "required.", parent=d)
                return
            if not list(Path(sketch).glob("*.ino")):
                messagebox.showerror("Add Board", "No .ino file in:\n%s\n\nPick "
                                     "the folder that contains the sketch's .ino."
                                     % sketch, parent=d)
                return
            chip = chip_v.get()
            s3 = chip == "esp32s3"
            self.config_data.setdefault("targets", []).append({
                "name":        name,
                "sketch":      sketch,
                "fqbn":        fqbn_v.get().strip(),
                "chip":        chip,
                "app_addr":    "0x10000",
                "bootloader":  str(SCRIPT_DIR / "WCB_S3_custom_bootloader_16MB_wdt3s.bin") if s3 else "",
                "expect_chip": "ESP32-S3" if s3 else "ESP32",
                "expect_size": "16MB" if s3 else "",
                "ports":       [],
            })
            self._write_config()
            self._build_board_cards()
            self.log_line("[OK] Added board: %s  (%s)" % (name, sketch), "ok")
            d.destroy()

        btns = ttk.Frame(frm, style="Card.TFrame")
        btns.grid(row=6, column=1, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Cancel", command=d.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="Add Board", style="Accent.TButton",
                   command=on_ok).pack(side="right")

    def _target_ports(self, t):
        return [p for p in (t.get("ports") or []) if p]

    def _display_for(self, device):
        """Full 'COMx  description' label for a saved bare device, if connected."""
        for s in self._detected_ports:
            if s.split(" ", 1)[0].strip() == device:
                return s
        return device

    def _port_row(self, t, port):
        """One COM-port row: port picker + its own action buttons + remove."""
        rows = self._port_row_frames.get(t["name"])
        if rows is None:
            return
        missing = not Path(t["sketch"]).is_dir()
        row = ttk.Frame(rows, style="Card.TFrame")
        row.pack(fill="x", pady=2)

        combo = ttk.Combobox(row, width=26, state="readonly",
                             values=self._detected_ports)
        if port:
            combo.set(self._display_for(port))
        combo.pack(side="left", padx=(0, 8))
        combo.bind("<<ComboboxSelected>>", lambda e, t=t: self._sync_ports(t))
        self._row_combos.setdefault(t["name"], []).append(combo)
        get_port = lambda c=combo: c.get().split(" ", 1)[0].strip()

        b1 = ttk.Button(row, text="⚡ Build + Flash", style="Accent.TButton")
        b2 = ttk.Button(row, text="Restore Bootloader")
        b3 = ttk.Button(row, text="Identify")
        btns = [b1, b2, b3]
        b1.config(state=("disabled" if missing else "normal"),
                  command=lambda t=t, g=get_port, bs=btns:
                      self._on_port_action(t, g, bs, self._do_build_flash))
        b2.config(command=lambda t=t, g=get_port, bs=btns:
                      self._on_port_action(t, g, bs, self._do_bootloader))
        b3.config(command=lambda t=t, g=get_port, bs=btns:
                      self._on_port_action(t, g, bs, self._do_identify))
        ttk.Button(row, text="✕", width=3, style="Danger.TButton",
                   command=lambda t=t, r=row, c=combo:
                       self._remove_port_row(t, r, c)).pack(side="right", padx=(6, 0))
        for b in (b1, b2, b3):
            b.pack(side="left", padx=3)

    def _add_port_row(self, t):
        self._port_row(t, "")

    def _remove_port_row(self, t, row, combo):
        try:
            self._row_combos.get(t["name"], []).remove(combo)
        except ValueError:
            pass
        row.destroy()
        self._sync_ports(t)

    def _sync_ports(self, t):
        """Persist the COM ports currently chosen across this card's rows."""
        sel = []
        for c in self._row_combos.get(t["name"], []):
            try:
                v = c.get().split(" ", 1)[0].strip()
            except tk.TclError:
                v = ""
            if v and v not in sel:
                sel.append(v)
        t["ports"] = sel
        self._write_config()

    def _on_port_action(self, t, get_port, buttons, do_fn):
        """Launch do_fn(t, port) for this row's port as a concurrent job. Guards
        against double-acting on a busy port and disables the row while it runs."""
        port = get_port()
        if not port:
            self.log_line("[!] Pick a COM port for this row first.", "err",
                          source=t["name"])
            return
        with self._jobs_lock:
            if port in self._busy_ports:
                self.log_line("[!] %s is already busy — wait for it to finish."
                              % port, "err", source="%s@%s" % (t["name"], port))
                return
            self._busy_ports.add(port)
        prev = [(b, b.cget("state")) for b in buttons]
        for b in buttons:
            b.config(state="disabled")

        def job():
            try:
                do_fn(t, port)
            finally:
                with self._jobs_lock:
                    self._busy_ports.discard(port)
                self.after(0, lambda: [self._safe_state(b, s) for b, s in prev])
        self._start_job(job)

    def copy_output(self):
        txt = self.log.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(txt)
        self.log_line("[OK] Output copied to clipboard (%d chars)." % len(txt), "ok")

    def open_logs(self):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(LOG_DIR))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(LOG_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(LOG_DIR)])
            self.log_line("Logs folder: %s" % LOG_DIR, "hdr")
        except Exception as e:
            self.log_line("[X] Couldn't open logs folder: %s" % e, "err")

    def edit_config(self):
        if not CONFIG_PATH.is_file():
            CONFIG_PATH.write_text(json.dumps(default_config(), indent=2),
                                   encoding="utf-8")
        try:
            os.startfile(str(CONFIG_PATH))               # Windows
        except AttributeError:
            subprocess.Popen(["open", str(CONFIG_PATH)])  # macOS
        self.log_line("Opened %s — save it, then press 'Reload Config'."
                      % CONFIG_PATH.name, "hdr")

    def reload_config(self):
        self.config_data, note = load_config()
        self._migrate_ports()
        self._build_board_cards()
        self.log_line(note or "[OK] Config reloaded — %d board(s)."
                      % len(self.config_data.get("targets", [])), "ok")

    # ---------------- logging / concurrency plumbing ----------------
    def log_line(self, text, tag=None, source=None):
        self.log_q.put((text, tag, source))

    def _source_tag(self, source):
        """A stable Text tag (cycled colour) per output source label."""
        tn = self._src_colors.get(source)
        if tn is None:
            color = SOURCE_PALETTE[len(self._src_colors) % len(SOURCE_PALETTE)]
            tn = "src%d" % len(self._src_colors)
            self.log.tag_configure(tn, foreground=color)
            self._src_colors[source] = tn
        return tn

    def _drain_log(self):
        try:
            while True:
                text, tag, source = self.log_q.get_nowait()
                now = datetime.now()
                self.log.configure(state="normal")
                if source:
                    self.log.insert("end", "[%s  %s]  " % (now.strftime("%H:%M:%S"),
                                    source), (self._source_tag(source),))
                else:
                    self.log.insert("end", "[%s]  " % now.strftime("%H:%M:%S"), ("ts",))
                self.log.insert("end", text + "\n", tag or ())
                self.log.see("end")
                self.log.configure(state="disabled")
                if self._logf is not None:
                    try:
                        label = ("[%s] " % source) if source else ""
                        self._logf.write("%s  %s%s\n"
                                         % (now.strftime("%Y-%m-%d %H:%M:%S"),
                                            label, text))
                        self._logf.flush()
                    except Exception:
                        pass
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    def _safe_state(self, widget, state):
        try:
            widget.config(state=state)
        except tk.TclError:
            pass

    def _update_running_state(self):
        self._safe_state(self.stop_btn, "normal" if self._active > 0 else "disabled")

    def _start_job(self, fn):
        """Run fn() on a daemon thread, counting it so STOP stays enabled while
        any job is in flight. Several jobs can run at once."""
        with self._jobs_lock:
            self._active += 1
        self.after(0, self._update_running_state)

        def wrapped():
            try:
                fn()
            except Exception as e:
                self.log_line("[X] %s" % e, "err")
            finally:
                with self._jobs_lock:
                    self._active -= 1
                self.after(0, self._update_running_state)
        threading.Thread(target=wrapped, daemon=True).start()

    def stop_task(self):
        with self._procs_lock:
            procs = list(self._procs)
        if not procs:
            self.log_line("[!] Nothing is running.", "err")
            return
        n = 0
        for p in procs:
            try:
                if p.poll() is None:
                    p.terminate()
                    n += 1
            except Exception:
                pass
        self.log_line("[!] STOP — terminated %d running task(s)." % n, "err")

    def run_cmd(self, cmd, source=None, quiet=False):
        """Stream a subprocess into the log (tagged with `source`). Returns
        (rc, captured_text). Registered so STOP can terminate it. quiet=True
        captures output without logging it (verification probes)."""
        if not quiet:
            self.log_line("$ " + " ".join(str(c) for c in cmd), "hdr", source=source)
        out = []
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with self._procs_lock:
            self._procs.add(proc)
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                out.append(line)
                if not quiet:
                    self.log_line(line, source=source)
            proc.wait()
        finally:
            with self._procs_lock:
                self._procs.discard(proc)
        return proc.returncode, "\n".join(out)

    def refresh_ports(self):
        try:
            import serial.tools.list_ports as lp
            self._detected_ports = ["%s  %s" % (p.device, p.description or "")
                                    for p in lp.comports()]
        except Exception as e:
            self._detected_ports = []
            self.log_line("[X] pyserial missing? %s" % e, "err")
        for combos in self._row_combos.values():
            for c in combos:
                try:
                    current = c.get()
                    c["values"] = self._detected_ports
                    if current:          # keep this row's chosen port selected
                        c.set(current)
                except tk.TclError:
                    pass

    def _startup_checks(self, cfg_note):
        self.log_line("ESP Flasher Companion ready.", "ok")
        if cfg_note:
            self.log_line(cfg_note, "hdr")
        try:
            import esptool  # noqa: F401
            self.log_line("[OK] esptool available")
        except Exception:
            self.log_line("[X] esptool not installed:  pip install esptool", "err")
        acli = find_arduino_cli()
        if acli:
            self.log_line("[OK] arduino-cli: %s" % acli)
        else:
            self.log_line("[!] arduino-cli not found — Build buttons will fail. "
                          "Install it or run the build_flash_app script once "
                          "(it downloads arduino-cli).", "err")
        ide_cfg = self.config_data.get("arduino_cli_config_file")
        if ide_cfg and Path(ide_cfg).is_file():
            self.log_line("[OK] Using the Arduino IDE's cli config -> libraries "
                          "resolve EXACTLY like the IDE: %s" % ide_cfg)
        else:
            self.log_line("[!] No Arduino IDE config found — arduino-cli will use "
                          "its own defaults (sketchbook may differ from the IDE).",
                          "err")

    # ---------------- the actual work (per port) ----------------
    def _do_identify(self, t, port):
        src = "%s@%s" % (t["name"], port)
        self.log_line("== Identify on %s ==" % port, "hdr", source=src)
        self.run_cmd(esptool_cmd("-p", port, "flash_id"), source=src)

    def _bootloaders_map(self, t):
        """flash-size -> custom-bootloader path for this board. Supports the new
        per-size 'bootloaders' dict and the legacy single 'bootloader' +
        'expect_size' pair."""
        bl = t.get("bootloaders")
        if isinstance(bl, dict):
            return {k: v for k, v in bl.items() if v}
        if t.get("bootloader") and t.get("expect_size"):
            return {t["expect_size"]: t["bootloader"]}
        return {}

    def _detect_board(self, t, port, src):
        """flash_id -> verify chip family, return the detected flash size string.
        Raises on read failure or a chip-family mismatch."""
        self.log_line("== Identifying %s ==" % port, "hdr", source=src)
        rc, out = self.run_cmd(esptool_cmd("-p", port, "flash_id"), source=src)
        if rc != 0:
            raise RuntimeError("Could not identify %s (esptool rc=%d). Close any "
                               "serial monitor and retry." % (port, rc))
        if t.get("expect_chip") and t["expect_chip"] not in out:
            raise RuntimeError("ABORT %s: board is not %s." % (port, t["expect_chip"]))
        m = re.search(r"Detected flash size:\s*(\S+)", out)
        if not m:
            raise RuntimeError("ABORT %s: could not read the flash size." % port)
        return m.group(1)

    def _exit_download_mode(self, t, port, src):
        """Some S3 boards stay in ROM download mode after esptool's post-flash
        reset (USB-Serial-JTAG control-line quirk — the app never starts until
        a physical RESET). Probe WITHOUT resetting: if the ROM bootloader still
        answers, the board is stuck — reboot it via the RTC WATCHDOG (register-
        based, no DTR/RTS involved; DTR/RTS is the very mechanism that re-enters
        download mode). Also call out a port held open by another program — a
        serial monitor that auto-reconnects can itself re-trigger download mode
        the instant esptool releases the port."""
        self.log_line("== Verifying the board left download mode ==", "hdr",
                      source=src)
        rc, out = self.run_cmd(esptool_cmd("-p", port, "--before", "no-reset",
                                           "--after", "watchdog-reset",
                                           "--connect-attempts", "2", "chip-id"),
                               source=src, quiet=True)
        low = out.lower()
        if rc == 0:
            self.log_line("[!] Board was STILL in download mode after the flash "
                          "reset — rebooted it via the RTC watchdog. If the LED "
                          "stays dark, press its RESET button.", "hdr", source=src)
        elif "failed to connect" in low or "no serial data received" in low:
            # The GOOD case: nothing answered the bootloader probe -> the chip
            # is out of download mode and the app is running.
            self.log_line("[OK] Board is out of download mode — app is running.",
                          "ok", source=src)
        elif ("could not open" in low or "access is denied" in low
              or "permission" in low or "busy" in low):
            self.log_line("[!] Couldn't verify %s — the port is held open by "
                          "another program (serial monitor?). An auto-reconnect "
                          "monitor can ITSELF push the board back into download "
                          "mode as esptool releases the port. Close it (e.g. the "
                          "Arduino IDE) and reflash." % port, "err", source=src)
        else:
            tail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
            self.log_line("[!] Boot check inconclusive on %s: %s"
                          % (port, tail), "err", source=src)

    def _do_bootloader(self, t, port):
        src = "%s@%s" % (t["name"], port)
        blmap = self._bootloaders_map(t)
        if not blmap:
            raise RuntimeError("No custom bootloader configured for %s." % t["name"])
        # Auto-pick the bootloader whose declared flash size matches the connected
        # board, so one card safely serves both the 8MB and 16MB WCB dev kits.
        size = self._detect_board(t, port, src)
        boot = blmap.get(size)
        if not boot:
            raise RuntimeError("ABORT %s: detected %s flash, but no custom bootloader "
                               "is configured for that size (have: %s). Flashing a "
                               "wrong-size bootloader silently corrupts NVS."
                               % (port, size, ", ".join(sorted(blmap)) or "none"))
        boot = Path(boot)
        if not boot.is_file():
            raise RuntimeError("Bootloader bin not found: %s" % boot)
        self.log_line("[OK] %s / %s flash — using %s"
                      % (t.get("expect_chip", "board"), size, boot.name),
                      "ok", source=src)
        self.log_line("== Flashing %s custom bootloader at 0x0 (app/NVS untouched) =="
                      % size, "hdr", source=src)
        bl_args = ["--chip", t["chip"], "-p", port]
        if t["chip"].startswith("esp32s"):
            bl_args += ["--after", "watchdog-reset"]   # see _do_build_flash note
        bl_args += ["write_flash", "0x0", str(boot)]
        rc, _ = self.run_cmd(esptool_cmd(*bl_args), source=src)
        if rc == 0:
            self.log_line("[DONE] %s bootloader restored on %s — reboot and confirm "
                          "a saved setting persists." % (size, port), "ok", source=src)
            self._exit_download_mode(t, port, src)
        else:
            raise RuntimeError("Bootloader flash failed on %s (rc=%d)." % (port, rc))

    def _do_build_flash(self, t, port):
        src = "%s@%s" % (t["name"], port)
        appbin = self._ensure_built(t)
        # ONE CLICK, FULL IMAGE: detect flash size, then write — in a single
        # esptool pass — the size-matched custom bootloader (0x0), the partition
        # table (0x8000), the OTA-data selector (0xe000) and the app (0x10000).
        # That's exactly the layout the Arduino IDE writes, with the custom
        # bootloader swapped in. Writing the PARTITION TABLE is what keeps the
        # on-flash layout in sync with the app: an app-only flash leaves whatever
        # table was there before, so changing the partition scheme (e.g. to
        # min_spiffs) silently mismatches the app vs the map and breaks boot. NVS
        # (0x9000) lives between the table and the OTA-data and is NOT written
        # here, so saved settings survive. The size check still guards the
        # bootloader — it's only written when its declared size matches the chip.
        args = ["--chip", t["chip"], "-p", port]
        if t["chip"].startswith("esp32s"):
            # Native-USB chips: leave the flash via RTC-watchdog reset instead of
            # the DTR/RTS hard reset — DTR/RTS over USB-Serial-JTAG is what keeps
            # re-entering download mode on these devkits.
            args += ["--after", "watchdog-reset"]
        args += ["write_flash"]
        parts = []
        blmap = self._bootloaders_map(t)
        if blmap:
            size = self._detect_board(t, port, src)   # flash_id: verify chip + size
            boot = blmap.get(size)
            if boot and not Path(boot).is_file():
                raise RuntimeError("Bootloader bin not found: %s" % boot)
            if boot:
                args += ["0x0", str(boot)]
                parts.append("%s bootloader" % size)
            else:
                self.log_line("[!] Detected %s flash but no custom bootloader is "
                              "configured for that size (have: %s) — leaving the "
                              "bootloader alone." % (size, ", ".join(sorted(blmap))
                                                     or "none"), "err", source=src)
        ptab = self._partition_table(appbin)
        if ptab:
            args += ["0x8000", str(ptab)]
            parts.append("partition table")
        else:
            self.log_line("[!] No partition-table .bin next to the app — flashing "
                          "without it (on-flash layout left as-is).", "err",
                          source=src)
        boot_app0 = self._find_boot_app0()
        if boot_app0:
            args += ["0xe000", str(boot_app0)]
            parts.append("OTA-data")
        args += [t["app_addr"], str(appbin)]
        parts.append("app")
        self.log_line("== Flashing %s on %s (NVS / saved settings untouched) =="
                      % (" + ".join(parts), port), "hdr", source=src)
        rc, _ = self.run_cmd(esptool_cmd(*args), source=src)
        if rc != 0:
            raise RuntimeError("Flash failed on %s (rc=%d). Hold BOOT, tap RESET, "
                               "release BOOT, then retry." % (port, rc))
        self.log_line("[DONE] Flashed %s on %s — full image, layout in sync, saved "
                      "config intact." % (" + ".join(parts), port), "ok", source=src)
        self._exit_download_mode(t, port, src)

    def _partition_table(self, appbin):
        """The partition-table .bin arduino-cli emitted next to the compiled app
        (defines where app / NVS / SPIFFS / OTA-data live)."""
        cands = sorted(appbin.parent.glob("*.partitions.bin"))
        return cands[0] if cands else None

    def _find_boot_app0(self):
        """Locate the esp32 core's boot_app0.bin — the OTA-data initializer that
        points the bootloader at the freshly-written app slot. Cached; None if
        the core layout can't be found (then OTA-data is left as-is)."""
        if hasattr(self, "_boot_app0_cache"):
            return self._boot_app0_cache
        found = None
        for r in (Path(os.environ.get("LOCALAPPDATA", "")) / "Arduino15",
                  Path.home() / ".arduino15",
                  Path.home() / "Library" / "Arduino15"):
            base = r / "packages" / "esp32" / "hardware" / "esp32"
            if base.is_dir():
                cands = sorted(base.glob("*/tools/partitions/boot_app0.bin"),
                               reverse=True)
                if cands:
                    found = str(cands[0])
                    break
        self._boot_app0_cache = found
        return found

    def _sketch_sig(self, t):
        """Signature that changes when the sketch sources change, so a cached
        build is reused across ports but rebuilt after an edit."""
        p = Path(t["sketch"])
        mt = 0.0
        if p.is_dir():
            for f in p.rglob("*"):
                if f.suffix.lower() in (".ino", ".h", ".hpp", ".c", ".cpp", ".cc"):
                    try:
                        mt = max(mt, f.stat().st_mtime)
                    except OSError:
                        pass
        return (str(p), t["fqbn"], round(mt, 3))

    def _ensure_built(self, t):
        """Compile once per card; reuse the binary for further ports unless the
        sketch changed. Serialised per card so concurrent clicks share one build."""
        lock = self._build_locks.setdefault(t["name"], threading.Lock())
        with lock:
            sig = self._sketch_sig(t)
            cached = self._build_cache.get(t["name"])
            if cached and cached[0] == sig and Path(cached[1]).is_file():
                self.log_line("[OK] Reusing this session's build (sketch unchanged).",
                              "ok", source=t["name"])
                return Path(cached[1])
            appbin = self._compile(t)
            self._build_cache[t["name"]] = (sig, str(appbin))
            return appbin

    def _compile(self, t):
        acli = find_arduino_cli()
        if not acli:
            raise RuntimeError("arduino-cli not found.")
        src = t["name"]
        sketch = Path(t["sketch"])
        build_out = BUILD_ROOT / re.sub(r"\W+", "_", t["name"]).strip("_")
        build_out.mkdir(parents=True, exist_ok=True)

        self.log_line("== Compiling %s (verbose) ==" % sketch.name, "hdr", source=src)
        cmd = [acli]
        ide_cfg = self.config_data.get("arduino_cli_config_file")
        if ide_cfg and Path(ide_cfg).is_file():
            cmd += ["--config-file", ide_cfg]    # same libraries as the IDE
        cmd += ["compile", "--verbose", "--fqbn", t["fqbn"],
                "--output-dir", str(build_out)]
        if t.get("libraries"):                   # optional extra libraries root
            cmd += ["--libraries", t["libraries"]]
        cmd.append(str(sketch))
        rc, out = self.run_cmd(cmd, source=src)
        if rc != 0 and "needs to be reinitialized" in out:
            # First use of the IDE's config file: arduino-cli must download the
            # package indexes it references (e.g. extra board URLs) once.
            self.log_line("[!] arduino-cli indexes missing for this config — "
                          "running one-time update-index, then retrying...", "hdr",
                          source=src)
            base = [acli]
            if ide_cfg and Path(ide_cfg).is_file():
                base += ["--config-file", ide_cfg]
            self.run_cmd(base + ["core", "update-index"], source=src)
            self.run_cmd(base + ["lib", "update-index"], source=src)
            rc, out = self.run_cmd(cmd, source=src)
        if rc != 0:
            raise RuntimeError("Compile FAILED for %s — nothing flashed." % t["name"])
        bins = sorted(build_out.glob("*.ino.bin"))
        if not bins:
            raise RuntimeError("No .ino.bin produced in %s" % build_out)
        self.log_line("[OK] Compiled: %s" % bins[0], "ok", source=src)
        return bins[0]

if __name__ == "__main__":
    # Hidden hand-off: a frozen build re-runs ITSELF as esptool (there is no
    # python.exe to run 'python -m esptool'). esptool_cmd() emits this in frozen
    # builds; here we catch it and delegate to esptool.main().
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-esptool":
        import esptool
        try:
            sys.exit(esptool.main(sys.argv[2:]))
        except Exception as e:
            # Programmatic esptool.main() RAISES (FatalError etc.) instead of
            # exiting — condense to one line; a raw traceback in the flash log
            # reads like a crash when it's often just "not in bootloader".
            try:
                print("esptool error: %s" % e)
            except Exception:
                pass
            sys.exit(2)

    if "--selftest" in sys.argv:
        # Headless check (build scripts / CI) that a frozen bundle carries esptool
        # + its flasher stubs and pyserial, AND that esptool runs as a subprocess
        # via the --run-esptool hand-off — the exact path the app uses to flash.
        try:
            import subprocess as _sp
            from esptool.loader import StubFlasher
            import serial.tools.list_ports  # noqa: F401  (verify pyserial bundled)
            stubs_ok = any(
                os.path.isfile(os.path.join(StubFlasher.STUB_DIR, sub, "esp32s3.json"))
                for sub in StubFlasher.STUB_SUBDIRS
            )
            r = _sp.run(esptool_cmd("version"), capture_output=True, text=True,
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            run_ok = r.returncode == 0 and "esptool" in (r.stdout + (r.stderr or "")).lower()
            sys.exit(0 if (stubs_ok and run_ok) else 3)
        except Exception:
            sys.exit(4)
    app = CompanionApp()
    app.mainloop()
