"""
gui_widgets.py - the visual building blocks of the NetChat window.

Everything here is pure presentation: colours, fonts, chat bubbles, peer rows,
the rounded message box and the start-up name dialog. None of it knows about
sockets, so gui.py can stay focused on "what happens when a message arrives".
"""

import os
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# ======================================================================
# Constants: fonts, sizes and the two colour themes
# ======================================================================
FONT_FAMILY = "Segoe UI"
FONT_BODY = (FONT_FAMILY, 10)
FONT_MESSAGE = (FONT_FAMILY, 11)
FONT_NAME = (FONT_FAMILY, 12, "bold")          # peer names in the list
FONT_TITLE = (FONT_FAMILY, 13, "bold")         # chat header, "NetChat"
FONT_BUBBLE_NAME = (FONT_FAMILY, 11, "bold")   # sender name inside a bubble
FONT_SMALL = (FONT_FAMILY, 9)                  # timestamps, IPs, status bar
FONT_BUTTON = (FONT_FAMILY, 10, "bold")
FONT_MONO = ("Consolas", 9)

# Sizes are in "96-dpi pixels"; px() scales them on high-DPI screens.
SIDEBAR_WIDTH = 240
HEADER_HEIGHT = 52
ROW_HEIGHT = 58
AVATAR_SIZE = 38
BUBBLE_PAD_X = 12
BUBBLE_PAD_Y = 7
BUBBLE_RADIUS = 14
BUBBLE_MAX_FRACTION = 0.65     # a bubble is never wider than 65% of the chat area
INPUT_MAX_LINES = 5            # the message box grows up to this many lines
LOG_LINES = 8                  # visible lines of the network log panel
MAX_LOG_LINES = 1000           # older log lines are dropped (the file keeps everything)

# Every colour lives in one of these two dicts, so switching theme = re-reading a dict.
LIGHT = {
    "header_bg": "#14213D", "header_fg": "#FFFFFF", "header_muted": "#AEB8CC",
    "side_bg": "#14213D", "side_fg": "#FFFFFF", "side_muted": "#9AA6BF",
    "side_hover": "#1D2E54", "side_sel": "#2A3F72",
    "side_thumb": "#3A4B73", "side_thumb_hover": "#52669A",
    "accent": "#1AA7A1", "accent_hover": "#169892", "accent_pressed": "#117873",
    "accent_fg": "#FFFFFF", "disabled_bg": "#C9CFD8", "disabled_fg": "#8A93A3",
    "chat_bg": "#F4F6F8", "card_bg": "#FFFFFF", "card_border": "#DDE2E8",
    "text": "#333333", "muted": "#7A8494", "system_fg": "#8A93A3",
    "mine_bg": "#1AA7A1", "mine_fg": "#FFFFFF", "mine_ts": "#D9F3F1",
    "input_bg": "#FFFFFF", "input_border": "#D0D6DE", "input_focus": "#1AA7A1",
    "status_bg": "#E6EAEE", "status_fg": "#6B7585",
    "log_bg": "#FFFFFF", "log_fg": "#333333",
    "thumb": "#C3CAD4", "thumb_hover": "#A9B2BF",
    "ok": "#2EB872", "err": "#E5484D", "badge_bg": "#F25C54", "badge_fg": "#FFFFFF",
}

DARK = dict(LIGHT)
DARK.update({
    "header_bg": "#0B1426", "header_muted": "#8E9AB3",
    "side_bg": "#0E182C", "side_hover": "#16233F", "side_sel": "#1F3158",
    "side_thumb": "#2B3A5C", "side_thumb_hover": "#42557F",
    "accent": "#17948F", "accent_hover": "#1AA7A1", "accent_pressed": "#0F7A75",
    "disabled_bg": "#2A3342", "disabled_fg": "#6C7788",
    "chat_bg": "#10161F", "card_bg": "#1C2531", "card_border": "#2A3646",
    "text": "#E6E9ED", "muted": "#8C97A8", "system_fg": "#7B8798",
    "mine_bg": "#138F8A", "mine_ts": "#BDEAE7",
    "input_bg": "#1C2531", "input_border": "#2F3B4D", "input_focus": "#17948F",
    "status_bg": "#0B1019", "status_fg": "#8C97A8",
    "log_bg": "#0B1019", "log_fg": "#C9D1DC",
    "thumb": "#2F3B4D", "thumb_hover": "#47566D",
})

