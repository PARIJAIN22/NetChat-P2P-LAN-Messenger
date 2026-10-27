"""
gui_chatview.py - the message area (bubbles) and the rows of the sidebar.

A message is a plain dict, the same one that is stored in the database:
    id, chat, direction ('in' | 'out' | 'system'), sender, text, shown (HH:MM:SS),
    status ('sending' | 'queued' | 'delivered' | 'failed'), kind ('text' | 'file'),
    extra (dict), acked, total
Bubbles only DRAW such a dict. Everything that has to be decided (send, retry, delete)
is handed back to gui.py through the callbacks that gui.py plugs into ChatView.
"""

import math
import os
import tkinter as tk
from tkinter import ttk

from .gui_widgets import (
    AVATAR_SIZE, BUBBLE_MAX_FRACTION, BUBBLE_PAD_X, BUBBLE_PAD_Y, BUBBLE_RADIUS, FONT_BODY,
    FONT_BUBBLE_NAME, FONT_MESSAGE, FONT_NAME, FONT_SMALL, ROW_HEIGHT, ScrollFrame,
    avatar_color, px, rounded_rect)

CLOCK = "◷"      # a small clock-like circle
TICK = "✓"


# ======================================================================
# One chat bubble
# ======================================================================
class Bubble(tk.Canvas):
    """A rounded rectangle drawn on a canvas with labels on top.

    Mine = teal, white text, right side. Theirs = white card with a thin border and the
    sender's name, left side. The small line at the bottom shows the time and, for my
    own messages, the delivery status.
    """

    def __init__(self, parent, t, item, wrap, view):
        self.mine = item["direction"] == "out"
        super().__init__(parent, bg=t["chat_bg"], highlightthickness=0, bd=0)
        self.t = t
        self.view = view
        self.item = item
        self.fill = t["mine_bg"] if self.mine else t["card_bg"]
        self.outline = t["mine_bg"] if self.mine else t["card_border"]
        self.fg = t["mine_fg"] if self.mine else t["text"]
        self.muted = t["mine_ts"] if self.mine else t["muted"]
        self._size = (0, 0)

        self.content = tk.Frame(self, bg=self.fill)
        if not self.mine:
            tk.Label(self.content, text=item["sender"], font=FONT_BUBBLE_NAME, bg=self.fill,
                     fg=avatar_color(item["sender"]), anchor="w").pack(fill="x")
        self.body = None
        self.build_body(wrap)
        bottom = tk.Frame(self.content, bg=self.fill)
        bottom.pack(fill="x", pady=(px(2), 0))
        tk.Label(bottom, text=item["shown"][:5], font=FONT_SMALL, bg=self.fill, fg=self.muted).pack(side="right")
        self.status_label = tk.Label(bottom, font=FONT_SMALL, bg=self.fill)
        if self.mine:
            self.status_label.pack(side="right", padx=(0, px(6)))
        self.set_status(item)

        self.create_window(px(BUBBLE_PAD_X), px(BUBBLE_PAD_Y), window=self.content, anchor="nw")
        # The labels decide how big the bubble must be; whenever that changes, redraw the shape.
        self.content.bind("<Configure>", self._fit)
        self._bind_menu(self)

    # ---- content ---------------------------------------------------------
    def build_body(self, wrap):
        """The part above the time line. FileBubble replaces this."""
        self.body = tk.Label(self.content, text=self.item["text"], font=FONT_MESSAGE, bg=self.fill,
                             fg=self.fg, justify="left", anchor="w", wraplength=wrap)
        self.body.pack(fill="x")

    def set_wrap(self, wrap):
        if self.body is not None:
            self.body.configure(wraplength=wrap)

    def set_status(self, item):
        """Show sending / waiting / delivered / failed under my own messages."""
        self.item = item
        if not self.mine:
            return
        status, acked, total = item["status"], item.get("acked", 0), item.get("total", 1)
        label = self.status_label
        label.unbind("<Button-1>")
        label.configure(cursor="", underline=-1, bg=self.fill)
        if status == "delivered":
            label.configure(text=TICK if total <= 1 else "%s %d/%d" % (TICK, acked, total), fg=self.muted)
        elif status == "queued":
            label.configure(text="%s waiting for peer" % CLOCK, fg=self.muted)
        elif status == "failed":
            # a red pill is readable on the teal bubble, unlike red text
            label.configure(text=" Failed - click to retry ", fg="#FFFFFF", bg=self.t["err"], cursor="hand2")
            label.bind("<Button-1>", lambda e: self.view.retry_callback(item["id"]))
        else:
            label.configure(text="%s sending" % CLOCK, fg=self.muted)

    # ---- right-click menu -------------------------------------------------
    def _bind_menu(self, widget):
        widget.bind("<Button-3>", lambda e: self.view.show_menu(e, self.item))
        for child in widget.winfo_children():
            self._bind_menu(child)

    def bind_menu_to_new_children(self):
        self._bind_menu(self.content)

    # ---- shape --------------------------------------------------------------
    def _fit(self, _event=None):
        w = self.content.winfo_reqwidth() + 2 * px(BUBBLE_PAD_X)
        h = self.content.winfo_reqheight() + 2 * px(BUBBLE_PAD_Y)
        if (w, h) == self._size:
            return
        self._size = (w, h)
        self.configure(width=w, height=h)
        self.delete("shape")
        rounded_rect(self, 1, 1, w - 1, h - 1, px(BUBBLE_RADIUS), fill=self.fill,
                     outline=self.outline, tags="shape")
        self.tag_lower("shape")


