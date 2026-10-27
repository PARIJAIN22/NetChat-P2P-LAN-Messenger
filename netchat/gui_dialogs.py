"""
gui_dialogs.py - the small windows that open on top of the main window:
new group, (later) settings, network info and the emoji picker.

All of them take the CURRENT colour dict `t`, so they match light and dark mode.
"""

import os
import time
import tkinter as tk
from tkinter import filedialog, ttk

from . import config
from .osmonitor import ProcessMonitor
from .gui_widgets import FONT_BODY, FONT_MONO, FONT_NAME, FONT_SMALL, ScrollFrame, px, validate_name
from .settings import valid_port


class ThemedDialog(tk.Toplevel):
    """A modal window: the main window waits until it is closed. `self.result` is what ask() returns."""

    def __init__(self, root, t, title):
        super().__init__(root)
        self.root = root
        self.t = t
        self.result = None
        self.title(title)
        self.configure(bg=t["card_bg"])
        self.resizable(False, False)
        self.transient(root)
        self.body = tk.Frame(self, bg=t["card_bg"])
        self.body.pack(fill="both", expand=True, padx=px(22), pady=px(18))
        self.bind("<Escape>", lambda e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    # ---- small building blocks ------------------------------------------------------
    def label(self, text, bold=False, muted=False, wrap=0, pady=(0, 0)):
        label = tk.Label(self.body, text=text, font=FONT_NAME if bold else (FONT_SMALL if muted else FONT_BODY),
                         bg=self.t["card_bg"], fg=self.t["muted"] if muted else self.t["text"], anchor="w",
                         justify="left", wraplength=wrap)
        label.pack(fill="x", pady=pady)
        return label

    def make_entry(self, parent, variable, show=""):
        """A text field with a 1 px border that turns teal while it has the focus.
        Returns (border frame, entry); the caller decides where to pack the border frame."""
        border = tk.Frame(parent, bg=self.t["input_border"], padx=1, pady=1)
        inner = tk.Frame(border, bg=self.t["input_bg"])      # the padding must be INSIDE the border
        inner.pack(fill="x")
        field = tk.Entry(inner, textvariable=variable, font=FONT_BODY, relief="flat", bd=0, show=show,
                         bg=self.t["input_bg"], fg=self.t["text"], insertbackground=self.t["text"])
        field.pack(fill="x", ipady=px(5), padx=px(8))
        field.bind("<FocusIn>", lambda e: border.configure(bg=self.t["input_focus"]))
        field.bind("<FocusOut>", lambda e: border.configure(bg=self.t["input_border"]))
        return border, field

    def entry(self, variable, show=""):
        border, field = self.make_entry(self.body, variable, show)
        border.pack(fill="x", pady=(px(4), px(8)))
        return field

    def error_label(self):
        return tk.Label(self.body, text="", font=FONT_SMALL, bg=self.t["card_bg"], fg=self.t["err"],
                        anchor="w", justify="left", wraplength=px(340))

    def buttons(self, ok_text, on_ok, cancel_text="Cancel"):
        row = tk.Frame(self.body, bg=self.t["card_bg"])
        row.pack(fill="x", pady=(px(12), 0))
        ttk.Button(row, text=ok_text, style="Send.TButton", cursor="hand2", command=on_ok).pack(side="right")
        ttk.Button(row, text=cancel_text, style="Cancel.TButton", cursor="hand2",
                   command=self.cancel).pack(side="right", padx=(0, px(8)))

    # ---- showing it ---------------------------------------------------------------------
    def ask(self):
        """Centre over the main window, wait until closed, return self.result."""
        self.update_idletasks()
        w, h = max(self.winfo_reqwidth(), px(380)), self.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        self.geometry("%dx%d+%d+%d" % (w, h, max(x, 0), max(y, 0)))
        self.grab_set()
        self.wait_window(self)
        return self.result

    def cancel(self):
        self.result = None
        self.destroy()


class NewGroupDialog(ThemedDialog):
    """Choose a group name and its members. result = (name, [members]) or None."""

    def __init__(self, root, t, candidates):
        super().__init__(root, t, "New group")
        self.label("New group", bold=True)
        self.label("Messages go to every member. Members who are offline get them when they are back.",
                   muted=True, wrap=px(340), pady=(0, px(8)))
        self.label("Group name")
        self.name_var = tk.StringVar()
        self.name_entry = self.entry(self.name_var)

        self.label("Members")
        self.checks = {}
        frame = tk.Frame(self.body, bg=t["chat_bg"], height=px(150))
        frame.pack(fill="x", pady=(px(4), px(2)))
        frame.pack_propagate(False)
        scroll = ScrollFrame(frame, t["chat_bg"], "Chat.Vertical.TScrollbar", reserve_bar=False)
        scroll.pack(fill="both", expand=True)
        for name in candidates:
            var = tk.BooleanVar(value=False)
            self.checks[name] = var
            tk.Checkbutton(scroll.inner, text=name, variable=var, font=FONT_BODY, anchor="w", bg=t["chat_bg"],
                           fg=t["text"], selectcolor=t["input_bg"], activebackground=t["chat_bg"],
                           activeforeground=t["text"], cursor="hand2", bd=0, highlightthickness=0
                           ).pack(fill="x", padx=px(8), pady=px(1))
        if not candidates:
            tk.Label(scroll.inner, text="Nobody to add yet - wait until peers show up.", font=FONT_SMALL,
                     bg=t["chat_bg"], fg=t["muted"]).pack(padx=px(8), pady=px(8))
        self.error = self.error_label()
        self.error.pack(fill="x")
        self.buttons("Create group", self._ok)
        self.bind("<Return>", lambda e: self._ok())
        self.after(100, self.name_entry.focus_set)

    def _ok(self):
        name = " ".join(self.name_var.get().split())
        members = [n for n, var in self.checks.items() if var.get()]
        if not name:
            self.error.configure(text="Please give the group a name.")
        elif len(name) > 40:
            self.error.configure(text="The name may have at most 40 characters.")
        elif not members:
            self.error.configure(text="Choose at least one member.")
        else:
            self.result = (name, members)
            self.destroy()


# ======================================================================
# Settings
# ======================================================================
class SettingsDialog(ThemedDialog):
    """Username, ports, download folder, sound, dark mode and passphrase.
    result = a dict with the new values, or None if cancelled."""

    def __init__(self, root, t, current, default_folder):
        super().__init__(root, t, "Settings")
        self.current = current
        self.default_folder = default_folder
        self.label("Settings", bold=True, pady=(0, px(6)))

        self.name_var = tk.StringVar(value=current["username"])
        self.tcp_var = tk.StringVar(value=str(current["tcp_port"]))
        self.udp_var = tk.StringVar(value=str(current["udp_port"]))
        self.folder_var = tk.StringVar(value=current["download_dir"] or default_folder)
        self.pass_var = tk.StringVar(value=current["passphrase"])
        self.sound_var = tk.BooleanVar(value=current["sound"])
        self.dark_var = tk.BooleanVar(value=current["dark_mode"])

        self.label("Username")
        self.entry(self.name_var)
        ports = tk.Frame(self.body, bg=t["card_bg"])
        ports.pack(fill="x")
        for column, (text, var) in enumerate((("TCP port (messages)", self.tcp_var), ("UDP port (discovery)", self.udp_var))):
            cell = tk.Frame(ports, bg=t["card_bg"])
            cell.grid(row=0, column=column, sticky="ew", padx=(0, px(10)) if column == 0 else 0)
            ports.columnconfigure(column, weight=1)
            tk.Label(cell, text=text, font=FONT_BODY, bg=t["card_bg"], fg=t["text"], anchor="w").pack(fill="x")
            border, _ = self.make_entry(cell, var)
            border.pack(fill="x", pady=(px(4), px(8)))

        self.label("Download folder")
        row = tk.Frame(self.body, bg=t["card_bg"])
        row.pack(fill="x")
        border, _ = self.make_entry(row, self.folder_var)
        border.pack(side="left", fill="x", expand=True, pady=(px(4), px(8)))
        ttk.Button(row, text="Browse...", style="Cancel.TButton", cursor="hand2",
                   command=self._browse).pack(side="left", padx=(px(8), 0))

        self.label("Shared passphrase (optional)")
        self.entry(self.pass_var, show="\u2022")
        self.label("Only peers with the same passphrase can talk to you. It is saved in settings.json as "
                   "plain text, so do not reuse a real password.", muted=True, wrap=px(380), pady=(0, px(8)))

        for text, var in (("Beep when a message arrives in a chat I am not looking at", self.sound_var),
                          ("Dark mode", self.dark_var)):
            tk.Checkbutton(self.body, text=text, variable=var, font=FONT_BODY, anchor="w", bg=t["card_bg"],
                           fg=t["text"], selectcolor=t["input_bg"], activebackground=t["card_bg"],
                           activeforeground=t["text"], cursor="hand2", bd=0, highlightthickness=0
                           ).pack(fill="x")
        self.label("Username and ports take effect after a restart; everything else applies at once.",
                   muted=True, wrap=px(380), pady=(px(8), 0))
        self.error = self.error_label()
        self.error.pack(fill="x", pady=(px(4), 0))
        self.buttons("Save", self._ok)
        self.bind("<Return>", lambda e: self._ok())

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.folder_var.get() or self.default_folder)
        if folder:
            self.folder_var.set(os.path.normpath(folder))

    def _ok(self):
        name = self.name_var.get().strip()
        problem = validate_name(name, config.MAX_NAME_LEN)
        tcp, udp = valid_port(self.tcp_var.get()), valid_port(self.udp_var.get())
        folder = self.folder_var.get().strip()
        if problem:
            self.error.configure(text="Username: " + problem)
        elif tcp is None or udp is None:
            self.error.configure(text="Ports must be numbers between 1024 and 65535.")
        elif tcp == udp:
            self.error.configure(text="The TCP and UDP port must be different.")
        else:
            try:
                os.makedirs(folder or self.default_folder, exist_ok=True)
            except OSError as e:
                self.error.configure(text="Cannot use that download folder: %s" % e)
                return
            self.result = {
                "username": name, "tcp_port": tcp, "udp_port": udp,
                "download_dir": "" if folder == self.default_folder else folder,
                "sound": self.sound_var.get(), "dark_mode": self.dark_var.get(),
                "passphrase": self.pass_var.get()}
            self.destroy()