THEMES = {"light": LIGHT, "dark": DARK}

# Avatar colours: mid-tones so the white letter is readable on every one.
AVATAR_COLORS = ["#E4572E", "#2E86AB", "#8E5DA8", "#3C9D5D",
                 "#D64F7A", "#C98A1B", "#4C6EF5", "#6B7F99"]


# ======================================================================
# Small helpers
# ======================================================================
_scale = [1.0]


def set_scale(root):
    """Remember the screen's DPI scale (1.0 = 96 dpi, 1.5 = 144 dpi ...)."""
    _scale[0] = root.winfo_fpixels("1i") / 96.0


def px(n):
    """Scale a pixel size so the layout looks the same on high-DPI screens."""
    return int(round(n * _scale[0]))


def avatar_color(name):
    """Same name -> same colour, always. (Python's hash() changes on every run, so we
    compute our own small hash instead.)"""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) % 1000003
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


def validate_name(name, max_len):
    """Return an error text for a bad username, or "" if it is fine."""
    name = name.strip()
    if not name:
        return "Please enter a username."
    if "|" in name:
        return 'The character "|" is not allowed.'
    if len(name) > max_len:
        return "At most %d characters (you typed %d)." % (max_len, len(name))
    return ""


def open_folder(path):
    """Show a file in the system's file manager (Explorer selects the file)."""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError as e:
        return str(e)                    # the caller tells the user
    return None


def rounded_rect(canvas, x1, y1, x2, y2, r, **options):
    """Draw a rounded rectangle. Tk has no such shape, so we draw a polygon with
    smooth=True whose corner points are 'pulled' into curves."""
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
              x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **options)


# ======================================================================
# ttk styles (buttons and scrollbars)
# ======================================================================
def _button_style(style, name, bg, fg, hover, pressed, disabled_bg, disabled_fg, padding, font=FONT_BUTTON,
                  width=None):
    # clam draws a 3D bevel from light/dark/border colours; giving them all the
    # same colour makes the button look flat and modern.
    style.configure(name, background=bg, foreground=fg, bordercolor=bg, lightcolor=bg,
                    darkcolor=bg, borderwidth=0, relief="flat", focusthickness=0,
                    focuscolor=bg, font=font, padding=padding)
    if width is not None:
        style.configure(name, width=width)       # clam's default is "at least 11 characters wide"
    states = [("disabled", disabled_bg), ("pressed", pressed), ("active", hover)]
    style.map(name, background=states, bordercolor=states, lightcolor=states,
              darkcolor=states, relief=[("pressed", "flat")],
              foreground=[("disabled", disabled_fg)])