# ======================================================================
# A file in the chat
# ======================================================================
def human_size(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return ("%d %s" % (count, unit)) if unit == "B" else ("%.1f %s" % (count, unit))
        count /= 1024.0


THUMB_MAX_PIXELS = 150            # thumbnails are shrunk to about this size
THUMB_MAX_FILE = 5 * 1024 * 1024  # bigger images are not decoded on the GUI thread
FILE_STATES_ACTIVE = ("hashing", "offered", "sending", "receiving", "verifying", "interrupted")


class FileBubble(Bubble):
    """A bubble for a file: thumbnail (PNG/GIF) or file icon, name, size and a line that
    follows the transfer: Accept/Decline, progress bar with speed, Cancel, 'verified'."""

    def build_body(self, wrap):
        self.shown_state = None
        self.bar = None
        self.info_label = None
        self.image = None                         # keep a reference or Tk would discard the picture
        self.top = tk.Frame(self.content, bg=self.fill)
        self.top.pack(fill="x")
        self.dynamic = tk.Frame(self.content, bg=self.fill)
        self.dynamic.pack(fill="x", pady=(px(6), 0))
        self._build_top()
        self._build_dynamic()

    def set_status(self, item):
        self.item = item                          # files show their own state instead of a tick
        if hasattr(self, "status_label"):
            self.status_label.configure(text="")

    def update_item(self, item):
        self.item = item
        if item["extra"].get("state") != self.shown_state:
            self._build_top()                     # e.g. the picture appears once the file is saved
            self._build_dynamic()
        else:
            self.update_progress()

    # ---- top part: picture / icon, name, size --------------------------------------
    def _build_top(self):
        for child in self.top.winfo_children():
            child.destroy()
        extra = self.item["extra"]
        name, size, path = extra.get("name", "file"), extra.get("size", 0), extra.get("path")
        visual = self._thumbnail(name, size, path) or self._file_icon(name)
        visual.pack(side="left", padx=(0, px(10)))
        text = tk.Frame(self.top, bg=self.fill)
        text.pack(side="left", fill="x")
        tk.Label(text, text=name, font=FONT_BUBBLE_NAME, bg=self.fill, fg=self.fg, anchor="w",
                 wraplength=px(200), justify="left").pack(fill="x")
        tk.Label(text, text=human_size(size), font=FONT_SMALL, bg=self.fill, fg=self.muted,
                 anchor="w").pack(fill="x")
        if path and extra.get("state") == "verified":
            # clicking a finished file opens its folder
            for widget in (self.top, visual, text, *text.winfo_children()):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda e: self.view.file_action("open", self.item))
        self.bind_menu_to_new_children()

    def _thumbnail(self, name, size, path):
        """A small picture for PNG/GIF files (tkinter can read only these without extra libraries)."""
        extra_ok = self.item["direction"] == "out" or self.item["extra"].get("state") == "verified"
        if not (path and extra_ok and name.lower().endswith((".png", ".gif")) and size <= THUMB_MAX_FILE
                and os.path.isfile(path)):
            return None
        try:
            image = tk.PhotoImage(file=path)
            factor = max(1, math.ceil(max(image.width(), image.height()) / px(THUMB_MAX_PIXELS)))
            self.image = image.subsample(factor) if factor > 1 else image
        except tk.TclError:
            return None                           # damaged picture: show the icon instead
        return tk.Label(self.top, image=self.image, bg=self.fill, bd=0)

    def _file_icon(self, name):
        """A little page with a folded corner and the file extension, drawn on a canvas."""
        w, h = px(40), px(48)
        icon = tk.Canvas(self.top, width=w, height=h, bg=self.fill, highlightthickness=0, bd=0)
        paper, edge = ("#FFFFFF", "#DDE2E8") if self.mine else (self.t["chat_bg"], self.t["card_border"])
        fold = px(12)
        icon.create_polygon(2, 2, w - fold, 2, w - 2, fold, w - 2, h - 2, 2, h - 2, fill=paper, outline=edge)
        icon.create_polygon(w - fold, 2, w - fold, fold, w - 2, fold, fill=edge, outline=edge)
        extension = os.path.splitext(name)[1][1:5].upper() or "FILE"
        icon.create_text(w // 2, h - px(14), text=extension, font=(FONT_SMALL[0], 8, "bold"),
                         fill=self.t["accent"])
        return icon

    # ---- bottom part: depends on the transfer state ---------------------------------------
    def _build_dynamic(self):
        for child in self.dynamic.winfo_children():
            child.destroy()
        extra = self.item["extra"]
        state, mine = extra.get("state", "failed"), self.mine
        peer = self.item["chat"][2:] if self.item["chat"].startswith("u:") else self.item["sender"]
        self.shown_state, self.bar, self.info_label = state, None, None
        if state == "hashing":
            self._line("Preparing the file (calculating its SHA-256)...")
            self._button("Cancel", "cancel")
        elif state == "offered" and mine:
            self._line("Waiting for %s to accept..." % peer)
            self._button("Cancel", "cancel")
        elif state == "offered":
            self._line("%s wants to send you this file." % self.item["sender"])
            row = tk.Frame(self.dynamic, bg=self.fill)
            row.pack(anchor="w", pady=(px(4), 0))
            ttk.Button(row, text="Accept", style="Send.TButton", cursor="hand2",
                       command=lambda: self.view.file_action("accept", self.item)).pack(side="left")
            ttk.Button(row, text="Decline", style="Cancel.TButton", cursor="hand2",
                       command=lambda: self.view.file_action("decline", self.item)).pack(side="left", padx=px(6))
        elif state in ("sending", "receiving", "verifying"):
            self.bar = ttk.Progressbar(self.dynamic, maximum=max(extra.get("size", 1), 1), length=px(220),
                                       style=("Mine" if mine else "Their") + ".Horizontal.TProgressbar")
            self.bar.pack(anchor="w", pady=(px(2), px(3)))
            self.info_label = self._line("")
            self.update_progress()
            if state != "verifying":
                self._button("Cancel", "cancel")
        elif state == "interrupted":
            self._line("Interrupted - continues automatically when %s is back" % peer)
            self._button("Cancel", "cancel")
        elif state == "verified":
            where = "sent" if mine else "saved - click to open the folder"
            self._line("✓ verified (SHA-256 matches), %s" % where)
        elif state == "failed":
            self._line("Failed: %s" % (extra.get("reason") or "unknown error"), error=True)
        elif state == "declined":
            self._line("Declined")
        elif state == "cancelled":
            self._line("Cancelled")
        self.bind_menu_to_new_children()

    def _line(self, text, error=False):
        label = tk.Label(self.dynamic, text=text, font=FONT_SMALL, bg=self.t["err"] if error else self.fill,
                         fg="#FFFFFF" if error else self.fg, anchor="w", justify="left", wraplength=px(260))
        label.pack(anchor="w")
        return label

    def _button(self, text, action):
        ttk.Button(self.dynamic, text=text, style="Cancel.TButton", cursor="hand2",
                   command=lambda: self.view.file_action(action, self.item)).pack(anchor="w", pady=(px(4), 0))

    def update_progress(self):
        """Update the bar and the 'x of y - speed' line (called up to 10 times a second)."""
        if self.bar is None:
            return
        done, speed = self.view.progress.get(self.item["id"], (0, 0.0))
        size = self.item["extra"].get("size", 0)
        if self.shown_state == "verifying":
            done = size
        self.bar.configure(value=done)
        if self.info_label is not None:
            text = "%s of %s" % (human_size(done), human_size(size))
            if speed > 0 and self.shown_state != "verifying":
                text += "  -  %s/s" % human_size(speed)
            if self.shown_state == "verifying":
                text += "  -  verifying..."
            self.info_label.configure(text=text)