# ======================================================================
# Network info
# ======================================================================
class NetworkInfoDialog(ThemedDialog):
    """My addresses and the table of peers with last-seen time and round-trip time.

    Not modal: it stays open and refreshes itself every second, so you can watch the RTT change
    (and copy everything as text for a report)."""

    COLUMNS = (("name", "Peer", 110), ("address", "Address", 150), ("seen", "Last seen", 80),
               ("rtt", "RTT", 80), ("lock", "Encrypted", 80), ("fp", "Key fingerprint", 190))

    def __init__(self, root, t, app):
        super().__init__(root, t, "Network info")
        self.app = app
        self.resizable(True, False)
        self.facts = tk.Label(self.body, text="", font=FONT_MONO, bg=t["card_bg"], fg=t["text"], anchor="w",
                              justify="left")
        self.facts.pack(fill="x")
        self.label("Peers on the network", bold=True, pady=(px(12), px(4)))
        self.tree = ttk.Treeview(self.body, columns=[c[0] for c in self.COLUMNS], show="headings", height=6)
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=px(width), anchor="w")
        self.tree.pack(fill="x")
        self.label("RTT = round-trip time of a PING/PONG frame, measured every %d seconds over the TCP "
                   "connection." % config.PING_INTERVAL, muted=True, wrap=px(780), pady=(px(6), 0))
        row = tk.Frame(self.body, bg=t["card_bg"])
        row.pack(fill="x", pady=(px(10), 0))
        ttk.Button(row, text="Close", style="Send.TButton", cursor="hand2", command=self.cancel).pack(side="right")
        ttk.Button(row, text="Copy as text", style="Cancel.TButton", cursor="hand2",
                   command=self._copy).pack(side="right", padx=(0, px(8)))
        self.refresh()

    def show(self):
        """Open without blocking the main window."""
        self.update_idletasks()
        x = self.root.winfo_rootx() + px(60)
        y = self.root.winfo_rooty() + px(60)
        self.geometry("+%d+%d" % (x, y))

    def _facts_text(self):
        info, peer = self.app.net_info, self.app.peer
        lines = ["My IP address      %s" % peer.my_ip]
        if info is None:
            lines.append("Subnet             detecting...")
        else:
            how = "" if info["source"] == "adapter" else "   (assumed: could not read the network card)"
            lines += ["Subnet mask        %s  (/%d)%s" % (info["netmask"], info["prefix"], how),
                      "Network address    %s" % info["network"],
                      "Subnet broadcast   %s" % info["directed_broadcast"],
                      "HELLO is sent to   %s  (limited broadcast, UDP port %d)" % (info["discovery_broadcast"], peer.udp_port)]
        lines.append("TCP / UDP ports    %d / %d" % (peer.tcp_port, peer.udp_port))
        lines.append("Encryption         %s" % ("AES-256-GCM, my fingerprint " + peer.fingerprint if peer.fingerprint
                                                else "off (unavailable)"))
        lines.append("Shared passphrase  %s" % ("set" if peer.passphrase else "not set"))
        return "\n".join(lines)

    def _rows(self):
        now = time.time()
        rows = []
        for s in self.app.peer.peer_stats():
            rows.append((s["name"], "%s:%d" % (s["ip"], s["port"]), "%.0f s ago" % (now - s["last_seen"]),
                         "%.1f ms" % (s["rtt"] * 1000) if s["rtt"] is not None else "measuring...",
                         "yes" if s["encrypted"] else "no", s["fingerprint"] or "-"))
        return rows

    def refresh(self):
        if not self.winfo_exists():
            return
        self.facts.configure(text=self._facts_text())
        self.tree.delete(*self.tree.get_children())
        for row in self._rows():
            self.tree.insert("", "end", values=row)
        self.after(1000, self.refresh)

    def _copy(self):
        text = self._facts_text() + "\n\n" + "\n".join("  ".join(str(cell).ljust(18) for cell in row) for row in
                                                      [tuple(c[1] for c in self.COLUMNS)] + self._rows())
        self.clipboard_clear()
        self.clipboard_append(text)