def _scrollbar_style(style, name, trough, thumb, hover):
    # A custom layout without the arrow buttons = a slim modern scrollbar.
    style.layout(name, [("Vertical.Scrollbar.trough", {
        "sticky": "ns",
        "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure(name, troughcolor=trough, background=thumb, bordercolor=trough,
                    lightcolor=thumb, darkcolor=thumb, borderwidth=0, relief="flat",
                    arrowsize=px(9), gripcount=0)
    states = [("pressed", hover), ("active", hover)]
    style.map(name, background=states, lightcolor=states, darkcolor=states)


def apply_ttk_styles(root, t):
    """(Re)configure every ttk style from the colour dict `t`."""
    style = ttk.Style(root)
    style.theme_use("clam")           # 'clam' respects custom colours; the native Windows theme does not
    _button_style(style, "Send.TButton", t["accent"], t["accent_fg"], t["accent_hover"],
                  t["accent_pressed"], t["disabled_bg"], t["disabled_fg"], (px(20), px(8)))
    _button_style(style, "Plain.TButton", t["status_bg"], t["status_fg"], t["chat_bg"],
                  t["card_border"], t["status_bg"], t["disabled_fg"], (px(10), px(2)), width=0)
    _button_style(style, "Side.TButton", t["side_hover"], t["side_fg"], t["side_sel"], t["side_thumb"],
                  t["side_hover"], t["disabled_fg"], (px(8), px(2)), width=0)
    _button_style(style, "Tool.TButton", t["card_bg"], t["muted"], t["chat_bg"],
                  t["card_border"], t["card_bg"], t["disabled_fg"], (px(10), px(4)), width=0)
    _button_style(style, "Icon.TButton", t["chat_bg"], t["muted"], t["card_border"], t["thumb"],
                  t["chat_bg"], t["disabled_fg"], (px(6), px(5)), font=("Segoe UI Emoji", 16), width=0)
    _button_style(style, "Cancel.TButton", t["card_bg"], t["muted"], t["chat_bg"],
                  t["card_border"], t["card_bg"], t["disabled_fg"], (px(16), px(8)))
    # The table in the Network info window.
    style.configure("Treeview", background=t["card_bg"], fieldbackground=t["card_bg"], foreground=t["text"],
                    rowheight=px(26), borderwidth=0, font=FONT_BODY)
    style.configure("Treeview.Heading", background=t["chat_bg"], foreground=t["muted"], font=FONT_BUTTON,
                    relief="flat", padding=(px(6), px(4)))
    style.map("Treeview", background=[("selected", t["accent"])], foreground=[("selected", "#FFFFFF")])
    style.map("Treeview.Heading", background=[("active", t["card_border"])])
    # Progress bars: white on the teal "mine" bubble, teal on the white "theirs" card.
    for name, trough, bar in (("Mine", t["accent_pressed"], "#FFFFFF"), ("Their", t["card_border"], t["accent"])):
        style.configure(name + ".Horizontal.TProgressbar", troughcolor=trough, background=bar,
                        bordercolor=trough, lightcolor=bar, darkcolor=bar, thickness=px(8))
    _scrollbar_style(style, "Chat.Vertical.TScrollbar", t["chat_bg"], t["thumb"], t["thumb_hover"])
    _scrollbar_style(style, "Side.Vertical.TScrollbar", t["side_bg"], t["side_thumb"], t["side_thumb_hover"])
    _scrollbar_style(style, "Log.Vertical.TScrollbar", t["log_bg"], t["thumb"], t["thumb_hover"])


# ======================================================================
# Scrolling container (used by the peer list and the chat area)
# ======================================================================
class ScrollFrame(tk.Frame):
    """A frame that scrolls vertically. Put your widgets into `self.inner`.

    Tk frames cannot scroll by themselves, so the usual trick is used: a Canvas
    shows one big 'window' (the inner frame) that is taller than the canvas.
    """

    def __init__(self, parent, bg, scrollbar_style, reserve_bar=True):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=px(24))
        self.bar = ttk.Scrollbar(self, orient="vertical", style=scrollbar_style,
                                 command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.bar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        if reserve_bar:                           # keep the bar's space even while it is hidden,
            self.columnconfigure(1, minsize=px(12))   # so showing it never re-wraps the content
        self.rowconfigure(0, weight=1)
        self.pinned = True                        # True = the user is following the newest content
        self.last_size = (0, 0)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self._update_region())
        self.canvas.bind("<Configure>", self._on_canvas_resize)

    def set_bg(self, bg):
        for widget in (self, self.canvas, self.inner):
            widget.configure(bg=bg)

    def _on_scroll(self, first, last):
        self.bar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.bar.grid_remove()                # everything fits: no scrollbar needed
        else:
            self.bar.grid()
        # Remember whether the user sits at the bottom. A resize also changes these numbers
        # without the user scrolling, so resizes are ignored here.
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if size == self.last_size:
            self.pinned = float(last) >= 0.999
        self.last_size = size

    def _on_canvas_resize(self, event):
        follow = self.pinned                      # read it before the layout changes
        self.canvas.itemconfigure(self.window, width=event.width)   # inner frame follows our width
        self.width_changed(event.width)
        self._update_region()
        if follow:
            self.after_idle(self.scroll_to_bottom)    # stay on the newest message after a resize

    def width_changed(self, width):
        """Hook for subclasses (the chat re-wraps its bubbles here)."""

    def _update_region(self):
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(),
                                            max(self.inner.winfo_reqheight(), 1)))

    def at_bottom(self):
        """True if the user is looking at the newest content (not scrolled up)."""
        return self.canvas.yview()[1] >= 0.999

    def scroll_to_bottom(self):
        self.update_idletasks()          # let Tk lay out new widgets first, or we would stop short
        self._update_region()
        self.canvas.yview_moveto(1.0)

    def scroll_by(self, steps):
        if self.canvas.yview() != (0.0, 1.0):     # nothing to scroll if everything fits
            self.canvas.yview_scroll(steps, "units")


