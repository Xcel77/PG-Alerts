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
from tkinter import ttk

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

TOAST_WIDTH = 350
TOAST_HEIGHT = 80
TOAST_PADDING = 12


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

        # --- Per-event rows ---
        frame_events = ttk.LabelFrame(self.root, text="Alert Events")
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
            self.root, text="Trade Search Keywords (comma-separated)")
        frame_trade.pack(fill="x", **pad)

        self.trade_entry = ttk.Entry(
            frame_trade, textvariable=self.trade_keywords_var, width=58)
        self.trade_entry.pack(fill="x", padx=8, pady=6)
        self.trade_entry.bind("<FocusOut>", self._persist)
        self.trade_entry.bind("<Return>", self._persist)

        # --- Volume section ---
        frame_vol = ttk.LabelFrame(self.root, text="Volume")
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

        # --- Toast settings ---
        frame_toast = ttk.LabelFrame(self.root, text="Toast Notifications")
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
            values=["1s", "2s", "3s", "4s", "5s"],
            state="readonly", width=4)
        toast_dur.pack(side="left")
        toast_dur.bind("<<ComboboxSelected>>", self._persist)

        self.btn_anchor = ttk.Button(
            toast_row, text="Move Anchor",
            command=self._toggle_anchor)
        self.btn_anchor.pack(side="right")

        # --- Start / Stop ---
        frame_ctrl = ttk.Frame(self.root)
        frame_ctrl.pack(fill="x", **pad)

        self.btn_toggle = ttk.Button(
            frame_ctrl, text="Start Monitoring", command=self._toggle)
        self.btn_toggle.pack(side="left")

        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(frame_ctrl, textvariable=self.status_var).pack(
            side="left", padx=12)

        # --- Alert log ---
        frame_log = ttk.LabelFrame(self.root, text="Alert Log")
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
        return (right - TOAST_WIDTH - TOAST_PADDING,
                bottom - TOAST_HEIGHT - TOAST_PADDING)

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
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.6)
            win.configure(bg="#ffaa00")
            win.geometry(f"{TOAST_WIDTH}x{TOAST_HEIGHT}+{x}+{y}")

            inner = tk.Frame(win, bg="#333333")
            inner.place(x=3, y=3, width=TOAST_WIDTH - 6,
                        height=TOAST_HEIGHT - 6)

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

        base_x, base_y = self._get_toast_position()
        offset = len(self._active_toasts) * (TOAST_HEIGHT + 4)
        color = TOAST_COLORS.get(event_key, "#ffffff")

        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.attributes("-alpha", 0.92)
        toast.configure(bg="#555555")
        toast.geometry(
            f"{TOAST_WIDTH}x{TOAST_HEIGHT}"
            f"+{base_x}+{base_y - offset}")

        content = tk.Frame(toast, bg="#2b2b2b")
        content.place(x=1, y=1, width=TOAST_WIDTH - 2,
                      height=TOAST_HEIGHT - 2)

        tk.Label(
            content, text=EVENT_LABELS[event_key],
            font=("Segoe UI", 10, "bold"),
            fg=color, bg="#2b2b2b", anchor="w"
        ).pack(fill="x", padx=10, pady=(8, 0))

        tk.Label(
            content, text=message, font=("Segoe UI", 9),
            fg="#dddddd", bg="#2b2b2b",
            wraplength=TOAST_WIDTH - 24,
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

    # ---- Actions ----------------------------------------------------------
    def _check_chat_dir(self):
        if not os.path.isdir(CHAT_LOG_DIR):
            self._log(
                "Chat log directory not found:\n" + CHAT_LOG_DIR, "info")
            self._log(
                "Make sure Project Gorgon has been run at least once.",
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
        self._stop_monitoring()
        pygame.mixer.quit()


def main():
    root = tk.Tk()
    app = PGAlertApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.destroy(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