# ======================================================================
# The scrolling message area
# ======================================================================
class ChatView(ScrollFrame):
    """The scrolling message area: bubbles, centered system messages and hints."""

    def __init__(self, parent, t):
        super().__init__(parent, t["chat_bg"], "Chat.Vertical.TScrollbar")
        self.t = t
        self.bubbles = {}            # message id -> Bubble
        self.hint = None
        self.wrap = px(300)
        self.bubble_class = {"text": Bubble, "file": FileBubble}
        self.progress = {}           # file id -> (bytes done, bytes per second); filled by gui.py
        # gui.py plugs its own functions in here:
        self.retry_callback = lambda msg_id: None
        self.delete_callback = lambda item: None
        self.file_action = lambda action, item: None     # 'accept' | 'decline' | 'cancel' | 'open'

    def set_theme(self, t):
        self.t = t
        self.set_bg(t["chat_bg"])

    def width_changed(self, width):
        # Re-wrap long messages whenever the window is resized.
        self.wrap = max(px(160), int(width * BUBBLE_MAX_FRACTION) - 2 * px(BUBBLE_PAD_X) - px(20))
        for bubble in self.bubbles.values():
            bubble.set_wrap(self.wrap)

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.bubbles = {}
        self.hint = None

    def _new_row(self):
        self._remove_hint()
        row = tk.Frame(self.inner, bg=self.t["chat_bg"])
        row.pack(fill="x", padx=px(16), pady=px(4))
        return row

    def _remove_hint(self):
        if self.hint is not None:
            self.hint.destroy()
            self.hint = None

    def add_item(self, item, scroll=True, force_scroll=False):
        """Draw one stored message (text, file or system line)."""
        if item["direction"] == "system":
            return self.add_system(item["text"], item["id"], scroll)
        # Decide BEFORE adding: if the user already scrolled up we must not yank them down.
        follow = scroll and (force_scroll or item["direction"] == "out" or self.at_bottom())
        row = self._new_row()
        bubble = self.bubble_class.get(item["kind"], Bubble)(row, self.t, item, self.wrap, self)
        bubble.pack(side="right" if item["direction"] == "out" else "left")
        bubble.bind_menu_to_new_children()
        self.bubbles[item["id"]] = bubble
        if follow:
            self.scroll_to_bottom()

    def add_system(self, text, item_id=None, scroll=True):
        follow = scroll and self.at_bottom()
        row = self._new_row()
        label = tk.Label(row, text=text, font=FONT_SMALL, bg=self.t["chat_bg"], fg=self.t["system_fg"])
        label.pack()
        if follow:
            self.scroll_to_bottom()

    def update_item(self, item):
        """A stored message changed (delivery status, file progress): redraw just that bubble."""
        bubble = self.bubbles.get(item["id"])
        if bubble is not None:
            bubble.set_status(item)
            if hasattr(bubble, "update_item"):
                bubble.update_item(item)

    def update_progress(self, fid):
        bubble = self.bubbles.get(fid)
        if bubble is not None and hasattr(bubble, "update_progress"):
            bubble.update_progress()

    def remove_item(self, message_id):
        bubble = self.bubbles.pop(message_id, None)
        if bubble is not None:
            bubble.master.destroy()          # the row frame around it

    def show_hint(self, text):
        """A big grey centered text for empty states ('Select a peer ...')."""
        self._remove_hint()
        self.hint = tk.Label(self.inner, text=text, font=FONT_BODY, bg=self.t["chat_bg"],
                             fg=self.t["muted"], justify="center", wraplength=px(360))
        self.hint.pack(pady=px(80))

    def show_menu(self, event, item):
        """Right-click menu on a bubble: copy, delete (only on this computer), open folder."""
        menu = tk.Menu(self, tearoff=False)
        if item["kind"] == "text":
            menu.add_command(label="Copy message", command=lambda: self._copy(item["text"]))
        if item["kind"] == "file" and item["extra"].get("path"):
            menu.add_command(label="Open folder", command=lambda: self.file_action("open", item))
        if item["direction"] == "out" and item["status"] == "failed":
            menu.add_command(label="Retry", command=lambda: self.retry_callback(item["id"]))
        menu.add_separator()
        menu.add_command(label="Delete message (only here)", command=lambda: self.delete_callback(item))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)


