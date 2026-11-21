"""PhotoNest - native Windows photo library (Tkinter).

No browser, no local web server: this is a real desktop window that talks to
SQLite and the scanner directly.
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk, ImageOps

import thumbs
import themes
import ui
import desktop
from sync import LibrarySync
from tray import TrayIcon
from db import DB
from scanner import Scanner
from ai_index import AIIndexer

APP_NAME = "PhotoNest"
APP_VERSION = "0.7.3"

from ui import (
    BG, CARD, SIDEBAR_BG, LINE, TEXT, MUTED, ACCENT, ACCENT_SOFT, DARK,
    DARK_CARD, DARK_LINE, DARK_MUTED, RED, FONT,
    SUCCESS, WARNING, EDITOR_BG, SCROLL, PRIMARY, ON_ACCENT,
    Button, icon, label, rounded, rounded_photo, divider, center_window,
)

THUMB = 174
SORTS = {"拍摄时间": "taken", "添加时间": "added", "文件名": "name", "文件大小": "size"}


def folder_ancestors(folder):
    """Current directory through its volume/share root, without probing the disk."""
    result = []
    folder = os.path.abspath(folder)
    while True:
        result.append(folder)
        parent = os.path.dirname(folder)
        if parent == folder:
            return result
        folder = parent


# --------------------------------------------------------------------- paths
def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_data_dir() -> str:
    if getattr(sys, "frozen", False):
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, APP_NAME)
    return os.path.join(app_dir(), "data")


def ensure_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".write-test-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def choose_data_dir(requested: str | None) -> tuple[str | None, str, list[str]]:
    tried: list[str] = []
    if requested:
        path = os.path.abspath(requested)
        tried.append(path)
        return (path, "argument", tried) if ensure_writable(path) else (None, "argument", tried)

    app_data = os.path.join(app_dir(), "data")
    candidates = []
    # A portable library already next to the executable stays authoritative, so
    # the data directory does not move when the working directory changes
    # (for example when the app is started as administrator from Explorer).
    if os.path.isdir(app_data):
        candidates.append((app_data, "app"))
    candidates += [
        (default_data_dir(), "localappdata"),
        (app_data, "app"),
        (os.path.join(os.environ.get("TEMP") or app_dir(), APP_NAME), "temp"),
    ]
    seen: set[str] = set()
    for path, source in candidates:
        path = os.path.abspath(path)
        if path in seen:
            continue
        seen.add(path)
        tried.append(path)
        if ensure_writable(path):
            return path, source, tried
    return None, "none", tried


def setup_logging(data_dir: str) -> None:
    stream = None
    for folder in (data_dir, app_dir()):
        try:
            os.makedirs(folder, exist_ok=True)
            stream = open(os.path.join(folder, "photonest.log"), "a", encoding="utf-8", buffering=1)
            break
        except OSError:
            continue
    if stream is None:
        stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = stream
    sys.stderr = stream


def message_box(text: str, title: str = APP_NAME) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        pass


def ask_album_name(parent, title: str = "新建相册", initial: str = "") -> str | None:
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.configure(bg=CARD)
    dialog.resizable(False, False)
    dialog.transient(parent)
    center_window(dialog, parent, 460, 325)
    body = tk.Frame(dialog, bg=CARD, padx=28, pady=24)
    body.pack(fill="both", expand=True)
    head = tk.Frame(body, bg=CARD)
    head.pack(fill="x")
    tk.Label(head, image=icon(dialog, "logo", size=46), bg=CARD).pack(side="left", padx=(0, 14))
    title_box = tk.Frame(head, bg=CARD)
    title_box.pack(side="left")
    label(title_box, title, 16, bold=True).pack(anchor="w")
    label(title_box, "给喜欢的照片一个归属。", 9, MUTED).pack(anchor="w", pady=(4, 0))
    label(body, "相册名称", 10).pack(anchor="w", pady=(24, 7))
    value = tk.StringVar(value=initial)
    entry = tk.Entry(body, textvariable=value, relief="flat", bd=0, bg=CARD, fg=TEXT,
                     insertbackground=TEXT, font=(FONT, 11), highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT)
    entry.pack(fill="x", ipady=9)
    hint = label(body, "最多 80 个字符", 9, MUTED)
    hint.pack(anchor="w", pady=(7, 0))
    result = {"value": None}

    def submit():
        name = value.get().strip()
        if not name or len(name) > 80:
            hint.configure(text="请输入相册名称" if not name else "相册名称不能超过 80 个字符", fg=RED)
            entry.focus_set()
            return
        result["value"] = name
        dialog.destroy()

    footer = tk.Frame(body, bg=CARD)
    footer.pack(side="bottom", fill="x", pady=(18, 0))
    label(footer, "Enter 创建 · Esc 取消", 8, MUTED).pack(side="left")
    Button(footer, "创建相册", submit, variant="primary").pack(side="right", padx=(8, 0))
    Button(footer, "取消", dialog.destroy).pack(side="right")
    dialog.bind("<Return>", lambda e: submit())
    dialog.bind("<Escape>", lambda e: dialog.destroy())
    dialog.grab_set()
    entry.focus_set()
    if initial:
        entry.select_range(0, "end")
    dialog.wait_window()
    return result["value"]


# ------------------------------------------------------------------------ UI
class PhotoNestApp(tk.Tk):
    def __init__(self, db: DB, scanner: Scanner, cache_dir: str, data_dir: str, *, autostart=True, background=False):
        super().__init__()
        self.db = db
        self.scanner = scanner
        self.cache_dir = cache_dir
        self.data_dir = data_dir
        self._automatic_processing = bool(autostart)
        if autostart and db.get_setting('processing_pipeline_version') != '1':
            with db.batch():
                db.set_setting('ai_enabled', '1')
                db.set_setting('ai_paused', db.get_setting('scan_paused', '0'))
                db.set_setting('processing_pipeline_version', '1')
        choice = db.get_setting("theme", "system")
        self.theme_choice = choice if choice in dict(themes.CHOICES) else "system"
        self.is_dark = self.theme_choice == "dark" or (self.theme_choice == "system" and themes.system_is_dark())
        self._palette = dict(ui.set_theme(self.is_dark))
        globals().update(self._palette)
        self.ai = AIIndexer(db, db.get_setting("ai_model_dir") or os.path.join(app_dir(), "models"))
        self._search_request = None
        self._ocr_windows = {}
        self._ocr_select = False
        self._ocr_drafts = {}
        self._ocr_asset_id = None
        self._media_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo-thumb")
        self._preview_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-viewer")
        self._media_results = queue.Queue()
        self._thumb_pending = set()
        self._tiles = {}
        self._preview_serial = 0
        self.selected_ids = set()
        self.multi_select_mode = False
        self._selection_anchor = None
        self._file_busy = False
        self._closing = False
        self._context_menu = None
        self._file_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-files")

        self.view: dict = {"type": "all"}
        self.items: list[dict] = []
        self.offset = 0
        self.limit = 120
        self.has_more = False
        self.photo_cache: dict[int, ImageTk.PhotoImage] = {}
        self.lb = None
        self.lb_window: tk.Toplevel | None = None
        self._search_after = None
        self._resize_after = None
        self._scan_was_running = False
        self._last_scan_refresh = 0
        self._grid_columns = 0
        self._thumb_size = THUMB
        self._viewer_after = None
        self._lb_preview = None
        self._lb_preview_key = None
        self._settings_root = None
        self._event_tag = "PhotoNestInput" + str(id(self))
        self._app_events = queue.Queue()
        self._tray = None
        self._instance = None
        self.sync = LibrarySync(db, scanner) if background and autostart else None
        self._sync_revision = getattr(scanner, 'revision', 0)
        self._ai_sync_revision = -1
        self._hidden_to_tray = False
        self._gif_after = None
        self._gif_playing = False
        self._gif_decoder = None
        self._gif_decoder_serial = None

        self.title(f"{APP_NAME} {APP_VERSION}  ·  本地相册")
        self.geometry("1440x900")
        self.minsize(1080, 680)
        self.configure(bg=BG)

        self._build_style()
        self._build_ui()
        self.show_view({"type": "all"})
        self.bind_class(self._event_tag, "<Button-1>", self._release_search_focus)
        self.bind_class(self._event_tag, "<MouseWheel>", self._route_mousewheel)
        self.bind_class(self._event_tag, "<Control-a>", self._select_all)
        self.bind_class(self._event_tag, "<Escape>", self._selection_escape)
        self.bind_all("<Map>", self._prepare_widget_events, add="+")
        self._prepare_widget_events()
        self.after(700, self._poll_scan)
        self.after(400, self._poll_ai)
        self.after(70, self._poll_media)
        self.after(1500, self._poll_theme)
        if autostart:
            self.after(350, self._startup_scan)
        if autostart:
            self.after(1200, self._sync_ai_pipeline)
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray if self.sync else self._on_close)
        if self.sync:
            icon_path = sys.executable if getattr(sys, 'frozen', False) else os.path.join(app_dir(), 'assets', 'photonest.ico')
            self._tray = TrayIcon(lambda: self._app_events.put('open'),
                lambda: self._app_events.put('pause'), lambda: self._app_events.put('exit'), icon_path)
            if not self._tray.start():
                self._tray = None
            else:
                self._tray.set_paused(self.sync.snapshot()['paused'])
            self.after(300, self._poll_background)

    # ------------------------------------------------------------- styling
    def _build_style(self) -> None:
        self.option_add("*Font", (FONT, 10))
        self.option_add("*TCombobox*Listbox.font", (FONT, 10))
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.iconphoto(True, icon(self, "logo", size=48))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", font=(FONT, 10), padding=(10, 7),
                        fieldbackground=CARD, background=CARD, foreground=TEXT,
                        bordercolor=LINE, arrowcolor=MUTED, lightcolor=CARD, darkcolor=CARD)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)],
                  selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)])
        style.configure("Vertical.TScrollbar", background=SCROLL, troughcolor=CARD,
                        borderwidth=0, arrowsize=11, arrowcolor=MUTED,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.map("Vertical.TScrollbar", background=[("active", MUTED), ("pressed", ACCENT)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=ACCENT_SOFT,
                        borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Folders.Treeview", font=(FONT, 10), rowheight=46,
                        background=CARD, fieldbackground=CARD, foreground=TEXT,
                        bordercolor=LINE, lightcolor=CARD, darkcolor=CARD)
        style.configure("Folders.Treeview.Heading", font=(FONT, 9), background=SIDEBAR_BG,
                        foreground=MUTED, relief="flat", padding=(10, 8))
        style.map("Folders.Treeview", background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", ACCENT)])

    def _set_theme(self, choice):
        if choice not in dict(themes.CHOICES):
            return
        self.theme_choice = choice
        self.db.set_setting("theme", choice)
        self._apply_theme()

    def _apply_theme(self):
        dark = self.theme_choice == "dark" or (self.theme_choice == "system" and themes.system_is_dark())
        old = self._palette
        self.is_dark = dark
        self._palette = dict(ui.set_theme(dark))
        globals().update(self._palette)
        ui.recolor(self, old, self._palette)
        self._build_style()
        self._refresh_sidebar()
        self._render_items()
        self._theme_title_bars(self)

    def _theme_title_bars(self, widget):
        if isinstance(widget, (tk.Tk, tk.Toplevel)):
            themes.title_bar(widget, self.is_dark)
        for child in widget.winfo_children():
            if isinstance(child, tk.Toplevel):
                self._theme_title_bars(child)

    def _poll_theme(self):
        system_dark = themes.system_is_dark()
        dark = self.theme_choice == "dark" or (self.theme_choice == "system" and system_dark)
        if hasattr(self, "system_theme_var"):
            self.system_theme_var.set("当前系统：" + ("深色" if system_dark else "浅色"))
        if dark != self.is_dark:
            self._apply_theme()
        self.after(1500, self._poll_theme)

    def _prepare_widget_events(self, event=None):
        # Run the shared input route before widget/class bindings can consume the wheel.
        def attach(widget):
            if self._event_tag not in widget.bindtags():
                widget.bindtags((self._event_tag,) + widget.bindtags())
            if isinstance(widget, (tk.Tk, tk.Toplevel)):
                themes.title_bar(widget, self.is_dark)
            if widget.winfo_class() == "TCombobox":
                return
            for child in widget.winfo_children():
                attach(child)
        if event is None:
            attach(self)
        elif isinstance(event.widget, tk.Misc):
            attach(event.widget)

    def _release_search_focus(self, event):
        if self.focus_get() is self.search_entry and event.widget is not self.search_entry:
            event.widget.focus_set()

    def _end_search_input(self, event=None):
        self.focus_set()
        return "break"

    def _clear_search(self):
        self._search_request = None
        self.search_var.set("")
        self.focus_set()
        self.show_view({"type": "all"})

    # ---------------------------------------------------------------- shell
    def _build_ui(self) -> None:
        top = tk.Frame(self, bg=CARD, height=76)
        top.pack(fill="x")
        top.pack_propagate(False)
        top.grid_columnconfigure(1, weight=1)
        brand = tk.Frame(top, bg=CARD, width=232, height=76)
        brand.grid(row=0, column=0, sticky="ns")
        brand.pack_propagate(False)
        tk.Label(brand, image=icon(self, "logo", size=38), bg=CARD).pack(side="left", padx=(24, 12))
        label(brand, APP_NAME, 18, bold=True).pack(side="left")

        self.search_wrap = tk.Frame(top, bg=SIDEBAR_BG, highlightthickness=1,
                                     highlightbackground=LINE)
        self.search_wrap.grid(row=0, column=1, sticky="ew", padx=(24, 16), pady=17)
        tk.Label(self.search_wrap, image=icon(self, "search", size=18),
                 bg=SIDEBAR_BG).pack(side="left", padx=(12, 8))
        self.search_placeholder = "搜索照片、文字或画面…"
        self.search_var = tk.StringVar(value=self.search_placeholder)
        self.search_entry = tk.Entry(self.search_wrap, textvariable=self.search_var,
                                     relief="flat", bd=0, width=15, bg=SIDEBAR_BG,
                                     fg=MUTED, insertbackground=TEXT, font=(FONT, 10))
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 12))
        self.search_entry.bind("<Return>", lambda e: self._apply_search())
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)
        self.search_entry.bind("<Escape>", self._end_search_input)
        Button(self.search_wrap, "", self._clear_search, icon_name="close", variant="quiet",
               width=28, height=28).pack(side="right", padx=(0, 5))
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())

        tools = tk.Frame(top, bg=CARD)
        tools.grid(row=0, column=2, sticky="e", padx=(0, 20), pady=17)
        self.status_var = tk.StringVar(value="准备就绪")
        self.status_dot = tk.Canvas(tools, width=8, height=8, bg=CARD, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_dot_id = self.status_dot.create_oval(1, 1, 7, 7, fill="#22a06b", outline="")
        self.status_label = label(tools, color=SUCCESS, size=9, textvariable=self.status_var)
        self.status_label.pack(side="left", padx=(0, 18))
        self.scan_btn = Button(tools, "扫描并识别", self._start_scan, icon_name="refresh")
        self.scan_btn.pack(side="left", padx=(0, 10))
        self.computer_scan_btn = Button(tools, "扫描本机", self._scan_computer, icon_name="drive",
               variant="primary")
        self.computer_scan_btn.pack(side="left")
        self.scan_pause_btn = Button(tools, "暂停处理", self._pause_scan)
        self.progress = ttk.Progressbar(self, mode="determinate", style="Horizontal.TProgressbar")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")

        body = self.body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(body, bg=SIDEBAR_BG, width=232)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        footer = tk.Frame(self.sidebar, bg=SIDEBAR_BG, padx=14, pady=12)
        footer.pack(side="bottom", fill="x")
        card = tk.Frame(footer, bg=SIDEBAR_BG, highlightthickness=1, highlightbackground=LINE,
                        padx=14, pady=12)
        card.pack(fill="x", pady=(0, 6))
        label(card, "本地照片库", 9, MUTED, image=icon(self, "drive"),
              compound="left", padx=6).pack(anchor="w")
        self.library_size = tk.StringVar()
        self.library_count = tk.StringVar()
        label(card, size=15, bold=True, textvariable=self.library_size).pack(anchor="w", padx=28, pady=(2, 0))
        label(card, size=9, color=MUTED, textvariable=self.library_count).pack(anchor="w", padx=28)
        label(card, "仅在本机保存", 8, MUTED, image=icon(self, "lock", size=14),
              compound="left", padx=5).pack(anchor="w", padx=28, pady=(5, 0))
        self.ai_status_var = tk.StringVar(value="AI 待开始" if self.ai.ready else "AI 模型未就绪")
        label(card, size=8, color=MUTED, textvariable=self.ai_status_var).pack(anchor="w", padx=28, pady=(5, 0))
        self.ai_nav_btn = Button(footer, "AI 识图", lambda: self.show_view({"type": "ai"}),
                                  icon_name="image", variant="quiet", align="left")
        self.ai_nav_btn.pack(fill="x")
        self.settings_btn = Button(footer, "设置", self._open_settings, icon_name="settings",
                                   variant="quiet", align="left")
        self.settings_btn.pack(fill="x")
        self.nav_canvas = tk.Canvas(self.sidebar, bg=SIDEBAR_BG, highlightthickness=0)
        self.nav_canvas.pack(fill="both", expand=True)
        self.sidebar_nav = tk.Frame(self.nav_canvas, bg=SIDEBAR_BG)
        nav_window = self.nav_canvas.create_window(0, 0, window=self.sidebar_nav, anchor="nw")
        self.sidebar_nav.bind("<Configure>", lambda e: self.nav_canvas.configure(
            scrollregion=self.nav_canvas.bbox("all")))
        self.nav_canvas.bind("<Configure>", lambda e: self.nav_canvas.itemconfigure(nav_window, width=e.width))
        self.sidebar._wheel_target = self.nav_canvas
        tk.Frame(body, bg=LINE, width=1).pack(side="left", fill="y")

        self.main = tk.Frame(body, bg=CARD)
        self.main.pack(side="left", fill="both", expand=True)
        self.head = tk.Frame(self.main, bg=CARD, padx=28, pady=22)
        self.head.pack(fill="x")
        headings = tk.Frame(self.head, bg=CARD)
        headings.pack(side="left", fill="x", expand=True)
        self.title_var = tk.StringVar(value="全部照片")
        self.count_var = tk.StringVar()
        label(headings, size=22, bold=True, textvariable=self.title_var).pack(anchor="w")
        self.subtitle = label(headings, size=10, color=MUTED, textvariable=self.count_var)
        self.subtitle.pack(anchor="w", pady=(5, 0))
        self.sort_var = tk.StringVar(value="拍摄时间")
        self.sort_box = ttk.Combobox(self.head, textvariable=self.sort_var, values=list(SORTS),
                                     width=11, state="readonly")
        self.sort_box.pack(side="right")
        self.sort_box.bind("<<ComboboxSelected>>", lambda e: self.show_view(self.view))
        self.empty_trash_btn = Button(self.head, "清空回收站", self._empty_trash,
                                       variant="danger", icon_name="trash")
        self.search_mode = tk.StringVar(value="综合搜索")
        self.search_mode_box = ttk.Combobox(self.head, textvariable=self.search_mode,
            values=("综合搜索", "文字匹配", "画面内容"), state="readonly", width=10)
        self.search_mode_box.bind("<<ComboboxSelected>>", lambda e: self._apply_search())
        self.selection_bar = tk.Frame(self.main, bg=CARD, padx=28, pady=6)
        self.selection_text = tk.StringVar(value="已选择 0 项")
        label(self.selection_bar, textvariable=self.selection_text, size=9, color=MUTED).pack(side="left")
        self.select_all_btn = Button(self.selection_bar, "全选", self._select_all, height=32)
        self.select_all_btn.pack(side="right")
        self.clear_selection_btn = Button(self.selection_bar, "结束多选", self._clear_selection, height=32)
        self.clear_selection_btn.pack(side="right", padx=6)
        self.selection_delete_btn = Button(self.selection_bar, "删除所选", self._delete_selected,
                                           icon_name="trash", variant="danger", height=32)
        self.selection_restore_btn = Button(self.selection_bar, "恢复所选", self._restore_selected, height=32)
        self.canvas = tk.Canvas(self.main, bg=CARD, highlightthickness=0, yscrollincrement=30)
        self.vsb = ttk.Scrollbar(self.main, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.main._wheel_target = self.canvas
        self.grid_frame = tk.Frame(self.canvas, bg=CARD)
        self.grid_window = self.canvas.create_window((24, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", self._on_grid_configure)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

    # ------------------------------------------------------------- sidebar
    def _sidebar_section(self, text: str, action=None) -> None:
        row = tk.Frame(self.sidebar_nav, bg=SIDEBAR_BG)
        row.pack(fill="x", padx=24, pady=(21, 5))
        label(row, text, 9, MUTED).pack(side="left")
        if action:
            Button(row, "", action, icon_name="plus", variant="quiet", height=24,
                   width=24).pack(side="right")

    def _sidebar_row(self, icon_name: str, text: str, count, command, active: bool = False):
        row = Button(self.sidebar_nav, text, command, icon_name=icon_name, align="left",
                      variant="active" if active else "quiet", height=39,
                      badge=f"{count:,}" if count is not None else None)
        row.pack(fill="x", padx=14, pady=1)
        return row

    def _refresh_sidebar(self) -> None:
        position = self.nav_canvas.yview()[0]
        for child in self.sidebar_nav.winfo_children():
            child.destroy()
        counts = self.db.counts()
        kind = self.view["type"]
        self._sidebar_section("照片库")
        for name, text, count, target in (
            ("image", "全部照片", counts["active"], "all"),
            ("file", "视频", counts.get("videos", 0), "videos"),
            ("image", "动图 GIF", counts.get("gifs", 0), "gifs"),
            ("clock", "最近添加", None, "recent"),
            ("heart", "收藏", counts["favorites"], "favorites"),
        ):
            self._sidebar_row(name, text, count, lambda k=target: self.show_view({"type": k}), kind == target)
        self._sidebar_section("相册", self._new_album)
        albums = self.db.albums()
        for album in albums:
            self._sidebar_row("album", album["name"], album["count"],
                lambda a=album: self.show_view({"type": "album", "id": a["id"], "name": a["name"]}),
                kind == "album" and self.view.get("id") == album["id"])
        if not albums:
            label(self.sidebar_nav, "点击 + 创建第一个相册", 8, MUTED).pack(anchor="w", padx=28, pady=6)
        self._sidebar_section("照片来源")
        sources = self.db.source_stats()
        for source in sources:
            self._sidebar_row("folder", source["name"], source["count"],
                lambda s=source: self.show_view({"type": "source", "name": s["name"]}),
                kind == "source" and self.view.get("name") == source["name"])
        if not sources:
            self._sidebar_row("drive", "扫描本机", None, self._scan_computer)
        self._sidebar_section("整理")
        self._sidebar_row("duplicates", "重复清理", None, self._open_duplicates, kind == "duplicates")
        self._sidebar_row("trash", "回收站", counts["trash"],
            lambda: self.show_view({"type": "trash"}), kind == "trash")
        self.settings_btn.configure(variant="active" if kind == "settings" else "quiet")
        self.ai_nav_btn.configure(variant="active" if kind == "ai" else "quiet")
        tags = self.ai.store.tag_counts()
        if tags:
            self._sidebar_section("智能分类")
            for tag in tags:
                self._sidebar_row("image", tag["name"], tag["count"],
                    lambda name=tag["name"]: self.show_view({"type": "tag", "name": name}),
                    kind == "tag" and self.view.get("name") == tag["name"])
        self.library_size.set(self._fmt_size(counts["bytes"]))
        self.library_count.set(f"{counts['active']:,} 张照片和视频")
        self.nav_canvas.update_idletasks()
        self.nav_canvas.yview_moveto(position)

    # ---------------------------------------------------------------- view
    def show_view(self, view: dict) -> None:
        self._close_context_menu()
        self.selected_ids.clear()
        self.multi_select_mode = False
        self._selection_anchor = None
        if self._search_after:
            self.after_cancel(self._search_after)
            self._search_after = None
        self.view = dict(view)
        self.offset = 0
        self.items = []
        self.has_more = False
        self.photo_cache.clear()
        kind = self.view["type"]
        titles = {"all": "全部照片", "recent": "最近添加", "favorites": "收藏", "videos": "视频", "gifs": "动图 GIF",
                  "trash": "回收站", "settings": "设置", "duplicates": "重复清理",
                  "search": "搜索结果", "ai": "AI 识图"}
        title = titles.get(kind, "照片")
        if kind == "album":
            title = self.view.get("name") or "相册"
        elif kind == "folder":
            title = os.path.basename(self.view["path"].rstrip("\\/")) or "文件夹"
        elif kind in ("tag", "source"):
            title = self.view.get("name", "智能分类")
        self.title_var.set(title)
        if kind == "search":
            self.search_var.set(self.view.get("q", ""))
            self.search_entry.configure(fg=TEXT)
        elif self.focus_get() is self.search_entry:
            self.search_var.set("")
        else:
            self.search_var.set(self.search_placeholder)
            self.search_entry.configure(fg=MUTED)
        self.sort_box.pack_forget()
        self.search_mode_box.pack_forget()
        self.empty_trash_btn.pack_forget()
        if kind not in ("settings", "duplicates", "recent", "trash", "ai", "search"):
            self.sort_box.pack(side="right")
        if kind == "search":
            self.search_mode_box.pack(side="right")
        if kind == "trash":
            self.empty_trash_btn.pack(side="right")
        if kind == "settings":
            self.count_var.set("管理本地照片库与文件夹")
        elif kind == "duplicates":
            self.count_var.set("内容完全相同的文件 · 移到回收站后可恢复")
        elif kind == "ai":
            self.count_var.set("本地文字识别、语义搜索与自动分类")
        else:
            data = self._query(0)
            self.items, self.offset, self.has_more = data["items"], len(data["items"]), data["has_more"]
            summary = f"{data['total']:,} 张照片和视频"
            if kind == "all":
                summary += f" · {self._fmt_size(self.db.counts()['bytes'])}"
            elif kind == "search":
                summary += f" · 关键词「{self.view.get('q', '')}」"
                if "ids" in self.view:
                    summary += " · 按相关度排序"
            elif kind == "recent":
                summary += " · 按添加时间排序"
            elif kind == "trash":
                summary += " · 可在照片查看器中恢复"
            self.count_var.set(summary)
        self._refresh_sidebar()
        self._render_items()
        self.canvas.yview_moveto(0)

    def _query(self, offset: int, *, ids_only=False, limit=None) -> dict:
        kwargs = {
            "view": "all",
            "q": "",
            "sort": SORTS.get(self.sort_var.get(), "taken"),
            "offset": offset,
            "limit": self.limit if limit is None else limit,
            "ids_only": ids_only,
        }
        kind = self.view["type"]
        if kind in ("recent", "favorites", "trash"):
            kwargs["view"] = kind
        elif kind in ("videos", "gifs"):
            kwargs["type_"] = "video" if kind == "videos" else "gif"
        elif kind == "album":
            kwargs["view"] = "album"
            kwargs["album_id"] = self.view.get("id")
        elif kind == "folder":
            kwargs["folder"] = self.view.get("path")
        elif kind == "search":
            if "ids" in self.view:
                kwargs["ranked_ids"] = self.view["ids"]
            else:
                kwargs["q"] = self.view.get("q", "")
        elif kind == "tag":
            kwargs["tag"] = self.view["name"]
        elif kind == "source":
            kwargs["source_app"] = self.view["name"]
        return self.db.query_assets(**kwargs)

    def _load_more(self) -> None:
        if not self.has_more:
            return
        data = self._query(self.offset)
        self.items.extend(data["items"])
        self.offset = len(self.items)
        self.has_more = data["has_more"]
        self._render_items()

    # ---------------------------------------------------------------- grid
    def _on_grid_configure(self, _event=None) -> None:
        bounds = self.canvas.bbox("all")
        if bounds:
            self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(),
                                                max(bounds[3], self.canvas.winfo_height())))

    def _on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self.grid_window, width=max(1, event.width - 48))
        if self._resize_after:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(160, self._render_items)

    def _on_mousewheel(self, event) -> None:
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def _columns(self) -> int:
        self.update_idletasks()
        width = max(1, self.canvas.winfo_width() - 48)
        cols = max(1, (width + 8) // 184)
        size = max(1, width // cols - 12)
        if size != self._thumb_size:
            self.photo_cache.clear()
        self._thumb_size = size
        return cols

    def _render_items(self) -> None:
        self._tiles.clear()
        self._update_selection_ui()
        self._resize_after = None
        for child in self.grid_frame.winfo_children():
            child.destroy()
        for col in range(self._grid_columns):
            self.grid_frame.grid_columnconfigure(col, weight=0, minsize=0, uniform="")
        cols = self._columns()
        self._grid_columns = cols
        if self.view["type"] in ("settings", "duplicates", "ai"):
            self.grid_frame.grid_columnconfigure(0, weight=1)
            if self.view["type"] == "settings":
                self._render_settings()
            elif self.view["type"] == "ai":
                self._render_ai_settings()
            else:
                self._render_duplicates()
            self._on_grid_configure()
            return
        for col in range(cols):
            self.grid_frame.grid_columnconfigure(col, weight=1, uniform="tile")
        if not self.items:
            self._render_empty(cols)
            self._on_grid_configure()
            return
        kind = self.view["type"]
        group_key = "indexed_at" if kind == "recent" or self.sort_var.get() == "添加时间" else "taken_at"
        group_dates = kind in ("recent", "trash") or self.sort_var.get() in ("拍摄时间", "添加时间")
        # Trash is ordered by deletion time; a date heading would suggest a different order.
        group_dates = group_dates and kind not in ("trash", "search")
        month_counts = {}
        for item in self.items:
            month = (item.get(group_key) or "")[:7]
            month_counts[month] = month_counts.get(month, 0) + 1
        row, col, previous = 0, 0, None
        for asset in self.items:
            month = (asset.get(group_key) or "")[:7]
            if group_dates and month != previous:
                if col:
                    row += 1
                    col = 0
                heading = tk.Frame(self.grid_frame, bg=CARD)
                heading.grid(row=row, column=0, columnspan=cols, sticky="ew", padx=2, pady=(10 if row == 0 else 24, 12))
                title = f"{month[:4]} 年 {int(month[5:7])} 月" if len(month) == 7 else "未知时间"
                label(heading, title, 12, bold=True).pack(side="left")
                count = f"{month_counts[month]} 张" + ("已加载" if self.has_more else "")
                label(heading, count, 9, MUTED).pack(side="left", padx=12)
                tk.Frame(heading, bg=LINE, height=1).pack(side="left", fill="x", expand=True)
                previous = month
                row += 1
            self._make_tile(asset, row, col)
            col += 1
            if col == cols:
                col = 0
                row += 1
        if col:
            row += 1
        if self.has_more:
            Button(self.grid_frame, "加载更多照片", self._load_more).grid(
                row=row, column=0, columnspan=cols, pady=24)
        else:
            label(self.grid_frame, "已显示全部照片", 9, MUTED).grid(
                row=row, column=0, columnspan=cols, pady=24)
        self._on_grid_configure()

    def _update_selection_ui(self):
        browsing = self.view["type"] not in ("settings", "duplicates", "ai")
        if not browsing or not self.multi_select_mode:
            self.selection_bar.pack_forget()
            self.selection_delete_btn.pack_forget()
            for asset_id, tile in self._tiles.items():
                if tile.winfo_exists():
                    self._paint_selection(tile, asset_id)
            return
        if not self.selection_bar.winfo_manager():
            self.selection_bar.pack(fill="x", after=self.head)
        if not self.selection_delete_btn.winfo_manager():
            self.selection_delete_btn.pack(side="right", padx=6)
        count = len(self.selected_ids)
        if not self._file_busy:
            self.selection_text.set(f"已选择 {count:,} 项 · 单击勾选，Shift 连续选择")
        state = "normal" if count and not self._file_busy else "disabled"
        self.selection_delete_btn.configure(state=state,
            text="永久删除所选" if self.view["type"] == "trash" else "删除所选")
        self.clear_selection_btn.configure(state="disabled" if self._file_busy else "normal")
        self.select_all_btn.configure(state="disabled" if self._file_busy else "normal")
        if self.view["type"] == "trash":
            self.selection_restore_btn.pack(side="right", padx=6)
            self.selection_restore_btn.configure(state=state)
        else:
            self.selection_restore_btn.pack_forget()
        for asset_id, tile in self._tiles.items():
            if tile.winfo_exists():
                self._paint_selection(tile, asset_id)

    def _paint_selection(self, tile, asset_id):
        tile.delete("selected")
        tile.delete("selection-toggle")
        if not self.multi_select_mode:
            return
        size = self._thumb_size
        selected = asset_id in self.selected_ids
        tile.create_oval(size-29, 8, size-7, 30, fill=ACCENT if selected else CARD,
                         outline=ON_ACCENT if selected else MUTED, width=2, tags="selection-toggle")
        if selected:
            border = rounded(tile, (1, 1, size+3, size+3), "", ACCENT, width=3)
            tile.addtag_withtag("selected", border)
            tile.create_text(size-18, 19, text="✓", fill=ON_ACCENT, font=(FONT, 10), tags="selected")

    def _enter_multi_select(self, asset_id=None):
        if self._file_busy or self.view["type"] in ("settings", "duplicates", "ai"):
            return
        self.multi_select_mode = True
        if asset_id is not None:
            self.selected_ids.add(asset_id)
            self._selection_anchor = asset_id
        self._update_selection_ui()

    def _select_photo(self, event, asset_id):
        if self._file_busy:
            return "break"
        self._tiles[asset_id].focus_set()
        visible = [a["id"] for a in self.items]
        control, shift = bool(event.state & 0x4), bool(event.state & 0x1)
        if not self.multi_select_mode and not (control or shift):
            self._open_lightbox(asset_id)
            return "break"
        self.multi_select_mode = True
        if shift and self._selection_anchor in visible:
            start, end = sorted((visible.index(self._selection_anchor), visible.index(asset_id)))
            span = set(visible[start:end+1])
            self.selected_ids = self.selected_ids | span if control else span
        else:
            self.selected_ids.symmetric_difference_update({asset_id})
            self._selection_anchor = asset_id
        self._update_selection_ui()
        return "break"

    def _select_all(self, event=None):
        if event is not None and (event.widget.winfo_toplevel() is not self or
                isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox))):
            return  # Preserve native text selection in search and OCR editors.
        if self._file_busy or self.view["type"] in ("settings", "duplicates", "ai"):
            return
        self.multi_select_mode = True
        data = self._query(0, ids_only=True, limit=-1)
        self.selected_ids = {row["id"] for row in data["items"]}
        self._update_selection_ui()
        return "break"

    def _open_from_tile(self, event, asset_id):
        if event.state & 0x5:
            return self._select_photo(event, asset_id)
        self._open_lightbox(asset_id)
        return "break"

    def _clear_selection(self):
        if self._file_busy:
            return
        self.selected_ids.clear()
        self.multi_select_mode = False
        self._selection_anchor = None
        self._update_selection_ui()

    def _selection_escape(self, event):
        if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return
        if event.widget.winfo_toplevel() is self and self.multi_select_mode:
            self._clear_selection()
            return "break"

    def _delete_selected(self):
        if self.selected_ids:
            self._delete_photos(sorted(self.selected_ids))

    def _restore_selected(self):
        if self.selected_ids:
            self._restore_photos(sorted(self.selected_ids))

    def _render_empty(self, cols: int) -> None:
        kind = self.view["type"]
        no_roots = not self.db.list_roots()
        title, subtitle, button, command = "还没有照片", "重新扫描文件夹，更新你的本地照片库。", "重新扫描", self._start_scan
        if kind == "search":
            title, subtitle, button, command = "没有找到相关照片", "试试更短的关键词，或按文件名、文件夹名搜索。", "清除搜索", lambda: self.show_view({"type": "all"})
        elif kind in ('videos', 'gifs'):
            title = '还没有视频' if kind == 'videos' else '还没有 GIF 动图'
            subtitle = '扫描范围内发现的媒体会自动显示在这里。'
            button = None
        elif kind == "trash":
            title, subtitle, button = "回收站是空的", "移到回收站的照片会显示在这里，随时可以恢复。", None
        elif kind == "favorites":
            title, subtitle, button = "收藏喜欢的瞬间", "打开一张照片，点击收藏，就能在这里找到它。", None
        elif kind == "album":
            title, subtitle, button = "相册还是空的", "打开照片，点击「加入相册」收藏到这里。", None
        elif self.scanner.snapshot()["running"]:
            title, subtitle = "正在查找电脑中的照片", "照片会陆续显示，你可以继续浏览或暂停扫描。"
            button, command = "暂停扫描", self._pause_scan
        elif self.db.get_setting("scan_paused", "0") == "1" or self.scanner.snapshot()["phase"] == "paused":
            title, subtitle = "扫描已暂停", "已完成的索引会保留，继续扫描时跳过未变化的照片。"
            button, command = "继续扫描", self._start_scan
        elif no_roots:
            title, subtitle = "正在准备照片扫描", "首次打开会自动扫描本机磁盘，无需选择文件夹。"
            button, command = "立即扫描", self._scan_computer
        frame = tk.Frame(self.grid_frame, bg=CARD)
        frame.grid(row=0, column=0, columnspan=cols, sticky="ew", pady=(75, 60))
        art = tk.Canvas(frame, width=120, height=100, bg=CARD, highlightthickness=0)
        art.pack()
        rounded(art, (18, 12, 102, 88), ACCENT_SOFT, "#cbd9fc", radius=10)
        art.create_image(60, 50, image=icon(self, "folder" if no_roots else "image", ACCENT, 56))
        label(frame, title, 20, bold=True).pack(pady=(20, 10))
        label(frame, subtitle, 10, MUTED, wraplength=500).pack()
        if button:
            Button(frame, button, command, icon_name="plus" if no_roots else None,
                   variant="primary", height=42).pack(pady=(24, 14))
        if no_roots and kind not in ("trash", "search", "favorites"):
            label(frame, "文件保留在原位置，无需上传", 9, MUTED,
                  image=icon(self, "lock", size=16), compound="left", padx=6).pack(pady=4)

    def _make_tile(self, asset: dict, row: int, col: int) -> None:
        size = self._thumb_size
        tile = tk.Canvas(self.grid_frame, width=size + 4, height=size + 4,
                          bg=CARD, highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        tile.grid(row=row, column=col, padx=4, pady=4)
        image = self._load_thumb(asset)
        if image is not None:
            tile.create_image(2, 2, image=image, anchor="nw")
            tile.photo = image
        else:
            rounded(tile, (2, 2, size + 2, size + 2), SIDEBAR_BG, LINE)
            is_video = asset.get("media_type") == "video"
            tile.create_image(size / 2 + 2, size / 2 - 10, image=icon(self, "file" if is_video else "image", size=30))
            tile.create_text(size / 2 + 2, size / 2 + 24,
                             text="视频 · 点击打开" if is_video else "正在加载…", fill=MUTED, font=(FONT, 9))
            tile.addtag_all("placeholder")
        self._tiles[asset["id"]] = tile
        if asset.get("favorite"):
            rounded(tile, (size - 30, size - 30, size - 2, size - 2), "#202632", radius=9)
            tile.create_image(size - 16, size - 16, image=icon(self, "heart", ON_ACCENT, 18))
        if asset.get("is_screenshot"):
            rounded(tile, (9, 9, 49, 31), CARD, radius=5)
            tile.create_text(29, 20, text="截图", fill=ACCENT, font=(FONT, 8))

        def outline(color):
            tile.delete("hover")
            if color:
                border = rounded(tile, (1, 1, size + 3, size + 3), "", color, width=2)
                tile.addtag_withtag("hover", border)

        tile.bind("<Enter>", lambda e: outline(ACCENT))
        tile.bind("<Leave>", lambda e: outline(ACCENT if tile.focus_get() is tile else None))
        tile.bind("<FocusIn>", lambda e: outline(ACCENT))
        tile.bind("<FocusOut>", lambda e: outline(None))
        tile.bind("<Button-1>", lambda e: self._select_photo(e, asset["id"]))
        tile.bind("<Double-Button-1>", lambda e: self._open_from_tile(e, asset["id"]))
        tile.bind("<Return>", lambda e: self._open_lightbox(asset["id"]))
        tile.bind("<Button-3>", lambda e: self._photo_menu(e, asset["id"]))
        tile.bind("<Shift-F10>", lambda e: self._photo_menu(e, asset["id"]))
        tile.bind("<Delete>", lambda e: self._delete_selected())
        self._paint_selection(tile, asset["id"])

    def _load_thumb(self, asset: dict) -> ImageTk.PhotoImage | None:
        cached = self.photo_cache.get(asset["id"])
        if cached is not None:
            return cached
        path = thumbs.thumb_path(self.cache_dir, asset["id"])
        if not os.path.isfile(path):
            key = (asset["id"], asset.get("mtime"))
            if key not in self._thumb_pending:
                self._thumb_pending.add(key)
                def load():
                    result = thumbs.ensure(asset["path"], asset["id"], self.cache_dir, "thumb")
                    self._media_results.put(("thumb", key, result))
                self._media_pool.submit(load)
            return None
        try:
            with Image.open(path) as image:
                photo = ImageTk.PhotoImage(rounded_photo(image, self._thumb_size), master=self)
        except (OSError, ValueError):
            return None
        self.photo_cache[asset["id"]] = photo
        return photo

    def _poll_media(self):
        for _ in range(30):
            try:
                kind, key, result = self._media_results.get_nowait()
            except queue.Empty:
                break
            if kind == "file-progress":
                self.selection_text.set(f"正在处理 {key:,} / {result:,} 项…")
                continue
            if kind == "file-done":
                self._file_busy = False
                for album in self.db.albums():
                    self._write_album_json(album["id"])
                self._file_errors(result)
                self._refresh_after_file_action()
                continue
            if kind in ("preview", "gif"):
                if key == self._preview_serial and self.lb_window and self.lb_window.winfo_exists():
                    if kind != 'gif' or result[0] is not None:
                        self._lb_preview = result[0] if kind == 'gif' else result
                    elif kind == 'gif':
                        self._pause_gif()
                    self._draw_lightbox()
                    if kind == 'gif' and result[0] is not None and self._gif_playing and result[2] > 1:
                        self._gif_after = self.after(result[1], lambda serial=key: self._next_gif(serial))
                    if self.lb_asset.get('media_type') == 'video':
                        current = self.db.get_asset(self.lb_asset['id'])
                        if current and current.get('width'):
                            self.lb_details['dimensions'].configure(text=f"{current['width']} × {current['height']}")
                continue
            self._thumb_pending.discard(key)
            asset = next((a for a in self.items if a["id"] == key[0] and a.get("mtime") == key[1]), None)
            tile = self._tiles.get(key[0])
            if not asset or not tile or not tile.winfo_exists():
                continue
            photo = self._load_thumb(asset) if result else None
            if photo:
                tile.delete("placeholder")
                item = tile.create_image(2, 2, image=photo, anchor="nw")
                tile.tag_lower(item)
                tile.photo = photo
            elif not result:
                for item in tile.find_withtag("placeholder"):
                    if tile.type(item) == "text":
                        tile.itemconfigure(item, text="暂时无法预览")
        self.after(70, self._poll_media)

    def _load_viewer_image(self, asset, *, animate=True):
        self._preview_serial += 1
        serial = self._preview_serial
        self._pause_gif()
        gif = asset.get('ext', '').lower() == '.gif'
        self._gif_playing = gif and animate
        if hasattr(self, 'lb_animation_btn'):
            self.lb_animation_btn.configure(text='暂停动图' if self._gif_playing else '播放动图')
        def load():
            if serial != self._preview_serial:
                return
            if self._gif_decoder is not None:
                self._gif_decoder.close()
                self._gif_decoder = None
            if gif:
                try:
                    # Keep the decoder in memory so playing a GIF never locks its source file.
                    with open(asset['path'], 'rb') as source:
                        data = source.read(64 * 1024 * 1024+1)
                    if len(data) <= 64 * 1024 * 1024:
                        decoder = Image.open(io.BytesIO(data))
                        self._gif_decoder, self._gif_decoder_serial = decoder, serial
                        self._media_results.put(('gif', serial, (decoder.convert('RGB'),
                            max(30, decoder.info.get('duration', 100)), getattr(decoder, 'n_frames', 1))))
                        return
                except (OSError, ValueError):
                    pass
            image = None
            if asset.get('media_type') == 'video':
                from media import video_probe
                preview = thumbs.ensure(asset['path'], asset['id'], self.cache_dir, 'preview')
                info = video_probe(asset['path'])
                current = self.db.get_asset(asset['id'])
                if current and current['size'] == asset['size'] and current['mtime'] == asset['mtime'] and info.get('width'):
                    self.db.update_asset(asset['id'], **info)
                candidates = [preview] if preview else []
            else:
                candidates = [asset["path"], thumbs.preview_path(self.cache_dir, asset["id"]),
                          thumbs.thumb_path(self.cache_dir, asset["id"])]
            for path in candidates:
                try:
                    with Image.open(path) as original:
                        image = ImageOps.exif_transpose(original).convert("RGB")
                    break
                except Exception:
                    continue
            self._media_results.put(("preview", serial, image))
        self._preview_pool.submit(load)

    def _pause_gif(self, *, reset=False):
        self._gif_playing = False
        if self._gif_after:
            self.after_cancel(self._gif_after)
            self._gif_after = None
        if hasattr(self, 'lb_animation_btn'):
            self.lb_animation_btn.configure(text='播放动图')
        if reset and self.lb and self.lb_asset.get('ext', '').lower() == '.gif':
            self._load_viewer_image(self.lb_asset, animate=False)

    def _toggle_gif(self):
        if self._gif_playing:
            self._pause_gif()
        else:
            self._gif_playing = True
            self.lb_animation_btn.configure(text='暂停动图')
            self._next_gif(self._preview_serial)

    def _next_gif(self, serial):
        self._gif_after = None
        if not self._gif_playing or serial != self._preview_serial or not self.lb:
            return
        def frame():
            decoder = self._gif_decoder
            if decoder is None or self._gif_decoder_serial != serial or serial != self._preview_serial:
                return
            try:
                try:
                    decoder.seek(decoder.tell()+1)
                except EOFError:
                    decoder.seek(0)
                self._media_results.put(('gif', serial, (decoder.convert('RGB'),
                    max(30, decoder.info.get('duration', 100)), decoder.n_frames)))
            except (OSError, ValueError):
                self._media_results.put(('gif', serial, (None, 100, 0)))
        self._preview_pool.submit(frame)

    def _photo_menu(self, event, asset_id):
        self._close_context_menu()
        asset = self.db.get_asset(asset_id)
        if not asset:
            return "break"
        tile = self._tiles.get(asset_id)
        if tile and tile.winfo_exists():
            tile.focus_set()
        if self.multi_select_mode and asset_id not in self.selected_ids:
            self.selected_ids = {asset_id}
            self._selection_anchor = asset_id
            self._update_selection_ui()
        ids = sorted(self.selected_ids) if self.multi_select_mode else [asset_id]
        multiple = len(ids) > 1
        self._context_menu = menu = ui.ContextMenu(self)
        if multiple:
            menu.add_header(f"已选择 {len(ids):,} 项")
        menu.add_command(label="查看照片", icon_name="image", shortcut="Enter", command=lambda: self._open_lightbox(asset_id))
        if asset.get("deleted"):
            menu.add_command(label="恢复所选到原位置" if multiple else "恢复到原位置", icon_name="restore", command=lambda: self._restore_photos(ids))
            menu.add_separator()
            menu.add_command(label="永久删除所选" if multiple else "永久删除", icon_name="trash", danger=True, command=lambda: self._delete_photos(ids))
        else:
            menu.add_command(label="收藏所选" if multiple else ("取消收藏" if asset.get("favorite") else "收藏"),
                             icon_name="heart",
                             command=lambda: self._favorite_photos(ids, True if multiple else not asset.get("favorite")))
            menu.add_command(label="加入相册", icon_name="album", command=lambda: self._add_to_album(ids))
            if not multiple and asset.get("media_type") != "video":
                menu.add_command(label="提取文字", icon_name="file", command=lambda: self._open_ocr(asset_id))
            menu.add_separator()
            menu.add_command(label="在文件夹中显示", icon_name="folder", command=lambda: self._file_action(asset, "reveal"))
            menu.add_command(label="打开原图", icon_name="external", command=lambda: self._file_action(asset, "open"))
            if not multiple:
                menu.add_command(label="忽略所在或上层文件夹…", icon_name="folder", command=lambda: self._ignore_photo_folder(asset))
            menu.add_separator()
            menu.add_command(label="将所选移到系统回收站" if multiple else "移到系统回收站", icon_name="trash", danger=True, shortcut="Delete",
                             command=lambda: self._delete_photos(ids))
        menu.add_separator()
        if not self.multi_select_mode:
            menu.add_command(label="多选", command=lambda: self._enter_multi_select(asset_id), icon_name="check")
        menu.add_command(label="全选当前列表", command=self._select_all, icon_name="check", shortcut="Ctrl+A")
        if self.multi_select_mode:
            menu.add_command(label="结束多选", command=self._clear_selection, icon_name="close", shortcut="Esc")
        menu.popup(event.x_root, event.y_root)
        return "break"

    def _close_context_menu(self):
        if self._context_menu is not None and self._context_menu.winfo_exists():
            self._context_menu.destroy()
        self._context_menu = None

    def _favorite_photo(self, asset):
        self.db.set_favorite([asset["id"]], not asset.get("favorite"))
        self.show_view(self.view)

    def _favorite_photos(self, ids, value):
        self.db.set_favorite(ids, value)
        self.show_view(self.view)

    def _file_action(self, asset, action):
        try:
            (desktop.reveal if action == "reveal" else desktop.open_file)(asset["path"], self.winfo_id())
        except Exception as exc:
            if action == "open" and asset.get("media_type") != "video" and os.path.isfile(asset["path"]):
                # A broken default viewer must not prevent viewing the actual original.
                try:
                    with open(asset["path"], "rb") as source:
                        source.read(1)
                    if not self.lb or self.lb_asset["id"] != asset["id"]:
                        self._open_lightbox(asset["id"])
                    self._set_zoom(1.0)
                    self.lb_position.set("系统查看器无法启动，已在 PhotoNest 中打开原图 · 100%")
                    return
                except OSError:
                    pass
            messagebox.showerror(APP_NAME, f"无法{'定位' if action == 'reveal' else '打开'}文件。\n"
                f"请确认文件仍在原位置且有访问权限。\n\n{asset['path']}\n\n{exc}", parent=self.lb_window or self)

    # --------------------------------------------------------------- search
    def _search_focus_in(self, _event=None) -> None:
        if self.search_var.get() == self.search_placeholder:
            self.search_var.set("")
        self.search_entry.configure(fg=TEXT)
        self.search_wrap.configure(highlightbackground=ACCENT)

    def _search_focus_out(self, _event=None) -> None:
        if not self.search_var.get().strip():
            self.search_var.set(self.search_placeholder)
            self.search_entry.configure(fg=MUTED)
        self.search_wrap.configure(highlightbackground=LINE)

    def _on_search_key(self, event=None) -> None:
        if event and event.keysym in ("Return", "Escape", "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        if self._search_after:
            self.after_cancel(self._search_after)
        self._search_after = self.after(320, self._apply_search)

    def _apply_search(self) -> None:
        if self._search_after:
            self.after_cancel(self._search_after)
        self._search_after = None
        text = self.search_var.get().strip()
        if text == getattr(self, "search_placeholder", ""):
            text = ""
        if text:
            self.show_view({"type": "search", "q": text})
            self._search_request = self.ai.request_search(text, self.search_mode.get())
            self.count_var.set(self.count_var.get() + " · 正在匹配…")
        elif self.view["type"] == "search":
            self.show_view({"type": "all"})

    # ------------------------------------------------------------- lightbox
    def _open_lightbox(self, asset_id: int, ids: list[int] | None = None) -> None:
        if self.lb_window is not None and self.lb_window.winfo_exists():
            self._close_lightbox()
        ids = ids or [item["id"] for item in self.items]
        if not ids:
            return
        self.lb = {"ids": ids, "index": ids.index(asset_id) if asset_id in ids else 0, "dirty": False}
        self._lb_preview_key = None
        win = tk.Toplevel(self)
        self.lb_window = win
        win.title(APP_NAME)
        win.configure(bg=DARK)
        win.minsize(980, 680)
        center_window(win, self, min(1280, self.winfo_screenwidth() - 80),
                       min(820, self.winfo_screenheight() - 100))
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", self._close_lightbox)
        themes.title_bar(win, self.is_dark)
        header = tk.Frame(win, bg=DARK_CARD, padx=14, pady=10)
        header.pack(fill="x")
        Button(header, "返回照片库", self._close_lightbox, icon_name="left", variant="dark").pack(side="left")
        self.lb_heading = label(header, size=10, color=TEXT)
        self.lb_heading.pack(side="left", padx=20)
        self.lb_split = tk.PanedWindow(win, orient="horizontal", bg=LINE, bd=0, sashwidth=6,
                                      sashrelief="flat", opaqueresize=True)
        self.lb_split.pack(fill="both", expand=True)
        left = tk.Frame(self.lb_split, bg=DARK)
        self.lb_panel = panel = tk.Frame(self.lb_split, bg=DARK_CARD)
        self.lb_split.add(left, minsize=420, stretch="always")
        self.lb_split.add(panel, minsize=390, width=440, stretch="never")
        tabs = tk.Frame(panel, bg=DARK_CARD, padx=16, pady=10)
        tabs.pack(fill="x")
        self.lb_tabs = {}
        for name, text in (("ocr", "识别文字"), ("info", "照片信息")):
            button = Button(tabs, text, lambda name=name: self._show_viewer_tab(name), variant="dark")
            button.pack(side="left", padx=(0, 8))
            self.lb_tabs[name] = button
        self.lb_info_frame = tk.Frame(panel, bg=DARK_CARD)
        self.lb_info_frame.pack(fill="both", expand=True)
        self.lb_ocr_frame = tk.Frame(panel, bg=DARK_CARD, padx=20, pady=14)
        self._build_ocr_panel()
        self._viewer_tab = "info"
        panel_footer = tk.Frame(self.lb_info_frame, bg=DARK_CARD, padx=22, pady=16)
        panel_footer.pack(side="bottom", fill="x")
        self.lb_info_canvas = tk.Canvas(self.lb_info_frame, bg=DARK_CARD, highlightthickness=0,
                                        yscrollincrement=25)
        self.lb_info_canvas.pack(fill="both", expand=True)
        self.lb_info_frame._wheel_target = self.lb_info_canvas
        right = tk.Frame(self.lb_info_canvas, bg=DARK_CARD, padx=22, pady=22)
        inner = self.lb_info_canvas.create_window(0, 0, window=right, anchor="nw")
        right.bind("<Configure>", lambda e: self.lb_info_canvas.configure(
            scrollregion=self.lb_info_canvas.bbox("all")))
        self.lb_info_canvas.bind("<Configure>", lambda e: self.lb_info_canvas.itemconfigure(
            inner, width=e.width))
        toolbar = tk.Frame(left, bg=DARK, padx=18, pady=10)
        toolbar.pack(fill="x")
        Button(toolbar, "适应窗口", self._fit_lightbox, variant="dark", icon_name="image").pack(side="left")
        Button(toolbar, "−", lambda: self._zoom_photo(.8), variant="dark", width=38).pack(side="left", padx=(8, 2))
        self.lb_zoom_label = Button(toolbar, "100%", lambda: self._set_zoom(1.0), variant="dark", width=90)
        self.lb_zoom_label.pack(side="left")
        Button(toolbar, "+", lambda: self._zoom_photo(1.25), variant="dark", width=38).pack(side="left", padx=2)
        self.lb_crop_btn = Button(toolbar, "框选文字", self._begin_ocr_crop, icon_name="image", variant="dark", height=38)
        self.lb_crop_btn.pack(side="left", padx=10)
        self.lb_animation_btn = Button(toolbar, '播放动图', self._toggle_gif, variant='dark', height=38)
        label(toolbar, "Esc 关闭", 9, DARK_MUTED).pack(side="right")
        bottom = tk.Frame(left, bg=DARK, height=48)
        bottom.pack(side="bottom", fill="x")
        self.lb_position = tk.StringVar()
        label(bottom, color=DARK_MUTED, size=10, textvariable=self.lb_position).pack(pady=14)
        self.lb_canvas = tk.Canvas(left, bg=DARK, bd=0, highlightthickness=0)
        self.lb_canvas.pack(fill="both", expand=True)
        self.lb_canvas._photo_zoom = self._viewer_wheel
        self.lb_previous = Button(self.lb_canvas, "", lambda: self._lb_nav(-1),
                                   icon_name="left", variant="dark", width=38, height=42)
        self.lb_next = Button(self.lb_canvas, "", lambda: self._lb_nav(1),
                               icon_name="right", variant="dark", width=38, height=42)
        self.lb_canvas.bind("<Configure>", self._schedule_viewer_fit)
        self.lb_canvas.bind("<Button-1>", self._ocr_drag_start)
        self.lb_canvas.bind("<B1-Motion>", self._ocr_drag_move)
        self.lb_canvas.bind("<ButtonRelease-1>", self._ocr_drag_end)
        label(right, "照片信息", 10, DARK_MUTED).pack(anchor="w")
        self.lb_title = label(right, size=16, color=TEXT, bold=True,
                              wraplength=340, justify="left", anchor="w")
        self.lb_title.pack(fill="x", pady=(14, 3))
        self.lb_type = label(right, size=9, color=DARK_MUTED)
        self.lb_type.pack(anchor="w", pady=(0, 18))
        self.lb_details = {}
        for key, name, symbol in (("taken", "拍摄时间", "calendar"),
                                   ("dimensions", "尺寸", "image"), ("size", "文件大小", "file")):
            row = tk.Frame(right, bg=DARK_CARD)
            row.pack(fill="x", pady=6)
            label(row, name, 9, DARK_MUTED, image=icon(self, symbol, DARK_MUTED, 17),
                  compound="left", padx=7).pack(side="left")
            value = label(row, size=9, color=TEXT)
            value.pack(side="right")
            self.lb_details[key] = value
        divider(right, DARK_LINE)
        label(right, "所在文件夹", 9, DARK_MUTED, image=icon(self, "folder", DARK_MUTED, 18),
              compound="left", padx=7).pack(anchor="w")
        self.lb_folder = label(right, size=9, color=DARK_MUTED, wraplength=280,
                               justify="left", anchor="w")
        self.lb_folder.pack(fill="x", pady=(8, 16))
        self.lb_tags = label(right, size=9, color=DARK_MUTED, wraplength=280, justify="left", anchor="w")
        self.lb_tags.pack(fill="x", pady=(0, 6))
        Button(right, "编辑标签", self._edit_tags, icon_name="image", variant="dark", height=34).pack(fill="x", pady=(0, 10))
        self.lb_ocr_btn = Button(right, "提取文字", lambda: self._open_ocr(), icon_name="file", variant="dark", height=38)
        self.lb_ocr_btn.pack(fill="x", pady=(0, 7))
        self.lb_fav_btn = Button(right, "收藏", self._lb_toggle_favorite,
                                  icon_name="heart", variant="primary", height=40)
        self.lb_fav_btn.pack(fill="x", pady=(0, 7))
        Button(right, "加入相册", self._lb_add_album, icon_name="album",
               variant="dark", height=40).pack(fill="x")
        divider(right, DARK_LINE)
        Button(right, "在文件夹中显示", self._lb_reveal, icon_name="folder",
               variant="dark", height=38).pack(fill="x", pady=(0, 7))
        self.lb_open_btn = Button(right, "打开原图", self._lb_open, icon_name="external",
                                    variant="dark", height=38)
        self.lb_open_btn.pack(fill="x")
        nav = tk.Frame(panel_footer, bg=DARK_CARD)
        nav.pack(fill="x")
        nav.grid_columnconfigure((0, 1), weight=1)
        Button(nav, "上一张", lambda: self._lb_nav(-1), icon_name="left",
               variant="dark").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        Button(nav, "下一张", lambda: self._lb_nav(1), icon_name="right",
               variant="dark").grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.lb_delete_btn = Button(right, "移到回收站", self._lb_delete,
                                     icon_name="trash", variant="dark-danger", height=38)
        self.lb_delete_btn.pack(fill="x", pady=(18, 0))
        self.lb_restore_btn = Button(right, "恢复照片", self._lb_restore,
                                      icon_name="restore", variant="dark", height=38)
        win.bind("<Escape>", lambda e: self._close_lightbox())
        win.bind("<Left>", lambda e: self._viewer_key_nav(e, -1))
        win.bind("<Right>", lambda e: self._viewer_key_nav(e, 1))
        self._show_viewer_tab("info")
        self._render_lightbox()
        win.focus_set()

    def _render_lightbox(self) -> None:
        if not self.lb:
            return
        asset = self.db.get_asset(self.lb["ids"][self.lb["index"]])
        if not asset:
            self._close_lightbox()
            return
        self.lb_asset = asset
        self.lb_heading.configure(text=asset["name"])
        self._ocr_select = False
        self.lb_canvas.configure(cursor="")
        self.lb_tags.configure(text="标签：" + (" · ".join(self.db.tags_for(asset["id"])) or "尚未分类"))
        self.lb_ocr_btn.configure(state="disabled" if asset.get("media_type") == "video" else "normal")
        self.lb_crop_btn.configure(state="disabled" if asset.get("media_type") == "video" else "normal")
        self.lb_title.configure(text=asset["name"])
        media_type = "视频文件" if asset.get("media_type") == "video" else "图像"
        self.lb_type.configure(text=f"{asset.get('ext', '').lstrip('.').upper()} {media_type}")
        self.lb_open_btn.configure(text="打开视频" if asset.get("media_type") == "video" else "打开原图")
        self.lb_details["taken"].configure(text=(asset.get("taken_at") or "")[:16].replace("T", "  ") or "未知")
        self.lb_details["dimensions"].configure(text=f"{asset['width']} × {asset['height']}" if asset.get("width") else "未知")
        self.lb_details["size"].configure(text=self._fmt_size(asset.get("size") or 0))
        self.lb_folder.configure(text=asset.get("dir") or "")
        self.lb_position.set(f"←      {self.lb['index'] + 1} / {len(self.lb['ids'])}      →")
        self.lb_fav_btn.configure(text="取消收藏" if asset.get("favorite") else "收藏")
        deleted = bool(asset.get("deleted"))
        self.lb_delete_btn.configure(text="永久删除" if deleted else "移到回收站")
        if deleted:
            self.lb_restore_btn.pack(fill="x", pady=(7, 0))
        else:
            self.lb_restore_btn.pack_forget()
        if self._lb_preview_key != asset["id"]:
            self._pause_gif()
            self._lb_preview = None
            self._lb_preview_key = asset["id"]
            self._load_viewer_image(asset, animate=self._viewer_tab != 'ocr')
            self._zoom_scale = None
            self._pan = (0, 0)
        if asset.get('ext', '').lower() == '.gif':
            self.lb_animation_btn.pack(side='left', padx=6)
        else:
            self.lb_animation_btn.pack_forget()
        self.lb_window.title(f"{asset['name']} · PhotoNest")
        self._sync_viewer_ocr(asset)
        self.lb_window.update_idletasks()
        self._draw_lightbox()

    def _lb_nav(self, direction: int) -> None:
        if not self.lb:
            return
        self.lb["index"] = (self.lb["index"] + direction) % len(self.lb["ids"])
        self._render_lightbox()

    def _viewer_key_nav(self, event, direction):
        if not isinstance(event.widget, (tk.Text, tk.Entry, ttk.Entry, ttk.Combobox)):
            self._lb_nav(direction)

    def _lb_toggle_favorite(self) -> None:
        asset = self.lb_asset
        self.db.set_favorite([asset["id"]], not asset.get("favorite"))
        self.lb["dirty"] = True
        self._render_lightbox()
        self._refresh_sidebar()

    def _lb_add_album(self) -> None:
        self._add_to_album([self.lb_asset["id"]])

    def _lb_reveal(self) -> None:
        self._file_action(self.lb_asset, "reveal")

    def _lb_open(self) -> None:
        self._file_action(self.lb_asset, "open")

    def _lb_delete(self) -> None:
        self._delete_photos([self.lb_asset["id"]])

    def _lb_restore(self) -> None:
        self._restore_photos([self.lb_asset["id"]])

    def _close_lightbox(self) -> None:
        self._pause_gif()
        dirty = self.lb.get("dirty") if self.lb else False
        self._stash_viewer_ocr()
        for request in list(self._ocr_windows):
            if self._ocr_windows[request]["window"] is self.lb_window:
                self._ocr_windows.pop(request)
        if self._viewer_after:
            self.after_cancel(self._viewer_after)
            self._viewer_after = None
        if self.lb_window is not None and self.lb_window.winfo_exists():
            self.lb_window.destroy()
        self.lb_window = None
        self._preview_serial += 1
        self.lb = None
        self._lb_preview = None
        self._ocr_asset_id = None
        if dirty:
            self.show_view(self.view)

    # --------------------------------------------------------------- albums
    def _new_album(self) -> None:
        name = ask_album_name(self, "新建相册")
        if not name:
            return
        album = self.db.create_album(name)
        self._write_album_json(album["id"])
        self._refresh_sidebar()

    def _add_to_album(self, ids: list[int]) -> None:
        albums = self.db.albums()
        parent = self.lb_window if self.lb_window and self.lb_window.winfo_exists() else self
        dialog = tk.Toplevel(parent)
        dialog.title("加入相册")
        dialog.configure(bg=CARD)
        dialog.resizable(False, False)
        dialog.transient(parent)
        center_window(dialog, parent, 460, 510)
        body = tk.Frame(dialog, bg=CARD, padx=26, pady=24)
        body.pack(fill="both", expand=True)
        label(body, "加入相册", 17, bold=True).pack(anchor="w")
        label(body, f"为这 {len(ids)} 张照片选择相册。", 10, MUTED).pack(anchor="w", pady=(5, 18))
        footer = tk.Frame(body, bg=CARD)
        footer.pack(side="bottom", fill="x", pady=(18, 0))
        selected = tk.StringVar(value=albums[0]["id"] if albums else "")

        def join():
            if not selected.get():
                return
            self.db.add_to_album(selected.get(), ids)
            self._write_album_json(selected.get())
            dialog.destroy()
            self._refresh_sidebar()
            if self.lb:
                self.lb["dirty"] = True
            elif self.view["type"] == "album":
                self.show_view(self.view)

        def create_and_join():
            name = ask_album_name(dialog)
            if name:
                album = self.db.create_album(name)
                selected.set(album["id"])
                join()

        Button(footer, "加入相册", join, variant="primary").pack(side="right", padx=(8, 0))
        Button(footer, "取消", dialog.destroy).pack(side="right")
        Button(body, "新建并加入", create_and_join, icon_name="plus", variant="quiet",
               align="left").pack(side="bottom", fill="x", pady=(12, 0))
        canvas = tk.Canvas(body, bg=CARD, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        choices = tk.Frame(canvas, bg=CARD)
        inner = canvas.create_window(0, 0, window=choices, anchor="nw")
        choices.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner, width=e.width))
        canvas._wheel_target = canvas
        rows = []

        def repaint(*args):
            for album_id, row, widgets in rows:
                active = selected.get() == album_id
                color = ACCENT_SOFT if active else CARD
                row.configure(bg=color, highlightbackground=ACCENT if active else LINE)
                for widget in widgets:
                    widget.configure(bg=color)

        for album in albums:
            row = tk.Frame(choices, bg=CARD, padx=10, pady=10, highlightthickness=1,
                            highlightbackground=LINE, cursor="hand2")
            row.pack(fill="x", pady=(0, 7))
            cover = None
            if album.get("cover"):
                asset = self.db.get_asset(album["cover"])
                cover_path = thumbs.ensure(asset["path"], asset["id"], self.cache_dir, "thumb") if asset else None
                if cover_path:
                    try:
                        with Image.open(cover_path) as image:
                            cover = ImageTk.PhotoImage(rounded_photo(image, 42), master=dialog)
                    except (OSError, ValueError):
                        pass
            cover_label = tk.Label(row, image=cover or icon(self, "album", ACCENT, 34), bg=CARD)
            cover_label.photo = cover
            cover_label.pack(side="left", padx=(0, 12))
            radio = tk.Radiobutton(row, text=f"{album['name']}  ·  {album['count']} 张",
                                   variable=selected, value=album["id"], bg=CARD, fg=TEXT,
                                   activebackground=ACCENT_SOFT, selectcolor=CARD,
                                   anchor="w", wraplength=260, font=(FONT, 10),
                                   highlightthickness=0, bd=0)
            radio.pack(side="left", fill="x", expand=True)
            for widget in (row, cover_label):
                widget.bind("<Button-1>", lambda e, album_id=album["id"]: selected.set(album_id))
            rows.append((album["id"], row, (cover_label, radio)))
        if not albums:
            label(choices, "还没有相册\n点击「新建并加入」创建第一个相册。", 10, MUTED,
                  justify="center").pack(pady=55)
        selected.trace_add("write", repaint)
        repaint()
        dialog.bind("<Return>", lambda e: join())
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.grab_set()
        dialog.wait_window()

    # ----------------------------------------------------------- duplicates
    def _open_duplicates(self) -> None:
        self.show_view({"type": "duplicates"})

    # --------------------------------------------------------------- trash
    def _write_album_json(self, album_id: str) -> None:
        album = self.db.get_album(album_id)
        if not album:
            return
        rows = self.db.q(
            """SELECT a.path FROM asset a JOIN album_asset x ON x.asset_id = a.id
               WHERE x.album_id = ? AND a.deleted = 0 ORDER BY x.added_at ASC""",
            (album_id,),
        )
        folder = os.path.join(self.data_dir, "albums")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, f"{album_id}.json")
        temporary = target + ".tmp"
        payload = {
            "id": album_id,
            "name": album["name"],
            "createdAt": album["created_at"],
            "updatedAt": album["updated_at"],
            "photos": [row["path"] for row in rows],
        }
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(temporary, target)

    def _purge_assets(self, ids: list[int]) -> None:
        self._run_photo_files(ids, "purge")

    def _file_errors(self, errors):
        if errors:
            messagebox.showerror(f"{APP_NAME} {APP_VERSION}", f"{len(errors)} 项未完成，照片记录已保留：\n\n" + "\n".join(errors[:8]),
                                 parent=self.lb_window or self)

    def _refresh_after_file_action(self):
        if self.lb:
            self.lb["dirty"] = True
            self._close_lightbox()
        else:
            self.show_view(self.view)

    def _delete_photos(self, ids, *, confirm=True):
        if self._file_busy:
            return
        rows = [r for i in ids if (r := self.db.get_asset(i))]
        if not rows:
            return
        permanent = all(r.get("deleted") for r in rows)
        prompt = (f"永久删除这 {len(rows)} 张照片？\n将清除系统回收站中对应的照片，无法恢复。" if permanent else
                  f"将这 {len(rows)} 张照片移到 Windows 回收站？\n可从 PhotoNest 或系统回收站恢复。")
        if confirm and not messagebox.askyesno(APP_NAME, prompt, parent=self.lb_window or self):
            return
        if permanent:
            self._purge_assets(ids)
        else:
            self._run_photo_files([row["id"] for row in rows], "recycle")

    def _restore_photos(self, ids):
        self._run_photo_files(ids, "restore")

    def _run_photo_files(self, ids, mode):
        if self._file_busy:
            return
        self._file_busy = True
        self.selection_text.set(f"正在处理 0 / {len(ids):,} 项…")
        self._update_selection_ui()
        def work():
            errors = []
            for index, asset_id in enumerate(ids, 1):
                if self._closing:
                    break
                row = self.db.get_asset(asset_id)
                if not row:
                    continue
                try:
                    self._change_photo_file(row, mode)
                except Exception as exc:
                    errors.append(f"{row['name']}：{exc}")
                if index % 10 == 0 or index == len(ids):
                    self._media_results.put(("file-progress", index, len(ids)))
            self._media_results.put(("file-done", mode, errors))
        self._file_pool.submit(work)

    def _change_photo_file(self, row, mode):
        asset_id = row["id"]
        if mode == "recycle" and not row.get("deleted"):
            if row.get("media_type") != "video":
                thumbs.ensure(row["path"], asset_id, self.cache_dir, "preview")
            token = desktop.recycle(row["path"], recovery_dir=os.path.join(self.data_dir, "recovery"))
            from db import now_iso
            self.db.update_asset(asset_id, deleted=1, deleted_at=now_iso(), recycle_token=token)
        elif mode == "restore" and row.get("deleted"):
            if row.get("recycle_token"):
                desktop.restore(row["recycle_token"], row["path"])
            elif not os.path.isfile(row["path"]):
                raise FileNotFoundError("原文件已不存在，无法恢复")
            self.db.restore([asset_id])
        elif mode == "purge" and row.get("deleted"):
            token = row.get("recycle_token")
            if not token and os.path.exists(row["path"]):
                token = desktop.recycle(row["path"], recovery_dir=os.path.join(self.data_dir, "recovery"))
                self.db.update_asset(asset_id, recycle_token=token)
            if token:
                desktop.purge(token)
            self.db.purge_assets([asset_id])
            thumbs.remove(self.cache_dir, asset_id)

    def _empty_trash(self) -> None:
        if self._file_busy:
            return
        if not messagebox.askyesno(APP_NAME, "永久清除 PhotoNest 回收站中的照片？\n只清除这些照片在 Windows 回收站中的对应项目，无法恢复。"):
            return
        rows = self.db.q("SELECT id FROM asset WHERE deleted = 1")
        ids = [row["id"] for row in rows]
        self._purge_assets(ids)
        self.show_view({"type": "trash"})
        self._refresh_sidebar()

    # ------------------------------------------------------------ settings
    def _welcome(self) -> None:
        messagebox.showinfo(APP_NAME, "欢迎使用 PhotoNest。\n\n先添加一个放照片的文件夹，程序会在本地建立索引。")
        self._open_settings()

    def _add_root(self) -> None:
        path = filedialog.askdirectory(title="选择照片文件夹", parent=self)
        if not path:
            return
        self.db.add_root(os.path.abspath(path))
        self._refresh_sidebar()
        self._start_scan()

    def _startup_scan(self):
        if self.sync:
            self.sync.start()
            return
        complete = self.db.get_setting("initial_scan_complete")
        if complete is None:
            self.db.set_setting("initial_scan_complete", "0")
        if self.db.get_setting("initial_scan_complete") != "1" and self.db.get_setting("scan_paused", "0") != "1":
            self._start_scan()

    def _scan_computer(self):
        self._start_scan(discover=True)

    def _pause_scan(self):
        self.ai.pause()
        if self.sync:
            self.sync.set_paused(True)
            if self._tray:
                self._tray.set_paused(True)
        self.db.set_setting("scan_paused", "1")
        self.scanner.stop()
        self.status_var.set("正在暂停扫描与识别…")

    def _sync_ai_pipeline(self):
        if (not self._automatic_processing or self._closing or not self.ai.ready
                or self.db.get_setting('scan_paused', '0') == '1'):
            return
        revision = getattr(self.scanner, 'revision', 0)
        if self._ai_sync_revision != revision:
            self._ai_sync_revision = revision
            self.ai.start()

    def _start_scan(self, *, discover=False) -> None:
        self._automatic_processing = True
        self.db.set_setting('scan_paused', '0')
        self.db.set_setting('ai_paused', '0')
        self.db.set_setting('ai_enabled', '1')
        self.ai.start()
        if self.sync:
            self.sync.set_paused(False)
            if self._tray:
                self._tray.set_paused(False)
        discover = (discover or not self.db.list_roots()
            or (self.db.get_setting("initial_scan_complete") == "0"
                and self.db.get_setting("drive_discovery_complete", "0") != "1"))
        if self.scanner.start(discover=discover,
                initial=self.db.get_setting("initial_scan_complete", "0") != "1"):
            self.db.set_setting("scan_paused", "0")
            self._scan_was_running = True
            self.status_var.set("正在扫描…")
            if not self.items and self.view["type"] in ("all", "recent"):
                self._render_items()
        else:
            self.status_var.set("扫描已在进行中")

    def _poll_scan(self) -> None:
        try:
            status = self.scanner.snapshot()
            self._sync_ai_pipeline()
            running = status["running"]
            errors = status.get("errors", 0)
            warning = errors or status.get("phase") == "error"
            self.status_dot.itemconfigure(self.status_dot_id,
                                          fill="#d69724" if running or warning else "#22a06b")
            self.status_label.configure(fg=WARNING if warning else SUCCESS)
            self.scan_btn.configure(state="disabled" if running else "normal")
            self.computer_scan_btn.configure(state="disabled" if running else "normal")
            counts = self.db.counts()
            if running:
                total = status["total"] or 0
                self.status_var.set(f"照片已就绪 · 正在校验重复文件" if status["phase"] == "hashing" else
                                    (f"扫描中 · 已发现 {total:,} 张" if total else "正在查找照片…"))
                if not self.scan_pause_btn.winfo_manager():
                    self.scan_pause_btn.pack(side="left", padx=(10, 0))
                if not self.progress.winfo_manager():
                    self.progress.configure(mode="indeterminate")
                    self.progress.pack(fill="x", before=self.body)
                    self.progress.start(20)
                if (not self.lb and not self.multi_select_mode and not self._file_busy
                        and self.view["type"] in ("all", "recent") and len(self.items) < self.limit
                        and counts["active"] > len(self.items) and self.canvas.yview()[0] < .01
                        and time.monotonic() - self._last_scan_refresh > 2):
                    self._last_scan_refresh = time.monotonic()
                    self.show_view(self.view)
                self.library_count.set(f"{counts['active']:,} 张照片和视频")
                self.library_size.set(self._fmt_size(counts["bytes"]))
            else:
                ai_running = self.ai.snapshot()['running']
                if ai_running:
                    if not self.scan_pause_btn.winfo_manager():
                        self.scan_pause_btn.pack(side='left', padx=(10, 0))
                else:
                    self.scan_pause_btn.pack_forget()
                self.progress.stop()
                self.progress.pack_forget()
                paused = status["phase"] == "paused" or (status["phase"] == "idle"
                    and self.db.get_setting("scan_paused", "0") == "1")
                self.scan_btn.configure(text="继续处理" if paused else "扫描并识别")
                if self._scan_was_running:
                    if not self._hidden_to_tray and not self.lb and not self.multi_select_mode and not self._file_busy:
                        self._refresh_synced_view()
                if status.get("phase") == "error":
                    self.status_var.set("扫描失败，请重新扫描")
                elif paused:
                    self.status_var.set(f"扫描已暂停 · 已索引 {counts['active']:,} 张")
                elif errors:
                    self.status_var.set(f"已索引 {counts['active']:,} 张 · {errors} 个失败")
                elif ai_running:
                    ai_state = self.ai.snapshot()
                    self.status_var.set(f"已索引 {counts['active']:,} 张 · 正在识别 {ai_state['done']:,}/{ai_state['total']:,}")
                else:
                    self.status_var.set(f"已索引 {counts['active']:,} 张")
            self._scan_was_running = running
        except (tk.TclError, RuntimeError):
            return
        self.after(2000 if self._hidden_to_tray else 700, self._poll_scan)

    def _open_settings(self) -> None:
        self.show_view({"type": "settings"})

    def _ignore_photo_folder(self, asset):
        folder = self._choose_ignore_folder(os.path.dirname(os.path.abspath(asset['path'])))
        if folder is None:
            return
        paths = self.db.scan_exclude_paths()
        paths.append(folder)
        self._set_scan_exclude_paths(paths)
        if self.lb:
            self._close_lightbox()
        self.show_view(self.view)

    def _choose_ignore_folder(self, folder):
        paths = folder_ancestors(folder)
        win = tk.Toplevel(self)
        win.title('选择忽略范围')
        win.configure(bg=CARD)
        win.resizable(False, False)
        body = tk.Frame(win, bg=CARD, padx=24, pady=22)
        body.pack(fill='both', expand=True)
        label(body, '选择要忽略的文件夹', 15, bold=True).pack(anchor='w')
        label(body, '可选择当前文件夹或任意上层目录，无需逐级操作。', 10, MUTED).pack(anchor='w', pady=(8, 16))
        selected = tk.StringVar(value=paths[0])
        chooser = ttk.Combobox(body, textvariable=selected, values=paths, state='readonly', width=68)
        chooser.pack(fill='x', ipady=6)
        label(body, textvariable=selected, size=10, wraplength=590, justify='left').pack(anchor='w', pady=(12, 12))
        notice = tk.StringVar()
        def update_notice(event=None):
            text = '所选文件夹及其全部子文件夹都会被忽略。\n照片不会被删除，可在设置中恢复。'
            if selected.get() == paths[-1]:
                text = '已选择磁盘根目录，此磁盘内已入库的照片都会隐藏。\n照片不会被删除，可在设置中恢复。'
            notice.set(text)
        update_notice()
        chooser.bind('<<ComboboxSelected>>', update_notice)
        label(body, textvariable=notice, size=10, color=MUTED, wraplength=590, justify='left').pack(anchor='w')
        actions = tk.Frame(body, bg=CARD)
        actions.pack(fill='x', pady=(22, 0))
        result = [None]
        def confirm():
            if selected.get() in paths:
                result[0] = selected.get()
                win.destroy()
        Button(actions, '忽略所选范围', confirm, icon_name='folder', variant='primary').pack(side='right')
        Button(actions, '取消', win.destroy).pack(side='right', padx=(0, 10))
        win.bind('<Escape>', lambda event: win.destroy())
        win.bind('<Return>', lambda event: confirm())
        win.transient(self)
        win.grab_set()
        win.update_idletasks()
        center_window(win, self, max(660, win.winfo_reqwidth()), max(320, win.winfo_reqheight()))
        chooser.focus_set()
        self.wait_window(win)
        return result[0]

    def _set_scan_exclude_paths(self, paths):
        with self.db.lock:
            self.db.set_setting('scan_exclude_paths', json.dumps(paths, ensure_ascii=False))
            self.scanner.refresh_exclusions()
            self.db.apply_scan_exclusions()

    def _rescan_included_paths(self, paths):
        self.scanner.refresh_exclusions()
        self._ai_sync_revision = -1
        scopes = self.db.root_paths()
        targets = {}
        for path in paths:
            path = os.path.abspath(path)
            for scope in scopes:
                scope = os.path.abspath(scope)
                try:
                    common = os.path.commonpath((os.path.normcase(path), os.path.normcase(scope)))
                except ValueError:
                    continue
                if common == os.path.normcase(scope):
                    target = path
                elif common == os.path.normcase(path):
                    target = scope
                else:
                    continue
                targets[os.path.normcase(target)] = target
        paths = list(targets.values())
        if not paths:
            return
        if self.sync:
            for path in paths:
                self.sync.notify(dict(path=path, rescan=True, action='included'))
        elif self.db.get_setting('scan_paused', '0') != '1':
            self.scanner.start(paths=paths)

    def _render_scan_exclusions(self, body):
        label(body, '过滤与忽略', 12, bold=True).pack(anchor='w', pady=(8, 8))
        enabled = tk.BooleanVar(value=self.db.get_setting('scan_filter_resources', '1') == '1')
        def toggle():
            self.db.set_setting('scan_filter_resources', '1' if enabled.get() else '0')
            self.scanner.refresh_exclusions()
            self.db.apply_scan_exclusions()
            self._rescan_included_paths(self.db.root_paths() if not enabled.get() else [])
            self.show_view(dict(type='settings'))
        tk.Checkbutton(body, text='过滤系统壁纸、软件图标和已知缓存目录', variable=enabled,
            command=toggle, bg=CARD, fg=TEXT, activebackground=CARD, selectcolor=CARD,
            font=(FONT, 10)).pack(anchor='w')
        label(body, '按所在目录判断，保留普通下载文件夹中的个人图片；忽略只影响照片库，不删除文件。',
              9, MUTED, wraplength=660, justify='left').pack(anchor='w', pady=(4, 10))
        paths = self.db.scan_exclude_paths()
        table = ttk.Treeview(body, columns=('path',), show='headings',
            height=max(2, min(5, len(paths))), style='Folders.Treeview', selectmode='browse')
        table.heading('path', text='忽略的文件夹', anchor='w')
        table.column('path', width=600, anchor='w')
        for index, path in enumerate(paths):
            table.insert('', 'end', iid=str(index), values=(path,))
        table.pack(fill='x')
        actions = tk.Frame(body, bg=CARD)
        actions.pack(fill='x', pady=(10, 20))
        def add():
            path = filedialog.askdirectory(title='选择要忽略的文件夹', parent=self)
            if path:
                self._set_scan_exclude_paths(paths+[os.path.abspath(path)])
                self.show_view(dict(type='settings'))
        def restore():
            selection = table.selection()
            if selection:
                restored = paths[int(selection[0])]
                self._set_scan_exclude_paths([p for p in paths if p != restored])
                self._rescan_included_paths([restored])
                self.show_view(dict(type='settings'))
        Button(actions, '忽略文件夹', add, icon_name='plus').pack(side='left')
        restore_button = Button(actions, '恢复扫描所选', restore, icon_name='restore')
        restore_button.pack(side='left', padx=10)
        restore_button.configure(state='disabled')
        table.bind('<<TreeviewSelect>>', lambda event: restore_button.configure(
            state='normal' if table.selection() else 'disabled'))


    def _on_sidebar_wheel(self, event):
        if self.nav_canvas.bbox("all") and self.nav_canvas.bbox("all")[3] > self.nav_canvas.winfo_height():
            self.nav_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _route_mousewheel(self, event):
        # Windows can send MouseWheel to the focused entry; coordinates identify the actual target.
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            # Native ttk popdown controls may not have a Python widget wrapper.
            return
        if widget is None:
            return
        if isinstance(widget, ttk.Combobox):
            return
        step = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        if not event.delta:
            return
        while widget is not None:
            zoom = getattr(widget, "_photo_zoom", None)
            if zoom:
                return zoom(event)
            if isinstance(widget, (tk.Text, tk.Listbox)):
                widget.yview_scroll(step, "units")
                return "break"
            target = getattr(widget, "_wheel_target", None)
            if target is not None:
                target.yview_scroll(step, "units")
                return "break"
            widget = widget.master

    def _schedule_viewer_fit(self, event=None):
        if self._viewer_after:
            self.after_cancel(self._viewer_after)
        self._viewer_after = self.after(100, self._draw_lightbox)

    def _fit_lightbox(self):
        self._zoom_scale = None
        self._pan = (0, 0)
        self._draw_lightbox()

    def _image_scale(self):
        if getattr(self, "_zoom_scale", None) is not None:
            return self._zoom_scale
        if self._lb_preview is None:
            return 1.0
        return min(1.0, max(1, self.lb_canvas.winfo_width()-56)/self._lb_preview.width,
                   max(1, self.lb_canvas.winfo_height()-30)/self._lb_preview.height)

    def _set_zoom(self, scale, anchor=None):
        previous = self._image_scale()
        self._zoom_scale = max(.02, min(16.0, scale))
        width, height = self.lb_canvas.winfo_width(), self.lb_canvas.winfo_height()
        x, y = anchor or (width/2, height/2)
        px, py = getattr(self, "_pan", (0, 0))
        ratio = self._zoom_scale / previous
        self._pan = (x-width/2-(x-width/2-px)*ratio, y-height/2-(y-height/2-py)*ratio)
        self._draw_lightbox()

    def _zoom_photo(self, factor, anchor=None):
        if not self._ocr_select:
            self._set_zoom(self._image_scale()*factor, anchor)

    def _viewer_wheel(self, event):
        self._zoom_photo(1.2 if event.delta > 0 else 1/1.2,
            (event.x_root-self.lb_canvas.winfo_rootx(), event.y_root-self.lb_canvas.winfo_rooty()))
        return "break"

    def _draw_lightbox(self):
        self._viewer_after = None
        if not self.lb_window or not self.lb_window.winfo_exists():
            return
        width, height = self.lb_canvas.winfo_width(), self.lb_canvas.winfo_height()
        self.lb_canvas.delete("all")
        if self._lb_preview is not None:
            scale = self._image_scale()
            iw, ih = self._lb_preview.size
            dw, dh = iw*scale, ih*scale
            px, py = getattr(self, "_pan", (0, 0))
            bound_x, bound_y = max(0, (dw-width)/2), max(0, (dh-height)/2)
            px, py = max(-bound_x, min(bound_x, px)), max(-bound_y, min(bound_y, py))
            self._pan = (px, py)
            left, top = (width-dw)/2+px, (height-dh)/2+py
            x0, y0 = max(0, left), max(0, top)
            x1, y1 = min(width, left+dw), min(height, top+dh)
            # Render the visible area only, even when a large original is zoomed to 1600%.
            image = self._lb_preview.transform((max(1, round(x1-x0)), max(1, round(y1-y0))),
                Image.Transform.EXTENT, ((x0-left)/scale, (y0-top)/scale, (x1-left)/scale, (y1-top)/scale),
                Image.Resampling.BICUBIC)
            self.lb_canvas.photo = ImageTk.PhotoImage(image, master=self)
            self.lb_canvas.create_image(x0, y0, image=self.lb_canvas.photo, anchor="nw")
            self._lb_display_rect = (left, top, dw, dh)
            self.lb_zoom_label.configure(text=f"{scale:.0%}")
        else:
            self._lb_display_rect = None
            is_video = self.lb_asset.get("media_type") == "video"
            self.lb_canvas.create_text(width / 2, height / 2,
                text="视频文件\n\n点击「打开视频」使用默认播放器播放" if is_video else "暂时无法预览\n\n可点击「打开原图」查看文件",
                fill=DARK_MUTED, font=(FONT, 12), justify="center")
        self.lb_previous.place(x=12, rely=0.5, anchor="w")
        self.lb_next.place(relx=1, x=-12, rely=0.5, anchor="e")
        if hasattr(self, "lb_ocr_text"):
            self._highlight_ocr_line()

    def _render_duplicates(self):
        groups = self.db.duplicate_groups()
        body = tk.Frame(self.grid_frame, bg=CARD)
        body.grid(row=0, column=0, sticky="ew", pady=(4, 24))
        if not groups:
            label(body, "没有发现重复文件", 18, bold=True).pack(pady=(80, 12))
            label(body, "你的照片库很整洁。这里只显示内容完全相同的文件。", 10, MUTED).pack()
            return
        columns = max(1, (self.canvas.winfo_width() - 96) // 160)
        for group in groups:
            card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=LINE,
                            padx=18, pady=16)
            card.pack(fill="x", pady=(0, 16))
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x", pady=(0, 16))
            label(head, f"{group['count']} 张重复", 12, bold=True).pack(side="left")
            label(head, f"共 {self._fmt_size(group['bytes'])}", 9, MUTED).pack(side="left", padx=14)
            ids = [item["id"] for item in group["items"]]

            def delete_rest(ids=ids):
                if messagebox.askyesno(APP_NAME, f"保留第一张，其余 {len(ids) - 1} 张移到回收站？\n之后仍可恢复。", parent=self):
                    self._delete_photos(ids[1:], confirm=False)

            Button(head, "保留第一张，其余移到回收站", delete_rest,
                   icon_name="trash").pack(side="right")
            photos = tk.Frame(card, bg=CARD)
            photos.pack(fill="x")
            for index, asset in enumerate(group["items"]):
                item = tk.Frame(photos, bg=CARD, width=144)
                item.grid(row=index // columns, column=index % columns, sticky="nw", padx=(0, 12), pady=(0, 12))
                path = thumbs.ensure(asset["path"], asset["id"], self.cache_dir, "thumb")
                photo = None
                if path:
                    try:
                        with Image.open(path) as image:
                            photo = ImageTk.PhotoImage(rounded_photo(image, 132), master=self)
                    except (OSError, ValueError):
                        pass
                preview = tk.Canvas(item, width=136, height=136, bg=CARD, highlightthickness=0, cursor="hand2", takefocus=True)
                preview.pack(anchor="w")
                if photo:
                    preview.create_image(2, 2, image=photo, anchor="nw")
                    preview.photo = photo
                else:
                    rounded(preview, (2, 2, 134, 134), SIDEBAR_BG)
                    preview.create_image(68, 68, image=icon(self, "file", size=30))
                if index == 0:
                    rounded(preview, (8, 105, 48, 128), "#258858", radius=5)
                    preview.create_text(28, 116, text="保留", fill=ON_ACCENT, font=(FONT, 8))
                for event in ("<Button-1>", "<Return>"):
                    preview.bind(event, lambda e, aid=asset["id"], all_ids=ids: self._open_lightbox(aid, all_ids))
                label(item, asset["name"], 9, wraplength=136, anchor="w", justify="left").pack(anchor="w", pady=(6, 2))
                label(item, self._fmt_size(asset["size"]), 9, MUTED).pack(anchor="w")

    def _render_appearance(self, parent):
        card = tk.Frame(parent, bg=SIDEBAR_BG, highlightthickness=1, highlightbackground=LINE,
                        padx=20, pady=18)
        card.pack(fill="x", pady=(0, 24))
        label(card, "外观", 13, bold=True, image=icon(self, "monitor", TEXT, 24),
              compound="left", padx=10).pack(anchor="w")
        label(card, "选择 PhotoNest 的显示主题", 9, MUTED).pack(anchor="w", pady=(6, 16))
        choices = tk.Frame(card, bg=SIDEBAR_BG)
        choices.pack(fill="x")
        self.theme_var = tk.StringVar(value=self.theme_choice)
        self.theme_radios = {}
        for index, (key, title) in enumerate(themes.CHOICES):
            choices.grid_columnconfigure(index, weight=1, uniform="theme")
            tile = tk.Frame(choices, bg=CARD, highlightthickness=1,
                            highlightbackground=ACCENT if key == self.theme_choice else LINE, padx=12, pady=12)
            tile.grid(row=0, column=index, sticky="nsew", padx=(0, 12) if index < 2 else 0)
            preview = tk.Canvas(tile, width=1, height=90, bg=CARD, highlightthickness=0, cursor="hand2")
            preview._theme_preview = True
            preview.pack(fill="x")

            def draw(event=None, canvas=preview, mode=key):
                canvas.delete("all")
                width = max(30, canvas.winfo_width())
                def sample(left, right, dark):
                    palette = ui.PALETTES["dark" if dark else "light"]
                    canvas.create_rectangle(left, 1, right, 87, fill=palette["BG"], outline=palette["LINE"])
                    canvas.create_rectangle(left + 1, 2, right - 1, 15, fill=palette["SIDEBAR_BG"], outline="")
                    canvas.create_rectangle(left + 1, 16, left + (right-left)*.22, 86, fill=palette["SIDEBAR_BG"], outline="")
                    for row in range(2):
                        for col in range(3):
                            x = left + (right-left)*(.29 + col*.22)
                            y = 25 + row*28
                            canvas.create_rectangle(x, y, x+(right-left)*.16, y+20,
                                fill=palette["LINE"], outline="")
                if mode == "system":
                    sample(0, width/2, False)
                    sample(width/2, width, True)
                else:
                    sample(0, width, mode == "dark")

            preview.bind("<Configure>", draw)
            choose = lambda event=None, value=key: self._set_theme(value)
            tile.bind("<Button-1>", choose)
            preview.bind("<Button-1>", choose)
            radio = tk.Radiobutton(tile, text=title + (" · 默认" if key == "system" else ""),
                variable=self.theme_var, value=key, command=lambda value=key: self._set_theme(value),
                bg=CARD, fg=TEXT, activebackground=CARD, activeforeground=TEXT,
                selectcolor=CARD, font=(FONT, 10), cursor="hand2")
            radio.pack(pady=(10, 0))
            self.theme_radios[key] = radio
        self.system_theme_var = tk.StringVar(value="当前系统：" + ("深色" if themes.system_is_dark() else "浅色"))
        label(card, size=9, color=MUTED, textvariable=self.system_theme_var).pack(anchor="w", pady=(15, 4))
        label(card, "主界面、照片查看器与文字识别同步切换；选择后立即生效。", 9, MUTED,
              wraplength=650, justify="left").pack(anchor="w")

    def _render_settings(self):
        body = tk.Frame(self.grid_frame, bg=CARD)
        body.grid(row=0, column=0, sticky="ew", pady=(6, 24))
        label(body, f"{APP_NAME} {APP_VERSION}", 12, bold=True).pack(anchor="w", pady=(0, 6))
        label(body, "当前程序："+os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__),
              9, MUTED, wraplength=700, justify="left").pack(anchor="w", pady=(0, 16))
        label(body, "照片仅在本机保存", 9, MUTED, image=icon(self, "lock", size=16),
              compound="left", padx=7).pack(anchor="w", pady=(0, 24))
        self._render_appearance(body)
        label(body, "扫描范围", 12, bold=True).pack(anchor="w", pady=(0, 8))
        label(body, "首次自动扫描本机固定磁盘；可移除不需要的范围，也可补充指定文件夹。", 9, MUTED,
              wraplength=650, justify="left").pack(anchor="w", pady=(0, 12))
        roots = self.db.list_roots()
        table = ttk.Treeview(body, columns=("name", "path", "count"), show="headings",
                              height=max(2, min(6, len(roots))), style="Folders.Treeview",
                              selectmode="browse")
        for column, title, width, stretch in (("name", "文件夹", 150, False),
                                              ("path", "路径", 280, True),
                                              ("count", "照片", 95, False)):
            table.heading(column, text=title, anchor="w")
            table.column(column, width=width, minwidth=60, stretch=stretch, anchor="w")
        table.pack(fill="x")
        for index, root in enumerate(roots):
            table.insert("", "end", iid=str(index), values=(os.path.basename(root["path"].rstrip("\\/")) or root["path"],
                                                             root["path"], f"{root['count']:,} 张"))
        actions = tk.Frame(body, bg=CARD)
        actions.pack(fill="x", pady=(12, 20))
        Button(actions, "添加文件夹", self._add_root, icon_name="plus").pack(side="left")

        def remove():
            selection = table.selection()
            if not selection:
                return
            root = roots[int(selection[0])]
            if not messagebox.askyesno(APP_NAME, f"移除库文件夹？\n{root['path']}\n\n磁盘上的照片不会被删除。", parent=self):
                return
            if self.scanner.snapshot()["running"]:
                self._pause_scan()
            for asset_id in self.db.remove_root(root["path"]):
                thumbs.remove(self.cache_dir, asset_id)
            self._settings_root = None
            self.show_view({"type": "settings"})

        remove_btn = Button(actions, "移除选中", remove, icon_name="trash")
        remove_btn.pack(side="left", padx=10)
        remove_btn.configure(state="disabled")

        def selected(event=None):
            values = table.selection()
            self._settings_root = roots[int(values[0])]["path"] if values else None
            remove_btn.configure(state="normal" if values else "disabled")

        table.bind("<<TreeviewSelect>>", selected)
        for index, root in enumerate(roots):
            if root["path"] == self._settings_root:
                table.selection_set(str(index))
        self._render_scan_exclusions(body)
        counts = self.db.counts()
        stats = tk.Frame(body, bg=CARD)
        stats.pack(fill="x", pady=(8, 24))
        stats.grid_columnconfigure((0, 1, 2), weight=1, uniform="stat")
        for index, (name, value, symbol) in enumerate((
            ("照片和视频", f"{counts['active']:,}", "image"),
            ("收藏", f"{counts['favorites']:,}", "heart"),
            ("库大小", self._fmt_size(counts["bytes"]), "drive"),
        )):
            card = tk.Frame(stats, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=18, pady=16)
            card.grid(row=0, column=index, sticky="ew", padx=(0, 12) if index < 2 else 0)
            label(card, name, 9, MUTED, image=icon(self, symbol, ACCENT, 20),
                  compound="left", padx=8).pack(anchor="w")
            label(card, value, 17, bold=True).pack(anchor="w", padx=28, pady=(5, 0))
        label(body, "数据目录", 12, bold=True).pack(anchor="w", pady=(0, 10))
        directory = tk.Frame(body, bg=CARD)
        directory.pack(fill="x")
        Button(directory, "打开数据目录", self._open_data_dir, icon_name="folder").pack(side="right", padx=(12, 0))
        value = tk.StringVar(value=self.data_dir)
        entry = tk.Entry(directory, textvariable=value, state="readonly", readonlybackground=SIDEBAR_BG,
                          relief="flat", fg=TEXT, font=(FONT, 10), highlightthickness=1, highlightbackground=LINE)
        entry.pack(side="left", fill="x", expand=True, ipady=9)
        label(body, "包含索引数据库、缩略图缓存和相册备份。", 9, MUTED).pack(anchor="w", pady=(8, 16))
        if self.sync:
            state = self.sync.snapshot()
            label(body, '后台自动同步', 12, bold=True).pack(anchor='w', pady=(8, 8))
            label(body, '关闭窗口后继续监听并自动识别新照片；托盘菜单可打开窗口、统一暂停或退出程序。',
                  9, MUTED, wraplength=660, justify='left').pack(anchor='w')
            label(body, state['message'], 9, MUTED).pack(anchor='w', pady=(6, 10))
        divider(body)
        footer = tk.Frame(body, bg=CARD)
        footer.pack(fill="x", pady=(0, 10))
        Button(footer, "退出程序", self._on_close, variant="danger", icon_name="power").pack(side="left")
        Button(footer, "完成", lambda: self.show_view({"type": "all"}), variant="primary").pack(side="right")
        Button(footer, "重新扫描", self._start_scan, icon_name="refresh").pack(side="right", padx=10)

    def _open_data_dir(self):
        try:
            os.startfile(self.data_dir)
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)


    # ---------------------------------------------------------- local AI UI
    def _start_ai(self):
        if not self.ai.ready:
            messagebox.showerror(APP_NAME, "模型未就绪。请完整解压程序和 models 文件夹。\n\n" + self.ai.error, parent=self)
            return
        self._automatic_processing = True
        self.db.set_setting('scan_paused', '0')
        self.db.set_setting('ai_paused', '0')
        if self.sync:
            self.sync.set_paused(False)
            if self._tray:
                self._tray.set_paused(False)
        self.ai.start(retry=True)
        if self.scanner.snapshot()['phase'] == 'paused' or not self.db.root_paths():
            self._start_scan()

    def _poll_ai(self):
        state = self.ai.snapshot()
        if state["phase"] in ("loading", "indexing"):
            self.ai_status_var.set("AI 准备中…" if state["phase"] == "loading" else f"AI 识别 {state['done']:,}/{state['total']:,}")
        elif state["phase"] == "paused":
            self.ai_status_var.set("AI 已暂停")
        elif state["phase"] == "done":
            self.ai_status_var.set("AI 识别完成" + (f" · {state['errors']} 个失败" if state["errors"] else ""))
        elif state["phase"] == "error":
            self.ai_status_var.set("AI 识别出错，点击查看")
        if self.view["type"] == "ai" and getattr(self, "ai_page_progress", None) and self.ai_page_progress.winfo_exists():
            self.ai_page_progress.configure(maximum=max(1, state["total"]), value=state["done"])
            detail = state["message"]
            if state["current"]:
                detail += "\n" + state["current"]
            self.ai_page_status.set(detail)
            self.ai_page_start.configure(text="处理中…" if state["running"] else "继续扫描与识别",
                                         state="disabled" if state["running"] or not self.ai.ready else "normal")
            self.ai_page_pause.configure(state="normal" if state["running"] or self.scanner.snapshot()['running'] else "disabled")
        refresh = False
        while True:
            try:
                kind, result = self.ai.events.get_nowait()
            except queue.Empty:
                break
            if kind == "finished":
                refresh = True
            elif kind == "search":
                if result["request"] == self._search_request and self.view["type"] == "search" and self.view.get("q") == result["q"]:
                    self.show_view({"type": "search", "q": result["q"], "ids": result["ids"]})
                    if result["error"]:
                        self.count_var.set(self.count_var.get() + " · 语义匹配不可用，已显示文字结果")
            elif kind == "ocr":
                request = self._ocr_windows.pop(result["request"], None)
                if request and request["window"].winfo_exists():
                    request["text"].delete("1.0", "end")
                    request["text"].insert("1.0", result["text"])
                    self._ocr_baseline = result["text"]
                    self._ocr_blocks = result["blocks"]
                    self._ocr_loaded = not result["error"]
                    request["text"].edit_modified(False)
                    request["status"].set(result["error"] or ("识别完成，可编辑后复制或导出。" if result["text"] else "没有检测到可识别的文字。"))
                    for button in request["buttons"]:
                        button.configure(state="normal" if not result["error"] else "disabled")
                    self.lb_ocr_save.configure(state="normal" if not result["error"] and self._ocr_crop is None else "disabled")
                    request["retry"].configure(state="normal")
                    if self.lb and self.lb_asset["id"] == result["asset_id"]:
                        self.lb_tags.configure(text="标签：" + (" · ".join(self.db.tags_for(result["asset_id"])) or "尚未分类"))
        if refresh:
            if self.view["type"] == "ai":
                self.show_view(self.view)
            else:
                self._refresh_sidebar()
        self.after(400, self._poll_ai)

    def _render_ai_settings(self):
        body = tk.Frame(self.grid_frame, bg=CARD)
        body.grid(row=0, column=0, sticky="ew", pady=(4, 24))
        label(body, "离线识图", 14, bold=True, image=icon(self, "image", ACCENT, 24),
              compound="left", padx=10).pack(anchor="w")
        label(body, "同一套本地索引，支持找照片、自动分类和图片转文字。", 10, MUTED).pack(anchor="w", pady=(8, 20))
        model_text = "模型已就绪 · OCR 文字识别 + RN50 画面理解" if self.ai.ready else "模型未就绪，请检查程序旁的 models 文件夹"
        label(body, model_text, 11, SUCCESS if self.ai.ready else RED, wraplength=650, justify="left").pack(anchor="w")
        if self.ai.ready:
            size = sum(item.get("bytes", 0) for item in self.ai.manifest["files"].values())
            label(body, f"模型大小 {self._fmt_size(size)} · 照片仅在本机处理", 9, MUTED).pack(anchor="w", pady=(7, 0))
        else:
            label(body, self.ai.error, 9, MUTED, wraplength=650, justify="left").pack(anchor="w", pady=8)
        divider(body)
        stats = self.ai.store.stats(self.ai.version)
        label(body, f"已识别 {stats['done']:,} / {stats['total']:,} 张照片", 16, bold=True).pack(anchor="w")
        label(body, f"待识别 {max(0, stats['total'] - stats['done']):,} 张 · 失败 {stats['errors']:,} 张", 10, MUTED).pack(anchor="w", pady=(6, 16))
        self.ai_page_progress = ttk.Progressbar(body, mode="determinate")
        self.ai_page_progress.pack(fill="x", pady=(0, 12))
        self.ai_page_status = tk.StringVar(value=self.ai.snapshot()["message"])
        label(body, size=10, color=MUTED, textvariable=self.ai_page_status,
              wraplength=650, justify="left").pack(anchor="w")
        actions = tk.Frame(body, bg=CARD)
        actions.pack(fill="x", pady=(18, 20))
        self.ai_page_start = Button(actions, "继续扫描与识别", self._start_ai, icon_name="image", variant="primary", height=40)
        self.ai_page_start.pack(side="left", padx=(0, 10))
        self.ai_page_pause = Button(actions, "暂停扫描与识别", self._pause_scan, height=40)
        self.ai_page_pause.pack(side="left")
        Button(actions, "导出已识别文字", self._export_all_ocr, icon_name="file", height=40).pack(side="right")
        label(body, "扫描到图片后自动进行文字识别和分类；只处理新增或修改的图片，后台运行时同样生效。", 10, MUTED,
              wraplength=650, justify="left").pack(anchor="w", pady=(0, 10))
        label(body, "自动分类结果可在照片查看器中编辑；纠正后的文字和标签会保留。", 10, MUTED,
              wraplength=650, justify="left").pack(anchor="w")
        label(body, "扫描与 AI 共用暂停和继续操作，无需单独启用识别。", 9, MUTED).pack(anchor='w', pady=(18, 0))

    def _export_all_ocr(self):
        rows = self.ai.store.export_text()
        if not rows:
            messagebox.showinfo(APP_NAME, "还没有识别结果，请先开始识别照片。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出识别文字", defaultextension=".txt",
                                           initialfile="PhotoNest-识别文字.txt", filetypes=[("文本文件", "*.txt")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as stream:
                    for row in rows:
                        stream.write(f"【{row['name']}】\n{row['path']}\n\n{row['text']}\n\n{'─' * 30}\n\n")
            except OSError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self)

    def _open_ocr(self, asset_id=None, crop=None):
        if getattr(self, '_gif_playing', False):
            self._pause_gif(reset=True)
        if asset_id is None:
            if not self.lb:
                return
            asset_id = self.lb_asset["id"]
        asset = self.db.get_asset(asset_id)
        if not asset or asset.get("media_type") == "video":
            return
        if not self.lb or self.lb_asset["id"] != asset_id:
            self._open_lightbox(asset_id, [asset_id])
        self._show_viewer_tab("ocr", load=False)
        if crop is None:
            if self._ocr_crop is not None:
                self._sync_viewer_ocr(asset, force=True)
            if self._ocr_loaded or self._ocr_windows:
                return
        self._request_viewer_ocr(crop)

    def _build_ocr_panel(self):
        body = self.lb_ocr_frame
        head = tk.Frame(body, bg=DARK_CARD)
        head.pack(fill="x")
        self.lb_ocr_title = label(head, "识别文字", 15, bold=True)
        self.lb_ocr_title.pack(side="left")
        self.lb_ocr_retry = Button(head, "重新识别", lambda: self._request_viewer_ocr(self._ocr_crop, True), variant="quiet")
        self.lb_ocr_retry.pack(side="right")
        self.lb_ocr_status = tk.StringVar(value="点击提取文字，在本机识别这张照片。")
        label(body, size=9, color=MUTED, textvariable=self.lb_ocr_status,
              wraplength=350, justify="left").pack(anchor="w", pady=(8, 16))
        footer = tk.Frame(body, bg=DARK_CARD)
        footer.pack(side="bottom", fill="x", pady=(14, 0))
        actions = tk.Frame(footer, bg=DARK_CARD)
        actions.pack(fill="x")
        self.lb_ocr_copy = Button(actions, "复制全部", self._copy_ocr, variant="primary")
        self.lb_ocr_copy.pack(side="left", padx=(0, 8))
        self.lb_ocr_export = Button(actions, "导出 TXT", self._export_ocr, variant="dark")
        self.lb_ocr_export.pack(side="left", padx=(0, 8))
        self.lb_ocr_save = Button(actions, "保存纠正", self._save_ocr, variant="quiet")
        self.lb_ocr_save.pack(side="left")
        label(footer, "在左侧框选，可重新识别局部文字。", 9, MUTED,
              wraplength=350, justify="left").pack(anchor="w", pady=(12, 0))
        area = tk.Frame(body, bg=EDITOR_BG, highlightthickness=1, highlightbackground=LINE)
        area.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(area, orient="vertical")
        scroll.pack(side="right", fill="y")
        self.lb_ocr_text = text = tk.Text(area, wrap="word", font=(FONT, 11), bg=EDITOR_BG, fg=TEXT,
                       width=1, height=1, insertbackground=TEXT, relief="flat", padx=16, pady=16,
                       spacing1=3, spacing3=7, selectbackground=ACCENT_SOFT, selectforeground=TEXT, undo=True,
                       yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        scroll.configure(command=text.yview)
        text.bind("<ButtonRelease-1>", self._highlight_ocr_line)
        text.bind("<KeyRelease>", self._highlight_ocr_line)
        self.lb_ocr_frame._wheel_target = text
        self._ocr_crop = None
        self._ocr_loaded = False
        self._ocr_baseline = ""
        self._ocr_blocks = []

    def _show_viewer_tab(self, name, load=True):
        if name == 'ocr' and getattr(self, '_gif_playing', False):
            self._pause_gif(reset=True)
        self._viewer_tab = name
        if hasattr(self, 'lb_animation_btn'):
            self.lb_animation_btn.configure(state='disabled' if name == 'ocr' else 'normal')
        self.lb_info_frame.pack_forget()
        self.lb_ocr_frame.pack_forget()
        (self.lb_ocr_frame if name == "ocr" else self.lb_info_frame).pack(fill="both", expand=True)
        for key, button in self.lb_tabs.items():
            button.configure(variant="active" if key == name else "dark")
        if name == "info":
            self.lb_canvas.delete("ocr-highlight")
        if name == "ocr" and load and self._ocr_asset_id and not self._ocr_loaded:
            if not self._ocr_windows:
                self._request_viewer_ocr()

    def _stash_viewer_ocr(self):
        if self._ocr_asset_id and self.lb_window and self.lb_window.winfo_exists() and self._ocr_loaded:
            self._ocr_drafts[(self._ocr_asset_id, self._ocr_crop)] = (
                self.lb_ocr_text.get("1.0", "end-1c"), self._ocr_baseline, self._ocr_blocks, self._ocr_file_stamp)

    def _sync_viewer_ocr(self, asset, force=False):
        stamp = (asset["size"], asset["mtime"])
        if self._ocr_asset_id == asset["id"] and self._ocr_file_stamp == stamp and not force:
            return
        self._stash_viewer_ocr()
        self._ocr_windows.clear()
        self._ocr_asset_id = asset["id"]
        self._ocr_file_stamp = stamp
        self._ocr_crop = None
        self.lb_ocr_title.configure(text="识别文字")
        cached = self.ai.store.result(asset["id"])
        draft = self._ocr_drafts.get((asset["id"], None))
        if draft and draft[3] != stamp:
            draft = None
        self._ocr_loaded = bool(draft or (cached and cached["ocr_done"]))
        value = draft[0] if draft else cached["text"] if cached else ""
        self._ocr_baseline = draft[1] if draft else value
        self._ocr_blocks = draft[2] if draft else (
            cached["blocks"] if cached and cached["text"] == cached["ocr_text"] else [])
        self.lb_ocr_text.delete("1.0", "end")
        self.lb_ocr_text.insert("1.0", value)
        self.lb_ocr_text.edit_reset()
        self.lb_ocr_text.edit_modified(False)
        self.lb_ocr_status.set("已加载识别结果，可直接编辑。" if self._ocr_loaded else "点击重新识别，提取这张照片的文字。")
        for button in (self.lb_ocr_copy, self.lb_ocr_export, self.lb_ocr_save):
            button.configure(state="normal" if self._ocr_loaded else "disabled")
        video = asset.get("media_type") == "video"
        self.lb_ocr_retry.configure(state="disabled" if video else "normal")
        if video:
            self.lb_ocr_status.set("视频不支持文字识别。")
        elif self._viewer_tab == "ocr" and not self._ocr_loaded:
            self._request_viewer_ocr()

    def _request_viewer_ocr(self, crop=None, force=False):
        if not self.lb or self.lb_asset.get("media_type") == "video":
            return
        self._stash_viewer_ocr()
        self._ocr_windows.clear()
        self._ocr_crop = tuple(crop) if crop is not None else None
        self.lb_ocr_title.configure(text="框选区域文字" if crop else "识别文字")
        self._ocr_loaded = False
        self.lb_ocr_status.set("正在本地识别，请稍候…")
        buttons = (self.lb_ocr_copy, self.lb_ocr_export, self.lb_ocr_save)
        for button in (*buttons, self.lb_ocr_retry):
            button.configure(state="disabled")
        request = self.ai.request_ocr(self.lb_asset["id"], crop, force)
        self._ocr_windows[request] = {"window": self.lb_window, "text": self.lb_ocr_text,
            "status": self.lb_ocr_status, "buttons": buttons, "retry": self.lb_ocr_retry}

    def _copy_ocr(self):
        self.clipboard_clear()
        self.clipboard_append(self.lb_ocr_text.get("1.0", "end-1c"))
        self.lb_ocr_status.set("已复制全部文字。")

    def _export_ocr(self):
        path = filedialog.asksaveasfilename(parent=self.lb_window, title="导出文字", defaultextension=".txt",
            initialfile=os.path.splitext(self.lb_asset["name"])[0] + ".txt", filetypes=[("文本文件", "*.txt")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(self.lb_ocr_text.get("1.0", "end-1c"))
                self.lb_ocr_status.set("文字已导出。")
            except OSError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self.lb_window)

    def _save_ocr(self):
        if self._ocr_crop is not None:
            return
        value = self.lb_ocr_text.get("1.0", "end-1c")
        if value != self._ocr_baseline:
            self._ocr_blocks = []
            self.lb_canvas.delete("ocr-highlight")
        self._ocr_baseline = value
        self.ai.store.correct_text(self.lb_asset["id"], self._ocr_baseline)
        self.lb["dirty"] = True
        self._stash_viewer_ocr()
        self.lb_ocr_status.set("纠正已保存，文字搜索将使用此结果。")

    def _highlight_ocr_line(self, event=None):
        self.lb_canvas.delete("ocr-highlight")
        if (self._viewer_tab != "ocr" or not self._ocr_loaded
                or not getattr(self, "_lb_display_rect", None) or self._ocr_select):
            return
        if self.lb_ocr_text.get("1.0", "end-1c") != self._ocr_baseline:
            return
        line = int(self.lb_ocr_text.index("insert").split(".")[0]) - 1
        if not 0 <= line < len(self._ocr_blocks):
            return
        width, height = self.lb_asset.get("width") or 1, self.lb_asset.get("height") or 1
        if self._lb_preview and ((width > height) != (self._lb_preview.width > self._lb_preview.height)):
            width, height = height, width
        x, y, dw, dh = self._lb_display_rect
        left, top = (self._ocr_crop or (0, 0, 1, 1))[:2]
        points = []
        for px, py in self._ocr_blocks[line]["box"]:
            points.extend((x + (left + px / width) * dw, y + (top + py / height) * dh))
        self.lb_canvas.create_polygon(*points, fill="", outline=ACCENT, width=2, tags="ocr-highlight")

    def _begin_ocr_crop(self):
        if not self.lb or self._lb_preview is None:
            return
        self._ocr_select = True
        self._show_viewer_tab("ocr", load=False)
        self._ocr_origin = None
        self.lb_canvas.configure(cursor="crosshair")
        self.lb_position.set("在照片上拖动，框选需要提取的文字")

    def _ocr_drag_start(self, event):
        self._pan_origin = (event.x, event.y, *getattr(self, "_pan", (0, 0)))
        if not self._ocr_select or not getattr(self, "_lb_display_rect", None):
            return
        x, y, width, height = self._lb_display_rect
        if x <= event.x <= x + width and y <= event.y <= y + height:
            self._ocr_origin = (event.x, event.y)
            self.lb_canvas.delete("ocr-selection")
            self.lb_canvas.create_rectangle(event.x, event.y, event.x, event.y,
                outline="#75a0ff", width=2, dash=(5, 3), tags="ocr-selection")

    def _ocr_drag_move(self, event):
        if not self._ocr_select and getattr(self, "_pan_origin", None):
            x, y, px, py = self._pan_origin
            self._pan = (px+event.x-x, py+event.y-y)
            self._draw_lightbox()
            return
        if self._ocr_select and getattr(self, "_ocr_origin", None):
            self.lb_canvas.coords("ocr-selection", *self._ocr_origin, event.x, event.y)

    def _ocr_drag_end(self, event):
        self._pan_origin = None
        if not self._ocr_select or not getattr(self, "_ocr_origin", None):
            return
        x, y, width, height = self._lb_display_rect
        start_x, start_y = self._ocr_origin
        left, right = sorted((start_x, event.x))
        top, bottom = sorted((start_y, event.y))
        self._ocr_select = False
        self.lb_canvas.configure(cursor="")
        self.lb_canvas.delete("ocr-selection")
        self.lb_position.set(f"←      {self.lb['index'] + 1} / {len(self.lb['ids'])}      →")
        if right - left < 6 or bottom - top < 6:
            return
        crop = (max(0, (left - x) / width), max(0, (top - y) / height),
                min(1, (right - x) / width), min(1, (bottom - y) / height))
        self._open_ocr(crop=crop)

    def _edit_tags(self):
        if not self.lb:
            return
        asset_id = self.lb_asset["id"]
        win = tk.Toplevel(self.lb_window)
        win.title("编辑标签")
        win.configure(bg=CARD)
        win.resizable(False, False)
        center_window(win, self.lb_window, 480, 245)
        body = tk.Frame(win, bg=CARD, padx=24, pady=22)
        body.pack(fill="both", expand=True)
        label(body, "编辑标签", 16, bold=True).pack(anchor="w")
        label(body, "用逗号分隔。移除的自动标签不会被重新添加。", 9, MUTED).pack(anchor="w", pady=(7, 15))
        entry = tk.Entry(body, relief="flat", bg=SIDEBAR_BG, fg=TEXT, font=(FONT, 11),
                          highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
        entry.pack(fill="x", ipady=9)
        entry.insert(0, ", ".join(self.db.tags_for(asset_id)))

        def save():
            names = [s.strip() for s in re.split(r"[,，;；\n]+", entry.get()) if s.strip()]
            if len(names) > 30 or any(len(name) > 40 for name in names):
                messagebox.showinfo(APP_NAME, "最多 30 个标签，每个标签不超过 40 个字符。", parent=win)
                return
            self.ai.store.set_tags(asset_id, names)
            self.lb_tags.configure(text="标签：" + (" · ".join(names) or "尚未分类"))
            self.lb["dirty"] = True
            self._refresh_sidebar()
            win.destroy()

        buttons = tk.Frame(body, bg=CARD)
        buttons.pack(side="bottom", fill="x", pady=(15, 0))
        Button(buttons, "保存", save, variant="primary").pack(side="right", padx=(8, 0))
        Button(buttons, "取消", win.destroy).pack(side="right")
        win.bind("<Return>", lambda e: save())
        win.bind("<Escape>", lambda e: win.destroy())
        win.transient(self.lb_window)
        win.grab_set()
        entry.focus_set()

    # ---------------------------------------------------------------- close
    def _hide_to_tray(self):
        if not self._tray or not self._tray.available:
            messagebox.showinfo(APP_NAME, '系统托盘暂不可用，窗口将保留。可在设置中选择「退出程序」。', parent=self)
            return
        if self.lb:
            self._close_lightbox()
        self._hidden_to_tray = True
        self.withdraw()

    def _restore_from_tray(self):
        self._hidden_to_tray = False
        self.deiconify()
        self.lift()
        self.focus_force()
        if not self.lb and not self.multi_select_mode:
            self.show_view(self.view)

    def _refresh_synced_view(self):
        if self.view['type'] in ('settings', 'ai', 'duplicates'):
            self._refresh_sidebar()
            return
        position = self.canvas.yview()[0]
        previous_limit = self.limit
        self.limit = max(self.limit, len(self.items))
        try:
            self.show_view(self.view)
        finally:
            self.limit = previous_limit
        self.update_idletasks()
        self.canvas.yview_moveto(position)

    def _poll_background(self):
        if self._closing:
            return
        if self._instance is not None and self._instance.consume_open():
            self._restore_from_tray()
        for _ in range(20):
            try:
                action = self._app_events.get_nowait()
            except queue.Empty:
                break
            if action == 'exit':
                self._on_close()
                return
            if action == 'open':
                self._restore_from_tray()
            elif action == 'pause':
                paused = not self.sync.snapshot()['paused']
                if paused:
                    self._pause_scan()
                else:
                    self._start_ai()
        revision = getattr(self.scanner, 'revision', 0)
        scan_state = self.scanner.snapshot()
        if revision != self._sync_revision and (not scan_state['running'] or scan_state.get('baseline_ready')):
            if self._hidden_to_tray:
                self._sync_revision = revision
            elif not self.lb and not self.multi_select_mode and not self._file_busy:
                self._refresh_synced_view()
                self._sync_revision = revision
        self.after(500, self._poll_background)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.sync:
            self.sync.close()
        if self._tray:
            self._tray.close()
        for callback in self.tk.call("after", "info"):
            self.after_cancel(callback)
        self.scanner.stop()
        self.ai.close()
        self._preview_serial += 1
        self._media_pool.shutdown(wait=True, cancel_futures=True)
        self._preview_pool.shutdown(wait=True, cancel_futures=True)
        if self._gif_decoder is not None:
            self._gif_decoder.close()
            self._gif_decoder = None
        self._file_pool.shutdown(wait=True, cancel_futures=True)
        try:
            self.scanner.stop(wait=True)
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        if self._instance is not None:
            self._instance.close()
        self.destroy()

    @staticmethod
    def _fmt_size(value: int) -> str:
        value = int(value or 0)
        if value < 1024:
            return f"{value} B"
        units = ["KB", "MB", "GB", "TB"]
        size = value / 1024
        index = 0
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        return f"{size:.1f} {units[index]}"


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PhotoNest native photo library")
    parser.add_argument("--data", default=None, help="数据库和缩略图目录")
    parser.add_argument("--check-ai", action="store_true", help="验证离线识图模型，结果写入数据目录 ai-check.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    data_dir, data_source, tried = choose_data_dir(args.data)
    if not data_dir:
        message_box("无法创建数据目录。已尝试：\n\n" + "\n".join(tried) + "\n\n请把程序放到可写目录，或用 --data 指定。")
        return 1

    if not args.check_ai and not desktop.is_administrator():
        # Resolve the original user's library before Windows changes the process
        # identity/working directory. The elevated instance opens this same library.
        arguments = ["--data", data_dir]
        if not getattr(sys, "frozen", False):
            arguments.insert(0, os.path.abspath(__file__))
        try:
            desktop.launch_elevated(sys.executable, arguments, app_dir())
        except Exception as error:
            code = getattr(error, "winerror", None) or getattr(error, "hresult", 0) or 0
            if code & 0xffff != 1223:  # ERROR_CANCELLED: declining UAC simply exits.
                message_box(f"无法取得管理员权限，PhotoNest 未启动。\n\n{error}")
                return 1
        return 0

    if getattr(sys, "frozen", False):
        setup_logging(data_dir)

    if args.check_ai:
        from ai_engine import self_check
        # Check the frozen Tcl/Tk libraries and model settings page as well as inference.
        check_db = DB(":memory:")
        check_app = PhotoNestApp(check_db, Scanner(check_db, data_dir), data_dir, data_dir, autostart=False)
        check_app.withdraw()
        try:
            check_app.show_view({"type": "ai"})
            check_app.update_idletasks()
        finally:
            check_app._on_close()
        self_check(os.path.join(app_dir(), "models"), os.path.join(data_dir, "ai-check.json"))
        return 0

    instance = desktop.SessionInstance(data_dir)
    if instance.already_running:
        instance.request_open()
        instance.close()
        return 0

    cache_dir = os.path.join(data_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    db = DB(os.path.join(data_dir, "library.db"))
    excluded = [data_dir] + [os.path.join(app_dir(), name) for name in
        ("_build", "_internal", "models", "assets", "dist", "dist-ai")]
    scanner = Scanner(db, cache_dir, exclude_paths=excluded)

    print(f"{APP_NAME} {APP_VERSION}", flush=True)
    print(f"数据目录: {data_dir} ({data_source})", flush=True)

    app = PhotoNestApp(db, scanner, cache_dir, data_dir, background=True)
    app._instance = instance
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            app._on_close()
        except Exception:
            pass
        instance.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        detail = traceback.format_exc()
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(root, APP_NAME, "photonest-crash.log")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n" + "=" * 70 + "\n" + detail)
        except Exception:
            path = ""
        if getattr(sys, "frozen", False) and "--check-ai" not in sys.argv:
            message_box(f"{APP_NAME} 启动失败。\n\n{detail.strip().splitlines()[-1]}\n\n日志：{path}")
        raise