class SystemMonitorDialog(ThemedDialog):
    """What this NetChat process costs the OS: CPU %, RAM, threads, TCP connections.
    Not modal, refreshes every second (same pattern as the Network info window)."""

    def __init__(self, root, t, app):
        super().__init__(root, t, "System monitor")
        self.app = app
        self.monitor = ProcessMonitor()
        self.facts = tk.Label(self.body, text="", font=FONT_MONO, bg=t["card_bg"], fg=t["text"], anchor="w",
                              justify="left")
        self.facts.pack(fill="x")
        self.label("CPU %% is the share of all %d cores, measured over the last second." % self.monitor.cores,
                   muted=True, wrap=px(420), pady=(px(6), 0))
        row = tk.Frame(self.body, bg=t["card_bg"])
        row.pack(fill="x", pady=(px(10), 0))
        ttk.Button(row, text="Close", style="Send.TButton", cursor="hand2", command=self.cancel).pack(side="right")
        self.refresh()

    def show(self):
        self.update_idletasks()
        self.geometry("+%d+%d" % (self.root.winfo_rootx() + px(80), self.root.winfo_rooty() + px(80)))

    @staticmethod
    def format(s):
        ram = "%.1f MB" % (s["ram"] / 1024 ** 2) if s["ram"] is not None else "unknown"
        return "\n".join(["Process ID          %d" % s["pid"],
                          "CPU                 %.1f %%" % s["cpu"],
                          "RAM (resident)      %s" % ram,
                          "Threads             %d" % s["threads"],
                          "TCP connections     %d  (%d dialed, %d accepted)"
                          % (s["outgoing"] + s["incoming"], s["outgoing"], s["incoming"])])

    def refresh(self):
        if not self.winfo_exists():
            return
        self.facts.configure(text=self.format(self.monitor.sample(self.app.peer)))
        self.after(1000, self.refresh)