# ======================================================================
# Sidebar row
# ======================================================================
class PeerRow(tk.Frame):
    """[avatar] name            [badge]
                last message (small grey)

    `entry` is a dict: key, name, sub (the grey line), online, unread, kind ('user' | 'group'),
    typing (True while that person is typing).
    """

    def __init__(self, parent, t, entry, selected, on_click):
        self.t = t
        self.selected = selected
        name, online, unread = entry["name"], entry["online"], entry["unread"]
        super().__init__(parent, height=px(ROW_HEIGHT), cursor="hand2")
        self.pack_propagate(False)                  # keep the fixed row height

        self.strip = tk.Frame(self, width=px(4))    # teal bar on the left of the selected row
        self.strip.pack(side="left", fill="y")

        avatar = tk.Canvas(self, width=px(AVATAR_SIZE), height=px(AVATAR_SIZE),
                           highlightthickness=0, bd=0)
        color = avatar_color(name) if online else t["side_thumb"]
        avatar.create_oval(1, 1, px(AVATAR_SIZE) - 1, px(AVATAR_SIZE) - 1, fill=color, outline=color)
        letter = "#" if entry["kind"] == "group" else name[:1].upper()
        avatar.create_text(px(AVATAR_SIZE) // 2, px(AVATAR_SIZE) // 2, text=letter,
                           fill="#FFFFFF", font=FONT_NAME)
        avatar.pack(side="left", padx=(px(10), px(10)))

        self.text_box = tk.Frame(self)
        self.text_box.pack(side="left", fill="x", expand=True)
        self.name_label = tk.Label(self.text_box, text=name, font=FONT_NAME, anchor="w",
                                   fg=t["side_fg"] if online else t["side_muted"])
        self.name_label.pack(fill="x")
        sub = entry["sub"]
        if len(sub) > 24:
            sub = sub[:23] + "…"               # one line only: the row has a fixed height
        self.ip_label = tk.Label(self.text_box, text=sub, font=FONT_SMALL, anchor="w",
                                 fg=t["accent_hover"] if entry.get("typing") else t["side_muted"])
        self.ip_label.pack(fill="x")

        self.parts = [self, self.text_box, self.name_label, self.ip_label, avatar]
        self.badge = None
        if unread:
            self._make_badge(unread)
            self.parts.append(self.badge)
        self._paint(self._base_bg())

        everything = [self, self.strip, avatar, self.text_box, self.name_label, self.ip_label]
        if self.badge is not None:
            everything.append(self.badge)
        for widget in everything:
            widget.bind("<Button-1>", lambda e: on_click(entry["key"]))
            widget.bind("<Enter>", lambda e: self._paint(t["side_hover"] if not selected else t["side_sel"]))
            widget.bind("<Leave>", lambda e: self._paint(self._base_bg()))

    def _make_badge(self, count):
        size = px(22)
        self.badge = tk.Canvas(self, width=size + px(6), height=size, highlightthickness=0,
                               bd=0, cursor="hand2")
        rounded_rect(self.badge, 1, 1, size + px(6) - 1, size - 1, size // 2,
                     fill=self.t["badge_bg"], outline=self.t["badge_bg"])
        self.badge.create_text((size + px(6)) // 2, size // 2, text=str(count) if count < 100 else "99+",
                               fill=self.t["badge_fg"], font=FONT_SMALL)
        self.badge.pack(side="right", padx=px(10))

    def _base_bg(self):
        return self.t["side_sel"] if self.selected else self.t["side_bg"]

    def _paint(self, bg):
        """Give the whole row one background colour (used for selected / hover / normal)."""
        for widget in self.parts:
            widget.configure(bg=bg)
        self.strip.configure(bg=self.t["accent"] if self.selected else bg)
