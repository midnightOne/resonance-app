"""
Resonance – main application window + hotkey orchestration.

Architecture
------------
* Main thread  : tkinter event loop
* Hotkey thread: keyboard.hook (daemon thread, started once)
* Worker thread: audio recording → transcription (one at a time)

Cross-thread communication is handled exclusively via a Queue that the
tkinter `after` loop drains every ~50 ms.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from datetime import datetime
from typing import Optional

import keyboard
import miniaudio
import numpy as np
import pyautogui
import pyperclip
import sounddevice as sd
import winreg

from . import config as cfg
from .audio import AudioRecorder, list_input_devices, list_output_devices
from .transcribe import transcribe

pyautogui.FAILSAFE = False

# ── colours ──────────────────────────────────────────────────────────────────
BG = "#1a1a2e"
BG2 = "#16213e"
ACCENT = "#0f3460"
RED = "#e94560"
GREEN = "#4ecca3"
YELLOW = "#f5a623"
TEXT = "#e0e0e0"
TEXT_DIM = "#888888"
ENTRY_BG = "#0d1b2a"

# ── status tokens ─────────────────────────────────────────────────────────────
S_IDLE = "idle"
S_RECORDING = "recording"
S_PROCESSING = "processing"
S_ERROR = "error"

# ── cost tracking ─────────────────────────────────────────────────────────────
# OpenAI Whisper pricing: $0.006 / minute, billed per second (rounded up).
_PRICE_PER_MIN: dict[str, float] = {
    "whisper-1": 0.006,
}
_DEFAULT_PRICE = 0.006


def _calc_cost(duration_sec: float | None, model: str) -> float:
    """Return USD cost for one transcription call."""
    if not duration_sec:
        return 0.0
    price = _PRICE_PER_MIN.get(model, _DEFAULT_PRICE)
    return (math.ceil(duration_sec) / 60.0) * price


# ── audio cues (MP3-based) ────────────────────────────────────────────────────

_SOUNDS_RATE = 44100
_SOUNDS: dict[str, np.ndarray] = {}   # populated by _load_sounds()


def _sounds_dir():
    """Return the directory that contains the bundled MP3 sound files."""
    from pathlib import Path
    if getattr(sys, "frozen", False):
        # PyInstaller onefile: resources land in sys._MEIPASS/sounds
        return Path(sys._MEIPASS) / "sounds"
    return Path(__file__).parent / "sounds"


def _load_sounds() -> None:
    """Decode all three MP3 cues into float32 numpy arrays."""
    sounds_dir = _sounds_dir()
    for kind in ("start", "stop", "error"):
        path = sounds_dir / f"{kind}_sound.mp3"
        if not path.exists():
            continue
        try:
            decoded = miniaudio.decode_file(
                str(path),
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=1,
                sample_rate=_SOUNDS_RATE,
            )
            arr = np.frombuffer(decoded.samples, dtype=np.float32).copy()
            _SOUNDS[kind] = arr
        except Exception:
            pass


def _play_cue(kind: str, device=None) -> None:
    """Play a named audio cue ("start", "stop", "error") on a daemon thread.

    Falls back to a sine-wave tone if the MP3 was not loaded.

    Parameters
    ----------
    kind:   one of "start", "stop", "error"
    device: sounddevice output device index, or None for system default.
    """
    _FALLBACK = {"start": (880, 250), "stop": (660, 220), "error": (300, 480)}

    def _play() -> None:
        try:
            arr = _SOUNDS.get(kind)
            rate = _SOUNDS_RATE
            if arr is not None and len(arr) > 0:
                # 80 ms silence tail prevents buffer-drain clipping
                tail = np.zeros(int(rate * 0.08), dtype=np.float32)
                sd.play(np.concatenate([arr, tail]),
                        samplerate=rate, device=device, blocking=True)
            elif kind in _FALLBACK:
                freq, duration_ms = _FALLBACK[kind]
                dur = duration_ms / 1000.0
                t = np.linspace(0, dur, int(rate * dur), endpoint=False)
                tone = (np.sin(2 * np.pi * freq * t) * 0.35).astype(np.float32)
                fade = min(int(rate * 0.012), len(tone) // 4)
                tone[:fade]  *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
                tone[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
                tail = np.zeros(int(rate * 0.08), dtype=np.float32)
                sd.play(np.concatenate([tone, tail]),
                        samplerate=rate, device=device, blocking=True)
        except Exception:
            pass

    threading.Thread(target=_play, daemon=True).start()


def _get_window_info(hwnd: int) -> dict:
    """Return {'app_process': str, 'app_title': str} for a given HWND."""
    info: dict = {"app_process": "", "app_title": ""}
    if not hwnd:
        return info

    # Window title
    tbuf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, tbuf, 512)
    info["app_title"] = tbuf.value

    # PID → executable name
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED
    if h:
        try:
            pbuf = ctypes.create_unicode_buffer(1024)
            sz = ctypes.c_ulong(1024)
            ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(sz))
            info["app_process"] = os.path.basename(pbuf.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Windows startup (registry)
# ─────────────────────────────────────────────────────────────────────────────

_RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "Resonance"


def _startup_exe() -> str:
    """Return the command string to register for auto-start."""
    if getattr(sys, "frozen", False):
        # Packaged exe – use its own path
        return f'"{sys.executable}"'
    # Dev mode – launch run.py via pythonw (no console window)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    run_py  = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "run.py"))
    return f'"{pythonw}" "{run_py}"'


def _get_startup_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY)
        winreg.QueryValueEx(key, _RUN_NAME)
        winreg.CloseKey(key)
        return True
    except (FileNotFoundError, OSError):
        return False


def _set_startup(enabled: bool) -> None:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        )
        if enabled:
            winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _startup_exe())
        else:
            try:
                winreg.DeleteValue(key, _RUN_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Hotkey monitor
# ─────────────────────────────────────────────────────────────────────────────

class HotkeyMonitor:
    """Tracks a configurable key combo, fires callbacks on press/release.

    Key name notes (keyboard library on Windows):
      - Windows key → "left windows" or "right windows"
      - Writing just "windows" in the hotkey string matches either side.
    """

    _WIN_ALIASES = {"windows", "win"}
    _WIN_NAMES = {"left windows", "right windows"}

    def __init__(self, hotkey_str: str, on_press, on_release):
        self._raw = hotkey_str
        self._keys, self._win_required = self._parse(hotkey_str)
        self._on_press = on_press
        self._on_release = on_release
        self._active = False
        self._held: set[str] = set()
        self._hook_id = None

    @classmethod
    def _parse(cls, hotkey_str: str) -> tuple[set[str], bool]:
        parts = {k.strip().lower() for k in hotkey_str.split("+")}
        win_required = bool(parts & cls._WIN_ALIASES)
        # Remove bare aliases; actual matching uses _WIN_NAMES
        parts -= cls._WIN_ALIASES
        return parts, win_required

    def _hotkey_active(self) -> bool:
        """Return True when all required keys are held."""
        if self._win_required and not (self._held & self._WIN_NAMES):
            return False
        return self._keys.issubset(self._held)

    def _is_trigger_key(self, name: str) -> bool:
        """Return True if releasing this key should cancel the combo."""
        if self._win_required and name in self._WIN_NAMES:
            return True
        return name in self._keys

    def start(self):
        self._hook_id = keyboard.hook(self._handle, suppress=False)

    def stop(self):
        if self._hook_id is not None:
            keyboard.unhook(self._hook_id)
            self._hook_id = None

    def update_keys(self, hotkey_str: str):
        self._raw = hotkey_str
        self._keys, self._win_required = self._parse(hotkey_str)
        self._active = False
        self._held.clear()

    def _handle(self, event: keyboard.KeyboardEvent):
        name = (event.name or "").lower()
        if event.event_type == keyboard.KEY_DOWN:
            self._held.add(name)
            if self._hotkey_active() and not self._active:
                self._active = True
                self._on_press()
        elif event.event_type == keyboard.KEY_UP:
            self._held.discard(name)
            if self._active and self._is_trigger_key(name):
                self._active = False
                self._on_release()


# ─────────────────────────────────────────────────────────────────────────────
# History entry widget
# ─────────────────────────────────────────────────────────────────────────────

class HistoryEntry(tk.Frame):
    def __init__(self, parent, timestamp: str, text: str, **kw):
        super().__init__(parent, bg=BG2, pady=4, padx=6, **kw)
        self.text = text

        top = tk.Frame(self, bg=BG2)
        top.pack(fill="x")

        ts_label = tk.Label(top, text=timestamp, fg=TEXT_DIM, bg=BG2,
                            font=("Consolas", 8))
        ts_label.pack(side="left")

        copy_btn = tk.Button(top, text="Copy", fg=TEXT, bg=ACCENT,
                             relief="flat", bd=0, padx=8, pady=1,
                             font=("Segoe UI", 8), cursor="hand2",
                             activebackground=GREEN, activeforeground=BG,
                             command=self._copy)
        copy_btn.pack(side="right")

        body = tk.Label(self, text=text, fg=TEXT, bg=BG2,
                        wraplength=360, justify="left",
                        font=("Segoe UI", 10), anchor="w")
        body.pack(fill="x", pady=(2, 0))

        sep = tk.Frame(self, height=1, bg=ACCENT)
        sep.pack(fill="x", pady=(4, 0))

    def _copy(self):
        pyperclip.copy(self.text)


# ─────────────────────────────────────────────────────────────────────────────
# Settings panel
# ─────────────────────────────────────────────────────────────────────────────

_DEVICE_DEFAULT_LABEL = "System Default"


class SettingsPanel(tk.Toplevel):
    def __init__(self, parent: "App"):
        super().__init__(parent.root)
        self.app = parent
        self.title("Resonance – Settings")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()

        pad = {"padx": 12, "pady": 5}

        def row(label_text, var, show=""):
            f = tk.Frame(self, bg=BG)
            f.pack(fill="x", **pad)
            tk.Label(f, text=label_text, fg=TEXT_DIM, bg=BG,
                     width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
            e = tk.Entry(f, textvariable=var, bg=ENTRY_BG, fg=TEXT,
                         insertbackground=TEXT, relief="flat",
                         font=("Consolas", 10), show=show, width=38)
            e.pack(side="left", fill="x", expand=True)
            return e

        def device_combo_row(label_text, device_list, current_idx):
            """Build a labelled combobox row for device selection."""
            labels = [lbl for _, lbl in device_list]
            current_lbl = _DEVICE_DEFAULT_LABEL
            for idx, lbl in device_list:
                if idx == current_idx:
                    current_lbl = lbl
                    break
            var = tk.StringVar(value=current_lbl)
            f = tk.Frame(self, bg=BG)
            f.pack(fill="x", **pad)
            tk.Label(f, text=label_text, fg=TEXT_DIM, bg=BG,
                     width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
            cb = ttk.Combobox(f, textvariable=var, values=labels,
                              state="readonly", style="Dark.TCombobox", width=36)
            cb.pack(side="left")
            return var

        c = parent.cfg
        self._api_key    = tk.StringVar(value=c["api_key"])
        self._base_url   = tk.StringVar(value=c["api_base_url"])
        self._model      = tk.StringVar(value=c["model"])
        self._hotkey     = tk.StringVar(value=c["hotkey"])
        self._language   = tk.StringVar(value=c["language"])
        self._auto_paste = tk.BooleanVar(value=c["auto_paste"])
        self._always_on_top = tk.BooleanVar(value=c["always_on_top"])
        self._start_with_windows = tk.BooleanVar(value=_get_startup_enabled())

        # Device lists
        self._in_device_list: list[tuple[int | None, str]] = (
            [(None, _DEVICE_DEFAULT_LABEL)] +
            [(i, n) for i, n in list_input_devices()]
        )
        self._out_device_list: list[tuple[int | None, str]] = (
            [(None, _DEVICE_DEFAULT_LABEL)] +
            [(i, n) for i, n in list_output_devices()]
        )

        # Apply combobox style once
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Dark.TCombobox",
                        fieldbackground=ENTRY_BG, background=ENTRY_BG,
                        foreground=TEXT, selectbackground=ACCENT,
                        selectforeground=TEXT, arrowcolor=TEXT)
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", ENTRY_BG)],
                  foreground=[("readonly", TEXT)])

        tk.Label(self, text="Settings", fg=TEXT, bg=BG,
                 font=("Segoe UI", 13, "bold")).pack(pady=(14, 6))

        row("API Key", self._api_key, show="•")
        row("Base URL", self._base_url)
        row("Model", self._model)
        row("Hotkey combo", self._hotkey)

        self._in_dev_var  = device_combo_row("Mic / Input",   self._in_device_list,  c.get("audio_device"))
        self._out_dev_var = device_combo_row("Audio Output",  self._out_device_list, c.get("audio_output_device"))

        # ── Sound test buttons ────────────────────────────────────────────────
        test_frame = tk.Frame(self, bg=BG)
        test_frame.pack(fill="x", **pad)
        tk.Label(test_frame, text="Test sounds", fg=TEXT_DIM, bg=BG,
                 width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
        for label, kind in [("▶ Start", "start"), ("▶ Stop", "stop"), ("▶ Error", "error")]:
            tk.Button(
                test_frame, text=label, fg=TEXT, bg=ACCENT,
                relief="flat", bd=0, padx=10, pady=3,
                font=("Segoe UI", 8), cursor="hand2",
                activebackground=GREEN, activeforeground=BG,
                command=lambda k=kind: _play_cue(k, device=self._selected_out_device()),
            ).pack(side="left", padx=(0, 4))

        # ── Language row ──────────────────────────────────────────────────────
        lang_frame = tk.Frame(self, bg=BG)
        lang_frame.pack(fill="x", **pad)
        tk.Label(lang_frame, text="Language", fg=TEXT_DIM, bg=BG,
                 width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
        tk.Entry(lang_frame, textvariable=self._language, bg=ENTRY_BG,
                 fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Consolas", 10), width=14).pack(side="left")
        tk.Label(lang_frame, text=" (blank = auto-detect)", fg=TEXT_DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")

        chk_frame = tk.Frame(self, bg=BG)
        chk_frame.pack(fill="x", **pad)
        tk.Checkbutton(chk_frame, text="Auto-paste after transcription",
                       variable=self._auto_paste, fg=TEXT, bg=BG,
                       activebackground=BG, selectcolor=ENTRY_BG,
                       font=("Segoe UI", 9)).pack(side="left")

        top_frame = tk.Frame(self, bg=BG)
        top_frame.pack(fill="x", **pad)
        tk.Checkbutton(top_frame, text="Always on top",
                       variable=self._always_on_top, fg=TEXT, bg=BG,
                       activebackground=BG, selectcolor=ENTRY_BG,
                       font=("Segoe UI", 9)).pack(side="left")

        startup_frame = tk.Frame(self, bg=BG)
        startup_frame.pack(fill="x", **pad)
        tk.Checkbutton(startup_frame, text="Start with Windows",
                       variable=self._start_with_windows, fg=TEXT, bg=BG,
                       activebackground=BG, selectcolor=ENTRY_BG,
                       font=("Segoe UI", 9)).pack(side="left")

        tk.Frame(self, height=1, bg=ACCENT).pack(fill="x", pady=(10, 0))

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=10)

        tk.Button(btn_row, text="Save", fg=BG, bg=GREEN,
                  relief="flat", bd=0, padx=20, pady=6,
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  command=self._save).pack(side="left", padx=6)
        tk.Button(btn_row, text="Cancel", fg=TEXT, bg=ACCENT,
                  relief="flat", bd=0, padx=20, pady=6,
                  font=("Segoe UI", 10), cursor="hand2",
                  command=self.destroy).pack(side="left", padx=6)

    def _selected_out_device(self) -> int | None:
        sel = self._out_dev_var.get()
        for idx, lbl in self._out_device_list:
            if lbl == sel:
                return idx
        return None

    def _resolve_device(self, var: tk.StringVar,
                        device_list: list[tuple[int | None, str]]) -> int | None:
        sel = var.get()
        for idx, lbl in device_list:
            if lbl == sel:
                return idx
        return None

    def _save(self):
        c = self.app.cfg
        c["api_key"]      = self._api_key.get().strip()
        c["api_base_url"] = self._base_url.get().strip()
        c["model"]        = self._model.get().strip()
        c["hotkey"]       = self._hotkey.get().strip()
        c["language"]     = self._language.get().strip()
        c["auto_paste"]   = self._auto_paste.get()
        c["always_on_top"] = self._always_on_top.get()
        c["audio_device"]        = self._resolve_device(self._in_dev_var,  self._in_device_list)
        c["audio_output_device"] = self._resolve_device(self._out_dev_var, self._out_device_list)
        cfg.save(c)
        _set_startup(self._start_with_windows.get())
        self.app.apply_settings()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        self.cfg = cfg.load()
        self._q: queue.Queue = queue.Queue()
        self._recorder = AudioRecorder()
        self._hotkey_monitor: Optional[HotkeyMonitor] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._foreground_hwnd: int = 0
        self._target_app: dict = {}
        self._record_start: float = 0.0
        self._status = S_IDLE
        self._history: list[HistoryEntry] = []
        self._blink_job = None
        self._total_cost: float = 0.0

        _load_sounds()
        self._build_ui()
        self._load_history_from_disk()
        self.apply_settings()
        self._start_queue_drain()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("Resonance")
        self.root.configure(bg=BG)
        self.root.geometry("460x540")
        self.root.minsize(360, 400)

        self._build_header()
        self._build_status_bar()
        self._build_history_area()
        self._build_footer()

    def _build_header(self):
        header = tk.Frame(self.root, bg=BG, pady=10)
        header.pack(fill="x", padx=14)

        title = tk.Label(header, text="Resonance", fg=GREEN, bg=BG,
                         font=("Segoe UI", 16, "bold"))
        title.pack(side="left")

        settings_btn = tk.Button(header, text="⚙ Settings", fg=TEXT, bg=ACCENT,
                                 relief="flat", bd=0, padx=10, pady=4,
                                 font=("Segoe UI", 9), cursor="hand2",
                                 activebackground=GREEN, activeforeground=BG,
                                 command=self._open_settings)
        settings_btn.pack(side="right")

        clear_btn = tk.Button(header, text="Clear", fg=TEXT_DIM, bg=BG,
                              relief="flat", bd=0, padx=8, pady=4,
                              font=("Segoe UI", 9), cursor="hand2",
                              activebackground=ACCENT, activeforeground=TEXT,
                              command=self._clear_history)
        clear_btn.pack(side="right", padx=(0, 6))

    def _build_status_bar(self):
        bar = tk.Frame(self.root, bg=ACCENT, pady=8, padx=14)
        bar.pack(fill="x")

        indicator_frame = tk.Frame(bar, bg=ACCENT)
        indicator_frame.pack(side="left")

        self._dot = tk.Canvas(indicator_frame, width=14, height=14,
                              bg=ACCENT, highlightthickness=0)
        self._dot.pack(side="left")
        self._dot_oval = self._dot.create_oval(2, 2, 12, 12, fill=TEXT_DIM, outline="")

        self._status_label = tk.Label(bar, text="Idle  •  Hold hotkey to record",
                                      fg=TEXT, bg=ACCENT,
                                      font=("Segoe UI", 9))
        self._status_label.pack(side="left", padx=8)

        # Hotkey hint on right
        self._hotkey_label = tk.Label(bar, text="", fg=TEXT_DIM, bg=ACCENT,
                                      font=("Consolas", 8))
        self._hotkey_label.pack(side="right")

    def _build_history_area(self):
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True, padx=0, pady=0)

        self._canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical",
                                 command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._history_frame = tk.Frame(self._canvas, bg=BG)
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._history_frame, anchor="nw"
        )

        self._history_frame.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # Placeholder shown when history is empty
        self._placeholder = tk.Label(
            self._history_frame,
            text="Transcriptions will appear here.\n\nHold your hotkey, speak, release.",
            fg=TEXT_DIM, bg=BG, font=("Segoe UI", 10),
            justify="center"
        )
        self._placeholder.pack(pady=60)

    def _build_footer(self):
        footer = tk.Frame(self.root, bg=BG2, pady=6)
        footer.pack(fill="x", side="bottom")

        copy_last_btn = tk.Button(footer, text="Copy Last", fg=TEXT, bg=ACCENT,
                                  relief="flat", bd=0, padx=12, pady=4,
                                  font=("Segoe UI", 9), cursor="hand2",
                                  activebackground=GREEN, activeforeground=BG,
                                  command=self._copy_last)
        copy_last_btn.pack(side="left", padx=10)

        self._copy_all_btn = tk.Button(footer, text="Copy All", fg=TEXT, bg=ACCENT,
                                       relief="flat", bd=0, padx=12, pady=4,
                                       font=("Segoe UI", 9), cursor="hand2",
                                       activebackground=GREEN, activeforeground=BG,
                                       command=self._copy_all)
        self._copy_all_btn.pack(side="left")

        self._cost_label = tk.Label(footer, text="$0.000000 spent", fg=TEXT_DIM, bg=BG2,
                                    font=("Segoe UI", 8))
        self._cost_label.pack(side="right", padx=10)

        version = tk.Label(footer, text="v1.0  •", fg=TEXT_DIM, bg=BG2,
                           font=("Segoe UI", 8))
        version.pack(side="right")

    def _update_cost_display(self) -> None:
        c = self._total_cost
        if c < 0.01:
            text = f"${c:.6f} spent"
        else:
            text = f"${c:.4f} spent"
        self._cost_label.config(text=text)

    # ── settings + apply ─────────────────────────────────────────────────────

    def apply_settings(self):
        """Apply current self.cfg to hotkey monitor, always-on-top, etc."""
        # Always-on-top
        self.root.attributes("-topmost", self.cfg.get("always_on_top", False))

        # Hotkey monitor
        hotkey = self.cfg.get("hotkey", "ctrl+alt+space")
        self._hotkey_label.config(text=hotkey)

        if self._hotkey_monitor is None:
            self._hotkey_monitor = HotkeyMonitor(
                hotkey,
                on_press=self._on_hotkey_press,
                on_release=self._on_hotkey_release,
            )
            t = threading.Thread(target=self._hotkey_monitor.start, daemon=True)
            t.start()
        else:
            self._hotkey_monitor.update_keys(hotkey)

    def _open_settings(self):
        SettingsPanel(self)

    # ── hotkey callbacks (called from keyboard thread) ────────────────────────

    def _on_hotkey_press(self):
        if self._status not in (S_IDLE, S_ERROR):
            return
        # Capture the currently focused window BEFORE we do anything
        self._foreground_hwnd = ctypes.windll.user32.GetForegroundWindow()
        self._target_app = _get_window_info(self._foreground_hwnd)
        self._record_start = time.time()
        self._q.put(("status", S_RECORDING))
        self._q.put(("start_recording",))

    def _on_hotkey_release(self):
        if self._status != S_RECORDING:
            return
        self._q.put(("status", S_PROCESSING))
        self._q.put(("stop_recording",))

    # ── queue drain (main thread) ─────────────────────────────────────────────

    def _start_queue_drain(self):
        self.root.after(50, self._drain_queue)

    def _drain_queue(self):
        try:
            while True:
                msg = self._q.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_queue)

    def _handle_msg(self, msg: tuple):
        kind = msg[0]

        if kind == "status":
            self._set_status(msg[1])

        elif kind == "start_recording":
            self._recorder.start(device=self.cfg.get("audio_device"))
            _play_cue("start", device=self.cfg.get("audio_output_device"))

        elif kind == "stop_recording":
            _play_cue("stop", device=self.cfg.get("audio_output_device"))
            self._worker_thread = threading.Thread(
                target=self._transcribe_worker,
                daemon=True,
            )
            self._worker_thread.start()

        elif kind == "result":
            text = msg[1]
            pasted = self.cfg.get("auto_paste", True)
            self._add_history(text)
            self._set_status(S_IDLE)
            if pasted:
                self._paste_text(text)
            cost = self._save_transcript(text, pasted=pasted)
            self._total_cost += cost
            self._update_cost_display()

        elif kind == "error":
            err = msg[1]
            self._add_history(f"[ERROR] {err}", is_error=True)
            self._set_status(S_ERROR)
            _play_cue("error", device=self.cfg.get("audio_output_device"))
            self._save_transcript(f"[ERROR] {err}", pasted=False)
            self.root.after(3000, lambda: self._set_status(S_IDLE))

    # ── worker thread ─────────────────────────────────────────────────────────

    def _transcribe_worker(self):
        wav_path = None
        try:
            wav_path = self._recorder.stop()
            if wav_path is None:
                self._q.put(("error", "No audio captured (too short or mic error)."))
                return
            text = transcribe(
                wav_path,
                api_key=self.cfg["api_key"],
                api_base_url=self.cfg["api_base_url"],
                model=self.cfg["model"],
                language=self.cfg["language"],
            )
            if text:
                self._q.put(("result", text))
            else:
                self._q.put(("error", "Transcription returned empty text."))
        except Exception as exc:
            self._q.put(("error", str(exc)))
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

    # ── transcript persistence ────────────────────────────────────────────────

    def _load_history_from_disk(self) -> None:
        """Populate the history panel from the persistent JSONL log on startup."""
        log_file = cfg.CONFIG_DIR / "history.jsonl"
        if not log_file.exists():
            return
        records: list[dict] = []
        try:
            with open(log_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            return

        # Sum costs from every record in the full log (not just the displayed slice)
        for record in records:
            # cost_usd is stored directly if available, otherwise recalculate
            cost = record.get("cost_usd")
            if cost is None:
                cost = _calc_cost(
                    record.get("duration_sec"),
                    record.get("model", "whisper-1"),
                )
            self._total_cost += cost
        self._update_cost_display()

        # Display at most the 300 most recent entries
        for record in records[-300:]:
            text = record.get("text", "")
            if not text:
                continue
            try:
                dt = datetime.fromisoformat(record["timestamp"])
                ts = dt.strftime("%m-%d %H:%M")
            except Exception:
                ts = record.get("time", "??:??")
            self._add_history(text, ts=ts)

        # Scroll to bottom after all entries are rendered
        self.root.after(100, lambda: self._canvas.yview_moveto(1.0))

    def _save_transcript(self, text: str, pasted: bool) -> float:
        """Append a JSONL record to ~/.resonance/history.jsonl.

        Returns the USD cost of this transcription call.
        """
        now = datetime.now()
        duration = round(time.time() - self._record_start, 1) if self._record_start else None
        model = self.cfg.get("model", "whisper-1")
        cost = _calc_cost(duration, model)
        record = {
            "timestamp": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "text": text,
            "pasted": pasted,
            "app_process": self._target_app.get("app_process", ""),
            "app_title": self._target_app.get("app_title", ""),
            "duration_sec": duration,
            "model": model,
            "cost_usd": round(cost, 6),
        }
        log_file = cfg.CONFIG_DIR / "history.jsonl"
        try:
            cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass  # non-critical, never crash the app over logging
        return cost

    # ── paste ─────────────────────────────────────────────────────────────────

    def _paste_text(self, text: str):
        hwnd = self._foreground_hwnd
        if not hwnd:
            pyperclip.copy(text)
            return
        try:
            pyperclip.copy(text)
            # Restore focus to the window that was active when recording started
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            time.sleep(0.08)
            pyautogui.hotkey("ctrl", "v")
        except Exception:
            # Fallback: at least the clipboard has the text
            pyperclip.copy(text)

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _set_status(self, status: str):
        self._status = status
        colours = {
            S_IDLE:       (TEXT_DIM, "Idle  •  Hold hotkey to record"),
            S_RECORDING:  (RED,      "Recording…  (release to stop)"),
            S_PROCESSING: (YELLOW,   "Processing…"),
            S_ERROR:      (RED,      "Error  •  Check history"),
        }
        dot_colour, label_text = colours.get(status, (TEXT_DIM, ""))
        self._dot.itemconfig(self._dot_oval, fill=dot_colour)
        self._status_label.config(text=label_text)

        # Blink the dot while recording
        if self._blink_job:
            self.root.after_cancel(self._blink_job)
            self._blink_job = None
        if status == S_RECORDING:
            self._blink(True)

    def _blink(self, visible: bool):
        if self._status != S_RECORDING:
            self._dot.itemconfig(self._dot_oval, fill=RED)
            return
        colour = RED if visible else BG
        self._dot.itemconfig(self._dot_oval, fill=colour)
        self._blink_job = self.root.after(500, lambda: self._blink(not visible))

    def _add_history(self, text: str, is_error: bool = False, ts: str | None = None):
        if self._placeholder.winfo_ismapped():
            self._placeholder.pack_forget()

        if ts is None:
            ts = datetime.now().strftime("%H:%M:%S")
        entry = HistoryEntry(self._history_frame, ts, text)
        entry.pack(fill="x", padx=8, pady=2)
        self._history.append(entry)

        # Scroll to bottom (skip during bulk load – caller handles it)
        self._canvas.update_idletasks()
        self._canvas.yview_moveto(1.0)

    def _clear_history(self):
        for e in self._history:
            e.destroy()
        self._history.clear()
        if not self._placeholder.winfo_ismapped():
            self._placeholder.pack(pady=60)

    def _copy_last(self):
        if self._history:
            pyperclip.copy(self._history[-1].text)

    def _copy_all(self):
        if self._history:
            combined = "\n".join(e.text for e in self._history)
            pyperclip.copy(combined)

    # ── canvas scroll helpers ─────────────────────────────────────────────────

    def _on_frame_configure(self, _event=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── shutdown ──────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._hotkey_monitor:
            self._hotkey_monitor.stop()
        if self._recorder.is_recording:
            self._recorder.stop()
        self.root.destroy()


def run():
    App()
