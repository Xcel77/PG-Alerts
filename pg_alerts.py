"""
PG Alerts — Project Gorgon Chat Log Monitor & Alert System

Monitors the Project Gorgon chat log files in real time and provides:
  - Sound alerts for incoming [Tell] messages, friend online/offline
    status changes, and trade chat keyword matches.
  - Toast popup notifications (dark themed, topmost windows) for the
    same alert events, with configurable duration, font size, and
    repositionable anchor point.
  - Click-through transparent overlays for Combat XP gains and Loot
    pickups, with per-overlay color, font size, duration, and anchor
    position settings.

The GUI is built with Tkinter and organized into two tabs:
  - Alerts tab:  event toggles, sound selection, trade keywords,
                 volume controls, start/stop, and alert log.
  - Overlays tab: toast notification settings, Combat XP overlay
                  settings, and Loot Pickup overlay settings (each
                  with enable, duration, font size, color, and anchor).

All user settings are persisted to a JSON file in %APPDATA%/PGAlerts/
so they survive across sessions.
"""

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
import json          # Settings serialization
import os            # File and path operations
import re            # Chat log line pattern matching
import sys           # Frozen executable detection (PyInstaller)
import glob          # Chat log file discovery
import threading     # Non-blocking sound playback

# ---------------------------------------------------------------------------
# GUI imports
# ---------------------------------------------------------------------------
import tkinter as tk
from tkinter import ttk, colorchooser, font as tkfont

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import pygame        # Sound playback engine
import ctypes        # Windows API calls (monitor detection, click-through)
import ctypes.wintypes

# ---------------------------------------------------------------------------
# DPI Awareness
#
# Declare this process as per-monitor DPI aware so that Windows does not
# silently scale coordinates and sizes when positioning Toplevel windows
# on a monitor that differs from the one the main app window is on.
# Without this, overlay anchors shrink and get displaced on multi-monitor
# setups with different DPI/scaling settings.
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-Monitor DPI Aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()    # Fallback: system-level
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Paths & Configuration
# ---------------------------------------------------------------------------

# Directory where Project Gorgon writes its chat log files.
# Located under the Unity persistent data path for the game.
CHAT_LOG_DIR = os.path.normpath(os.path.join(
    os.environ["APPDATA"], "..", "LocalLow", "Elder Game",
    "Project Gorgon", "ChatLogs",
))

# Base directory — points to the PyInstaller temp folder when frozen,
# otherwise the script's own directory. Used to locate bundled sounds.
if getattr(sys, "frozen", False):
    _BASE_DIR = sys._MEIPASS
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Path to the bundled sound files directory.
SOUNDS_DIR = os.path.join(_BASE_DIR, "sounds")

# Persistent settings file stored in the user's AppData.
SETTINGS_FILE = os.path.join(
    os.environ["APPDATA"], "PGAlerts", "settings.json")

# How often (in milliseconds) the app polls the chat log for new lines.
POLL_INTERVAL_MS = 1000

# ---------------------------------------------------------------------------
# Chat Log Regex Patterns
#
# Each pattern matches a specific type of chat log line. Groups capture
# the relevant data (sender name, message text, XP amount, skill, etc.).
# ---------------------------------------------------------------------------

# Matches incoming tell messages: "[Tell] SomePlayer->You: message text"
# Uses a negative lookahead (?!You->) to exclude outgoing tells.
TELL_PATTERN = re.compile(r"\[Tell\]\s+(?!You->)(\S+)->You:\s+(.*)")

# Matches friend online notifications: "[Status] Your friend PlayerName is now online."
FRIEND_ONLINE_PATTERN = re.compile(
    r"\[Status\]\s+Your friend (\S+) is now online\.")

# Matches friend offline notifications: "[Status] Your friend PlayerName is now offline."
FRIEND_OFFLINE_PATTERN = re.compile(
    r"\[Status\]\s+Your friend (\S+) is now offline\.")

# Matches trade chat messages: "[Trade] SellerName: message text"
TRADE_PATTERN = re.compile(r"\[Trade\]\s+(\S+):\s+(.*)")

# Matches combat XP gain: "[Status] You earned 81 XP in Staff."
# Group 1 = XP amount, Group 2 = skill name.
XP_PATTERN = re.compile(r"\[Status\]\s+You earned (\d+) XP in (.+?)\.")

# Matches loot pickup: "[Status] Rough Animal Skin x2 added to inventory."
# Group 1 = item name (including quantity like "x2" if present).
LOOT_PATTERN = re.compile(r"\[Status\]\s+(.+?)\s+added to inventory\.")

# ---------------------------------------------------------------------------
# Event Keys & Display Labels
#
# Each alert-capable event has a string key used internally and a
# human-readable label shown in the UI.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Toast Notification Colors
#
# Each event type gets a distinct accent color used for the toast title
# text so the user can quickly identify the alert type at a glance.
# ---------------------------------------------------------------------------
TOAST_COLORS = {
    EVENT_TELL: "#e6a800",      # Gold — tells
    EVENT_FRIEND_ON: "#44cc44",  # Green — friend came online
    EVENT_FRIEND_OFF: "#cc4444",  # Red — friend went offline
    EVENT_TRADE: "#55aaff",     # Blue — trade keyword match
}

# ---------------------------------------------------------------------------
# Overlay & Toast Defaults
# ---------------------------------------------------------------------------

# Minimum pixel width for toast notification popups.
TOAST_MIN_WIDTH = 350

# Padding (in pixels) from the edge of the screen for default toast position.
TOAST_PADDING = 12

# Minimum pixel width for the transparent XP/Loot overlay text windows.
OVERLAY_MIN_WIDTH = 300

# Default font size for XP and Loot overlays.
OVERLAY_DEFAULT_FONT_SIZE = 11