def install_mousewheel(root):
    """One global wheel handler: find the ScrollFrame under the pointer and scroll it.
    (Per-widget bindings do not work because the pointer is usually over a child label.)"""
    def on_wheel(event):
        try:
            widget = root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        while widget is not None and not isinstance(widget, ScrollFrame):
            widget = widget.master
        if widget is not None:
            widget.scroll_by(-1 if event.delta > 0 else 1)
    root.bind_all("<MouseWheel>", on_wheel)


# ======================================================================
# Message input box
# ======================================================================
class RoundedInput(tk.Frame):
    """A multi-line text box inside a rounded outline.

    Enter sends, Shift+Enter inserts a new line, the box grows up to
    INPUT_MAX_LINES lines, and a grey placeholder shows while it is empty.
    """

    PLACEHOLDER = "Type a message..."

    def __init__(self, parent, t, on_send, on_typing=None):
        super().__init__(parent, bg=t["chat_bg"])
        self.t = t
        self.on_send = on_send
        self.on_typing = on_typing          # called whenever the user changes the text
        self.placeholder_on = False
        self.focused = False
        self.line_height = tkfont.Font(font=FONT_MESSAGE).metrics("linespace")

        self.box = tk.Canvas(self, highlightthickness=0, bd=0, height=self._height_for(1))
        self.box.pack(fill="x", expand=True)
        self.text = tk.Text(self.box, height=1, wrap="word", font=FONT_MESSAGE, relief="flat",
                            bd=0, highlightthickness=0, padx=0, pady=0, undo=True, insertwidth=2)
        self.window = self.box.create_window(px(16), px(9), window=self.text, anchor="nw")

        self.box.bind("<Configure>", lambda e: self._draw())
        self.text.bind("<Return>", self._on_return)
        self.text.bind("<Shift-Return>", lambda e: None)   # fall through to Tk's default: newline
        self.text.bind("<FocusIn>", self._on_focus_in)
        self.text.bind("<FocusOut>", self._on_focus_out)
        self.text.bind("<<Modified>>", self._on_modified)
        self.apply_theme(t)
        self._show_placeholder()

    # ---- public API ---------------------------------------------------
    def get_text(self):
        return "" if self.placeholder_on else self.text.get("1.0", "end-1c")

    def clear(self):
        self._hide_placeholder()
        self.text.delete("1.0", "end")
        if not self.focused:
            self._show_placeholder()

    def focus_input(self):
        self.text.focus_set()

    def insert_text(self, text):
        """Insert at the cursor (used by the emoji picker)."""
        self.focus_input()
        self._hide_placeholder()
        self.text.insert("insert", text)

    def apply_theme(self, t):
        self.t = t
        self.configure(bg=t["chat_bg"])
        self.box.configure(bg=t["chat_bg"])
        self.text.configure(bg=t["input_bg"], fg=t["muted"] if self.placeholder_on else t["text"],
                            insertbackground=t["text"], selectbackground=t["accent"],
                            selectforeground="#FFFFFF")
        self._draw()

    # ---- internals ----------------------------------------------------
    def _height_for(self, lines):
        return lines * self.line_height + 2 * px(9)

    def _draw(self):
        w, h = self.box.winfo_width(), self.box.winfo_height()
        if w < 10:
            return                                    # not laid out yet
        self.box.itemconfigure(self.window, width=w - 2 * px(16))
        self._grow()          # the width decides how many lines the text needs, so re-measure
        self.box.delete("shape")
        rounded_rect(self.box, 1, 1, w - 1, h - 1, min(px(20), h // 2), fill=self.t["input_bg"],
                     outline=self.t["input_focus"] if self.focused else self.t["input_border"],
                     width=2 if self.focused else 1, tags="shape")
        self.box.tag_lower("shape")

    def _on_return(self, _event):
        self.on_send()
        return "break"                                # stop Tk from also inserting a newline

    def _on_modified(self, _event):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._grow()
            if self.on_typing and not self.placeholder_on and self.text.get("1.0", "end-1c"):
                self.on_typing()

    def _grow(self):
        """Make the box as tall as its text (1 to INPUT_MAX_LINES lines)."""
        self.text.update_idletasks()
        if self.text.winfo_width() < px(50):
            return                                    # not laid out yet; _draw() calls us again
        counted = self.text.count("1.0", "end", "displaylines")
        lines = counted[0] if counted else 1
        lines = max(1, min(INPUT_MAX_LINES, lines))
        self.text.configure(height=lines)
        self.box.configure(height=self._height_for(lines))

    def _on_focus_in(self, _event):
        self.focused = True
        self._hide_placeholder()
        self._draw()

    def _on_focus_out(self, _event):
        self.focused = False
        if self.text.get("1.0", "end-1c") == "":
            self._show_placeholder()
        self._draw()

    def _show_placeholder(self):
        if not self.placeholder_on and self.text.get("1.0", "end-1c") == "":
            self.placeholder_on = True
            self.text.insert("1.0", self.PLACEHOLDER)
            self.text.configure(fg=self.t["muted"])

    def _hide_placeholder(self):
        if self.placeholder_on:
            self.placeholder_on = False
            self.text.delete("1.0", "end")
            self.text.configure(fg=self.t["text"])


# ======================================================================
# Start-up dialog
# ======================================================================
class NameDialog(tk.Toplevel):
    """Small centered window that asks for the username and validates it."""

    def __init__(self, root, max_len):
        super().__init__(root)
        self.max_len = max_len
        self.result = None
        t = LIGHT
        self.title("NetChat")
        self.configure(bg=t["card_bg"])
        self.resizable(False, False)

        banner = tk.Frame(self, bg=t["header_bg"])
        banner.pack(fill="x")
        tk.Label(banner, text="NetChat", font=(FONT_FAMILY, 18, "bold"), bg=t["header_bg"],
                 fg=t["header_fg"]).pack(anchor="w", padx=px(24), pady=(px(16), 0))
        tk.Label(banner, text="Peer-to-peer chat on your network", font=FONT_SMALL,
                 bg=t["header_bg"], fg=t["header_muted"]).pack(anchor="w", padx=px(24),
                                                              pady=(0, px(14)))

        body = tk.Frame(self, bg=t["card_bg"])
        body.pack(fill="both", expand=True, padx=px(24), pady=px(16))
        tk.Label(body, text="Choose a username", font=FONT_NAME, bg=t["card_bg"],
                 fg=t["text"]).pack(anchor="w")

        self.var = tk.StringVar()
        border = tk.Frame(body, bg=t["input_border"], padx=1, pady=1)    # 1px border around the entry
        border.pack(fill="x", pady=(px(8), px(2)))
        field = tk.Frame(border, bg=t["input_bg"])
        field.pack(fill="x")
        self.entry = tk.Entry(field, textvariable=self.var, font=FONT_MESSAGE, relief="flat",
                              bd=0, bg=t["input_bg"], fg=t["text"], insertbackground=t["text"])
        self.entry.pack(fill="x", ipady=px(6), padx=px(8))
        self.entry.bind("<FocusIn>", lambda e: border.configure(bg=t["input_focus"]))
        self.entry.bind("<FocusOut>", lambda e: border.configure(bg=t["input_border"]))

        self.error = tk.Label(body, text="", font=FONT_SMALL, bg=t["card_bg"], fg=t["err"],
                              anchor="w")
        self.error.pack(fill="x")
        tk.Label(body, text="Up to %d characters, no \"|\"." % max_len, font=FONT_SMALL,
                 bg=t["card_bg"], fg=t["muted"], anchor="w").pack(fill="x")

        buttons = tk.Frame(body, bg=t["card_bg"])
        buttons.pack(fill="x", pady=(px(14), 0))
        ttk.Button(buttons, text="Join chat", style="Send.TButton", cursor="hand2",
                   command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", style="Cancel.TButton", cursor="hand2",
                   command=self._cancel).pack(side="right", padx=(0, px(8)))

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._center()
        self.grab_set()                      # the dialog is modal: nothing else can be clicked
        self.entry.focus_force()

    def ask(self):
        self.wait_window(self)
        return self.result

    def _ok(self):
        message = validate_name(self.var.get(), self.max_len)
        if message:
            self.error.configure(text=message)
            return
        self.result = self.var.get().strip()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    def _center(self):
        self.update_idletasks()
        w = max(self.winfo_reqwidth(), px(360))
        h = self.winfo_reqheight()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))