# ======================================================================
# Emoji picker
# ======================================================================
EMOJIS = [
    "\U0001F600", "\U0001F603", "\U0001F604", "\U0001F601", "\U0001F606", "\U0001F605", "\U0001F602", "\U0001F923",
    "\U0001F60A", "\U0001F607", "\U0001F642", "\U0001F609", "\U0001F60D", "\U0001F618", "\U0001F60B", "\U0001F60E",
    "\U0001F914", "\U0001F610", "\U0001F634", "\U0001F622", "\U0001F62D", "\U0001F621", "\U0001F631", "\U0001F917",
    "\U0001F644", "\U0001F62C", "\U0001F91D", "\U0001F44D", "\U0001F44E", "\U0001F44F", "\U0001F64F", "\U0001F4AA",
    "\U0001F44B", "\u2764", "\U0001F494", "\U0001F525", "\U0001F389", "\u2705", "\u274C", "\u2B50",
]


class EmojiPicker(tk.Toplevel):
    """A small window with 40 common emojis. Clicking one inserts it and closes the window."""

    COLUMNS = 8

    def __init__(self, root, t, on_pick, anchor):
        super().__init__(root)
        self.title("Emoji")
        self.configure(bg=t["card_bg"])
        self.resizable(False, False)
        self.transient(root)
        self.attributes("-topmost", True)
        for index, emoji in enumerate(EMOJIS):
            label = tk.Label(self, text=emoji, font=("Segoe UI Emoji", 16), bg=t["card_bg"], fg=t["text"],
                             cursor="hand2", width=2, pady=px(2))
            label.grid(row=index // self.COLUMNS, column=index % self.COLUMNS, padx=px(2), pady=px(2))
            label.bind("<Button-1>", lambda e, value=emoji: self._pick(on_pick, value))
            label.bind("<Enter>", lambda e, w=label: w.configure(bg=t["chat_bg"]))
            label.bind("<Leave>", lambda e, w=label: w.configure(bg=t["card_bg"]))
        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        # open just above the button that was clicked
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() - self.winfo_reqheight() - px(8)
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        self.focus_set()

    def _pick(self, on_pick, emoji):
        self.destroy()
        on_pick(emoji)