# Default colors for overlay floating text.
OVERLAY_DEFAULT_XP_COLOR = "#ffdd44"    # Warm gold for XP gains
OVERLAY_DEFAULT_LOOT_COLOR = "#44ddff"  # Cyan for loot pickups

# Default font size for toast notification popups.
TOAST_DEFAULT_FONT_SIZE = 10

# Font size options available in all font size dropdowns (8pt through 24pt).
FONT_SIZE_OPTIONS = [str(s) for s in range(8, 25)]

# ---------------------------------------------------------------------------
# Win32 Constants for Click-Through Windows
#
# These are used with SetWindowLongW to modify the extended window style
# so that the overlay windows pass all mouse input through to the game.
# ---------------------------------------------------------------------------
GWL_EXSTYLE = -20                   # Index for extended window styles
WS_EX_LAYERED = 0x00080000         # Enables layered (transparent) window
WS_EX_TRANSPARENT = 0x00000020     # Makes window click-through


# ===========================================================================
# Helper Functions
# ===========================================================================

def get_latest_chat_log(log_dir: str) -> str | None:
    """Find the most recently modified Chat-*.log file in the given directory.

    Returns the full path to the newest log file, or None if no logs exist.
    Project Gorgon creates a new log file each session with a timestamp
    in the filename.
    """
    files = glob.glob(os.path.join(log_dir, "Chat-*.log"))
    return max(files, key=os.path.getmtime) if files else None


def discover_sounds(sounds_dir: str) -> list[str]:
    """Scan the sounds directory for playable audio files.

    Returns a sorted list of filenames (not full paths) with supported
    extensions: .wav, .mp3, .ogg. These are presented in the UI dropdowns
    for the user to assign to each event type.
    """
    exts = (".wav", ".mp3", ".ogg")
    if not os.path.isdir(sounds_dir):
        return []
    return sorted(
        f for f in os.listdir(sounds_dir)
        if os.path.splitext(f)[1].lower() in exts
    )


def get_pg_monitor_work_area() -> tuple[int, int, int, int]:
    """Detect which monitor Project Gorgon is running on and return its
    usable work area as (left, top, right, bottom) in screen coordinates.

    Uses the Windows API to:
    1. Find the PG window by its title ("Project Gorgon").
    2. Determine which monitor that window is on.
    3. Query that monitor's work area (excludes the taskbar).

    If PG isn't running, falls back to the primary monitor.
    If anything fails, returns a safe default of (0, 0, 1920, 1080).
    """
    try:
        user32 = ctypes.windll.user32
        # Try to find the Project Gorgon game window
        hwnd = user32.FindWindowW(None, "Project Gorgon")
        MONITOR_DEFAULTTOPRIMARY = 1
        MONITOR_DEFAULTTONEAREST = 2

        if hwnd:
            # PG is running — get the monitor it's on
            hmon = user32.MonitorFromWindow(
                hwnd, MONITOR_DEFAULTTONEAREST)
        else:
            # PG not found — fall back to the primary monitor
            hmon = user32.MonitorFromWindow(0, MONITOR_DEFAULTTOPRIMARY)

        # Define the MONITORINFO structure to receive monitor details
        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),  # Full monitor area
                # Work area (minus taskbar)
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
    """Apply WS_EX_TRANSPARENT and WS_EX_LAYERED extended window styles
    to a Tkinter Toplevel so that all mouse clicks pass through it to
    whatever is underneath (typically the game window).

    This is essential for the XP and Loot overlays so they don't
    interfere with gameplay.
    """
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception:
        pass


def load_settings() -> dict:
    """Load user settings from the JSON file.

    Returns an empty dict if the file doesn't exist or is malformed,
    allowing the app to fall back to defaults gracefully.
    """
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    """Write user settings to the JSON file.

    Creates the directory if it doesn't already exist. Uses pretty
    printing (indent=2) so the file is human-readable for debugging.
    """
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ===========================================================================
# Main Application Class
# ===========================================================================

