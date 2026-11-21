"""Small, shared Tkinter primitives for PhotoNest's desktop interface."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

FONT = "Microsoft YaHei UI"
PRIMARY = "#3565e9"
ON_ACCENT = "#ffffff"
PALETTES = {
    "light": dict(BG="#ffffff", CARD="#ffffff", SIDEBAR_BG="#f7f8fb", LINE="#e7ebf1",
        TEXT="#202632", MUTED="#788393", ACCENT="#3565e9", ACCENT_SOFT="#edf2ff",
        DARK="#ffffff", DARK_CARD="#f7f8fb", DARK_LINE="#e7ebf1", DARK_MUTED="#788393",
        RED="#d34e45", HOVER="#eceff5", DISABLED_BG="#f7f8fa", DISABLED_FG="#a6aebb",
        SUCCESS="#258858", WARNING="#956b21", EDITOR_BG="#f7f8fb", SCROLL="#d5dbe5"),
    "dark": dict(BG="#101319", CARD="#101319", SIDEBAR_BG="#191e27", LINE="#343d4b",
        TEXT="#e7ebf1", MUTED="#a6afbe", ACCENT="#75a0ff", ACCENT_SOFT="#203552",
        DARK="#101319", DARK_CARD="#191e27", DARK_LINE="#343d4b", DARK_MUTED="#a6afbe",
        RED="#f0776e", HOVER="#252d3a", DISABLED_BG="#191e27", DISABLED_FG="#677386",
        SUCCESS="#6ac78a", WARNING="#e6b565", EDITOR_BG="#191e27", SCROLL="#4b5668"),
}


def set_theme(dark):
    palette = PALETTES["dark" if dark else "light"]
    globals().update(palette)
    return palette


set_theme(False)


def recolor(root, old, new):
    """Update existing widgets in place, preserving text edits, selection and dialog state."""
    colors = {value: new[key] for key, value in old.items()}
    cached = {str(photo): key for key, photo in getattr(root, "_icons", {}).items()}

    def updated_image(name):
        if name in cached:
            symbol, color, size = cached[name]
            if color == ON_ACCENT:
                return None
            return icon(root, symbol, colors.get(color, color), size)
        return None

    def visit(widget):
        if getattr(widget, "_theme_preview", False):
            return
        options = widget.keys()
        for option in ("background", "foreground", "activebackground", "activeforeground",
                "highlightbackground", "highlightcolor", "insertbackground", "selectbackground",
                "selectforeground", "selectcolor", "readonlybackground", "disabledbackground", "disabledforeground"):
            if option in options:
                value = str(widget.cget(option)).lower()
                if value in colors:
                    widget.configure(**{option: colors[value]})
        if "image" in options:
            replacement = updated_image(str(widget.cget("image")))
            if replacement:
                widget.configure(image=replacement)
        if widget.winfo_class() == "TCombobox":
            # The cached popdown is created by Tcl, and has no Python widget wrapper.
            try:
                popup = widget.tk.call("ttk::combobox::PopdownWindow", str(widget))
                widget.tk.call(str(popup) + ".f.l", "configure", "-background", CARD,
                    "-foreground", TEXT, "-selectbackground", ACCENT_SOFT, "-selectforeground", TEXT)
            except tk.TclError:
                pass
            return
        if isinstance(widget, Button):
            widget._draw()
        elif isinstance(widget, tk.Canvas):
            for item in widget.find_all():
                for option in ("fill", "outline"):
                    try:
                        value = widget.itemcget(item, option).lower()
                        if option == "fill" and widget.type(item) == "text" and value == ON_ACCENT:
                            continue
                        if value in colors:
                            widget.itemconfigure(item, **{option: colors[value]})
                    except tk.TclError:
                        pass
                if widget.type(item) == "image":
                    replacement = updated_image(widget.itemcget(item, "image"))
                    if replacement:
                        widget.itemconfigure(item, image=replacement)
        for child in widget.winfo_children():
            visit(child)

    visit(root)


def icon(master, name: str, color: str = None, size: int = 20):
    """Draw one consistent outline icon; cache images on the owning Tk root."""
    root = master._root()
    color = MUTED if color is None else color
    cache = getattr(root, "_icons", None)
    if cache is None:
        cache = root._icons = {}
    key = (name, color, size)
    if key in cache:
        return cache[key]
    scale = 3
    im = Image.new("RGBA", (size * scale, size * scale))
    d = ImageDraw.Draw(im)
    factor = size * scale / 24
    width = max(2, round(1.7 * factor))

    def points(values):
        return [round(v * factor) for v in values]

    def line(values):
        d.line(points(values), fill=color, width=width, joint="curve")

    def box(values, radius=2):
        d.rounded_rectangle(points(values), radius=radius * factor,
                            outline=color, width=width)

    if name == "logo":
        d.rounded_rectangle(points((0, 0, 24, 24)), radius=6 * factor, fill=PRIMARY)
        color = "#ffffff"
        box((5, 6, 19, 18), 1)
        line((6, 17, 11, 12, 14, 15, 17, 12, 19, 14))
        d.ellipse(points((8, 8, 10, 10)), fill=color)
    elif name in ("image", "album"):
        box((3, 3, 21, 21))
        line((4, 18, 10, 12, 14, 16, 18, 11, 21, 14))
        d.ellipse(points((7, 7, 10, 10)), outline=color, width=width)
    elif name == "folder":
        line((3, 20, 3, 5, 10, 5, 12, 8, 21, 8, 21, 20, 3, 20))
    elif name == "monitor":
        box((2, 3, 22, 17), 1)
        line((12, 17, 12, 22))
        line((7, 22, 17, 22))
    elif name == "clock":
        d.ellipse(points((3, 3, 21, 21)), outline=color, width=width)
        line((12, 7, 12, 12, 16, 14))
    elif name == "heart":
        line((12, 21, 3, 12, 2, 8, 4, 4, 8, 3, 12, 7,
              16, 3, 20, 4, 22, 8, 21, 12, 12, 21))
    elif name == "search":
        d.ellipse(points((3, 3, 17, 17)), outline=color, width=width)
        line((16, 16, 22, 22))
    elif name == "trash":
        line((3, 6, 21, 6))
        line((6, 6, 7, 21, 17, 21, 18, 6))
        line((9, 6, 9, 3, 15, 3, 15, 6))
        line((10, 10, 10, 17))
        line((14, 10, 14, 17))
    elif name == "duplicates":
        box((3, 3, 15, 15), 1)
        box((9, 9, 21, 21), 1)
    elif name == "settings":
        for x, y in ((5, 9), (12, 15), (19, 7)):
            line((x, 3, x, 21))
            d.ellipse(points((x - 2, y - 2, x + 2, y + 2)),
                      fill=SIDEBAR_BG, outline=color, width=width)
    elif name == "lock":
        box((5, 10, 19, 21), 1)
        d.arc(points((8, 2, 16, 16)), 180, 360, fill=color, width=width)
        line((12, 14, 12, 17))
    elif name == "drive":
        line((3, 16, 6, 5, 18, 5, 21, 16, 3, 16, 3, 20, 21, 20, 21, 16))
    elif name == "refresh":
        d.arc(points((3, 3, 21, 21)), 45, 300, fill=color, width=width)
        line((17, 2, 21, 6, 16, 7))
    elif name == "plus":
        line((12, 4, 12, 20))
        line((4, 12, 20, 12))
    elif name in ("left", "right"):
        line((15, 5, 8, 12, 15, 19) if name == "left" else (9, 5, 16, 12, 9, 19))
    elif name == "check":
        line((4, 12, 9, 17, 20, 6))
    elif name == "external":
        line((14, 3, 21, 3, 21, 10))
        line((21, 3, 11, 13))
        line((10, 5, 4, 5, 4, 21, 20, 21, 20, 14))
    elif name == "file":
        line((5, 2, 14, 2, 20, 8, 20, 22, 5, 22, 5, 2))
        line((14, 2, 14, 8, 20, 8))
    elif name == "calendar":
        box((3, 5, 21, 21))
        line((3, 10, 21, 10))
        line((8, 2, 8, 7))
        line((16, 2, 16, 7))
    elif name == "restore":
        d.arc(points((3, 4, 21, 22)), 190, 500, fill=color, width=width)
        line((3, 3, 3, 10, 10, 10))
    elif name == "close":
        line((6, 6, 18, 18))
        line((18, 6, 6, 18))
    elif name == "power":
        d.arc(points((3, 3, 21, 21)), -55, 235, fill=color, width=width)
        line((12, 1, 12, 12))
    result = ImageTk.PhotoImage(im.resize((size, size), Image.Resampling.LANCZOS), master=master)
    cache[key] = result
    return result


def rounded(canvas, bounds, fill, outline="", radius=8, width=1):
    x, y, right, bottom = bounds
    r = min(radius, (right - x) / 2, (bottom - y) / 2)
    return canvas.create_polygon(
        x + r, y, right - r, y, right, y, right, y + r,
        right, bottom - r, right, bottom, right - r, bottom,
        x + r, bottom, x, bottom, x, bottom - r, x, y + r, x, y,
        smooth=True, splinesteps=24, fill=fill, outline=outline, width=width)


class Button(tk.Canvas):
    """Rounded, keyboard-focusable button with optional icon and sidebar count."""
    def __init__(self, master, text="", command=None, *, icon_name=None,
                 variant="default", height=38, width=None, align="center", badge=None):
        self.text, self.command, self.icon_name = text, command, icon_name
        self.variant, self.align, self.badge = variant, align, badge
        self.disabled = self.hover = False
        self.font = tkfont.Font(family=FONT, size=10)
        natural_width = self.font.measure(text) + (56 if icon_name else 28)
        super().__init__(master, width=width or natural_width, height=height,
                         bg=master.cget("bg"), highlightthickness=0,
                         bd=0, takefocus=True, cursor="hand2")
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)
        self.bind("<ButtonRelease-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _invoke(self, event=None):
        if not self.disabled and self.command:
            if event is not None and event.type == tk.EventType.ButtonRelease:
                if not (0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()):
                    return
            self.command()
        return "break"

    def configure(self, cnf=None, **kwargs):
        for name in ("text", "badge", "variant"):
            if name in kwargs:
                setattr(self, name, kwargs.pop(name))
        if "state" in kwargs:
            self.disabled = kwargs.pop("state") == "disabled"
        result = super().configure(cnf, **kwargs)
        if hasattr(self, "font"):
            self._draw()
        return result

    config = configure

    def _draw(self, event=None):
        self.delete("all")
        palettes = {
            "default": (CARD, TEXT, LINE, HOVER),
            "primary": (PRIMARY, ON_ACCENT, PRIMARY, "#2855cf"),
            "quiet": (self.cget("bg"), TEXT, "", HOVER),
            "active": (ACCENT_SOFT, ACCENT, "", HOVER),
            "danger": (CARD, RED, LINE, HOVER),
            "menu-danger": (CARD, RED, "", HOVER),
            "dark": (DARK_CARD, TEXT, DARK_LINE, HOVER),
            "dark-danger": (DARK_CARD, RED, DARK_LINE, HOVER),
        }
        bg, fg, border, hover = palettes[self.variant]
        if self.hover and not self.disabled:
            bg = hover
        if self.disabled:
            bg, fg, border = DISABLED_BG, DISABLED_FG, LINE
        if self.focus_get() is self:
            border = ACCENT
        w, h = self.winfo_width(), self.winfo_height()
        rounded(self, (1, 1, w - 1, h - 1), bg, border)
        badge_width = self.font.measure(str(self.badge)) + 18 if self.badge is not None else 0
        available = max(0, w - 28 - (28 if self.icon_name else 0) - badge_width)
        display = self.text
        while display and self.font.measure(display) > available:
            display = display[:-2] + "…" if len(display) > 2 else ""
        content = self.font.measure(display) + ((28 if display else 20) if self.icon_name else 0)
        x = 14 if self.align == "left" else max(2, (w - content) / 2)
        if self.icon_name:
            self._icon = icon(self, self.icon_name, fg)
            self.create_image(x, h / 2, image=self._icon, anchor="w")
            x += 28
        self.create_text(x, h / 2, text=display, fill=fg, anchor="w", font=self.font)
        if self.badge is not None:
            self.create_text(w - 14, h / 2, text=str(self.badge), anchor="e",
                             fill=fg if self.variant == "active" else MUTED,
                             font=(FONT, 9))


class ContextMenu(tk.Toplevel):
    """Application-drawn popup, sharing the main UI's font, icons and buttons."""
    def __init__(self, master):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.transient(master)
        self.attributes("-topmost", True)
        self.configure(bg=LINE, padx=1, pady=1)
        self.body = tk.Frame(self, bg=CARD, padx=6, pady=6)
        self.body.pack(fill="both", expand=True)
        self.entries = []
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Button-1>", self._outside)
        self.bind("<Button-3>", self._outside)
        self.bind("<FocusOut>", lambda e: master.after_idle(self._check_focus))
        self.bind("<Down>", lambda e: self._step(1))
        self.bind("<Up>", lambda e: self._step(-1))

    def add_command(self, *, label, command=None, icon_name=None, shortcut=None, danger=False, state="normal"):
        def invoke():
            self.destroy()
            if command:
                command()
            return "break"
        button = Button(self.body, label, invoke, icon_name=icon_name, align="left",
                        variant="menu-danger" if danger else "quiet", height=38, badge=shortcut)
        button.configure(state=state)
        button.pack(fill="x", pady=1)
        self.entries.append(button)
        sequence = {"Enter": "<Return>", "Ctrl+A": "<Control-a>", "Delete": "<Delete>"}.get(shortcut)
        if sequence:
            self.bind(sequence, lambda event: invoke())
        return button

    def add_separator(self):
        tk.Frame(self.body, bg=LINE, height=1).pack(fill="x", padx=10, pady=6)

    def add_header(self, text):
        label(self.body, text, 9, MUTED).pack(anchor="w", padx=14, pady=(4, 8))

    def popup(self, x, y):
        width = max([244] + [entry.font.measure(entry.text)+70+
            (entry.font.measure(str(entry.badge))+26 if entry.badge else 0) for entry in self.entries])
        for entry in self.entries:
            entry.configure(width=width)
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = max(0, min(x, self.winfo_screenwidth()-width-8))
        y = max(0, min(y, self.winfo_screenheight()-height-8))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_set()

    def _outside(self, event):
        x, y = self.winfo_rootx(), self.winfo_rooty()
        if not (x <= event.x_root < x+self.winfo_width() and y <= event.y_root < y+self.winfo_height()):
            self.destroy()
            return "break"

    def _check_focus(self):
        if self.winfo_exists():
            try:
                focused = self.focus_get()
            except (KeyError, tk.TclError):
                focused = None
            if focused is None or focused.winfo_toplevel() is not self:
                self.destroy()

    def _step(self, direction):
        entries = [entry for entry in self.entries if not entry.disabled]
        if entries:
            focused = self.focus_get()
            current = entries.index(focused) if focused in entries else (-1 if direction > 0 else 0)
            entries[(current+direction) % len(entries)].focus_set()
        return "break"

    def destroy(self):
        if self.winfo_exists():
            if self.grab_current() is self:
                self.grab_release()
            super().destroy()


def label(parent, text="", size=10, color=None, bold=False, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=TEXT if color is None else color,
                    font=(FONT, size, "bold" if bold else "normal"), **kwargs)


def divider(parent, color=None):
    tk.Frame(parent, height=1, bg=LINE if color is None else color).pack(fill="x", pady=16)


def center_window(window, parent, width, height):
    parent.update_idletasks()
    x = max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2)
    y = max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def rounded_photo(image, size, radius=7):
    from PIL import ImageOps
    result = ImageOps.fit(image.convert("RGB"), (size, size),
                          method=Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    result.putalpha(mask)
    return result