# ======================================================================
# Window icon (drawn pixel by pixel, so no .ico file is needed)
# ======================================================================
def _distance_to_segment(x, y, ax, ay, bx, by):
    """Distance from point (x, y) to the line segment A-B."""
    dx, dy = bx - ax, by - ay
    t = ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return ((x - (ax + t * dx)) ** 2 + (y - (ay + t * dy)) ** 2) ** 0.5


def _icon_color(x, y, teal):
    """Colour of one icon pixel (x, y in 0..1): white 'N' on a teal circle, or None outside."""
    if (x - 0.5) ** 2 + (y - 0.5) ** 2 > 0.47 ** 2:
        return None
    half = 0.05                                           # half the stroke thickness of the N
    strokes = [(0.34, 0.29, 0.34, 0.71), (0.66, 0.29, 0.66, 0.71), (0.34, 0.29, 0.66, 0.71)]
    if any(_distance_to_segment(x, y, *s) <= half for s in strokes):
        return "#FFFFFF"
    return teal


def make_app_icon(teal="#1AA7A1", size=64):
    """Build a teal circle with a white N as a tk.PhotoImage.

    Each pixel looks at a 3x3 grid of sample points and takes the majority colour,
    which smooths the edges a little. Transparent pixels are simply never painted.
    """
    img = tk.PhotoImage(width=size, height=size)
    for row in range(size):
        run_start, run_color = 0, None
        for col in range(size + 1):                       # size+1 flushes the last run
            color = None
            if col < size:
                votes = {}
                for sy in (0.2, 0.5, 0.8):
                    for sx in (0.2, 0.5, 0.8):
                        c = _icon_color((col + sx) / size, (row + sy) / size, teal)
                        votes[c] = votes.get(c, 0) + 1
                color = max(votes, key=votes.get)
            if color != run_color:
                if run_color is not None:
                    img.put(run_color, to=(run_start, row, col, row + 1))
                run_start, run_color = col, color
    return img