class PGAlertApp:
    """Root application class that manages the entire PG Alerts GUI,
    chat log monitoring, sound playback, toast notifications, and
    click-through overlays.
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PG Alerts")
        self.root.resizable(False, False)

        # -- Monitoring state --
        # Tracks whether we're actively polling the chat log.
        self.monitoring = False
        self.current_file: str | None = None   # Path to the current log file
        self.file_handle = None                # Open file handle for reading
        self.file_pos = 0                      # Read position in the log file
        self.poll_id: str | None = None        # Tkinter after() callback ID

        # -- Initialize sound engine --
        pygame.mixer.init()

        # -- Discover available sound files and load saved settings --
        self.sound_files = discover_sounds(SOUNDS_DIR)
        self.settings = load_settings()

        # -- Per-event configuration --
        # Each event (tell, friend online/offline, trade) has its own
        # sound file selection, enabled toggle, and individual volume.
        self.events: dict[str, dict] = {}
        for key in ALL_EVENTS:
            self.events[key] = {
                "sound_var": tk.StringVar(value=self._saved(key, "sound")),
                "enabled_var": tk.BooleanVar(
                    value=self._saved(key, "enabled", True)),
                "volume_var": tk.DoubleVar(
                    value=self._saved(key, "volume", 0.7)),
            }

        # -- Trade keyword filter --
        # Comma-separated list of keywords to watch for in [Trade] chat.
        self.trade_keywords_var = tk.StringVar(
            value=self.settings.get("trade_keywords", ""))

        # -- Volume mode --
        # "master" = single slider controls all events.
        # "individual" = each event has its own volume slider.
        self.vol_mode_var = tk.StringVar(
            value=self.settings.get("vol_mode", "master"))
        self.master_vol_var = tk.DoubleVar(
            value=self.settings.get("volume", 0.7))

        # -- Toast notification settings --
        # Toasts are dark-themed popup windows that appear briefly for alerts.
        self.toast_enabled_var = tk.BooleanVar(
            value=self.settings.get("toast_enabled", True))
        self.toast_duration_var = tk.StringVar(
            value=self.settings.get("toast_duration", "3s"))
        self.toast_anchor_x: int | None = self.settings.get(
            "toast_anchor_x", None)  # Saved screen X position (or None for default)
        self.toast_anchor_y: int | None = self.settings.get(
            "toast_anchor_y", None)  # Saved screen Y position (or None for default)
        # Currently visible toasts
        self._active_toasts: list[tk.Toplevel] = []
        self._anchor_window: tk.Toplevel | None = None       # Draggable anchor preview
        self.toast_font_size_var = tk.StringVar(
            value=self.settings.get(
                "toast_font_size", str(TOAST_DEFAULT_FONT_SIZE)))

        # -- Combat XP overlay settings --
        # Transparent, click-through floating text showing XP gains.
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

        # -- Loot pickup overlay settings --
        # Transparent, click-through floating text showing looted items.
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

        # -- Build the GUI and apply initial state --
        self._build_ui()
        self._update_volume_visibility()
        self._check_chat_dir()

    # =======================================================================
    # Settings Helpers
    # =======================================================================

    def _saved(self, event_key: str, field: str, default=None):
        """Retrieve a saved per-event setting from the loaded config.

        Looks up settings["events"][event_key][field]. Falls back to
        sensible defaults if the key doesn't exist (first sound file,
        enabled=True, volume=0.7).
        """
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
        """Save all current settings to disk.

        Called whenever any setting changes (checkbox toggled, slider
        moved, combobox selected, anchor repositioned, etc.). Gathers
        the current value of every tk variable and writes to JSON.
        """
        data: dict = {
            # Volume settings
            "volume": round(self.master_vol_var.get(), 2),
            "vol_mode": self.vol_mode_var.get(),

            # Trade keyword filter
            "trade_keywords": self.trade_keywords_var.get(),

            # Toast notification settings
            "toast_enabled": self.toast_enabled_var.get(),
            "toast_duration": self.toast_duration_var.get(),
            "toast_anchor_x": self.toast_anchor_x,
            "toast_anchor_y": self.toast_anchor_y,
            "toast_font_size": self.toast_font_size_var.get(),

            # Combat XP overlay settings
            "xp_overlay_enabled": self.xp_overlay_enabled_var.get(),
            "xp_overlay_duration": self.xp_overlay_duration_var.get(),
            "xp_anchor_x": self.xp_anchor_x,
            "xp_anchor_y": self.xp_anchor_y,
            "xp_color": self.xp_color_var.get(),
            "xp_font_size": self.xp_font_size_var.get(),

            # Loot pickup overlay settings
            "loot_overlay_enabled": self.loot_overlay_enabled_var.get(),
            "loot_overlay_duration": self.loot_overlay_duration_var.get(),
            "loot_anchor_x": self.loot_anchor_x,
            "loot_anchor_y": self.loot_anchor_y,
            "loot_color": self.loot_color_var.get(),
            "loot_font_size": self.loot_font_size_var.get(),

            # Per-event sound/enabled/volume settings
            "events": {},
        }
        for key, ev in self.events.items():
            data["events"][key] = {
                "sound": ev["sound_var"].get(),
                "enabled": ev["enabled_var"].get(),
                "volume": round(ev["volume_var"].get(), 2),
            }
        save_settings(data)

    # =======================================================================
    # UI Construction
    # =======================================================================

    def _build_ui(self):
        """Build the entire GUI layout.

        The interface is split into two tabs via a ttk.Notebook:
          - Alerts tab:   event config, trade keywords, volume, controls, log
          - Overlays tab: toast settings, XP overlay settings, loot overlay settings
        """
        pad = dict(padx=8, pady=4)

        # -- Top-level tabbed notebook --
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        tab_alerts = ttk.Frame(self.notebook)
        tab_overlays = ttk.Frame(self.notebook)
        self.notebook.add(tab_alerts, text="Alerts")
        self.notebook.add(tab_overlays, text="Overlays")

        # ===================================================================
        # ALERTS TAB
        # ===================================================================

        # --- Alert Events Section ---
        # Grid of checkboxes, labels, sound dropdowns, and preview buttons
        # for each event type (Tell, Friend Online, Friend Offline, Trade).
        frame_events = ttk.LabelFrame(tab_alerts, text="Alert Events")
        frame_events.pack(fill="x", **pad)

        # Column headers
        headers = ["Enable", "Event", "Sound", ""]
        for col, h in enumerate(headers):
            ttk.Label(frame_events, text=h, font=("", 9, "bold")).grid(
                row=0, column=col, padx=4, pady=(4, 2), sticky="w")

        # One row per event type
        for row_idx, key in enumerate(ALL_EVENTS, start=1):
            ev = self.events[key]

            # Enable/disable checkbox for this event
            cb = ttk.Checkbutton(
                frame_events, variable=ev["enabled_var"],
                command=self._persist)
            cb.grid(row=row_idx, column=0, padx=4, pady=2)

            # Event name label
            ttk.Label(frame_events, text=EVENT_LABELS[key]).grid(
                row=row_idx, column=1, sticky="w", padx=4, pady=2)

            # Sound file dropdown
            combo = ttk.Combobox(
                frame_events, textvariable=ev["sound_var"],
                values=self.sound_files, state="readonly", width=24)
            combo.grid(row=row_idx, column=2, padx=4, pady=2)
            combo.bind("<<ComboboxSelected>>", self._persist)

            # Preview (play) button
            btn = ttk.Button(
                frame_events, text="\u25b6",
                command=lambda k=key: self._preview_event(k), width=3)
            btn.grid(row=row_idx, column=3, padx=4, pady=2)

        frame_events.columnconfigure(2, weight=1)

        # --- Trade Keywords Section ---
        # Text entry for comma-separated keywords to match in [Trade] chat.
        frame_trade = ttk.LabelFrame(
            tab_alerts, text="Trade Search Keywords (comma-separated)")
        frame_trade.pack(fill="x", **pad)

        self.trade_entry = ttk.Entry(
            frame_trade, textvariable=self.trade_keywords_var, width=58)
        self.trade_entry.pack(fill="x", padx=8, pady=6)
        self.trade_entry.bind("<FocusOut>", self._persist)
        self.trade_entry.bind("<Return>", self._persist)

        # --- Volume Section ---
        # Provides two modes: a single master volume slider, or individual
        # per-event volume sliders. A radio button toggles between them.
        frame_vol = ttk.LabelFrame(tab_alerts, text="Volume")
        frame_vol.pack(fill="x", **pad)

        # Volume mode radio buttons
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

        # Master volume slider (shown when mode is "master")
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

        # Individual volume sliders (shown when mode is "individual")
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

        # --- Start / Stop Controls ---
        # Button to begin/end chat log monitoring, plus a status label.
        frame_ctrl = ttk.Frame(tab_alerts)
        frame_ctrl.pack(fill="x", **pad)

        self.btn_toggle = ttk.Button(
            frame_ctrl, text="Start Monitoring", command=self._toggle)
        self.btn_toggle.pack(side="left")

        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(frame_ctrl, textvariable=self.status_var).pack(
            side="left", padx=12)

        # --- Alert Log ---
        # Scrollable text area showing all triggered alerts with colored
        # tags for different event types.
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

        # Color tags for different log entry types
        self.log_text.tag_configure(
            "sender", foreground="#e6a800")   # Tell sender
        self.log_text.tag_configure(
            "online", foreground="#44cc44")   # Friend online
        self.log_text.tag_configure(
            "offline", foreground="#cc4444")  # Friend offline
        self.log_text.tag_configure(
            "trade", foreground="#55aaff")    # Trade match
        self.log_text.tag_configure(
            "info", foreground="#888888")     # System messages

        # ===================================================================
        # OVERLAYS TAB
        # ===================================================================

        # --- Toast Notifications Section ---
        # Configures the dark-themed popup toasts that appear for alert events.
        # Row 1: Enable toggle, duration dropdown, move anchor button.
        # Row 2: Font size selector.
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

        # Move Anchor button — shows a draggable window to reposition toasts
        ttk.Button(
            toast_row, text="Reset",
            command=self._reset_toast_anchor, width=5).pack(side="right")
        self.btn_anchor = ttk.Button(
            toast_row, text="Move Anchor",
            command=self._toggle_anchor)
        self.btn_anchor.pack(side="right", padx=(0, 4))

        # Toast font size selector
        toast_row2 = ttk.Frame(frame_toast)
        toast_row2.pack(fill="x", padx=8, pady=(2, 6))

        ttk.Label(toast_row2, text="Font Size:").pack(
            side="left")
        toast_fs = ttk.Combobox(
            toast_row2, textvariable=self.toast_font_size_var,
            values=FONT_SIZE_OPTIONS, state="readonly", width=4)
        toast_fs.pack(side="left", padx=(4, 0))
        toast_fs.bind("<<ComboboxSelected>>", self._persist)

        # --- Combat XP Overlay Section ---
        # Configures the transparent, click-through floating text for XP gains.
        # Row 1: Enable toggle, duration dropdown, move anchor button.
        # Row 2: Font size selector, color picker with swatch preview.
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

        ttk.Button(
            xp_row1, text="Reset",
            command=self._reset_xp_anchor, width=5).pack(side="right")
        self.btn_xp_anchor = ttk.Button(
            xp_row1, text="Move Anchor",
            command=self._toggle_xp_anchor)
        self.btn_xp_anchor.pack(side="right", padx=(0, 4))

        # Font size and color picker row
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
        # Small colored square showing the current XP overlay color
        self.xp_color_swatch = tk.Label(
            xp_row2, width=3, bg=self.xp_color_var.get(),
            relief="solid", borderwidth=1)
        self.xp_color_swatch.pack(side="left")
        ttk.Button(
            xp_row2, text="Pick",
            command=self._pick_xp_color, width=5).pack(
            side="left", padx=(4, 0))

        # --- Loot Pickup Overlay Section ---
        # Configures the transparent, click-through floating text for loot.
        # Same layout as the XP section: enable, duration, anchor, font, color.
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

        ttk.Button(
            loot_row1, text="Reset",
            command=self._reset_loot_anchor, width=5).pack(side="right")
        self.btn_loot_anchor = ttk.Button(
            loot_row1, text="Move Anchor",
            command=self._toggle_loot_anchor)
        self.btn_loot_anchor.pack(side="right", padx=(0, 4))

        # Font size and color picker row
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
        # Small colored square showing the current Loot overlay color
        self.loot_color_swatch = tk.Label(
            loot_row2, width=3, bg=self.loot_color_var.get(),
            relief="solid", borderwidth=1)
        self.loot_color_swatch.pack(side="left")
        ttk.Button(
            loot_row2, text="Pick",
            command=self._pick_loot_color, width=5).pack(
            side="left", padx=(4, 0))

    # =======================================================================
    # Color Pickers
    # =======================================================================

    def _pick_xp_color(self):
        """Open the system color chooser dialog for the XP overlay color.
        Updates the swatch preview and persists the selection.
        """
        color = colorchooser.askcolor(
            color=self.xp_color_var.get(),
            title="XP Overlay Color")[1]
        if color:
            self.xp_color_var.set(color)
            self.xp_color_swatch.configure(bg=color)
            self._persist()

    def _pick_loot_color(self):
        """Open the system color chooser dialog for the Loot overlay color.
        Updates the swatch preview and persists the selection.
        """
        color = colorchooser.askcolor(
            color=self.loot_color_var.get(),
            title="Loot Overlay Color")[1]
        if color:
            self.loot_color_var.set(color)
            self.loot_color_swatch.configure(bg=color)
            self._persist()

    # =======================================================================
    # Anchor Reset
    #
    # Clears the saved anchor position so the overlay reverts to its
    # default screen location. Also closes the anchor preview window
    # if it happens to be open.
    # =======================================================================

    def _reset_toast_anchor(self):
        """Reset the toast anchor to the default bottom-right position."""
        self.toast_anchor_x = None
        self.toast_anchor_y = None
        if self._anchor_window is not None:
            self._anchor_window.destroy()
            self._anchor_window = None
            self.btn_anchor.configure(text="Move Anchor")
        self._persist()

    def _reset_xp_anchor(self):
        """Reset the Combat XP overlay anchor to its default position."""
        self.xp_anchor_x = None
        self.xp_anchor_y = None
        if self._xp_anchor_window is not None:
            self._xp_anchor_window.destroy()
            self._xp_anchor_window = None
            self.btn_xp_anchor.configure(text="Move Anchor")
        self._persist()

    def _reset_loot_anchor(self):
        """Reset the Loot Pickup overlay anchor to its default position."""
        self.loot_anchor_x = None
        self.loot_anchor_y = None
        if self._loot_anchor_window is not None:
            self._loot_anchor_window.destroy()
            self._loot_anchor_window = None
            self.btn_loot_anchor.configure(text="Move Anchor")
        self._persist()

    # =======================================================================
    # Volume Controls
    # =======================================================================

    def _on_vol_mode_change(self):
        """Handle switching between master and individual volume modes.
        Shows/hides the appropriate slider frames.
        """
        self._update_volume_visibility()
        self._persist()

    def _update_volume_visibility(self):
        """Show the master slider or individual sliders based on the
        current volume mode selection.
        """
        if self.vol_mode_var.get() == "master":
            self.master_vol_frame.pack(fill="x", padx=8, pady=4)
            self.indiv_vol_frame.pack_forget()
        else:
            self.master_vol_frame.pack_forget()
            self.indiv_vol_frame.pack(fill="x", padx=8, pady=4)

    def _on_master_vol_change(self, _=None):
        """Update the master volume percentage label when the slider moves."""
        vol = self.master_vol_var.get()
        self.master_vol_label.configure(text=f"{int(vol * 100)}%")
        self._persist()

    def _on_indiv_vol_change(self, event_key: str):
        """Update an individual event's volume percentage label."""
        vol = self.events[event_key]["volume_var"].get()
        self.indiv_vol_labels[event_key].configure(
            text=f"{int(vol * 100)}%")
        self._persist()

    def _get_volume(self, event_key: str) -> float:
        """Return the effective volume for an event, respecting the
        current volume mode (master or individual).
        """
        if self.vol_mode_var.get() == "master":
            return self.master_vol_var.get()
        return self.events[event_key]["volume_var"].get()

    # =======================================================================
    # Toast Notifications
    #
    # Toasts are dark-themed, borderless Toplevel windows that appear
    # briefly near the anchor point. They stack upward when multiple
    # toasts are active simultaneously. Width is dynamically calculated
    # based on font size and message content to prevent text cutoff.
    # =======================================================================

    def _get_toast_position(self) -> tuple[int, int]:
        """Return the (x, y) screen coordinates for the toast anchor.

        Uses the user's saved anchor position if available, otherwise
        defaults to the bottom-right corner of the PG monitor's work area.
        """
        if self.toast_anchor_x is not None and self.toast_anchor_y is not None:
            return (self.toast_anchor_x, self.toast_anchor_y)
        left, top, right, bottom = get_pg_monitor_work_area()
        return (right - TOAST_MIN_WIDTH - TOAST_PADDING,
                bottom - 80 - TOAST_PADDING)

    def _toggle_anchor(self):
        """Toggle the toast anchor positioning window.

        First click: shows a draggable orange-bordered preview window at
        the current anchor position. The user drags it to their desired
        location. Button text changes to "Save Anchor".

        Second click: saves the window's current screen position as the
        new anchor coordinates and destroys the preview window.
        """
        if self._anchor_window is not None:
            # Save the anchor's current position and close the preview
            self.toast_anchor_x = self._anchor_window.winfo_x()
            self.toast_anchor_y = self._anchor_window.winfo_y()
            self._anchor_window.destroy()
            self._anchor_window = None
            self.btn_anchor.configure(text="Move Anchor")
            self._persist()
        else:
            # Create the draggable anchor preview window
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

            # Bind drag events to all child widgets so dragging works
            # regardless of where the user clicks within the window.
            self._drag_data = {"x": 0, "y": 0}
            for w in (win, inner, lbl):
                w.bind("<Button-1>", self._anchor_start_drag)
                w.bind("<B1-Motion>", self._anchor_on_drag)

            self._anchor_window = win
            self.btn_anchor.configure(text="Save Anchor")

    def _anchor_start_drag(self, event):
        """Record the mouse offset relative to the anchor window's position
        when the user starts dragging.
        """
        self._drag_data["x"] = event.x_root - self._anchor_window.winfo_x()
        self._drag_data["y"] = event.y_root - self._anchor_window.winfo_y()

    def _anchor_on_drag(self, event):
        """Move the anchor window to follow the mouse during a drag."""
        x = event.x_root - self._drag_data["x"]
        y = event.y_root - self._drag_data["y"]
        self._anchor_window.geometry(f"+{x}+{y}")

    def _show_toast(self, event_key: str, message: str):
        """Display a toast notification popup for the given event.

        The toast is a dark-themed, borderless, always-on-top window with:
          - A colored title line (event type name)
          - A white body line (the alert message)
          - Dynamic width based on text measurement to prevent cutoff
          - Auto-dismiss after the configured duration

        Multiple simultaneous toasts stack upward from the anchor point.
        """
        if not self.toast_enabled_var.get():
            return

        # Remove references to toasts that have already been closed
        self._active_toasts = [
            t for t in self._active_toasts if t.winfo_exists()]

        # Parse the duration setting (e.g. "3s" -> 3000ms)
        dur_str = self.toast_duration_var.get()
        duration_ms = int(dur_str.replace("s", "")) * 1000
        t_fs = int(self.toast_font_size_var.get())
        color = TOAST_COLORS.get(event_key, "#ffffff")

        # Dynamically measure text width to size the toast window
        title_font = tkfont.Font(family="Segoe UI", size=t_fs, weight="bold")
        body_font = tkfont.Font(family="Segoe UI", size=t_fs - 1)
        title_w = title_font.measure(EVENT_LABELS[event_key]) + 24
        body_w = body_font.measure(message) + 24
        t_w = max(TOAST_MIN_WIDTH, title_w, body_w)
        t_h = t_fs * 5 + 20

        # Position: stack upward from anchor for multiple toasts
        base_x, base_y = self._get_toast_position()
        offset = len(self._active_toasts) * (t_h + 4)

        # Create the toast window
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)       # No title bar or borders
        toast.attributes("-topmost", True)  # Always on top
        toast.attributes("-alpha", 0.92)   # Slightly transparent
        toast.configure(bg="#555555")       # Thin border color
        toast.geometry(
            f"{t_w}x{t_h}"
            f"+{base_x}+{base_y - offset}")

        # Inner content area (dark background)
        content = tk.Frame(toast, bg="#2b2b2b")
        content.place(x=1, y=1, width=t_w - 2,
                      height=t_h - 2)

        # Event type title (colored)
        tk.Label(
            content, text=EVENT_LABELS[event_key],
            font=title_font,
            fg=color, bg="#2b2b2b", anchor="w"
        ).pack(fill="x", padx=10, pady=(8, 0))

        # Alert message body (white text)
        tk.Label(
            content, text=message, font=body_font,
            fg="#dddddd", bg="#2b2b2b",
            wraplength=t_w - 24,
            justify="left", anchor="w"
        ).pack(fill="x", padx=10, pady=(2, 8))

        self._active_toasts.append(toast)

        # Schedule auto-close after the configured duration
        def _close():
            try:
                if toast.winfo_exists():
                    toast.destroy()
                if toast in self._active_toasts:
                    self._active_toasts.remove(toast)
            except tk.TclError:
                pass

        toast.after(duration_ms, _close)

    # =======================================================================
    # Click-Through Overlays (XP & Loot)
    #
    # These are transparent, borderless Toplevel windows that display
    # floating text over the game. They use:
    #   - `-transparentcolor` to make the background invisible
    #   - Win32 WS_EX_TRANSPARENT style for full click-through
    #   - Dynamic width based on text measurement
    #   - Upward stacking when multiple overlays are active
    #
    # The "kind" parameter is either "xp" or "loot", and attribute names
    # are constructed dynamically (e.g. self.xp_color_var, self.loot_anchor_x)
    # to avoid duplicating code for each overlay type.
    # =======================================================================

    def _get_overlay_position(self, kind: str) -> tuple[int, int]:
        """Return the (x, y) screen coordinates for an overlay anchor.

        Uses the user's saved position if available, otherwise defaults
        to the bottom-left area of the PG monitor (different Y offset
        for XP vs Loot so they don't overlap by default).
        """
        ax = getattr(self, f"{kind}_anchor_x")
        ay = getattr(self, f"{kind}_anchor_y")
        if ax is not None and ay is not None:
            return (ax, ay)
        left, top, right, bottom = get_pg_monitor_work_area()
        if kind == "xp":
            return (left + 20, bottom - 200)
        return (left + 20, bottom - 400)

    def _toggle_overlay_anchor(self, kind: str):
        """Toggle the anchor positioning window for an overlay (XP or Loot).

        Works the same as the toast anchor: first click shows a draggable
        colored preview, second click saves the position and closes it.
        The preview window uses the overlay's current color setting.
        """
        anchor_attr = f"_{kind}_anchor_window"
        btn_attr = f"btn_{kind}_anchor"
        anchor_win = getattr(self, anchor_attr)
        btn = getattr(self, btn_attr)
        color = getattr(self, f"{kind}_color_var").get()
        label_text = "Combat XP" if kind == "xp" else "Loot Pickup"

        if anchor_win is not None:
            # Save position and close the preview
            setattr(self, f"{kind}_anchor_x", anchor_win.winfo_x())
            setattr(self, f"{kind}_anchor_y", anchor_win.winfo_y())
            anchor_win.destroy()
            setattr(self, anchor_attr, None)
            btn.configure(text="Move Anchor")
            self._persist()
        else:
            # Create the draggable anchor preview
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

            # Drag handling — uses closures to capture the specific window
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
        """Convenience wrapper — toggle the Combat XP overlay anchor."""
        self._toggle_overlay_anchor("xp")

    def _toggle_loot_anchor(self):
        """Convenience wrapper — toggle the Loot Pickup overlay anchor."""
        self._toggle_overlay_anchor("loot")

    def _show_overlay(self, kind: str, text: str):
        """Display a click-through floating text overlay for XP or Loot.

        Creates a transparent, borderless, always-on-top window with:
          - A color-key background that becomes fully invisible
          - WS_EX_TRANSPARENT style so clicks pass through to the game
          - Bold text in the user's chosen color and font size
          - Dynamic width measured from the actual text content
          - Auto-dismiss after the configured duration

        Multiple simultaneous overlays stack upward from the anchor.
        """
        enabled_var = getattr(self, f"{kind}_overlay_enabled_var")
        if not enabled_var.get():
            return

        active_list = getattr(self, f"_active_{kind}_overlays")
        # Remove references to overlays that have already been closed
        active_list[:] = [w for w in active_list if w.winfo_exists()]

        # Parse duration setting
        dur_str = getattr(self, f"{kind}_overlay_duration_var").get()
        duration_ms = int(dur_str.replace("s", "")) * 1000

        # Get user-configured appearance settings
        color = getattr(self, f"{kind}_color_var").get()
        font_size = int(getattr(self, f"{kind}_font_size_var").get())
        base_x, base_y = self._get_overlay_position(kind)
        line_h = font_size + 12
        offset = len(active_list) * line_h

        # Dynamically measure text width to prevent cutoff
        measure_font = tkfont.Font(
            family="Segoe UI", size=font_size, weight="bold")
        text_w = measure_font.measure(text) + 24
        overlay_w = max(OVERLAY_MIN_WIDTH, text_w)

        # Use a specific dark color as the transparency key.
        # This color (#010101) becomes fully invisible, leaving
        # only the text label visible on screen.
        trans_color = "#010101"

        # Create the overlay window
        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.configure(bg=trans_color)
        overlay.attributes("-transparentcolor", trans_color)
        overlay.geometry(
            f"{overlay_w}x{line_h}"
            f"+{base_x}+{base_y - offset}")

        # Force the window to render so we can get a valid HWND,
        # then apply the click-through window style.
        overlay.update_idletasks()
        make_click_through(overlay)

        # The actual floating text label
        lbl = tk.Label(
            overlay, text=text,
            font=("Segoe UI", font_size, "bold"),
            fg=color, bg=trans_color, anchor="w")
        lbl.pack(fill="both", expand=True)

        active_list.append(overlay)

        # Schedule auto-close after the configured duration
        def _close():
            try:
                if overlay.winfo_exists():
                    overlay.destroy()
                if overlay in active_list:
                    active_list.remove(overlay)
            except tk.TclError:
                pass

        overlay.after(duration_ms, _close)

    # =======================================================================
    # Sound & Alert Actions
    # =======================================================================

    def _check_chat_dir(self):
        """Check if the PG chat log directory exists on startup.
        Logs a helpful message if it's missing (game hasn't been run yet).
        """
        if not os.path.isdir(CHAT_LOG_DIR):
            self._log(
                "Chat log directory not found:\n" + CHAT_LOG_DIR, "info")
            self._log(
                "Make sure Project Gorgon has been run at least once, and "
                "ensure logging is enabled in the game settings.",
                "info")

    def _sound_path_for(self, event_key: str) -> str | None:
        """Return the full filesystem path to the selected sound file
        for an event, or None if no valid sound is configured.
        """
        name = self.events[event_key]["sound_var"].get()
        if not name:
            return None
        path = os.path.join(SOUNDS_DIR, name)
        return path if os.path.isfile(path) else None

    def _preview_event(self, event_key: str):
        """Play the sound assigned to an event (triggered by the ▶ button)."""
        path = self._sound_path_for(event_key)
        if path:
            self._play_sound(path, self._get_volume(event_key))

    def _play_sound(self, path: str, volume: float):
        """Play a sound file at the specified volume in a background thread.

        Uses pygame.mixer.Sound for playback. Runs in a daemon thread
        to avoid blocking the UI while the sound loads and plays.
        """
        def _do():
            try:
                sound = pygame.mixer.Sound(path)
                sound.set_volume(volume)
                sound.play()
            except Exception as e:
                self._log(f"[!] Sound error: {e}", "info")
        threading.Thread(target=_do, daemon=True).start()

    def _log(self, text: str, tag: str | None = None):
        """Append a line of text to the alert log widget.

        If a tag is provided, the text is colored according to the tag's
        configured foreground color (sender, online, offline, trade, info).
        """
        self.log_text.configure(state="normal")
        if tag:
            self.log_text.insert("end", text + "\n", tag)
        else:
            self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # =======================================================================
    # Trade Keyword Matching
    # =======================================================================

    def _get_trade_keywords(self) -> list[str]:
        """Parse the trade keywords entry into a list of lowercase keywords.
        Splits on commas and strips whitespace.
        """
        raw = self.trade_keywords_var.get()
        return [kw.strip().lower() for kw in raw.split(",") if kw.strip()]

    def _matches_trade(self, message: str) -> str | None:
        """Check if a trade message contains any of the configured keywords.
        Returns the first matching keyword, or None if no match.
        """
        msg_lower = message.lower()
        for kw in self._get_trade_keywords():
            if kw in msg_lower:
                return kw
        return None

    # =======================================================================
    # Chat Log Monitoring
    #
    # The monitoring loop uses Tkinter's after() scheduler to poll the
    # chat log file at a regular interval. It:
    #   1. Detects when PG creates a new log file (new session).
    #   2. Reads only newly appended lines since the last poll.
    #   3. Matches each line against the regex patterns.
    #   4. Triggers the appropriate alerts (sound, toast, overlay).
    # =======================================================================

    def _toggle(self):
        """Toggle monitoring on or off."""
        if self.monitoring:
            self._stop_monitoring()
        else:
            self._start_monitoring()

    def _start_monitoring(self):
        """Begin polling the chat log for new lines.

        Resets the file tracking state, updates the UI, and kicks off
        the first poll cycle.
        """
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
        """Stop polling the chat log and clean up file handles."""
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
        """Single poll cycle: check for new log file, read new lines,
        match patterns, and trigger alerts.

        Automatically reschedules itself via after() to keep polling
        as long as monitoring is active.
        """
        if not self.monitoring:
            return

        # Check if a newer log file has appeared (new game session)
        latest = get_latest_chat_log(CHAT_LOG_DIR)
        if latest and latest != self.current_file:
            if self.file_handle:
                self.file_handle.close()
            self.current_file = latest
            self.file_handle = open(
                self.current_file, "r", encoding="utf-8", errors="replace")
            # Seek to the end so we only process NEW lines going forward
            self.file_handle.seek(0, os.SEEK_END)
            self.file_pos = self.file_handle.tell()
            self._log(
                f"Now watching: {os.path.basename(self.current_file)}",
                "info")

        # Read any new lines appended since our last read position
        if self.file_handle:
            self.file_handle.seek(self.file_pos)
            new_lines = self.file_handle.readlines()
            self.file_pos = self.file_handle.tell()

            for raw_line in new_lines:
                line = raw_line.strip()
                # Extract timestamp (tab-separated at start of line)
                timestamp = (
                    line.split("\t")[0].strip() if "\t" in line else "")

                # --- Incoming Tell ---
                # Matches: [Tell] PlayerName->You: message text
                m = TELL_PATTERN.search(line)
                if m and self.events[EVENT_TELL]["enabled_var"].get():
                    sender, message = m.group(1), m.group(2)
                    self._log_rich(timestamp, f"{sender}: ", "sender",
                                   message)
                    self._alert(EVENT_TELL, f"{sender}: {message}")
                    continue

                # --- Friend Online ---
                # Matches: [Status] Your friend PlayerName is now online.
                m = FRIEND_ONLINE_PATTERN.search(line)
                if m and self.events[EVENT_FRIEND_ON]["enabled_var"].get():
                    friend = m.group(1)
                    self._log_rich(timestamp, f"{friend} ", "online",
                                   "is now online.")
                    self._alert(EVENT_FRIEND_ON, f"{friend} is now online")
                    continue

                # --- Friend Offline ---
                # Matches: [Status] Your friend PlayerName is now offline.
                m = FRIEND_OFFLINE_PATTERN.search(line)
                if m and self.events[EVENT_FRIEND_OFF]["enabled_var"].get():
                    friend = m.group(1)
                    self._log_rich(timestamp, f"{friend} ", "offline",
                                   "is now offline.")
                    self._alert(EVENT_FRIEND_OFF, f"{friend} is now offline")
                    continue

                # --- Trade Keyword Search ---
                # Matches: [Trade] SellerName: message text
                # Only triggers if the message contains a configured keyword.
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

                # --- Combat XP Gain ---
                # Matches: [Status] You earned 81 XP in Staff.
                # Displays as floating overlay text: "Staff : 81 XP"
                m = XP_PATTERN.search(line)
                if m:
                    xp_amount, skill = m.group(1), m.group(2)
                    self._show_overlay("xp", f"{skill} : {xp_amount} XP")
                    continue

                # --- Loot Pickup ---
                # Matches: [Status] Rough Animal Skin x2 added to inventory.
                # Displays as floating overlay text: "Rough Animal Skin x2"
                m = LOOT_PATTERN.search(line)
                if m:
                    item = m.group(1)
                    self._show_overlay("loot", item)
                    continue

        # Schedule the next poll cycle
        self.poll_id = self.root.after(POLL_INTERVAL_MS, self._poll)

    def _log_rich(self, timestamp: str, highlight: str, tag: str, rest: str):
        """Write a multi-styled line to the alert log.

        Combines a gray timestamp, a colored highlight portion (using
        the specified tag), and plain text for the remainder.
        """
        self.log_text.configure(state="normal")
        if timestamp:
            self.log_text.insert("end", f"[{timestamp}] ", "info")
        self.log_text.insert("end", highlight, tag)
        self.log_text.insert("end", rest + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _alert(self, event_key: str, message: str = ""):
        """Trigger a full alert for an event: play the assigned sound
        and show a toast notification popup.
        """
        path = self._sound_path_for(event_key)
        if path:
            self._play_sound(path, self._get_volume(event_key))
        self._show_toast(event_key, message)

    # =======================================================================
    # Cleanup
    # =======================================================================

    def destroy(self):
        """Clean up all resources before the application exits.

        Persists settings, closes all overlay/anchor windows, stops
        monitoring, and shuts down the sound engine.
        """
        # Save current settings
        self._persist()

        # Close the toast anchor preview if it's open
        if self._anchor_window is not None:
            self._anchor_window.destroy()
            self._anchor_window = None

        # Close any active toast windows
        for t in self._active_toasts:
            try:
                t.destroy()
            except tk.TclError:
                pass
        self._active_toasts.clear()

        # Close XP and Loot anchor preview windows
        for attr in ("_xp_anchor_window", "_loot_anchor_window"):
            win = getattr(self, attr)
            if win is not None:
                win.destroy()
                setattr(self, attr, None)

        # Close any active XP/Loot overlay windows
        for lst in (self._active_xp_overlays, self._active_loot_overlays):
            for w in lst:
                try:
                    w.destroy()
                except tk.TclError:
                    pass
            lst.clear()

        # Stop monitoring and shut down pygame
        self._stop_monitoring()
        pygame.mixer.quit()


# ===========================================================================
# Entry Point
# ===========================================================================

def main():
    """Create the Tkinter root window, instantiate the app, and start
    the main event loop. Ensures clean shutdown on window close.
    """
    root = tk.Tk()
    app = PGAlertApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.destroy(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
