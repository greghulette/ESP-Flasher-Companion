#!/usr/bin/env python3
"""
============================================================
 ESP Flasher Companion — build/flash control panel + Claude usage
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
  • Claude Usage tab — token usage parsed from your local Claude Code
    session logs (~/.claude/projects/**/*.jsonl), today + last 7 days.

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
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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

SCRIPT_DIR  = Path(__file__).resolve().parent          # .../Code/bin
CONFIG_PATH = SCRIPT_DIR / "esp_flasher_config.json"
BUILD_ROOT  = Path(os.environ.get("TEMP", "/tmp")) / "esp_flasher_build"

# Claude usage — rough cost estimate. EDIT to current pricing if you care
# about the $ column; token counts are exact either way.
# (input $/MTok, output $/MTok); cache read = 10% of input, cache write = 125%.
PRICING = {
    "opus":   (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (1.0, 5.0),
}

# ----------------------------------------------------------------------------
# Config — auto-generated on first run, then user-editable JSON.
# ----------------------------------------------------------------------------
def default_config():
    """Auto-detected defaults. Known boards are only added if their sketch
    folder exists on this machine; everyone else starts empty and uses the
    app's '+ Add Board' button (no code edits needed)."""
    github_dir = SCRIPT_DIR.parent            # repo usually sits in .../GitHub
    wcb_bin    = github_dir / "Wireless_Communication_Board-WCB" / "Code" / "bin"
    s3_boot    = wcb_bin / "WCB_S3_custom_bootloader_16MB_wdt3s.bin"
    ide_cfg    = Path.home() / ".arduinoIDE" / "arduino-cli.yaml"

    def s3_target(name, sketch, fqbn):
        return {
            "name":        name,
            "sketch":      str(sketch),
            "fqbn":        fqbn,
            "chip":        "esp32s3",
            "app_addr":    "0x10000",
            "bootloader":  str(s3_boot) if s3_boot.is_file() else "",
            "expect_chip": "ESP32-S3",
            "expect_size": "16MB",
            "port":        "",
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
                  "esp32:esp32:esp32s3:PartitionScheme=min_spiffs"),
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
            "  options exactly as the IDE's Tools menu uses;  bootloader/expect_*",
            "  drive the guarded 'Restore Bootloader' button (expect_size must",
            "  match the board's real flash size or it refuses to flash);",
            "  port = this board's serial port, remembered per board.",
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
    return [sys.executable, "-m", "esptool", *args]


# ----------------------------------------------------------------------------
# Claude usage scanning (reads ~/.claude/projects/**/*.jsonl)
# ----------------------------------------------------------------------------
def scan_claude_usage(days=7):
    """Aggregate token usage per (date, model). Dedupes streamed updates by
    (message.id, requestId) keeping the LAST seen usage for that pair."""
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return None, "No Claude Code logs found at %s" % root

    cutoff_dt = datetime.now() - timedelta(days=days)
    cutoff_mtime = (cutoff_dt - timedelta(days=1)).timestamp()  # mtime pre-filter
    entries = {}   # (msg_id, req_id) -> (date_str, model, usage)
    anon = 0

    for f in root.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff_mtime:
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message") or {}
                    usage = msg.get("usage")
                    model = msg.get("model") or ""
                    ts = obj.get("timestamp")
                    if not usage or not ts or model in ("", "<synthetic>"):
                        continue
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                    except Exception:
                        continue
                    if dt < cutoff_dt.astimezone():
                        continue
                    mid, rid = msg.get("id"), obj.get("requestId")
                    if mid:
                        key = (mid, rid)
                    else:
                        anon += 1
                        key = ("anon", anon)
                    entries[key] = (dt.strftime("%Y-%m-%d"), model, usage)
        except OSError:
            continue

    agg = {}  # (date, model) -> dict of token sums
    for date_str, model, u in entries.values():
        k = (date_str, model)
        a = agg.setdefault(k, {"in": 0, "out": 0, "cr": 0, "cw": 0, "n": 0})
        a["in"] += u.get("input_tokens", 0) or 0
        a["out"] += u.get("output_tokens", 0) or 0
        a["cr"] += u.get("cache_read_input_tokens", 0) or 0
        a["cw"] += u.get("cache_creation_input_tokens", 0) or 0
        a["n"] += 1
    return agg, None


