#!/usr/bin/env python3
"""
ElevateX Live Traffic Injector - Dashboard
Clean dark-themed GUI for controlling the MSFS AI traffic injector.
"""

import os
import json
import math
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import app_update
from app_update import CURRENT_VERSION

# When frozen by PyInstaller, make sure the bundled module is importable
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))

from elevatex_live_traffic import (
    LiveTrafficInjector,
    ModelCatalog,
    SIMCONNECT_AVAILABLE,
    haversine_distance_nm,
)

# ── persistent settings (remembers a manually picked Community folder) ──────
def _config_path() -> str:
    base = os.getenv("APPDATA", os.path.expanduser("~"))
    d = os.path.join(base, "ElevateXTrafficInjector")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "config.json")


def _load_saved_community() -> str:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            p = (json.load(f) or {}).get("community_path", "")
            return p if p and os.path.isdir(p) else ""
    except Exception:
        return ""


def _save_community(path: str) -> None:
    try:
        cfg = {}
        try:
            with open(_config_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception:
            pass
        cfg["community_path"] = path
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def _load_saved_username() -> str:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("username", "")
    except Exception:
        return ""


def _save_username(name: str) -> None:
    try:
        cfg = {}
        try:
            with open(_config_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception:
            pass
        cfg["username"] = name
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def _tips_seen() -> bool:
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return bool((json.load(f) or {}).get("tips_done", False))
    except Exception:
        return False


def _save_tips_seen() -> None:
    try:
        cfg = {}
        try:
            with open(_config_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception:
            pass
        cfg["tips_done"] = True
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


# ── Palette ──────────────────────────────────────────────────────────────────
BG           = "#0B0C0E"   # window
SURFACE      = "#121417"   # panels / cards
HEADER_BG    = "#0B0C0E"
BORDER       = "#23262C"   # hairline
RAISED       = "#1A1D22"   # inputs, ghost buttons
ACCENT       = "#F5A623"   # brand yellow
ACCENT_HOVER = "#FFB43A"
ACCENT_FG    = "#0B0C0E"
GREEN        = "#4ADE80"
RED          = "#FF6B6B"
TEXT         = "#E8EAED"
MUTED        = "#8A919B"
INFO         = "#AEB6C2"

FONT_UI     = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_TITLE  = ("Segoe UI", 15, "bold")
FONT_LABEL  = ("Segoe UI", 8, "bold")
FONT_SMALL  = ("Segoe UI", 9)
FONT_TINY   = ("Segoe UI", 8)
FONT_NUM    = ("Segoe UI", 20, "bold")
FONT_MONO   = ("Consolas", 9)


# ── small UI toolkit (rounded panels / pill buttons / steppers) ─────────────
def _rounded_points(x1, y1, x2, y2, r, steps=8):
    """Polygon points for a rectangle with rounded corners."""
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = []
    for cx, cy, a0, a1 in ((x2 - r, y1 + r, -90, 0),
                           (x2 - r, y2 - r, 0, 90),
                           (x1 + r, y2 - r, 90, 180),
                           (x1 + r, y1 + r, 180, 270)):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class RoundPanel(tk.Canvas):
    """A flat panel with rounded corners and an embedded content frame."""

    def __init__(self, master, *, fill=SURFACE, border=BORDER, radius=14,
                 pad=16, **kw):
        super().__init__(master, bg=master.cget("bg"), highlightthickness=0,
                         bd=0, **kw)
        self._fill, self._border = fill, border
        self._radius, self._pad = radius, pad
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(0, 0, window=self.body, anchor="nw")
        self._shape = None
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _evt=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        pts = _rounded_points(0.5, 0.5, w - 0.5, h - 0.5, self._radius)
        if self._shape is None:
            self._shape = self.create_polygon(pts, fill=self._fill,
                                              outline=self._border, width=1)
            self.tag_lower(self._shape)
        else:
            self.coords(self._shape, *pts)
        p = self._pad
        self.coords(self._win, p, p)
        self.itemconfigure(self._win, width=max(w - 2 * p, 1),
                           height=max(h - 2 * p, 1))


class PillButton(tk.Canvas):
    """Flat rounded button with hover and a disabled state."""

    def __init__(self, master, text="", command=None, *, bg=ACCENT,
                 fg=ACCENT_FG, hover=None, disabled_bg=RAISED,
                 disabled_fg=MUTED, font=FONT_BOLD, height=38, width=120,
                 radius=10, border=None):
        super().__init__(master, bg=master.cget("bg"), highlightthickness=0,
                         bd=0, height=height, width=width, cursor="hand2")
        self._text, self._command = text, command
        self._bg, self._fg = bg, fg
        self._hover = hover or bg
        self._dbg, self._dfg = disabled_bg, disabled_fg
        self._font, self._radius = font, radius
        self._border = border
        self._state = "normal"
        self._hovering = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<ButtonRelease-1>", lambda _e: self._draw())

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w = int(self.cget("width"))
        if h <= 1:
            h = int(self.cget("height"))
        self.delete("all")
        fill = self._bg if self._state == "normal" else self._dbg
        if self._state == "normal" and self._hovering:
            fill = self._hover
        fg = self._fg if self._state == "normal" else self._dfg
        pts = _rounded_points(0.5, 0.5, w - 0.5, h - 0.5, self._radius)
        self.create_polygon(pts, fill=fill, outline=self._border or fill,
                            width=1)
        self.create_text(w / 2, h / 2, text=self._text, fill=fg,
                         font=self._font)

    def _on_enter(self, _e=None):
        self._hovering = True
        self._draw()

    def _on_leave(self, _e=None):
        self._hovering = False
        self._draw()

    def _on_click(self, _e=None):
        if self._state == "normal" and self._command:
            self._command()

    def configure(self, cnf=None, **kw):
        text = kw.pop("text", None)
        state = kw.pop("state", None)
        command = kw.pop("command", None)
        kw.pop("cursor", None)
        if text is not None:
            self._text = text
        if command is not None:
            self._command = command
        if state is not None:
            self._state = state
            try:
                super().configure(cursor="hand2" if state == "normal" else "arrow")
            except Exception:
                pass
        if cnf or kw:
            super().configure(cnf, **kw)
        self._draw()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


class SlimScrollbar(tk.Canvas):
    """Minimal dark scrollbar: no arrows, no trough borders."""

    def __init__(self, master, command, *, width=8, fill="#1B1E24",
                 thumb="#3A4049", hover="#4A515B"):
        super().__init__(master, width=width, bg=master.cget("bg"),
                         highlightthickness=0, bd=0)
        self._command = command
        self._fill, self._thumb, self._hover = fill, thumb, hover
        self._first, self._last = 0.0, 1.0
        self._drag = None
        self._thumb_id = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))

    def set(self, first, last):
        try:
            self._first, self._last = float(first), float(last)
        except (TypeError, ValueError):
            return
        self._draw()

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        self.delete("all")
        if self._last - self._first >= 0.999:
            return  # everything fits: leave the background clean
        self.create_rectangle(0, 0, w, h, fill=self._fill, outline="")
        top = self._first * h
        bottom = self._last * h
        if bottom - top < 26:
            mid = (top + bottom) / 2.0
            top, bottom = mid - 13.0, mid + 13.0
            top = max(0.0, top)
            bottom = min(float(h), top + 26.0)
        self._thumb_id = self.create_rectangle(1, top + 1, w - 1, bottom - 1,
                                               fill=self._thumb, outline="")

    def _on_press(self, evt):
        h = max(self.winfo_height(), 1)
        if self._thumb_id is not None:
            x1, y1, x2, y2 = self.coords(self._thumb_id)
            if y1 <= evt.y <= y2:
                self._drag = evt.y - y1
                return
        frac = max(0.0, min(1.0, evt.y / h))
        self._command("moveto", frac)

    def _on_drag(self, evt):
        if self._drag is None:
            return
        h = max(self.winfo_height(), 1)
        frac = max(0.0, min(1.0, (evt.y - self._drag) / h))
        self._command("moveto", frac)


class CoachTip(tk.Toplevel):
    """One-time orange hint bubble with black text."""

    def __init__(self, master, parent_bg, title, body, step, total,
                 on_next, on_skip, last=False):
        super().__init__(master)
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        self.configure(bg=parent_bg)

        canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        card = tk.Frame(canvas, bg=ACCENT)
        # Invisible spacer fixes the width so the bubble does not jump around
        tk.Frame(card, bg=ACCENT, width=330, height=1).pack()
        tk.Label(card, text=title, bg=ACCENT, fg="#0B0C0E",
                 font=("Segoe UI", 10, "bold"), anchor="w", justify="left",
                 wraplength=310).pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(card, text=body, bg=ACCENT, fg="#1A1608",
                 font=("Segoe UI", 9), anchor="w", justify="left",
                 wraplength=310).pack(fill="x", padx=16)

        foot = tk.Frame(card, bg=ACCENT)
        foot.pack(fill="x", padx=16, pady=(12, 12))
        tk.Label(foot, text=f"{step} of {total}", bg=ACCENT, fg="#5A4308",
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        PillButton(foot, text="Done" if last else "Next", command=on_next,
                   bg="#0B0C0E", fg=ACCENT, hover="#241F14", height=28,
                   width=64, radius=8,
                   font=("Segoe UI", 8, "bold")).pack(side="right")
        PillButton(foot, text="Skip", command=on_skip, bg=ACCENT, fg="#0B0C0E",
                   hover="#E0941A", height=28, width=52, radius=8,
                   font=("Segoe UI", 8, "bold")).pack(side="right", padx=(0, 6))

        card.update_idletasks()
        cw, ch = card.winfo_reqwidth(), card.winfo_reqheight()
        pad = 2
        self._tip_w, self._tip_h = cw + pad * 2, ch + pad * 2
        canvas.configure(width=self._tip_w, height=self._tip_h)
        canvas.create_polygon(
            _rounded_points(0.5, 0.5, self._tip_w - 0.5, self._tip_h - 0.5, 12),
            fill=ACCENT, outline="", tags="bg")
        canvas.tag_lower("bg")
        canvas.create_window(pad, pad, window=card, anchor="nw")
        self.bind("<Escape>", lambda _e: on_skip())

    def place_near(self, widget, where="right"):
        self.update_idletasks()
        try:
            wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
            ww, wh = widget.winfo_width(), widget.winfo_height()
        except Exception:
            return
        W, H = self._tip_w, self._tip_h
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        gap = 14
        if where == "right":
            x, y = wx + ww + gap, wy + wh // 2 - H // 2
        elif where == "left":
            x, y = wx - W - gap, wy + wh // 2 - H // 2
        elif where == "above":
            x, y = wx + ww // 2 - W // 2, wy - H - gap
        elif where == "inside":
            x, y = wx + ww - W - 26, wy + 20
        else:  # below
            x, y = wx + ww // 2 - W // 2, wy + wh + gap
        x = max(8, min(x, sw - W - 8))
        y = max(8, min(y, sh - H - 56))
        self.geometry(f"{W}x{H}+{x}+{y}")


class Stepper(tk.Frame):
    """Compact numeric field: −  [ 40 ]  +"""

    def __init__(self, master, variable, lo, hi, step=1, width=128):
        super().__init__(master, bg=master.cget("bg"), width=width, height=30)
        self.pack_propagate(False)
        self.var, self.lo, self.hi, self.step = variable, lo, hi, step

        self.minus = PillButton(self, "−", self._dec, bg=RAISED, fg=TEXT,
                                hover="#242830", height=30, width=30,
                                radius=8, font=("Segoe UI", 12, "bold"))
        self.minus.pack(side="left")
        holder = tk.Frame(self, bg=RAISED, height=30,
                          highlightthickness=1, highlightbackground=BORDER)
        holder.pack(side="left", fill="x", expand=True, padx=6)
        holder.pack_propagate(False)
        self.entry = tk.Entry(holder, textvariable=self.var, justify="center",
                              bg=RAISED, fg=TEXT, insertbackground=ACCENT,
                              relief="flat", bd=0, font=FONT_UI,
                              highlightthickness=0, width=4)
        self.entry.pack(fill="both", expand=True)
        self.entry.bind("<FocusOut>", lambda _e: self._commit())
        self.entry.bind("<Return>", lambda _e: self._commit())
        self.plus = PillButton(self, "+", self._inc, bg=RAISED, fg=TEXT,
                               hover="#242830", height=30, width=30,
                               radius=8, font=("Segoe UI", 12, "bold"))
        self.plus.pack(side="left")

    def _clamp(self, value):
        try:
            value = int(round(float(value)))
        except (TypeError, ValueError):
            value = self.lo
        return max(self.lo, min(self.hi, value))

    def _commit(self):
        self.var.set(self._clamp(self.var.get()))

    def _inc(self):
        self.var.set(self._clamp(self.var.get() + self.step))

    def _dec(self):
        self.var.set(self._clamp(self.var.get() - self.step))



# ── stdout / stderr redirector → queue ───────────────────────────────────────
class _QueueWriter:
    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, s: str):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._q.put(line)

    def flush(self):
        if self._buf.strip():
            self._q.put(self._buf)
        self._buf = ""


# ── Background runner ────────────────────────────────────────────────────────
class InjectorRunner:
    """Runs LiveTrafficInjector.start() on a daemon thread with clean stop."""

    def __init__(self, log_q: queue.Queue):
        self.log_q = log_q
        self.injector: LiveTrafficInjector | None = None
        self._thread: threading.Thread | None = None
        self.running = False

    # ---- start ------------------------------------------------------------
    def start(self, catalog: ModelCatalog, radius: float, max_ac: float,
              my_user_id: str = ""):
        if self.running:
            return
        self.injector = LiveTrafficInjector(
            radius_nm=radius,
            max_aircraft=int(max_ac),
            catalog=catalog,
            my_elevatex_username=my_user_id,
        )
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- worker -----------------------------------------------------------
    def _run(self):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _QueueWriter(self.log_q)
        sys.stderr = _QueueWriter(self.log_q)
        try:
            assert self.injector is not None
            self.injector.start()
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001
            self.log_q.put(f"[Error] {exc}")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            self.running = False

    # ---- stop -------------------------------------------------------------
    def stop(self):
        if self.injector:
            self.injector.stop()
        self.running = False


# ── Dashboard ────────────────────────────────────────────────────────────────
class Dashboard:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ElevateX Traffic Injector")
        self.root.geometry("1180x800")
        self.root.minsize(1040, 700)
        self.root.configure(bg=BG)

        self.log_q: queue.Queue = queue.Queue()
        self.runner = InjectorRunner(self.log_q)
        self.catalog: ModelCatalog | None = None
        self.catalog_status = "Scanning FSLTL models…"
        self._need_community_prompt = False  # set by bg scan, shown on main thread
        self._prompted_this_run = False
        self._update_info: dict | None = None
        self._update_win: tk.Toplevel | None = None
        self._update_cancel = False
        self._checking = False
        self._installing = False
        self._tip_win: CoachTip | None = None
        self._tip_index = 0
        self._tip_steps = []
        self._build_styles()
        self._build_header()
        self._build_stats()
        self._build_main()

        # Folders left behind by a previous in-app update can be deleted now
        self._cleanup_leftovers()

        # Kick off catalog scan in background
        threading.Thread(target=self._scan_catalog, daemon=True).start()

        # Check for a newer release shortly after start (no network on the UI
        # thread, and a failure is only a log line, never a popup)
        self.root.after(1500, self._start_update_check)

        # One-time guided tips, only on the very first launch
        self.root.after(900, self._maybe_show_tips)

        # GUI refresh tick
        self._tick()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── styles ──────────────────────────────────────────────────────────────
    def _build_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=FONT_UI)

        # Aircraft table: borderless, roomy rows, muted uppercase headings.
        s.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXT, font=("Segoe UI", 9), rowheight=30,
                    borderwidth=0, relief="flat", bordercolor=SURFACE,
                    lightcolor=SURFACE, darkcolor=SURFACE)
        s.configure("Treeview.Heading", background=SURFACE, foreground=MUTED,
                    font=("Segoe UI", 8, "bold"), relief="flat",
                    padding=(10, 9), borderwidth=0, bordercolor=SURFACE,
                    lightcolor=SURFACE, darkcolor=SURFACE)
        s.map("Treeview", background=[("selected", "#1F2329")],
              foreground=[("selected", TEXT)])
        s.map("Treeview.Heading", background=[("active", SURFACE)],
              foreground=[("active", TEXT)])

        # Slim scrollbar so it does not look like Windows 95.
        try:
            s.configure("Slim.Vertical.TScrollbar", background="#2A2E35",
                        troughcolor=SURFACE, bordercolor=SURFACE,
                        arrowcolor=MUTED, width=8, arrowsize=8,
                        gripcount=0, relief="flat")
            s.map("Slim.Vertical.TScrollbar",
                  background=[("active", "#3A3F48")])
        except Exception:
            pass

        try:
            s.configure("Accent.Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=RAISED, bordercolor=RAISED,
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        except Exception:
            pass

    # ── header ──────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=BG)
        hdr.pack(fill="x")

        inner = tk.Frame(hdr, bg=BG)
        inner.pack(fill="x", padx=22, pady=(16, 14))

        brand = tk.Frame(inner, bg=BG)
        brand.pack(side="left")
        try:
            if hasattr(sys, "_MEIPASS"):
                _icon_path = os.path.join(sys._MEIPASS, "icon.png")
            else:
                _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "icon.png")
            if not os.path.exists(_icon_path):
                _icon_path = os.path.join(os.path.dirname(sys.executable),
                                          "icon.png")
            _logo = tk.PhotoImage(file=_icon_path).subsample(8, 8)
            _logo_lbl = tk.Label(brand, image=_logo, bg=BG)
            _logo_lbl.image = _logo
            _logo_lbl.pack(side="left", padx=(0, 10))
        except Exception:
            pass

        tk.Label(brand, text="ElevateX", font=("Segoe UI", 15, "bold"),
                 bg=BG, fg=TEXT).pack(side="left")
        tk.Label(brand, text="Traffic Injector", font=("Segoe UI", 11),
                 bg=BG, fg=MUTED).pack(side="left", padx=(8, 0))

        self.version_lbl = tk.Label(brand, text=f"v{CURRENT_VERSION}",
                                    font=FONT_TINY, bg=RAISED, fg=MUTED,
                                    padx=7, pady=2, cursor="hand2")
        self.version_lbl.pack(side="left", padx=(12, 0))
        self.version_lbl.bind("<Button-1>", self._manual_check)

        self.help_btn = PillButton(brand, text="?", command=self._replay_tips,
                                   bg=RAISED, fg=MUTED, hover="#242830",
                                   height=22, width=24, radius=11,
                                   font=("Segoe UI", 9, "bold"))
        self.help_btn.pack(side="left", padx=(6, 0))

        # Status pill (dot + label)
        self.status_var = tk.StringVar(value="Stopped")
        pill = tk.Frame(inner, bg=RAISED, padx=12, pady=5)
        pill.pack(side="right")
        self.status_dot = tk.Canvas(pill, width=8, height=8, bg=RAISED,
                                    highlightthickness=0)
        self._dot = self.status_dot.create_oval(0, 0, 8, 8, fill=MUTED,
                                                outline="")
        self.status_dot.pack(side="left", padx=(0, 7))
        self.status_lbl = tk.Label(pill, textvariable=self.status_var,
                                   font=("Segoe UI", 9, "bold"),
                                   bg=RAISED, fg=MUTED)
        self.status_lbl.pack(side="left")

        # Update pill — hidden until a newer release is found
        self.update_btn = PillButton(inner, text="Update available",
                                     command=self._show_update_dialog,
                                     bg=ACCENT, fg=ACCENT_FG,
                                     hover=ACCENT_HOVER, height=30, width=160,
                                     radius=15, font=("Segoe UI", 9, "bold"))

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

    # ── stats cards ─────────────────────────────────────────────────────────
    def _build_stats(self):
        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=22, pady=(18, 0))

        self.stat_vars = {}
        cards = [
            ("msfs",   "MSFS",             "—",      TEXT),
            ("active", "ELEVATEX AIRCRAFT", "0",     ACCENT),
            ("feed",   "NEARBY FLIGHTS",    "0",     TEXT),
        ]
        for key, title, default, colour in cards:
            card = RoundPanel(row, height=86, pad=16)
            card.pack(side="left", fill="x", expand=True,
                      padx=(0, 12) if key != "feed" else 0)
            tk.Label(card.body, text=title, font=FONT_LABEL, bg=SURFACE,
                     fg=MUTED).pack(anchor="w")
            var = tk.StringVar(value=default)
            tk.Label(card.body, textvariable=var, font=FONT_NUM, bg=SURFACE,
                     fg=colour).pack(anchor="w", pady=(2, 0))
            self.stat_vars[key] = var

    # ── main area (settings + table) ────────────────────────────────────────
    def _build_main(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=22, pady=(16, 18))

        # ── left: control sidebar ──
        left = RoundPanel(main, width=300, pad=18)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        side = left.body

        # Actions pinned to the bottom
        btn_frame = tk.Frame(side, bg=SURFACE)
        btn_frame.pack(side="bottom", fill="x")

        self.btn_stop = PillButton(btn_frame, text="Stop", command=self._on_stop,
                                   bg=RAISED, fg=TEXT, hover="#242830",
                                   disabled_bg=RAISED, disabled_fg="#4A4F57",
                                   height=42, radius=11, font=FONT_BOLD)
        self.btn_stop.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_stop.configure(state="disabled")

        self.btn_start = PillButton(btn_frame, text="Start injection",
                                    command=self._on_start, bg=ACCENT,
                                    fg=ACCENT_FG, hover=ACCENT_HOVER,
                                    height=42, radius=11,
                                    font=("Segoe UI", 10, "bold"))
        self.btn_start.pack(side="bottom", fill="x")

        content = tk.Frame(side, bg=SURFACE)
        content.pack(side="top", fill="both", expand=True)

        def section(text):
            tk.Label(content, text=text, font=FONT_LABEL, bg=SURFACE,
                     fg=MUTED).pack(anchor="w", pady=(0, 10))

        def divider():
            tk.Frame(content, bg=BORDER, height=1).pack(fill="x", pady=14)

        # ── CONFIGURATION ──
        section("CONFIGURATION")

        self.var_radius = tk.IntVar(value=40)
        self.var_max = tk.IntVar(value=40)
        for label, var, frm, to in [("Search radius", self.var_radius, 5, 200),
                                    ("Max aircraft", self.var_max, 1, 200)]:
            row = tk.Frame(content, bg=SURFACE)
            row.pack(fill="x", pady=(0, 8))
            tk.Label(row, text=label, font=FONT_SMALL, bg=SURFACE,
                     fg=TEXT).pack(side="left")
            Stepper(row, var, frm, to).pack(side="right")
            if var is self.var_radius:
                self.radius_row = row
            else:
                self.max_row = row

        tk.Label(content, text="NM · auto tracking at 30 Hz", font=FONT_TINY,
                 bg=SURFACE, fg=MUTED).pack(anchor="w", pady=(0, 2))

        divider()

        # ── PILOT ──
        section("PILOT")
        tk.Label(content, text="ElevateX username", font=FONT_SMALL,
                 bg=SURFACE, fg=TEXT).pack(anchor="w", pady=(0, 6))

        self.entry_panel = entry_panel = tk.Frame(content, bg=RAISED, highlightthickness=1,
                               highlightbackground=BORDER,
                               highlightcolor=ACCENT)
        entry_panel.pack(fill="x")
        self.var_user_id = tk.StringVar(value=_load_saved_username())
        uid_entry = tk.Entry(entry_panel, textvariable=self.var_user_id,
                             bg=RAISED, fg=TEXT, insertbackground=ACCENT,
                             relief="flat", bd=0, font=FONT_UI,
                             highlightthickness=0)
        uid_entry.pack(fill="x", padx=10, pady=7)

        status_row = tk.Frame(content, bg=SURFACE)
        status_row.pack(fill="x", pady=(6, 0))
        self.var_user_saved = tk.StringVar(
            value="Saved" if _load_saved_username() else "")
        tk.Label(status_row, textvariable=self.var_user_saved, font=FONT_TINY,
                 bg=SURFACE, fg=GREEN).pack(side="left")
        self.var_user_status = tk.StringVar()
        self.user_status_lbl = tk.Label(content,
                                        textvariable=self.var_user_status,
                                        font=FONT_TINY, bg=SURFACE, fg=MUTED,
                                        anchor="w", justify="left",
                                        wraplength=248)
        self.user_status_lbl.pack(anchor="w", pady=(2, 0))

        tk.Label(content,
                 text="Your own aircraft is excluded while you fly on "
                      "ElevateX. Leave empty to inject all traffic.",
                 font=FONT_TINY, bg=SURFACE, fg=MUTED, justify="left",
                 wraplength=248).pack(anchor="w", pady=(6, 0))

        divider()

        # ── MODELS ──
        section("FSLTL MODELS")
        self.cat_var = tk.StringVar(value=self.catalog_status)
        tk.Label(content, textvariable=self.cat_var, font=FONT_TINY,
                 bg=SURFACE, fg=MUTED, justify="left", wraplength=248)\
            .pack(anchor="w", pady=(0, 8))

        self.btn_folder = PillButton(content, text="Choose folder",
                                     command=self._on_pick_folder,
                                     bg=RAISED, fg=TEXT, hover="#242830",
                                     height=30, width=140, radius=8,
                                     font=FONT_SMALL)
        self.btn_folder.pack(anchor="w")

        # ── right: table + activity ──
        right = tk.Frame(main, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))

        table_card = RoundPanel(right, pad=0)
        table_card.pack(fill="both", expand=True)
        tbl = table_card.body

        thead = tk.Frame(tbl, bg=SURFACE)
        thead.pack(fill="x", padx=18, pady=(13, 9))
        tk.Label(thead, text="Live aircraft", font=("Segoe UI", 11, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side="left")
        self.tree_count = tk.StringVar(value="")
        tk.Label(thead, textvariable=self.tree_count, font=FONT_SMALL,
                 bg=SURFACE, fg=MUTED).pack(side="right")

        tk.Frame(tbl, bg=BORDER, height=1).pack(fill="x")

        self.tree_wrap = tree_wrap = tk.Frame(tbl, bg=SURFACE)
        tree_wrap.pack(fill="both", expand=True, padx=6, pady=(6, 10))

        cols = ("callsign", "type", "airline", "dist", "alt", "gs", "model",
                "status")
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings",
                                 selectmode="none", style="Treeview")
        headings = {
            "callsign": ("CALLSIGN", 116, "w"),
            "type":     ("TYPE",      62, "center"),
            "airline":  ("AIRLINE",   82, "w"),
            "dist":     ("DIST",      62, "e"),
            "alt":      ("ALT",       74, "e"),
            "gs":       ("GS",        56, "e"),
            "model":    ("MODEL",    180, "w"),
            "status":   ("STATUS",    76, "center"),
        }
        for cid, (text, width, anchor) in headings.items():
            self.tree.heading(cid, text=text, anchor=anchor)
            self.tree.column(cid, width=width, anchor=anchor,
                             stretch=(cid == "model"), minwidth=60)
        self.tree.tag_configure("odd", background="#16191E")
        self.tree.tag_configure("pending", foreground=MUTED)

        vsb = SlimScrollbar(tree_wrap, command=self.tree.yview, width=8)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", pady=(0, 4))
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree_empty = tk.Label(tree_wrap, text="Waiting for traffic…",
                                   font=FONT_SMALL, bg=SURFACE, fg=MUTED)
        self.tree_empty.place(relx=0.5, rely=0.5, anchor="center")

        self.log_card = log_card = RoundPanel(right, height=170, pad=0)
        log_card.pack(fill="x", pady=(16, 0))
        logbox = log_card.body

        loghead = tk.Frame(logbox, bg=SURFACE)
        loghead.pack(fill="x", padx=18, pady=(12, 8))
        tk.Label(loghead, text="Activity", font=("Segoe UI", 11, "bold"),
                 bg=SURFACE, fg=TEXT).pack(side="left")
        PillButton(loghead, text="Clear", command=self._clear_log, bg=RAISED,
                   fg=MUTED, hover="#242830", height=24, width=64, radius=7,
                   font=FONT_TINY).pack(side="right")

        self.log_text = tk.Text(logbox, height=5, bg=SURFACE, fg=INFO,
                                font=FONT_MONO, insertbackground=TEXT,
                                selectbackground=ACCENT,
                                selectforeground=ACCENT_FG, relief="flat",
                                bd=0, state="disabled", wrap="word",
                                padx=18, pady=0, spacing1=1, spacing3=1)
        self.log_text.pack(fill="both", expand=True, pady=(0, 12))

        self.log_text.tag_configure("err", foreground=RED)
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("info", foreground=INFO)
        self.log_text.tag_configure("warn", foreground=ACCENT)
        self.log_text.tag_configure("dim", foreground=MUTED)


    def _clear_log(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    # ── catalog scan ────────────────────────────────────────────────────────
    def _scan_catalog(self, custom_path: str = ""):
        try:
            saved = custom_path or _load_saved_community()
            self.catalog = ModelCatalog(
                custom_community_path=saved or None)
            n = sum(len(v) for v in self.catalog.models_by_icao.values())
            types = len(self.catalog.models_by_icao)
            if self.catalog.fsltl_found:
                self.cat_var.set(f"FSLTL: {n} liveries / {types} types ✓")
            else:
                self.cat_var.set("FSLTL not found!\nInstall via FBW Installer.")
                # No Community folder at all -> ask the user to pick it
                # (dialog must run on the main thread; the tick shows it).
                # If a folder WAS found but FSLTL is missing, they need to
                # install FSLTL instead - no popup, Folder button is there.
                if (not self.catalog.checked_paths
                        and not self._prompted_this_run):
                    self._need_community_prompt = True
        except Exception as exc:  # noqa: BLE001
            self.cat_var.set(f"Catalog error: {exc}")

    def _maybe_prompt_community(self):
        """Main-thread folder picker when no Community folder was found."""
        if not self._need_community_prompt or self._prompted_this_run:
            return
        self._need_community_prompt = False
        self._prompted_this_run = True
        try:
            want = messagebox.askyesno(
                "Community folder not found",
                "No Flight Simulator Community folder was found on this PC.\n\n"
                "Do you want to select it manually?",
                parent=self.root)
        except Exception:
            return
        if not want:
            return
        try:
            picked = filedialog.askdirectory(
                title="Select your MSFS Community folder",
                mustexist=True, parent=self.root)
        except Exception:
            return
        if not picked:
            return  # user cancelled
        _save_community(picked)
        self.cat_var.set("Scanning FSLTL models…")
        self._log(f"Community folder set, rescanning…", "info")
        threading.Thread(target=self._scan_catalog,
                         args=(picked,), daemon=True).start()

    def _on_pick_folder(self):
        """Manual re-pick via the Folder button."""
        try:
            picked = filedialog.askdirectory(
                title="Select your MSFS Community folder",
                mustexist=True, parent=self.root)
        except Exception:
            return
        if not picked:
            return
        _save_community(picked)
        self._prompted_this_run = True  # don't auto-prompt afterwards
        self.cat_var.set("Scanning FSLTL models…")
        self._log(f"Community folder set, rescanning…", "info")
        threading.Thread(target=self._scan_catalog,
                         args=(picked,), daemon=True).start()

    # ── button handlers ─────────────────────────────────────────────────────
    def _set_status(self, text: str, colour: str):
        try:
            self.status_var.set(text)
            self.status_lbl.configure(fg=colour)
            self.status_dot.itemconfigure(self._dot, fill=colour)
        except Exception:
            pass

    def _on_start(self):
        if self.runner.running or self.catalog is None:
            return
        if self._tip_win is not None:
            self._finish_tips()  # engaging with the app dismisses the tour
        if not self.catalog.fsltl_found:
            self._log("[Error] FSLTL Traffic Base Models not found - install via the "
                      "FlyByWire Installer, or pick your Community folder with the "
                      "folder button.", "err")
            # Offer the picker right away if we never found any folder
            if not self.catalog.checked_paths and not self._prompted_this_run:
                self._need_community_prompt = True
            return
        self._log("Starting injector…", "info")
        uname = self.var_user_id.get().strip()
        _save_username(uname)
        self.var_user_saved.set("Saved" if uname else "")
        try:
            radius = float(self.var_radius.get())
            max_ac = float(self.var_max.get())
        except Exception:
            radius, max_ac = 40.0, 40.0
        self.runner.start(
            catalog=self.catalog,
            radius=radius,
            max_ac=max_ac,
            my_user_id=uname,
        )
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_status("Running", GREEN)

    def _on_stop(self):
        self._log("Stopping injector…", "warn")
        self.runner.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._set_status("Stopped", MUTED)
        self.var_user_status.set("")

    # ── log helper ──────────────────────────────────────────────────────────
    def _log(self, msg: str, tag: str = ""):
        self.log_text.configure(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ── periodic GUI refresh ────────────────────────────────────────────────
    def _tick(self):
        try:
            self._maybe_prompt_community()  # main-thread folder picker, if needed
            self._refresh_once()
        except Exception as exc:  # never let the refresh loop die
            try:
                self._log(f"[GUI] refresh error: {exc}", "err")
            except Exception:
                pass
        finally:
            self.root.after(400, self._tick)

    def _refresh_once(self):
        # 1. Drain log queue
        while True:
            try:
                line = self.log_q.get_nowait()
            except queue.Empty:
                break
            tag = ""
            low = line.lower()
            if "[error" in low or "failed" in low:
                tag = "err"
            elif "connected" in low or "[spawn]" in low:
                tag = "ok"
            elif "[inject]" in low or "[simconnect]" in low:
                tag = "info"
            elif "[despawn]" in low or "warning" in low:
                tag = "warn"
            elif "[catalog]" in low or "[feed]" in low:
                tag = "dim"
            self._log(line, tag)

        # 2. Update stats from injector state
        inj = self.runner.injector
        if inj and self.runner.running:
            # Live settings: radius/max apply immediately without restart.
            # The next 1 Hz sync re-selects (nearest-N) and trims/desawns.
            try:
                inj.radius_nm = float(self.var_radius.get())
                inj.max_aircraft = int(float(self.var_max.get()))
            except Exception:
                pass
            # MSFS status
            self.stat_vars["msfs"].set(
                "Online" if inj.sim else "Offline")
            # Thread-safe snapshots (engine mutates these on its own thread)
            with inj.aircraft_lock:
                actives = list(inj.active_aircraft.items())
            feed_map = {f.id: f for f in inj.feed.snapshot()}
            # ElevateX aircraft count: only objects confirmed live in sim
            n_confirmed = sum(1 for _, a in actives if a.object_id is not None)
            self.stat_vars["active"].set(str(n_confirmed))
            # Nearby flights (from last feed poll)
            n_near = 0
            for f in feed_map.values():
                if haversine_distance_nm(inj.user_lat, inj.user_lon,
                                         f.position.latitude,
                                         f.position.longitude) <= inj.radius_nm:
                    n_near += 1
            self.stat_vars["feed"].set(str(n_near))
            # Username verification status
            try:
                ustate, ucs = inj.username_status()
            except Exception:
                ustate, ucs = "", ""
            if not ustate:
                self.var_user_status.set("")
            elif ustate == "verified":
                self.var_user_status.set(f"✓ Flying as {ucs} — own aircraft excluded")
            elif ustate == "missing":
                self.var_user_status.set("○ Not in live feed — nothing excluded")
            else:
                self.var_user_status.set("… Checking live feed…")
            # Refresh aircraft table
            self._refresh_table(inj, actives, feed_map)
        elif inj and not self.runner.running and self.status_var.get() == "Running":
            # Worker died unexpectedly (e.g. SimConnect connect failed):
            # unstick the UI instead of sitting on an empty list.
            self._log("[Error] Injector stopped unexpectedly - see log above. Check MSFS is running and in a flight.", "err")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self._set_status("Stopped", RED)
            self.stat_vars["msfs"].set("—")
            self.stat_vars["active"].set("0")
        elif not self.runner.running:
            self.stat_vars["msfs"].set("—")
            self.stat_vars["active"].set("0")

    def _refresh_table(self, inj: LiveTrafficInjector, actives, feed_map):
        # Build snapshot of active aircraft + feed data (keyed by flight id
        # so two aircraft sharing a callsign don't collapse into one row)
        rows = []
        for fid, active in actives:
            flight = feed_map.get(fid)
            if flight:
                dist = haversine_distance_nm(
                    inj.user_lat, inj.user_lon,
                    flight.position.latitude, flight.position.longitude)
                rows.append((fid,
                             active.callsign,
                             active.icao_type,
                             flight.airline_icao or "—",
                             f"{dist:.1f}",
                             f"{int(flight.position.altitude_ft):,}",
                             f"{int(flight.position.ground_speed_kt)}",
                             active.container_title[:28],
                             "Live" if active.object_id else "Pend"))
        rows.sort(key=lambda r: float(r[4]))
        try:
            if rows:
                self.tree_empty.place_forget()
            else:
                self.tree_empty.place(relx=0.5, rely=0.5, anchor="center")
        except Exception:
            pass
        n_live = sum(1 for r in rows if r[8] == "Live")
        n_pending = len(rows) - n_live
        if n_pending:
            self.tree_count.set(f"{n_live} live · {n_pending} pending")
        else:
            self.tree_count.set(f"{n_live} shown" if rows else "")

        # Update tree (flight id is the tree item id)
        current = set(self.tree.get_children())
        new_fids = {r[0] for r in rows}

        # Remove stale
        for iid in current - new_fids:
            self.tree.delete(iid)

        # Insert / update (alternating row stripe)
        for idx, r in enumerate(rows):
            display = r[1:]
            tags = ("odd",) if idx % 2 else ()
            if r[8] != "Live":
                tags = tags + ("pending",)
            if r[0] in current:
                self.tree.item(r[0], values=display, tags=tags)
            else:
                self.tree.insert("", "end", iid=r[0], values=display,
                                 tags=tags)

    # ── first-run tips ──────────────────────────────────────────────────────
    def _maybe_show_tips(self):
        if not _tips_seen():
            self._start_tips()

    def _replay_tips(self, _evt=None):
        self._start_tips(force=True)

    def _start_tips(self, force=False):
        if not force and _tips_seen():
            return
        self._close_tip()
        self._tip_steps = [
            (self.radius_row, "Search radius",
             "How far around your aircraft to look for live flights, in "
             "nautical miles. 40 NM is about 74 km. Flights outside this "
             "circle are never injected.", "right"),
            (self.max_row, "Max aircraft",
             "The most aircraft injected at the same time. A higher number "
             "makes the sky busier and asks a little more of the sim; 40 is "
             "a good starting point.", "right"),
            (self.entry_panel, "ElevateX username",
             "Your ElevateX name. While you are flying on the network, your "
             "own aircraft is skipped so you never see yourself. Leave it "
             "empty to inject every flight.", "right"),
            (self.btn_folder, "FSLTL models",
             "Each flight is matched to an FSLTL livery found in your "
             "Community folder. If the line above shows 0 liveries, install "
             "FSLTL Traffic Base or use this button to point at the folder.",
             "right"),
            (self.btn_start, "Start injection",
             "Load your flight in MSFS first, then press Start. Traffic "
             "appears within about 30 seconds and follows the real pilots.",
             "right"),
            (self.tree_wrap, "Live aircraft",
             "Everything currently injected: callsign, type, airline, "
             "distance, altitude and speed. \"Pend\" means MSFS has not "
             "confirmed the model yet.", "inside"),
            (self.log_card, "Activity",
             "What the injector is doing. Errors show in red, warnings in "
             "orange, and the update check writes its result here too.",
             "above"),
        ]
        self._tip_index = 0
        self._show_tip()

    def _show_tip(self):
        self._close_tip()
        if self._tip_index >= len(self._tip_steps):
            self._finish_tips()
            return
        widget, title, body, where = self._tip_steps[self._tip_index]
        try:
            if not widget.winfo_exists():
                self._tip_index += 1
                self._show_tip()
                return
        except Exception:
            self._tip_index += 1
            self._show_tip()
            return
        tip = CoachTip(self.root, SURFACE, title, body,
                       self._tip_index + 1, len(self._tip_steps),
                       self._next_tip, self._skip_tips,
                       last=self._tip_index == len(self._tip_steps) - 1)
        tip.place_near(widget, where)
        self._tip_win = tip

    def _next_tip(self):
        self._tip_index += 1
        self._show_tip()

    def _skip_tips(self):
        self._finish_tips()

    def _finish_tips(self):
        self._close_tip()
        _save_tips_seen()

    def _close_tip(self):
        if self._tip_win is not None:
            try:
                self._tip_win.destroy()
            except Exception:
                pass
            self._tip_win = None

    # ── updates ─────────────────────────────────────────────────────────────
    def _cleanup_leftovers(self, attempt: int = 0):
        """Delete the previous EXE after an in-app update.

        The new process starts before the old one has fully exited, and Windows
        keeps the renamed ``.old`` file locked until then. Retry a few times
        instead of leaving it on disk until the next launch.
        """
        stuck = app_update.cleanup_old()
        if stuck and attempt < 4:
            delay = 2000 if attempt < 2 else 8000
            self.root.after(delay, lambda: self._cleanup_leftovers(attempt + 1))

    def _start_update_check(self):
        threading.Thread(target=self._update_check_worker, daemon=True).start()

    def _update_check_worker(self):
        try:
            info = app_update.check_latest()
        except Exception as e:
            self._ui(lambda: self._log(f"[Update] Check failed: {e}", "dim"))
            return
        if info and app_update.is_newer(info["version"], CURRENT_VERSION):
            self._update_info = info
            self._ui(self._on_update_found)
        else:
            self._ui(lambda: self._log(
                f"[Update] Up to date (v{CURRENT_VERSION}).", "dim"))

    def _on_update_found(self):
        info = self._update_info
        if not info:
            return
        self.update_btn.configure(text=f"↑ v{info['version']} available")
        if not self.update_btn.winfo_manager():
            self.update_btn.pack(side="right", padx=(0, 10))
        self._log(f"[Update] v{info['version']} is available "
                  f"(you have v{CURRENT_VERSION}).", "warn")

    def _manual_check(self, _event=None):
        """Click the version label to check again."""
        if self._checking:
            return
        self._checking = True
        self._log(f"[Update] Checking for updates (you have "
                  f"v{CURRENT_VERSION})…", "dim")
        threading.Thread(target=self._manual_check_worker, daemon=True).start()

    def _manual_check_worker(self):
        try:
            info = app_update.check_latest()
        except Exception as e:
            def failed():
                self._checking = False
                self._log(f"[Update] Check failed: {e}", "warn")
            self._ui(failed)
            return

        def done():
            self._checking = False
            if info and app_update.is_newer(info["version"], CURRENT_VERSION):
                self._update_info = info
                self._on_update_found()
            else:
                self._log(f"[Update] You are on the latest version "
                          f"(v{CURRENT_VERSION}).", "dim")
        self._ui(done)

    def _ui(self, cb):
        """Run ``cb`` on the Tk thread; ignore late callbacks after close."""
        try:
            self.root.after(0, cb)
        except Exception:
            pass

    @staticmethod
    def _plain_notes(body: str, limit: int = 5000) -> str:
        """Make the release markdown readable in a plain Tk text box."""
        lines = []
        for line in (body or "").splitlines():
            s = line.rstrip()
            if s.startswith("#"):
                s = s.lstrip("#").strip()
            s = s.replace("**", "").replace("`", "")
            if s.startswith("> "):
                s = "  " + s[2:]
            if set(s) and set(s) <= set("-|: "):
                continue
            lines.append(s)
        text = "\n".join(lines).strip()
        if not text:
            text = "No release notes were provided."
        return text[:limit] + ("\n\n… (see the full notes on GitHub)"
                               if len(text) > limit else "")

    def _show_update_dialog(self):
        info = self._update_info
        if not info:
            return
        self._close_tip()
        if self._update_win is not None and self._update_win.winfo_exists():
            self._update_win.lift()
            self._update_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._update_win = win
        self._update_cancel = False
        win.title(f"Update available - v{info['version']}")
        win.configure(bg=BG)
        win.geometry("720x700")
        win.minsize(640, 560)
        win.transient(self.root)
        try:
            win.update_idletasks()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - 720) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - 700) // 2
            win.geometry(f"720x700+{max(0, x)}+{max(0, y)}")
            _enable_dark_titlebar(win)
        except Exception:
            pass
        try:
            win.iconphoto(False, self.root._app_icon)
        except Exception:
            pass

        head = tk.Frame(win, bg=SURFACE)
        head.pack(fill="x", padx=16, pady=(16, 10))
        tk.Frame(head, bg=ACCENT, height=2).pack(fill="x", side="top")
        tk.Label(head, text=f"v{info['version']} is ready",
                 font=("Segoe UI", 16, "bold"), bg=SURFACE, fg=TEXT)\
            .pack(anchor="w", padx=16, pady=(12, 0))
        tk.Label(head, text=f"You have v{CURRENT_VERSION}  ·  {info['name']}",
                 font=("Segoe UI", 9), bg=SURFACE, fg=MUTED)\
            .pack(anchor="w", padx=16, pady=(2, 12))

        notes_wrap = tk.Frame(win, bg=BG)
        notes_wrap.pack(fill="both", expand=True, padx=16)
        notes = tk.Text(notes_wrap, bg=SURFACE, fg=INFO, bd=0,
                        highlightthickness=1, highlightbackground=BORDER,
                        wrap="word", font=("Segoe UI", 9), padx=12, pady=10,
                        insertbackground=TEXT, selectbackground="#2b3442")
        vsb = SlimScrollbar(notes_wrap, command=notes.yview, width=8)
        notes.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        notes.pack(side="left", fill="both", expand=True)
        notes.insert("1.0", self._plain_notes(info["body"]))
        notes.configure(state="disabled")

        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=16, pady=(10, 16))

        self.upd_status = tk.StringVar(
            value=("Ready to download "
                   f"({info['asset_size'] / (1024 * 1024):.1f} MB)."))
        tk.Label(foot, textvariable=self.upd_status, font=("Segoe UI", 9),
                 bg=BG, fg=MUTED).pack(anchor="w", pady=(0, 6))
        self.upd_prog = ttk.Progressbar(foot, mode="determinate",
                                        maximum=100, value=0,
                                        style="Accent.Horizontal.TProgressbar")
        self.upd_prog.pack(fill="x", pady=(0, 12))

        btns = tk.Frame(foot, bg=BG)
        btns.pack(fill="x")
        PillButton(btns, text="Later", command=self._close_update_dialog,
                   bg=RAISED, fg=TEXT, hover="#242830", height=34, width=84,
                   radius=9, font=FONT_SMALL).pack(side="right")
        PillButton(btns, text="Release notes",
                   command=lambda: self._open_url(info["url"]),
                   bg=RAISED, fg=TEXT, hover="#242830", height=34, width=124,
                   radius=9, font=FONT_SMALL).pack(side="right", padx=(0, 8))
        self.btn_upd_install = PillButton(
            btns, text="Download & install", command=self._install_update,
            bg=ACCENT, fg=ACCENT_FG, hover=ACCENT_HOVER, height=34, width=170,
            radius=9, font=FONT_BOLD)
        self.btn_upd_install.pack(side="left")

        tip = ("The app closes and restarts itself when the download is "
               "verified." if app_update.is_frozen() else
               "Running from source: the zip will be saved to Downloads.")
        tk.Label(foot, text=tip, font=("Segoe UI", 8), bg=BG, fg=MUTED)\
            .pack(anchor="w", pady=(8, 0))

        win.protocol("WM_DELETE_WINDOW", self._close_update_dialog)
        try:
            win.grab_set()
        except tk.TclError:
            pass

    def _close_update_dialog(self):
        self._update_cancel = True
        self._installing = False
        if self._update_win is not None:
            try:
                self._update_win.grab_release()
                self._update_win.destroy()
            except Exception:
                pass
            self._update_win = None

    def _open_url(self, url: str):
        try:
            import webbrowser
            webbrowser.open(url, new=2)
        except Exception:
            pass

    def _install_update(self):
        info = self._update_info
        if not info or getattr(self, "_installing", False):
            return
        self._installing = True
        self._update_cancel = False
        self.btn_upd_install.configure(state="disabled",
                                       text="Downloading…")
        threading.Thread(target=self._install_worker, args=(info,),
                         daemon=True).start()

    def _install_worker(self, info: dict):
        tmp = tempfile.mkdtemp(prefix="elevatex_upd_")
        try:
            zip_path = app_update.download_update(
                info["asset_url"], tmp,
                expected_size=info.get("asset_size") or 0,
                expected_sha256=info.get("asset_sha256"),
                progress=self._update_progress,
                cancel=lambda: self._update_cancel)
            if self._update_cancel:
                return
            self._ui(lambda: self._set_update_status(
                "Verified. Preparing the new build…"))
            if not app_update.is_frozen():
                saved = app_update.save_zip_for_manual_install(zip_path)
                app_update.open_folder(os.path.dirname(saved))
                self._ui(lambda: self._finish_manual(saved))
                return

            new_exe = app_update.prepare_new_exe(zip_path)
            if self._update_cancel:
                return
            self._ui(lambda: self._set_update_status(
                "Installing - the app will restart…"))
            # Stop injecting first so the old instance releases its sim
            # objects before the new one connects.
            try:
                if self.runner.running:
                    self.runner.stop()
                    time.sleep(0.6)
            except Exception:
                pass
            if self._update_cancel:
                return
            app_update.install_and_restart(new_exe)
        except app_update.UpdateError as e:
            if str(e) == "cancelled":
                return
            self._ui(lambda: self._update_failed(str(e)))
            return
        except Exception as e:  # never let a worker kill the app silently
            self._ui(lambda: self._update_failed(str(e)))
            return
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        # The replacement process is starting; close this instance.
        self._ui(self.root.destroy)

    def _update_progress(self, done: int, total: int):
        self._ui(lambda: self._render_progress(done, total))

    def _render_progress(self, done: int, total: int):
        if self._update_win is None or not self._update_win.winfo_exists():
            return
        mb = 1024 * 1024
        if total > 0:
            pct = min(100.0, done * 100.0 / total)
            self.upd_prog.configure(mode="determinate", maximum=100,
                                    value=pct)
            self.upd_status.set(
                f"Downloading… {done / mb:.1f} / {total / mb:.1f} MB "
                f"({pct:.0f} %)")
        else:
            self.upd_status.set(f"Downloading… {done / mb:.1f} MB")

    def _set_update_status(self, text: str):
        if self._update_win is not None and self._update_win.winfo_exists():
            self.upd_status.set(text)

    def _finish_manual(self, saved: str):
        self._installing = False
        if self._update_win is not None and self._update_win.winfo_exists():
            self.upd_prog.configure(value=100)
            self.upd_status.set(f"Saved to {saved} - extract it over the app.")
            self.btn_upd_install.configure(state="normal", text="Close",
                                           command=self._close_update_dialog)

    def _update_failed(self, message: str):
        self._installing = False
        if self._update_win is not None and self._update_win.winfo_exists():
            self.upd_status.set(
                f"Update failed: {message} "
                "You can still download it from the release page "
                "(Release notes button).")
            self.btn_upd_install.configure(state="normal",
                                           text="Retry download")
        self._log(f"[Update] {message}", "err")

    # ── close ───────────────────────────────────────────────────────────────
    def _on_close(self):
        # Abort an in-flight update first: closing the window must never let a
        # download finish in the background and swap the EXE behind the user.
        self._update_cancel = True
        self._close_tip()
        if self.runner.running:
            self.runner.stop()
            # Give the worker a moment to clean up sim objects
            time.sleep(0.5)
        self.root.destroy()


# ── entry point ──────────────────────────────────────────────────────────────
def _enable_dark_titlebar(root):
    """Ask DWM for the dark window frame (Windows 10 1809+); ignore failures."""
    if os.name != "nt":
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        enabled = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20, then 19)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(enabled),
                    ctypes.sizeof(enabled)) == 0:
                break
    except Exception:
        pass


def main():
    root = tk.Tk()
    root.title("ElevateX Traffic Injector")
    root.configure(bg=BG)
    try:
        _icon_file = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
            "icon.png")
        if os.path.exists(_icon_file):
            _app_icon = tk.PhotoImage(file=_icon_file)
            root.iconphoto(True, _app_icon)
            root._app_icon = _app_icon  # keep reference so Tk doesn't GC it
    except Exception:
        pass

    Dashboard(root)
    _enable_dark_titlebar(root)

    # Centre the window on the current screen (and never open off-screen).
    root.update_idletasks()
    w = max(root.winfo_reqwidth(), 1180)
    h = max(root.winfo_reqheight(), 800)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(w, sw - 40), min(h, sh - 80)
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 24)}")

    root.mainloop()


if __name__ == "__main__":
    main()
