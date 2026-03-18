"""
Project Gorgon Chat Alert
  - Incoming [Tell] messages
  - Friend online / offline notifications
  - Trade chat keyword search
Tkinter GUI with per-event sound selection, enable checkboxes,
master / individual volume control, and persistent settings.
"""

import json
import os
import re
import sys
import glob
import threading
import tkinter as tk
from tkinter import ttk, colorchooser, font as tkfont

import pygame
import ctypes
import ctypes.wintypes

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CHAT_LOG_DIR = os.path.normpath(os.path.join(
    os.environ["APPDATA"], "..", "LocalLow", "Elder Game",
    "Project Gorgon", "ChatLogs",
))

if getattr(sys, "frozen", False):
    _BASE_DIR = sys._MEIPASS
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SOUNDS_DIR = os.path.join(_BASE_DIR, "sounds")
SETTINGS_FILE = os.path.join(
    os.environ["APPDATA"], "PGAlerts", "settings.json")
POLL_INTERVAL_MS = 1000

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
TELL_PATTERN = re.compile(r"\[Tell\]\s+(?!You->)(\S+)->You:\s+(.*)")
FRIEND_ONLINE_PATTERN = re.compile(
    r"\[Status\]\s+Your friend (\S+) is now online\.")
FRIEND_OFFLINE_PATTERN = re.compile(
    r"\[Status\]\s+Your friend (\S+) is now offline\.")
TRADE_PATTERN = re.compile(r"\[Trade\]\s+(\S+):\s+(.*)")
XP_PATTERN = re.compile(r"\[Status\]\s+You earned (\d+) XP in (.+?)\.")
LOOT_PATTERN = re.compile(r"\[Status\]\s+(.+?)\s+added to inventory\.")

# Event keys
EVENT_TELL = "tell"
EVENT_FRIEND_ON = "friend_online"
EVENT_FRIEND_OFF = "friend_offline"
EVENT_TRADE = "trade"

ALL_EVENTS = (EVENT_TELL, EVENT_FRIEND_ON, EVENT_FRIEND_OFF, EVENT_TRADE)

EVENT_LABELS = {
    EVENT_TELL: "Incoming Tell",
    EVENT_FRIEND_ON: "Friend Online",
    EVENT_FRIEND_OFF: "Friend Offline",
    EVENT_TRADE: "Trade Search",
}

TOAST_COLORS = {
    EVENT_TELL: "#e6a800",
    EVENT_FRIEND_ON: "#44cc44",
    EVENT_FRIEND_OFF: "#cc4444",
    EVENT_TRADE: "#55aaff",
}

TOAST_MIN_WIDTH = 350
TOAST_PADDING = 12

OVERLAY_MIN_WIDTH = 300
OVERLAY_DEFAULT_FONT_SIZE = 11
OVERLAY_DEFAULT_XP_COLOR = "#ffdd44"
OVERLAY_DEFAULT_LOOT_COLOR = "#44ddff"
TOAST_DEFAULT_FONT_SIZE = 10
FONT_SIZE_OPTIONS = [str(s) for s in range(8, 25)]

# Win32 constants for click-through
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_latest_chat_log(log_dir: str) -> str | None:
    files = glob.glob(os.path.join(log_dir, "Chat-*.log"))
    return max(files, key=os.path.getmtime) if files else None


def discover_sounds(sounds_dir: str) -> list[str]:
    exts = (".wav", ".mp3", ".ogg")
    if not os.path.isdir(sounds_dir):
        return []
    return sorted(
        f for f in os.listdir(sounds_dir)
        if os.path.splitext(f)[1].lower() in exts
    )


def get_pg_monitor_work_area() -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) work area of the monitor
    that the Project Gorgon window is on, or the primary monitor."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Project Gorgon")
        MONITOR_DEFAULTTOPRIMARY = 1
        MONITOR_DEFAULTTONEAREST = 2

        if hwnd:
            hmon = user32.MonitorFromWindow(
                hwnd, MONITOR_DEFAULTTONEAREST)
        else:
            hmon = user32.MonitorFromWindow(0, MONITOR_DEFAULTTOPRIMARY)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))

        work = mi.rcWork
        return (work.left, work.top, work.right, work.bottom)
    except Exception:
        return (0, 0, 1920, 1080)