def estimate_cost(model, a):
    m = model.lower()
    for fam, (pin, pout) in PRICING.items():
        if fam in m:
            return (a["in"] * pin + a["out"] * pout
                    + a["cr"] * pin * 0.10 + a["cw"] * pin * 1.25) / 1_000_000
    return None


# ----------------------------------------------------------------------------
# GUI app
# ----------------------------------------------------------------------------
class CompanionApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP Flasher Companion — Build / Flash / Usage")
        self.geometry("1020x800")
        self.minsize(860, 580)
        self.configure(bg=BG)
        self._setup_style()

        self.log_q = queue.Queue()
        self.proc = None
        self.worker = None
        self.action_buttons = []      # (button, idle_state)
        self.config_data, cfg_note = load_config()

        # Header bar
        head = ttk.Frame(self, style="Head.TFrame", padding=(14, 10))
        head.pack(fill="x")
        ttk.Label(head, text="ESP Flasher Companion", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="   build · flash · monitor",
                  style="Sub.TLabel").pack(side="left", pady=(4, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.flash_tab = ttk.Frame(nb, style="TFrame")
        self.usage_tab = ttk.Frame(nb, style="TFrame")
        nb.add(self.flash_tab, text="   Flash   ")
        nb.add(self.usage_tab, text="   Claude Usage   ")

        self._build_flash_tab()
        self._build_usage_tab()
        self.after(80, self._drain_log)
        self.refresh_ports()
        self._startup_checks(cfg_note)

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
        # Treeview (usage table)
        s.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=FG, borderwidth=0, rowheight=24)
        s.configure("Treeview.Heading", background=BTN, foreground=FG,
                    borderwidth=0, font=("Segoe UI Semibold", 9))
        s.map("Treeview.Heading", background=[("active", BTN_HI)])
        s.map("Treeview", background=[("selected", "#2f3450")])
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

    def _build_board_cards(self):
        for w in self.boards_frame.winfo_children():
            w.destroy()
        self.action_buttons = []
        self.port_combos = {}        # target name -> per-board port combobox
        for t in self.config_data.get("targets", []):
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

        # Row 2 — per-board port (remembered in config) + actions
        r2 = ttk.Frame(f, style="Card.TFrame")
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="Port", style="CardPath.TLabel").pack(side="left")
        combo = ttk.Combobox(r2, width=30, state="readonly")
        combo.pack(side="left", padx=(6, 14))
        if t.get("port"):
            combo.set(t["port"])
        combo.bind("<<ComboboxSelected>>",
                   lambda e, t=t, c=combo: self._save_port(t, c.get()))
        self.port_combos[t["name"]] = combo

        b1 = ttk.Button(r2, text="⚡ Build + Flash", style="Accent.TButton",
                        command=lambda t=t: self.run_task(self.task_build_flash, t))
        b2 = ttk.Button(r2, text="Restore Bootloader",
                        command=lambda t=t: self.run_task(self.task_bootloader, t))
        b3 = ttk.Button(r2, text="Identify",
                        command=lambda t=t: self.run_task(self.task_identify, t))
        b1_state = "disabled" if missing else "normal"
        b1.config(state=b1_state)
        for b, idle in ((b1, b1_state), (b2, "normal"), (b3, "normal")):
            b.pack(side="left", padx=4)
            self.action_buttons.append((b, idle))

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
                "port":        "",
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

    def _save_port(self, t, value):
        port = value.split(" ", 1)[0].strip()
        t["port"] = port
        self._write_config()
        self.log_line("[OK] %s -> %s (remembered)" % (t["name"], port), "ok")

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
        self._build_board_cards()
        self.log_line(note or "[OK] Config reloaded — %d board(s)."
                      % len(self.config_data.get("targets", [])), "ok")

    # ---------------- Usage tab ----------------
    def _build_usage_tab(self):
        top = ttk.Frame(self.usage_tab, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="Refresh Usage", command=self.refresh_usage).pack(side="left")
        self.usage_status = ttk.Label(top, text="")
        self.usage_status.pack(side="left", padx=10)

        cols = ("date", "model", "inp", "out", "cread", "cwrite", "msgs", "cost")
        heads = ("Date", "Model", "Input", "Output", "Cache read",
                 "Cache write", "Msgs", "Est. $")
        self.tree = ttk.Treeview(self.usage_tab, columns=cols, show="headings")
        for c, h in zip(cols, heads):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=110, anchor="e")
        self.tree.column("date", width=95, anchor="w")
        self.tree.column("model", width=230, anchor="w")
        ys = ttk.Scrollbar(self.usage_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Label(self.usage_tab, foreground=SUB, text=(
            "Token counts are exact (from ~/.claude session logs). $ is a rough "
            "estimate — edit PRICING at the top of this file to tune."
        )).pack(pady=(0, 8))

    def refresh_usage(self):
        self.usage_status.config(text="Scanning logs...")
        self.update_idletasks()

        def work():
            agg, err = scan_claude_usage(days=7)
            self.after(0, lambda: self._fill_usage(agg, err))
        threading.Thread(target=work, daemon=True).start()

    def _fill_usage(self, agg, err):
        for i in self.tree.get_children():
            self.tree.delete(i)
        if err:
            self.usage_status.config(text=err)
            return
        today = datetime.now().strftime("%Y-%m-%d")
        tot = {"in": 0, "out": 0, "cr": 0, "cw": 0, "n": 0}
        cost_tot = 0.0
        have_cost = False
        for (date_str, model), a in sorted(agg.items(), reverse=True):
            cost = estimate_cost(model, a)
            if cost is not None:
                cost_tot += cost
                have_cost = True
            tag = "today" if date_str == today else ""
            self.tree.insert("", "end", tags=(tag,), values=(
                date_str, model, f"{a['in']:,}", f"{a['out']:,}",
                f"{a['cr']:,}", f"{a['cw']:,}", a["n"],
                f"{cost:.2f}" if cost is not None else "-"))
            for k in tot:
                tot[k] += a[k]
        self.tree.insert("", "end", values=(
            "TOTAL 7d", "", f"{tot['in']:,}", f"{tot['out']:,}",
            f"{tot['cr']:,}", f"{tot['cw']:,}", tot["n"],
            f"{cost_tot:.2f}" if have_cost else "-"))
        self.tree.tag_configure("today", background="#1f3d2b")
        self.usage_status.config(
            text="Done — %d day/model rows. Today is highlighted." % len(agg))

    # ---------------- logging / task plumbing ----------------
    def log_line(self, text, tag=None):
        self.log_q.put((text, tag))

    def _drain_log(self):
        try:
            while True:
                text, tag = self.log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", text + "\n", tag or ())
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    def set_busy(self, busy):
        for b, idle in self.action_buttons:
            b.config(state="disabled" if busy else idle)
        self.stop_btn.config(state="normal" if busy else "disabled")

    def run_task(self, fn, target):
        if self.worker and self.worker.is_alive():
            self.log_line("[!] A task is already running.", "err")
            return
        self.set_busy(True)

        def wrapped():
            try:
                fn(target)
            except Exception as e:
                self.log_line("[X] %s" % e, "err")
            finally:
                self.after(0, lambda: self.set_busy(False))
        self.worker = threading.Thread(target=wrapped, daemon=True)
        self.worker.start()

    def stop_task(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            self.log_line("[!] Task stopped by user.", "err")

    def run_cmd(self, cmd, capture=False):
        """Stream a subprocess into the log. Returns (rc, captured_text)."""
        self.log_line("$ " + " ".join(str(c) for c in cmd), "hdr")
        out = []
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            out.append(line)
            self.log_line(line)
        self.proc.wait()
        rc = self.proc.returncode
        self.proc = None
        return rc, "\n".join(out)

    def need_port(self, t):
        port = ""
        combo = self.port_combos.get(t["name"])
        if combo is not None:
            port = combo.get().split(" ", 1)[0].strip()
        if not port:
            port = (t.get("port") or "").strip()
        if not port:
            raise RuntimeError("No port set for '%s' — pick one on its card "
                               "(it will be remembered)." % t["name"])
        return port

    def refresh_ports(self):
        try:
            import serial.tools.list_ports as lp
            ports = ["%s  %s" % (p.device, p.description or "") for p in lp.comports()]
        except Exception as e:
            ports = []
            self.log_line("[X] pyserial missing? %s" % e, "err")
        for combo in self.port_combos.values():
            current = combo.get()
            combo["values"] = ports
            if current:                 # keep the remembered choice selected
                combo.set(current)

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

    # ---------------- the actual tasks ----------------
    def task_identify(self, t):
        port = self.need_port(t)
        self.log_line("== Identify board on %s ==" % port, "hdr")
        self.run_cmd(esptool_cmd("-p", port, "flash_id"))

    def _guard_board(self, t, port):
        """flash_id + verify chip family and real flash size. Raises on mismatch."""
        self.log_line("== Guard: identifying board on %s ==" % port, "hdr")
        rc, out = self.run_cmd(esptool_cmd("-p", port, "flash_id"))
        if rc != 0:
            raise RuntimeError("Could not identify board (esptool rc=%d). "
                               "Close any serial monitor and retry." % rc)
        if t["expect_chip"] not in out:
            raise RuntimeError(
                "ABORT: board is not %s. Flashing this bootloader onto a "
                "different chip would brick it." % t["expect_chip"])
        m = re.search(r"Detected flash size:\s*(\S+)", out)
        size = m.group(1) if m else "?"
        if size != t["expect_size"]:
            raise RuntimeError(
                "ABORT: detected flash size %s, bootloader needs %s. "
                "A size mismatch silently corrupts NVS (settings won't "
                "persist)." % (size, t["expect_size"]))
        self.log_line("[OK] %s / %s — matches bootloader."
                      % (t["expect_chip"], size), "ok")

    def task_bootloader(self, t):
        port = self.need_port(t)
        boot = Path(t["bootloader"])
        if not boot.is_file():
            raise RuntimeError("Bootloader bin not found: %s" % boot)
        self._guard_board(t, port)
        self.log_line("== Flashing custom bootloader at 0x0 (app/NVS untouched) ==", "hdr")
        rc, _ = self.run_cmd(esptool_cmd("--chip", t["chip"], "-p", port,
                                         "write_flash", "0x0", str(boot)))
        if rc == 0:
            self.log_line("[DONE] Custom bootloader restored. Reboot the board "
                          "and confirm a saved setting persists.", "ok")
        else:
            raise RuntimeError("Bootloader flash failed (rc=%d)." % rc)

    def task_build_flash(self, t):
        port = self.need_port(t)
        acli = find_arduino_cli()
        if not acli:
            raise RuntimeError("arduino-cli not found.")
        sketch = Path(t["sketch"])
        build_out = BUILD_ROOT / re.sub(r"\W+", "_", t["name"]).strip("_")
        build_out.mkdir(parents=True, exist_ok=True)

        self.log_line("== Compiling %s (verbose) ==" % sketch.name, "hdr")
        cmd = [acli]
        ide_cfg = self.config_data.get("arduino_cli_config_file")
        if ide_cfg and Path(ide_cfg).is_file():
            cmd += ["--config-file", ide_cfg]    # same libraries as the IDE
        cmd += ["compile", "--verbose", "--fqbn", t["fqbn"],
                "--output-dir", str(build_out)]
        if t.get("libraries"):                   # optional extra libraries root
            cmd += ["--libraries", t["libraries"]]
        cmd.append(str(sketch))
        rc, out = self.run_cmd(cmd)
        if rc != 0 and "needs to be reinitialized" in out:
            # First use of the IDE's config file: arduino-cli must download the
            # package indexes it references (e.g. extra board URLs) once.
            self.log_line("[!] arduino-cli indexes missing for this config — "
                          "running one-time update-index, then retrying...", "hdr")
            base = [acli]
            if ide_cfg and Path(ide_cfg).is_file():
                base += ["--config-file", ide_cfg]
            self.run_cmd(base + ["core", "update-index"])
            self.run_cmd(base + ["lib", "update-index"])
            rc, out = self.run_cmd(cmd)
        if rc != 0:
            raise RuntimeError("Compile FAILED — nothing flashed.")

        bins = sorted(build_out.glob("*.ino.bin"))
        if not bins:
            raise RuntimeError("No .ino.bin produced in %s" % build_out)
        appbin = bins[0]
        self.log_line("[OK] Compiled: %s" % appbin, "ok")

        self.log_line("== Flashing app at %s on %s (bootloader/NVS untouched) =="
                      % (t["app_addr"], port), "hdr")
        rc, _ = self.run_cmd(esptool_cmd("--chip", t["chip"], "-p", port,
                                         "write_flash", t["app_addr"], str(appbin)))
        if rc == 0:
            self.log_line("[DONE] Built and flashed. Custom bootloader and "
                          "saved config are intact.", "ok")
        else:
            raise RuntimeError("Flash failed (rc=%d). Hold BOOT, tap RESET, "
                               "release BOOT, then retry." % rc)


if __name__ == "__main__":
    app = CompanionApp()
    app.mainloop()
