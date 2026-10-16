#!/usr/bin/env python3
"""
ElevateX Live Traffic Injector - Dashboard
Clean dark-themed GUI for controlling the MSFS AI traffic injector.
"""

import os
import json
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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


# ── Palette (ElevateX brand: dark slate + elvtx-yellow) ──────────────────────
BG       = "#060608"  # site background
SURFACE  = "#141416"  # card surface
HEADER_BG = "#0c0c0e" # header band
BORDER   = "#222222"
ACCENT   = "#faa307"  # --color-elvtx-yellow (brand)
ACCENT_HOVER = "#ffae1a"
ACCENT_FG    = "#060608"  # dark text on yellow
GREEN    = "#3fb950"
RED      = "#f87171"
TEXT     = "#f4f6f9"
MUTED    = "#99a1af"
INFO     = "#a8b3c2"

FONT_UI    = ("Segoe UI", 10)
FONT_BOLD  = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI", 14, "bold")
FONT_MONO  = ("Consolas", 9)


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
        self.root.geometry("1040x900")
        self.root.minsize(940, 760)
        self.root.configure(bg=BG)

        self.log_q: queue.Queue = queue.Queue()
        self.runner = InjectorRunner(self.log_q)
        self.catalog: ModelCatalog | None = None
        self.catalog_status = "Scanning FSLTL models…"
        self._need_community_prompt = False  # set by bg scan, shown on main thread
        self._prompted_this_run = False
        self._build_styles()
        self._build_header()
        self._build_stats()
        self._build_main()
        self._build_log()

        # Kick off catalog scan in background
        threading.Thread(target=self._scan_catalog, daemon=True).start()

        # GUI refresh tick
        self._tick()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── styles ──────────────────────────────────────────────────────────────
    def _build_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=FONT_UI)
        s.configure("TFrame", background=BG)
        s.configure("Surface.TFrame", background=SURFACE)
        s.configure("TLabel", background=BG, foreground=TEXT, font=FONT_UI)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_UI)
        s.configure("Card.TLabel", background=SURFACE, foreground=TEXT, font=FONT_UI)
        s.configure("CardMuted.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Section.TLabel", background=SURFACE, foreground=MUTED,
                    font=("Segoe UI", 9, "bold"))
        s.configure("CardValue.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI", 16, "bold"))
        s.configure("CardAccent.TLabel", background=SURFACE, foreground=ACCENT, font=("Segoe UI", 16, "bold"))
        s.configure("Title.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
        s.configure("Header.TLabel", background=HEADER_BG, foreground=TEXT, font=FONT_UI)
        s.configure("HeaderMuted.TLabel", background=HEADER_BG, foreground=MUTED, font=("Segoe UI", 9))

        # Buttons — START is brand yellow, STOP is a quiet dark-red
        s.configure("Start.TButton", background=ACCENT, foreground=ACCENT_FG,
                    font=("Segoe UI", 10, "bold"), padding=(0, 8))
        s.map("Start.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#1e1e22")],
              foreground=[("disabled", MUTED)])
        s.configure("Stop.TButton", background="#3a232c", foreground=RED,
                    font=("Segoe UI", 10, "bold"), padding=(0, 8))
        s.map("Stop.TButton", background=[("active", "#4d2c37"), ("disabled", "#1e1e22")],
              foreground=[("disabled", MUTED)])
        # Small ghost button (Clear log, folder picker)
        s.configure("Ghost.TButton", background="#1e1e22", foreground=TEXT,
                    font=("Segoe UI", 9), padding=(10, 4))
        s.map("Ghost.TButton", background=[("active", "#2e3a52")],
              foreground=[("active", TEXT)])

        # Treeview — monospace for FIDS-style alignment
        s.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXT, font=("Consolas", 10), rowheight=26,
                    borderwidth=0)
        s.configure("Treeview.Heading", background="#141416", foreground=MUTED,
                    font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", ACCENT_FG)])
        s.map("Treeview.Heading", background=[("active", "#1e2940")])

        # Spinbox
        s.configure("TSpinbox", fieldbackground=SURFACE, background=SURFACE,
                    foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=4)

        # Entry (dark theme - default Windows Entry is white-on-white here)
        s.configure("TEntry", fieldbackground="#0d1117", background=SURFACE,
                    foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=5)
        s.map("TEntry", fieldbackground=[("focus", "#0d1117")],
              foreground=[("focus", TEXT)])

    # ── header ──────────────────────────────────────────────────────────────
    def _build_header(self):
        hdr = tk.Frame(self.root, bg=HEADER_BG, bd=0, highlightthickness=1,
                       highlightbackground=BORDER)
        hdr.pack(fill="x", padx=0, pady=0)

        inner = tk.Frame(hdr, bg=HEADER_BG)
        inner.pack(fill="x", padx=18, pady=12)

        brand = tk.Frame(inner, bg=HEADER_BG)
        brand.pack(side="left")
        try:
            import os as _os
            if hasattr(sys, "_MEIPASS"):
                _icon_path = _os.path.join(sys._MEIPASS, "icon.png")
            else:
                _icon_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "icon.png")
            if not _os.path.exists(_icon_path):
                _icon_path = _os.path.join(_os.path.dirname(sys.executable), "icon.png")
            _logo = tk.PhotoImage(file=_icon_path).subsample(8, 8)  # 256 -> 32px
            _logo_lbl = tk.Label(brand, image=_logo, bg=HEADER_BG)
            _logo_lbl.image = _logo
            _logo_lbl.pack(side="left", padx=(0, 8))
        except Exception:
            pass
        tk.Label(brand, text="ELEVATEX", font=("Segoe UI", 16, "bold"),
                 bg=HEADER_BG, fg=ACCENT).pack(side="left")
        tk.Label(brand, text="  LIVE TRAFFIC", font=("Segoe UI", 12),
                 bg=HEADER_BG, fg=TEXT).pack(side="left")

        self.status_var = tk.StringVar(value="STOPPED")
        self.status_lbl = tk.Label(inner, textvariable=self.status_var,
                                   font=("Segoe UI", 10, "bold"),
                                   bg="#1e1e22", fg=RED, padx=12, pady=3)
        self.status_lbl.pack(side="right")

    # ── stats cards ─────────────────────────────────────────────────────────
    def _build_stats(self):
        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=18, pady=(14, 4))

        self.stat_vars = {}
        cards = [
            ("msfs",   "MSFS",           "—"),
            ("active", "ElevateX Aircraft", "0"),
            ("feed",   "Nearby Flights", "0"),
        ]
        for key, title, default in cards:
            card = tk.Frame(row, bg=SURFACE, bd=0, highlightthickness=1,
                            highlightbackground=BORDER)
            card.pack(side="left", fill="both", expand=True, padx=(0, 10))

            if key == "active":
                # thin brand-accent strip on top of the hero card
                tk.Frame(card, bg=ACCENT, height=2)\
                    .pack(fill="x", side="top")
            ttk.Label(card, text=title.upper(), style="CardMuted.TLabel")\
                .pack(anchor="w", padx=12, pady=(8, 0))
            var = tk.StringVar(value=default)
            style = "CardAccent.TLabel" if key == "active" else "CardValue.TLabel"
            ttk.Label(card, textvariable=var, style=style)\
                .pack(anchor="w", padx=12, pady=(0, 8))
            self.stat_vars[key] = var

    # ── main area (settings + table) ────────────────────────────────────────
    def _build_main(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=18, pady=8)

        # ── left: control sidebar (scrollable; actions stay pinned) ──
        left = tk.Frame(main, bg=SURFACE, bd=0, highlightthickness=1,
                        highlightbackground=BORDER, width=256)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        # Actions pinned to the bottom (packed first so they stay there)
        btn_frame = tk.Frame(left, bg=SURFACE)
        btn_frame.pack(side="bottom", fill="x", padx=14, pady=(12, 14))

        self.btn_start = ttk.Button(btn_frame, text="▶  START", style="Start.TButton",
                                    command=self._on_start)
        self.btn_start.pack(fill="x", pady=(0, 6))

        self.btn_stop = ttk.Button(btn_frame, text="■  STOP", style="Stop.TButton",
                                   command=self._on_stop, state="disabled")
        self.btn_stop.pack(fill="x")

        # Scrollable content: sections scroll when the window is short,
        # the START/STOP buttons above always stay visible.
        side_scroll = tk.Canvas(left, bg=SURFACE, highlightthickness=0)
        side_vsb = ttk.Scrollbar(left, orient="vertical",
                                 command=side_scroll.yview)
        side_scroll.configure(yscrollcommand=side_vsb.set)
        side_vsb.pack(side="right", fill="y")
        side_scroll.pack(side="left", fill="both", expand=True)
        content = tk.Frame(side_scroll, bg=SURFACE)
        content_win = side_scroll.create_window((0, 0), window=content,
                                                anchor="nw")

        def _sync_side_scroll(_evt=None):
            try:
                side_scroll.configure(scrollregion=side_scroll.bbox("all"))
                side_scroll.itemconfig(content_win,
                                       width=side_scroll.winfo_width())
            except Exception:
                pass
        content.bind("<Configure>", _sync_side_scroll)
        side_scroll.bind("<Configure>", _sync_side_scroll)

        def _side_wheel(evt):
            # Only scroll the sidebar when the pointer is over it,
            # so the aircraft table keeps its own behaviour.
            try:
                w = self.root.winfo_containing(evt.x_root, evt.y_root)
                if w is not None and str(w).startswith(str(left)):
                    side_scroll.yview_scroll(-1 * (evt.delta // 120),
                                             "units")
            except Exception:
                pass
        self.root.bind_all("<MouseWheel>", _side_wheel, add="+")

        # ── TRAFFIC section ──
        ttk.Label(content, text="TRAFFIC", style="Section.TLabel")\
            .pack(anchor="w", padx=14, pady=(14, 8))

        self.var_radius = tk.DoubleVar(value=40)
        self.var_max    = tk.DoubleVar(value=40)

        for label, var, frm, to in [
            ("Radius (NM)",       self.var_radius, 5, 200),
            ("Max Traffic",       self.var_max,    1, 200),
        ]:
            ttk.Label(content, text=label, style="Card.TLabel")\
                .pack(anchor="w", padx=14, pady=(4, 0))
            spin = ttk.Spinbox(content, from_=frm, to=to, textvariable=var,
                               width=10, increment=1)
            spin.pack(anchor="w", padx=14, pady=(2, 4))

        ttk.Label(content, text="Tracking: auto (30 Hz smoothed)",
                  style="CardMuted.TLabel", wraplength=208)\
            .pack(anchor="w", padx=14, pady=(8, 0))

        # ── PILOT section ──
        ttk.Label(content, text="PILOT", style="Section.TLabel")\
            .pack(anchor="w", padx=14, pady=(14, 6))
        ttk.Label(content, text="ElevateX Username",
                  style="Card.TLabel")\
            .pack(anchor="w", padx=14, pady=(0, 0))
        self.var_user_id = tk.StringVar(value=_load_saved_username())
        uid_entry = tk.Entry(content, textvariable=self.var_user_id, width=20,
                             bg=BG, fg=TEXT, insertbackground=ACCENT,
                             relief="flat", bd=0, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT,
                             font=FONT_UI)
        uid_entry.pack(anchor="w", padx=14, pady=(4, 2), ipady=5)
        # Saved indicator (checkmark when username is persisted)
        self.var_user_saved = tk.StringVar(
            value="✓ Saved" if _load_saved_username() else "")
        ttk.Label(content, textvariable=self.var_user_saved,
                  style="CardMuted.TLabel")\
            .pack(anchor="w", padx=14, pady=(0, 0))
        # Live verification status (updated every tick while running)
        self.var_user_status = tk.StringVar()
        self.user_status_lbl = ttk.Label(content, textvariable=self.var_user_status,
                                         style="CardMuted.TLabel", wraplength=208)
        self.user_status_lbl.pack(anchor="w", padx=14, pady=(0, 2))
        ttk.Label(content,
                  text="Your aircraft is excluded only while you're flying on ElevateX. Leave empty to inject all traffic.",
                  style="CardMuted.TLabel", wraplength=208)\
            .pack(anchor="w", padx=14, pady=(2, 0))

        # ── MODELS section ──
        ttk.Label(content, text="FSLTL MODELS", style="Section.TLabel")\
            .pack(anchor="w", padx=14, pady=(14, 6))
        self.cat_var = tk.StringVar(value=self.catalog_status)
        ttk.Label(content, textvariable=self.cat_var, style="CardMuted.TLabel",
                  wraplength=208)\
            .pack(anchor="w", padx=14, pady=(0, 4))

        # Manual Community-folder picker (also used when auto-search fails)
        self.btn_folder = ttk.Button(content, text="📁  Community folder…",
                                     style="Ghost.TButton",
                                     command=self._on_pick_folder)
        self.btn_folder.pack(anchor="w", padx=14, pady=(0, 4))

        # ── right: aircraft table ──
        right = tk.Frame(main, bg=SURFACE, bd=0, highlightthickness=1,
                         highlightbackground=BORDER)
        right.pack(side="left", fill="both", expand=True)

        thead = tk.Frame(right, bg=SURFACE)
        thead.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(thead, text="LIVE AIRCRAFT", style="Section.TLabel")\
            .pack(side="left")
        self.tree_count = tk.StringVar(value="")
        ttk.Label(thead, textvariable=self.tree_count, style="CardMuted.TLabel")\
            .pack(side="right")

        cols = ("callsign", "type", "airline", "dist", "alt", "gs", "model", "status")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="none")
        # Total fixed widths ≈ 520 px; MODEL stretches to fill the rest
        headings = {
            "callsign": ("CALLSIGN",    90, "w"),
            "type":     ("TYPE",        50, "center"),
            "airline":  ("AIRLINE",     70, "w"),
            "dist":     ("DIST",        55, "e"),
            "alt":      ("ALT",         60, "e"),
            "gs":       ("GS",          50, "e"),
            "model":    ("MODEL",      160, "w"),
            "status":   ("STATUS",      65, "center"),
        }
        for cid, (text, width, anchor) in headings.items():
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor,
                             stretch=(cid == "model"), minwidth=0)
        self.tree.tag_configure("odd", background="#1f2940")

        vsb = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(right, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.pack(side="top", fill="both", expand=True, padx=(12, 0), pady=(0, 0))
        hsb.pack(side="bottom", fill="x", padx=(12, 0), pady=(0, 2))
        vsb.pack(side="right", fill="y", pady=(0, 10), padx=(0, 10))

    # ── log console ─────────────────────────────────────────────────────────
    def _build_log(self):
        frame = tk.Frame(self.root, bg=SURFACE, bd=0, highlightthickness=1,
                         highlightbackground=BORDER)
        frame.pack(fill="x", padx=18, pady=(0, 14))

        loghead = tk.Frame(frame, bg=SURFACE)
        loghead.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(loghead, text="LOG", style="Section.TLabel")\
            .pack(side="left")
        ttk.Button(loghead, text="Clear", style="Ghost.TButton",
                   command=self._clear_log).pack(side="right")

        self.log_text = tk.Text(frame, height=6, bg=SURFACE, fg=TEXT,
                                 font=FONT_MONO, insertbackground=TEXT,
                                 selectbackground=ACCENT, selectforeground=ACCENT_FG,
                                relief="flat", bd=0, state="disabled",
                                wrap="word")
        self.log_text.pack(fill="x", padx=12, pady=(4, 12))

        # Tag colours for log lines
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
    def _on_start(self):
        if self.runner.running or self.catalog is None:
            return
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
        self.var_user_saved.set("✓ Saved" if uname else "")
        self.runner.start(
            catalog=self.catalog,
            radius=self.var_radius.get(),
            max_ac=self.var_max.get(),
            my_user_id=uname,
        )
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status_var.set("RUNNING")
        self.status_lbl.configure(fg=GREEN)

    def _on_stop(self):
        self._log("Stopping injector…", "warn")
        self.runner.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status_var.set("STOPPED")
        self.status_lbl.configure(fg=RED)
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
        elif inj and not self.runner.running and self.status_var.get() == "RUNNING":
            # Worker died unexpectedly (e.g. SimConnect connect failed):
            # unstick the UI instead of sitting on an empty list.
            self._log("[Error] Injector stopped unexpectedly - see log above. Check MSFS is running and in a flight.", "err")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.status_var.set("STOPPED")
            self.status_lbl.configure(fg=RED)
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
            if r[0] in current:
                self.tree.item(r[0], values=display, tags=tags)
            else:
                self.tree.insert("", "end", iid=r[0], values=display,
                                 tags=tags)

    # ── close ───────────────────────────────────────────────────────────────
    def _on_close(self):
        if self.runner.running:
            self.runner.stop()
            # Give the worker a moment to clean up sim objects
            time.sleep(0.5)
        self.root.destroy()


# ── entry point ──────────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
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
    root.mainloop()


if __name__ == "__main__":
    main()