def make_click_through(widget: tk.Toplevel) -> None:
    """Make a Toplevel window click-through on Windows."""
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception:
        pass


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class PGAlertApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PG Alerts")
        self.root.resizable(False, False)

        self.monitoring = False
        self.current_file: str | None = None
        self.file_handle = None
        self.file_pos = 0
        self.poll_id: str | None = None

        pygame.mixer.init()

        self.sound_files = discover_sounds(SOUNDS_DIR)
        self.settings = load_settings()

        # Per-event variables
        self.events: dict[str, dict] = {}
        for key in ALL_EVENTS:
            self.events[key] = {
                "sound_var": tk.StringVar(value=self._saved(key, "sound")),
                "enabled_var": tk.BooleanVar(
                    value=self._saved(key, "enabled", True)),
                "volume_var": tk.DoubleVar(
                    value=self._saved(key, "volume", 0.7)),
            }

        # Trade keywords
        self.trade_keywords_var = tk.StringVar(
            value=self.settings.get("trade_keywords", ""))

        # Volume mode: "master" or "individual"
        self.vol_mode_var = tk.StringVar(
            value=self.settings.get("vol_mode", "master"))
        self.master_vol_var = tk.DoubleVar(
            value=self.settings.get("volume", 0.7))

        # Toast settings
        self.toast_enabled_var = tk.BooleanVar(
            value=self.settings.get("toast_enabled", True))
        self.toast_duration_var = tk.StringVar(
            value=self.settings.get("toast_duration", "3s"))
        self.toast_anchor_x: int | None = self.settings.get(
            "toast_anchor_x", None)
        self.toast_anchor_y: int | None = self.settings.get(
            "toast_anchor_y", None)
        self._active_toasts: list[tk.Toplevel] = []
        self._anchor_window: tk.Toplevel | None = None
        self.toast_font_size_var = tk.StringVar(
            value=self.settings.get(
                "toast_font_size", str(TOAST_DEFAULT_FONT_SIZE)))

        # XP overlay settings
        self.xp_overlay_enabled_var = tk.BooleanVar(
            value=self.settings.get("xp_overlay_enabled", True))
        self.xp_overlay_duration_var = tk.StringVar(
            value=self.settings.get("xp_overlay_duration", "3s"))
        self.xp_anchor_x: int | None = self.settings.get(
            "xp_anchor_x", None)
        self.xp_anchor_y: int | None = self.settings.get(
            "xp_anchor_y", None)
        self._active_xp_overlays: list[tk.Toplevel] = []
        self._xp_anchor_window: tk.Toplevel | None = None
        self.xp_color_var = tk.StringVar(
            value=self.settings.get("xp_color", OVERLAY_DEFAULT_XP_COLOR))
        self.xp_font_size_var = tk.StringVar(
            value=self.settings.get(
                "xp_font_size", str(OVERLAY_DEFAULT_FONT_SIZE)))

        # Loot overlay settings
        self.loot_overlay_enabled_var = tk.BooleanVar(
            value=self.settings.get("loot_overlay_enabled", True))
        self.loot_overlay_duration_var = tk.StringVar(
            value=self.settings.get("loot_overlay_duration", "3s"))
        self.loot_anchor_x: int | None = self.settings.get(
            "loot_anchor_x", None)
        self.loot_anchor_y: int | None = self.settings.get(
            "loot_anchor_y", None)
        self._active_loot_overlays: list[tk.Toplevel] = []
        self._loot_anchor_window: tk.Toplevel | None = None
        self.loot_color_var = tk.StringVar(
            value=self.settings.get(
                "loot_color", OVERLAY_DEFAULT_LOOT_COLOR))
        self.loot_font_size_var = tk.StringVar(
            value=self.settings.get(
                "loot_font_size", str(OVERLAY_DEFAULT_FONT_SIZE)))

        self._build_ui()
        self._update_volume_visibility()
        self._check_chat_dir()

    # ---- Settings helpers -------------------------------------------------
    def _saved(self, event_key: str, field: str, default=None):
        events_cfg = self.settings.get("events", {})
        val = events_cfg.get(event_key, {}).get(field)
        if val is not None:
            return val
        if field == "sound":
            return self.sound_files[0] if self.sound_files else ""
        if field == "enabled":
            return default if default is not None else True
        if field == "volume":
            return default if default is not None else 0.7
        return default

    def _persist(self, *_args):
        data: dict = {
            "volume": round(self.master_vol_var.get(), 2),
            "vol_mode": self.vol_mode_var.get(),
            "trade_keywords": self.trade_keywords_var.get(),
            "toast_enabled": self.toast_enabled_var.get(),
            "toast_duration": self.toast_duration_var.get(),
            "toast_anchor_x": self.toast_anchor_x,
            "toast_anchor_y": self.toast_anchor_y,
            "toast_font_size": self.toast_font_size_var.get(),
            "xp_overlay_enabled": self.xp_overlay_enabled_var.get(),
            "xp_overlay_duration": self.xp_overlay_duration_var.get(),
            "xp_anchor_x": self.xp_anchor_x,
            "xp_anchor_y": self.xp_anchor_y,
            "loot_overlay_enabled": self.loot_overlay_enabled_var.get(),
            "loot_overlay_duration": self.loot_overlay_duration_var.get(),
            "loot_anchor_x": self.loot_anchor_x,
            "loot_anchor_y": self.loot_anchor_y,
            "xp_color": self.xp_color_var.get(),
            "xp_font_size": self.xp_font_size_var.get(),
            "loot_color": self.loot_color_var.get(),
            "loot_font_size": self.loot_font_size_var.get(),
            "events": {},
        }
        for key, ev in self.events.items():
            data["events"][key] = {
                "sound": ev["sound_var"].get(),
                "enabled": ev["enabled_var"].get(),
                "volume": round(ev["volume_var"].get(), 2),
            }
        save_settings(data)

    # ---- UI ---------------------------------------------------------------
    def _build_ui(self):
        pad = dict(padx=8, pady=4)

        # --- Tabbed notebook ---
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        tab_alerts = ttk.Frame(self.notebook)
        tab_overlays = ttk.Frame(self.notebook)
        self.notebook.add(tab_alerts, text="Alerts")
        self.notebook.add(tab_overlays, text="Overlays")

        # ==================================================================
        # ALERTS TAB
        # ==================================================================

        # --- Per-event rows ---
        frame_events = ttk.LabelFrame(tab_alerts, text="Alert Events")
        frame_events.pack(fill="x", **pad)

        headers = ["Enable", "Event", "Sound", ""]
        for col, h in enumerate(headers):
            ttk.Label(frame_events, text=h, font=("", 9, "bold")).grid(
                row=0, column=col, padx=4, pady=(4, 2), sticky="w")

        for row_idx, key in enumerate(ALL_EVENTS, start=1):
            ev = self.events[key]

            cb = ttk.Checkbutton(
                frame_events, variable=ev["enabled_var"],
                command=self._persist)
            cb.grid(row=row_idx, column=0, padx=4, pady=2)

            ttk.Label(frame_events, text=EVENT_LABELS[key]).grid(
                row=row_idx, column=1, sticky="w", padx=4, pady=2)

            combo = ttk.Combobox(
                frame_events, textvariable=ev["sound_var"],
                values=self.sound_files, state="readonly", width=24)
            combo.grid(row=row_idx, column=2, padx=4, pady=2)
            combo.bind("<<ComboboxSelected>>", self._persist)

            btn = ttk.Button(
                frame_events, text="\u25b6",
                command=lambda k=key: self._preview_event(k), width=3)
            btn.grid(row=row_idx, column=3, padx=4, pady=2)

        frame_events.columnconfigure(2, weight=1)

        # --- Trade keywords ---
        frame_trade = ttk.LabelFrame(
            tab_alerts, text="Trade Search Keywords (comma-separated)")
        frame_trade.pack(fill="x", **pad)

        self.trade_entry = ttk.Entry(
            frame_trade, textvariable=self.trade_keywords_var, width=58)
        self.trade_entry.pack(fill="x", padx=8, pady=6)
        self.trade_entry.bind("<FocusOut>", self._persist)
        self.trade_entry.bind("<Return>", self._persist)

        # --- Volume section ---
        frame_vol = ttk.LabelFrame(tab_alerts, text="Volume")
        frame_vol.pack(fill="x", **pad)

        # Mode selector row
        mode_row = ttk.Frame(frame_vol)
        mode_row.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Radiobutton(
            mode_row, text="Master", variable=self.vol_mode_var,
            value="master",
            command=self._on_vol_mode_change).pack(side="left")
        ttk.Radiobutton(
            mode_row, text="Individual", variable=self.vol_mode_var,
            value="individual",
            command=self._on_vol_mode_change).pack(side="left", padx=(12, 0))

        # Master slider
        self.master_vol_frame = ttk.Frame(frame_vol)
        self.master_vol_frame.pack(fill="x", padx=8, pady=4)

        self.master_slider = ttk.Scale(
            self.master_vol_frame, from_=0.0, to=1.0, orient="horizontal",
            variable=self.master_vol_var,
            command=self._on_master_vol_change)
        self.master_slider.pack(side="left", fill="x", expand=True)
        self.master_vol_label = ttk.Label(
            self.master_vol_frame,
            text=f"{int(self.master_vol_var.get() * 100)}%", width=5)
        self.master_vol_label.pack(side="left", padx=(4, 0))

        # Individual sliders
        self.indiv_vol_frame = ttk.Frame(frame_vol)
        self.indiv_vol_frame.pack(fill="x", padx=8, pady=4)

        self.indiv_vol_labels: dict[str, ttk.Label] = {}
        for row_idx, key in enumerate(ALL_EVENTS):
            ev = self.events[key]
            ttk.Label(self.indiv_vol_frame,
                      text=EVENT_LABELS[key] + ":").grid(
                row=row_idx, column=0, sticky="w", padx=(0, 4), pady=1)
            slider = ttk.Scale(
                self.indiv_vol_frame, from_=0.0, to=1.0,
                orient="horizontal", variable=ev["volume_var"],
                command=lambda _v, k=key: self._on_indiv_vol_change(k))
            slider.grid(row=row_idx, column=1, sticky="ew", pady=1)
            lbl = ttk.Label(
                self.indiv_vol_frame,
                text=f"{int(ev['volume_var'].get() * 100)}%", width=5)
            lbl.grid(row=row_idx, column=2, padx=(4, 0), pady=1)
            self.indiv_vol_labels[key] = lbl

        self.indiv_vol_frame.columnconfigure(1, weight=1)

        # --- Start / Stop ---
        frame_ctrl = ttk.Frame(tab_alerts)
        frame_ctrl.pack(fill="x", **pad)

        self.btn_toggle = ttk.Button(
            frame_ctrl, text="Start Monitoring", command=self._toggle)
        self.btn_toggle.pack(side="left")

        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(frame_ctrl, textvariable=self.status_var).pack(
            side="left", padx=12)

        # --- Alert log ---
        frame_log = ttk.LabelFrame(tab_alerts, text="Alert Log")
        frame_log.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(
            frame_log, height=14, width=64, state="disabled",
            wrap="word", font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(
            frame_log, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log_text.tag_configure("sender", foreground="#e6a800")
        self.log_text.tag_configure("online", foreground="#44cc44")
        self.log_text.tag_configure("offline", foreground="#cc4444")
        self.log_text.tag_configure("trade", foreground="#55aaff")
        self.log_text.tag_configure("info", foreground="#888888")

        # ==================================================================
        # OVERLAYS TAB
        # ==================================================================

        # --- Toast notifications ---
        frame_toast = ttk.LabelFrame(
            tab_overlays, text="Toast Notifications")
        frame_toast.pack(fill="x", **pad)

        toast_row = ttk.Frame(frame_toast)
        toast_row.pack(fill="x", padx=8, pady=6)

        ttk.Checkbutton(
            toast_row, text="Enable Toast",
            variable=self.toast_enabled_var,
            command=self._persist).pack(side="left")

        ttk.Label(toast_row, text="Duration:").pack(
            side="left", padx=(16, 4))
        toast_dur = ttk.Combobox(
            toast_row, textvariable=self.toast_duration_var,
            values=["1s", "2s", "3s", "4s", "5s",
                    "6s", "7s", "8s", "9s", "10s"],
            state="readonly", width=4)
        toast_dur.pack(side="left")
        toast_dur.bind("<<ComboboxSelected>>", self._persist)

        self.btn_anchor = ttk.Button(
            toast_row, text="Move Anchor",
            command=self._toggle_anchor)
        self.btn_anchor.pack(side="right")

        toast_row2 = ttk.Frame(frame_toast)
        toast_row2.pack(fill="x", padx=8, pady=(2, 6))

        ttk.Label(toast_row2, text="Font Size:").pack(
            side="left")
        toast_fs = ttk.Combobox(
            toast_row2, textvariable=self.toast_font_size_var,
            values=FONT_SIZE_OPTIONS, state="readonly", width=4)
        toast_fs.pack(side="left", padx=(4, 0))
        toast_fs.bind("<<ComboboxSelected>>", self._persist)

        # --- Combat XP overlay ---
        frame_xp = ttk.LabelFrame(
            tab_overlays, text="Combat XP Overlay")
        frame_xp.pack(fill="x", **pad)

        xp_row1 = ttk.Frame(frame_xp)
        xp_row1.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Checkbutton(
            xp_row1, text="Enable",
            variable=self.xp_overlay_enabled_var,
            command=self._persist).pack(side="left")

        ttk.Label(xp_row1, text="Duration:").pack(
            side="left", padx=(16, 4))
        xp_dur = ttk.Combobox(
            xp_row1, textvariable=self.xp_overlay_duration_var,
            values=["1s", "2s", "3s", "4s", "5s"],
            state="readonly", width=4)
        xp_dur.pack(side="left")
        xp_dur.bind("<<ComboboxSelected>>", self._persist)

        self.btn_xp_anchor = ttk.Button(
            xp_row1, text="Move Anchor",
            command=self._toggle_xp_anchor)
        self.btn_xp_anchor.pack(side="right")

        xp_row2 = ttk.Frame(frame_xp)
        xp_row2.pack(fill="x", padx=8, pady=(2, 6))

        ttk.Label(xp_row2, text="Font Size:").pack(
            side="left")
        xp_fs = ttk.Combobox(
            xp_row2, textvariable=self.xp_font_size_var,
            values=FONT_SIZE_OPTIONS, state="readonly", width=4)
        xp_fs.pack(side="left", padx=(4, 0))
        xp_fs.bind("<<ComboboxSelected>>", self._persist)

        ttk.Label(xp_row2, text="Color:").pack(
            side="left", padx=(16, 4))
        self.xp_color_swatch = tk.Label(
            xp_row2, width=3, bg=self.xp_color_var.get(),
            relief="solid", borderwidth=1)
        self.xp_color_swatch.pack(side="left")
        ttk.Button(
            xp_row2, text="Pick",
            command=self._pick_xp_color, width=5).pack(
            side="left", padx=(4, 0))

        # --- Loot Pickup overlay ---
        frame_loot = ttk.LabelFrame(
            tab_overlays, text="Loot Pickup Overlay")
        frame_loot.pack(fill="x", **pad)

        loot_row1 = ttk.Frame(frame_loot)
        loot_row1.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Checkbutton(
            loot_row1, text="Enable",
            variable=self.loot_overlay_enabled_var,
            command=self._persist).pack(side="left")

        ttk.Label(loot_row1, text="Duration:").pack(
            side="left", padx=(16, 4))
        loot_dur = ttk.Combobox(
            loot_row1, textvariable=self.loot_overlay_duration_var,
            values=["1s", "2s", "3s", "4s", "5s"],
            state="readonly", width=4)
        loot_dur.pack(side="left")
        loot_dur.bind("<<ComboboxSelected>>", self._persist)

        self.btn_loot_anchor = ttk.Button(
            loot_row1, text="Move Anchor",
            command=self._toggle_loot_anchor)
        self.btn_loot_anchor.pack(side="right")

        loot_row2 = ttk.Frame(frame_loot)
        loot_row2.pack(fill="x", padx=8, pady=(2, 6))

        ttk.Label(loot_row2, text="Font Size:").pack(
            side="left")
        loot_fs = ttk.Combobox(
            loot_row2, textvariable=self.loot_font_size_var,
            values=FONT_SIZE_OPTIONS, state="readonly", width=4)
        loot_fs.pack(side="left", padx=(4, 0))
        loot_fs.bind("<<ComboboxSelected>>", self._persist)

        ttk.Label(loot_row2, text="Color:").pack(
            side="left", padx=(16, 4))
        self.loot_color_swatch = tk.Label(
            loot_row2, width=3, bg=self.loot_color_var.get(),
            relief="solid", borderwidth=1)
        self.loot_color_swatch.pack(side="left")
        ttk.Button(
            loot_row2, text="Pick",
            command=self._pick_loot_color, width=5).pack(
            side="left", padx=(4, 0))

    # ---- Color pickers ----------------------------------------------------
    def _pick_xp_color(self):
        color = colorchooser.askcolor(
            color=self.xp_color_var.get(),
            title="XP Overlay Color")[1]
        if color:
            self.xp_color_var.set(color)
            self.xp_color_swatch.configure(bg=color)
            self._persist()

    def _pick_loot_color(self):
        color = colorchooser.askcolor(
            color=self.loot_color_var.get(),
            title="Loot Overlay Color")[1]
        if color:
            self.loot_color_var.set(color)
            self.loot_color_swatch.configure(bg=color)
            self._persist()

    # ---- Volume controls --------------------------------------------------
    def _on_vol_mode_change(self):
        self._update_volume_visibility()
        self._persist()

    def _update_volume_visibility(self):
        if self.vol_mode_var.get() == "master":
            self.master_vol_frame.pack(fill="x", padx=8, pady=4)
            self.indiv_vol_frame.pack_forget()
        else:
            self.master_vol_frame.pack_forget()
            self.indiv_vol_frame.pack(fill="x", padx=8, pady=4)

    def _on_master_vol_change(self, _=None):
        vol = self.master_vol_var.get()
        self.master_vol_label.configure(text=f"{int(vol * 100)}%")
        self._persist()

    def _on_indiv_vol_change(self, event_key: str):
        vol = self.events[event_key]["volume_var"].get()
        self.indiv_vol_labels[event_key].configure(
            text=f"{int(vol * 100)}%")
        self._persist()

    def _get_volume(self, event_key: str) -> float:
        if self.vol_mode_var.get() == "master":
            return self.master_vol_var.get()
        return self.events[event_key]["volume_var"].get()

    # ---- Toast notifications ----------------------------------------------
    def _get_toast_position(self) -> tuple[int, int]:
        """Return (x, y) for the toast anchor point."""
        if self.toast_anchor_x is not None and self.toast_anchor_y is not None:
            return (self.toast_anchor_x, self.toast_anchor_y)
        left, top, right, bottom = get_pg_monitor_work_area()
        return (right - TOAST_MIN_WIDTH - TOAST_PADDING,
                bottom - 80 - TOAST_PADDING)

    def _toggle_anchor(self):
        if self._anchor_window is not None:
            self.toast_anchor_x = self._anchor_window.winfo_x()
            self.toast_anchor_y = self._anchor_window.winfo_y()
            self._anchor_window.destroy()
            self._anchor_window = None
            self.btn_anchor.configure(text="Move Anchor")
            self._persist()
        else:
            x, y = self._get_toast_position()
            t_fs = int(self.toast_font_size_var.get())
            t_w = max(TOAST_MIN_WIDTH, t_fs * 30)
            t_h = t_fs * 5 + 20
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.6)
            win.configure(bg="#ffaa00")
            win.geometry(f"{t_w}x{t_h}+{x}+{y}")

            inner = tk.Frame(win, bg="#333333")
            inner.place(x=3, y=3, width=t_w - 6,
                        height=t_h - 6)

            lbl = tk.Label(
                inner,
                text="Drag to reposition\nClick 'Save Anchor' to confirm",
                font=("Segoe UI", 9), fg="#ffaa00", bg="#333333")
            lbl.place(relx=0.5, rely=0.5, anchor="center")

            self._drag_data = {"x": 0, "y": 0}
            for w in (win, inner, lbl):
                w.bind("<Button-1>", self._anchor_start_drag)
                w.bind("<B1-Motion>", self._anchor_on_drag)

            self._anchor_window = win
            self.btn_anchor.configure(text="Save Anchor")

    def _anchor_start_drag(self, event):
        self._drag_data["x"] = event.x_root - self._anchor_window.winfo_x()
        self._drag_data["y"] = event.y_root - self._anchor_window.winfo_y()

    def _anchor_on_drag(self, event):
        x = event.x_root - self._drag_data["x"]
        y = event.y_root - self._drag_data["y"]
        self._anchor_window.geometry(f"+{x}+{y}")

    def _show_toast(self, event_key: str, message: str):
        if not self.toast_enabled_var.get():
            return

        self._active_toasts = [
            t for t in self._active_toasts if t.winfo_exists()]

        dur_str = self.toast_duration_var.get()
        duration_ms = int(dur_str.replace("s", "")) * 1000
        t_fs = int(self.toast_font_size_var.get())
        color = TOAST_COLORS.get(event_key, "#ffffff")

        # Measure text width to determine toast size
        title_font = tkfont.Font(family="Segoe UI", size=t_fs, weight="bold")
        body_font = tkfont.Font(family="Segoe UI", size=t_fs - 1)
        title_w = title_font.measure(EVENT_LABELS[event_key]) + 24
        body_w = body_font.measure(message) + 24
        t_w = max(TOAST_MIN_WIDTH, title_w, body_w)
        t_h = t_fs * 5 + 20

        base_x, base_y = self._get_toast_position()
        offset = len(self._active_toasts) * (t_h + 4)

        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.attributes("-alpha", 0.92)
        toast.configure(bg="#555555")
        toast.geometry(
            f"{t_w}x{t_h}"
            f"+{base_x}+{base_y - offset}")

        content = tk.Frame(toast, bg="#2b2b2b")
        content.place(x=1, y=1, width=t_w - 2,
                      height=t_h - 2)

        tk.Label(
            content, text=EVENT_LABELS[event_key],
            font=title_font,
            fg=color, bg="#2b2b2b", anchor="w"
        ).pack(fill="x", padx=10, pady=(8, 0))

        tk.Label(
            content, text=message, font=body_font,
            fg="#dddddd", bg="#2b2b2b",
            wraplength=t_w - 24,
            justify="left", anchor="w"
        ).pack(fill="x", padx=10, pady=(2, 8))

        self._active_toasts.append(toast)

        def _close():
            try:
                if toast.winfo_exists():
                    toast.destroy()
                if toast in self._active_toasts:
                    self._active_toasts.remove(toast)
            except tk.TclError:
                pass

        toast.after(duration_ms, _close)

    # ---- Click-through overlays -------------------------------------------
    def _get_overlay_position(self, kind: str) -> tuple[int, int]:
        """Return (x, y) for an overlay anchor. kind is 'xp' or 'loot'."""
        ax = getattr(self, f"{kind}_anchor_x")
        ay = getattr(self, f"{kind}_anchor_y")
        if ax is not None and ay is not None:
            return (ax, ay)
        left, top, right, bottom = get_pg_monitor_work_area()
        if kind == "xp":
            return (left + 20, bottom - 200)
        return (left + 20, bottom - 400)

    def _toggle_overlay_anchor(self, kind: str):
        anchor_attr = f"_{kind}_anchor_window"
        btn_attr = f"btn_{kind}_anchor"
        anchor_win = getattr(self, anchor_attr)
        btn = getattr(self, btn_attr)
        color = getattr(self, f"{kind}_color_var").get()
        label_text = "Combat XP" if kind == "xp" else "Loot Pickup"

        if anchor_win is not None:
            setattr(self, f"{kind}_anchor_x", anchor_win.winfo_x())
            setattr(self, f"{kind}_anchor_y", anchor_win.winfo_y())
            anchor_win.destroy()
            setattr(self, anchor_attr, None)
            btn.configure(text="Move Anchor")
            self._persist()
        else:
            x, y = self._get_overlay_position(kind)
            font_size = int(getattr(self, f"{kind}_font_size_var").get())
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.7)
            win.configure(bg=color)
            w = max(OVERLAY_MIN_WIDTH, font_size * 25)
            h = (font_size + 12) * 4
            win.geometry(f"{w}x{h}+{x}+{y}")

            inner = tk.Frame(win, bg="#222222")
            inner.place(x=2, y=2, width=w - 4, height=h - 4)

            lbl = tk.Label(
                inner,
                text=f"{label_text} Overlay\nDrag to reposition",
                font=("Segoe UI", 9), fg=color, bg="#222222")
            lbl.place(relx=0.5, rely=0.5, anchor="center")

            drag = {"x": 0, "y": 0}

            def start_drag(event, _d=drag, _w=win):
                _d["x"] = event.x_root - _w.winfo_x()
                _d["y"] = event.y_root - _w.winfo_y()

            def on_drag(event, _d=drag, _w=win):
                _w.geometry(
                    f"+{event.x_root - _d['x']}+{event.y_root - _d['y']}")

            for widget in (win, inner, lbl):
                widget.bind("<Button-1>", start_drag)
                widget.bind("<B1-Motion>", on_drag)

            setattr(self, anchor_attr, win)
            btn.configure(text="Save Anchor")

    def _toggle_xp_anchor(self):
        self._toggle_overlay_anchor("xp")

    def _toggle_loot_anchor(self):
        self._toggle_overlay_anchor("loot")

    def _show_overlay(self, kind: str, text: str):
        enabled_var = getattr(self, f"{kind}_overlay_enabled_var")
        if not enabled_var.get():
            return

        active_list = getattr(self, f"_active_{kind}_overlays")
        # Prune dead windows
        active_list[:] = [w for w in active_list if w.winfo_exists()]

        dur_str = getattr(self, f"{kind}_overlay_duration_var").get()
        duration_ms = int(dur_str.replace("s", "")) * 1000

        color = getattr(self, f"{kind}_color_var").get()
        font_size = int(getattr(self, f"{kind}_font_size_var").get())
        base_x, base_y = self._get_overlay_position(kind)
        line_h = font_size + 12
        offset = len(active_list) * line_h

        # Measure text to determine overlay width
        measure_font = tkfont.Font(
            family="Segoe UI", size=font_size, weight="bold")
        text_w = measure_font.measure(text) + 24
        overlay_w = max(OVERLAY_MIN_WIDTH, text_w)

        # Transparent background color key
        trans_color = "#010101"

        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.configure(bg=trans_color)
        overlay.attributes("-transparentcolor", trans_color)
        overlay.geometry(
            f"{overlay_w}x{line_h}"
            f"+{base_x}+{base_y - offset}")

        # Force window to render so winfo_id() returns a valid HWND
        overlay.update_idletasks()
        make_click_through(overlay)

        lbl = tk.Label(
            overlay, text=text,
            font=("Segoe UI", font_size, "bold"),
            fg=color, bg=trans_color, anchor="w")
        lbl.pack(fill="both", expand=True)

        active_list.append(overlay)

        def _close():
            try:
                if overlay.winfo_exists():
                    overlay.destroy()
                if overlay in active_list:
                    active_list.remove(overlay)
            except tk.TclError:
                pass

        overlay.after(duration_ms, _close)

    # ---- Actions ----------------------------------------------------------
    def _check_chat_dir(self):
        if not os.path.isdir(CHAT_LOG_DIR):
            self._log(
                "Chat log directory not found:\n" + CHAT_LOG_DIR, "info")
            self._log(
                "Make sure Project Gorgon has been run at least once, and ensure logging is enabled in the game settings.",
                "info")

    def _sound_path_for(self, event_key: str) -> str | None:
        name = self.events[event_key]["sound_var"].get()
        if not name:
            return None
        path = os.path.join(SOUNDS_DIR, name)
        return path if os.path.isfile(path) else None

    def _preview_event(self, event_key: str):
        path = self._sound_path_for(event_key)
        if path:
            self._play_sound(path, self._get_volume(event_key))

    def _play_sound(self, path: str, volume: float):
        def _do():
            try:
                sound = pygame.mixer.Sound(path)
                sound.set_volume(volume)
                sound.play()
            except Exception as e:
                self._log(f"[!] Sound error: {e}", "info")
        threading.Thread(target=_do, daemon=True).start()

    def _log(self, text: str, tag: str | None = None):
        self.log_text.configure(state="normal")
        if tag:
            self.log_text.insert("end", text + "\n", tag)
        else:
            self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---- Trade keyword matching -------------------------------------------
    def _get_trade_keywords(self) -> list[str]:
        raw = self.trade_keywords_var.get()
        return [kw.strip().lower() for kw in raw.split(",") if kw.strip()]

    def _matches_trade(self, message: str) -> str | None:
        """Return the first matching keyword, or None."""
        msg_lower = message.lower()
        for kw in self._get_trade_keywords():
            if kw in msg_lower:
                return kw
        return None

    # ---- Monitoring -------------------------------------------------------
    def _toggle(self):
        if self.monitoring:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    def _start_monitoring(self):
        if not os.path.isdir(CHAT_LOG_DIR):
            self._log("Cannot start — chat log directory not found.", "info")
            return
        self.monitoring = True
        self.current_file = None
        self.file_handle = None
        self.file_pos = 0
        self.btn_toggle.configure(text="Stop Monitoring")
        self.status_var.set("Monitoring...")
        self._log("--- Started monitoring ---", "info")
        self._poll()

    def _stop_monitoring(self):
        self.monitoring = False
        if self.poll_id:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None
        self.btn_toggle.configure(text="Start Monitoring")
        self.status_var.set("Stopped")
        self._log("--- Stopped monitoring ---", "info")

    def _poll(self):
        if not self.monitoring:
            return

        latest = get_latest_chat_log(CHAT_LOG_DIR)
        if latest and latest != self.current_file:
            if self.file_handle:
                self.file_handle.close()
            self.current_file = latest
            self.file_handle = open(
                self.current_file, "r", encoding="utf-8", errors="replace")
            self.file_handle.seek(0, os.SEEK_END)
            self.file_pos = self.file_handle.tell()
            self._log(
                f"Now watching: {os.path.basename(self.current_file)}",
                "info")

        if self.file_handle:
            self.file_handle.seek(self.file_pos)
            new_lines = self.file_handle.readlines()
            self.file_pos = self.file_handle.tell()

            for raw_line in new_lines:
                line = raw_line.strip()
                timestamp = (
                    line.split("\t")[0].strip() if "\t" in line else "")

                # --- Incoming Tell ---
                m = TELL_PATTERN.search(line)
                if m and self.events[EVENT_TELL]["enabled_var"].get():
                    sender, message = m.group(1), m.group(2)
                    self._log_rich(timestamp, f"{sender}: ", "sender",
                                   message)
                    self._alert(EVENT_TELL, f"{sender}: {message}")
                    continue

                # --- Friend Online ---
                m = FRIEND_ONLINE_PATTERN.search(line)
                if m and self.events[EVENT_FRIEND_ON]["enabled_var"].get():
                    friend = m.group(1)
                    self._log_rich(timestamp, f"{friend} ", "online",
                                   "is now online.")
                    self._alert(EVENT_FRIEND_ON, f"{friend} is now online")
                    continue

                # --- Friend Offline ---
                m = FRIEND_OFFLINE_PATTERN.search(line)
                if m and self.events[EVENT_FRIEND_OFF]["enabled_var"].get():
                    friend = m.group(1)
                    self._log_rich(timestamp, f"{friend} ", "offline",
                                   "is now offline.")
                    self._alert(EVENT_FRIEND_OFF, f"{friend} is now offline")
                    continue

                # --- Trade keyword search ---
                m = TRADE_PATTERN.search(line)
                if m and self.events[EVENT_TRADE]["enabled_var"].get():
                    seller, msg = m.group(1), m.group(2)
                    keyword = self._matches_trade(msg)
                    if keyword:
                        self._log_rich(
                            timestamp, f"[Trade] {seller}: ", "trade",
                            f"{msg}  (matched: {keyword})")
                        self._alert(EVENT_TRADE, f"{seller}: {msg}")
                    continue

                # --- Combat XP ---
                m = XP_PATTERN.search(line)
                if m:
                    xp_amount, skill = m.group(1), m.group(2)
                    self._show_overlay("xp", f"{skill} : {xp_amount} XP")
                    continue

                # --- Loot pickup ---
                m = LOOT_PATTERN.search(line)
                if m:
                    item = m.group(1)
                    self._show_overlay("loot", item)
                    continue

        self.poll_id = self.root.after(POLL_INTERVAL_MS, self._poll)

    def _log_rich(self, timestamp: str, highlight: str, tag: str, rest: str):
        self.log_text.configure(state="normal")
        if timestamp:
            self.log_text.insert("end", f"[{timestamp}] ", "info")
        self.log_text.insert("end", highlight, tag)
        self.log_text.insert("end", rest + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _alert(self, event_key: str, message: str = ""):
        path = self._sound_path_for(event_key)
        if path:
            self._play_sound(path, self._get_volume(event_key))
        self._show_toast(event_key, message)

    # ---- Cleanup ----------------------------------------------------------
    def destroy(self):
        self._persist()
        if self._anchor_window is not None:
            self._anchor_window.destroy()
            self._anchor_window = None
        for t in self._active_toasts:
            try:
                t.destroy()
            except tk.TclError:
                pass
        self._active_toasts.clear()
        for attr in ("_xp_anchor_window", "_loot_anchor_window"):
            win = getattr(self, attr)
            if win is not None:
                win.destroy()
                setattr(self, attr, None)
        for lst in (self._active_xp_overlays, self._active_loot_overlays):
            for w in lst:
                try:
                    w.destroy()
                except tk.TclError:
                    pass
            lst.clear()
        self._stop_monitoring()
        pygame.mixer.quit()


def main():
    root = tk.Tk()
    app = PGAlertApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.destroy(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
