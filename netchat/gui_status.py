"""
gui_status.py - Status bar, network log panel, sound and the blinking window title.
"""

import os
import time
import tkinter as tk
from tkinter import ttk

try:
    import winsound                      # Windows only; used for the "new message" beep
except ImportError:
    winsound = None

from .gui_common import FLASH_MS, MIN_CHAT_HEIGHT, NOTICE_SECONDS
from .gui_widgets import FONT_MONO, FONT_SMALL, LOG_LINES, MAX_LOG_LINES, px


class StatusMixin:
    """Status bar, network log panel, sound and the blinking window title. (mixed into ChatApp in gui.py)"""

    def _build_log_panel(self):
        self.log_panel = self._themed(tk.Frame(self.root), bg="log_bg")
        self.log_panel.columnconfigure(0, weight=1)
        self._themed(tk.Label(self.log_panel, text="Network log  -  also saved in %s" % os.path.basename(self.log.filename),
                              font=FONT_SMALL, anchor="w"),
                     bg="status_bg", fg="status_fg").grid(row=0, column=0, columnspan=2, sticky="ew",
                                                          ipadx=px(10), ipady=px(2))
        self.log_box = tk.Text(self.log_panel, height=LOG_LINES, state="disabled", font=FONT_MONO,
                               relief="flat", bd=0, wrap="none", padx=px(10), pady=px(4),
                               highlightthickness=0)
        bar = ttk.Scrollbar(self.log_panel, orient="vertical", style="Log.Vertical.TScrollbar",
                            command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=bar.set)
        self.log_box.grid(row=1, column=0, sticky="ew")
        bar.grid(row=1, column=1, sticky="ns")

    def _build_status_bar(self, args):
        self.ports_text = "Listening on TCP %d, UDP %d" % (args.tcp_port, args.udp_port)
        bar = self._themed(tk.Frame(self.root), bg="status_bg")
        bar.grid(row=3, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        self.status = self._themed(tk.Label(bar, text="", font=FONT_SMALL, anchor="w"),
                                   bg="status_bg", fg="status_fg")
        self.status.grid(row=0, column=0, sticky="ew", padx=px(14), pady=px(3))
        ttk.Button(bar, text="Network info", style="Plain.TButton", cursor="hand2",
                   command=self._open_network_info).grid(row=0, column=1, padx=(0, px(4)))
        self.log_toggle = ttk.Button(bar, text="Network log", style="Plain.TButton",
                                     cursor="hand2", command=self._toggle_log)
        self.log_toggle.grid(row=0, column=2, padx=(0, px(8)))
        self._update_status_bar()

    def _append_log(self, line):
        if any(word in line for word in ("failed", "Ignored", "Dropping", "Could not", "Rejected")):
            tag = "warn"
        elif "Message" in line:
            tag = "msg"
        else:
            tag = "normal"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n", tag)
        extra = int(self.log_box.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log_box.delete("1.0", "%d.0" % (extra + 1))   # keep the panel from growing forever
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_notice(self, text):
        self.notice = (text, time.time() + NOTICE_SECONDS)
        self._update_status_bar()
        self.root.after(NOTICE_SECONDS * 1000 + 100, self._update_status_bar)

    def _update_status_bar(self):
        text, until = self.notice
        if text and time.time() < until:
            self.status.configure(text=text)
            return
        count = len(self.peer.peer_names())
        queued = self.db.outbox_count()
        extra = "  |  %d message%s waiting to be delivered" % (queued, "" if queued == 1 else "s") if queued else ""
        self.status.configure(text="%d peer%s online  |  %s%s" % (count, "" if count == 1 else "s",
                                                                  self.ports_text, extra))

    def _toggle_log(self):
        self.log_var.set(not self.log_var.get())
        self._apply_log_visibility()

    def _apply_log_visibility(self):
        if self.log_var.get():
            self.log_panel.grid(row=2, column=0, sticky="ew")
            self.log_box.see("end")
            self._make_room_for_log()
        else:
            self.log_panel.grid_remove()
            self._give_back_log_room()

    def _make_room_for_log(self):
        """Grow a small window by as much as needed so the panel does not squash the chat."""
        if self.root.state() != "normal":
            return                                   # maximised windows already have their size
        self.root.update_idletasks()
        grow = max(0, px(MIN_CHAT_HEIGHT) - self.chat.winfo_height())
        if grow:
            self.root.geometry("%dx%d" % (self.root.winfo_width(), self.root.winfo_height() + grow))
        self.log_grew = grow

    def _give_back_log_room(self):
        if self.log_grew and self.root.state() == "normal":
            self.root.geometry("%dx%d" % (self.root.winfo_width(),
                                          self.root.winfo_height() - self.log_grew))
        self.log_grew = 0

    def _beep(self):
        if not self.sound:
            return                       # switched off in Settings
        try:
            winsound.MessageBeep()
        except Exception:               # no winsound (not Windows) or no sound device: just stay silent
            pass

    def _start_flash(self):
        self.alerts += 1
        if self.flash_job is None:
            self._flash_tick(True)

    def _flash_tick(self, show_alert):
        self.root.title("(%d) New message" % self.alerts if show_alert else self.base_title)
        self.flash_job = self.root.after(FLASH_MS, lambda: self._flash_tick(not show_alert))

    def _stop_flash(self):
        if self.flash_job is not None:
            self.root.after_cancel(self.flash_job)
            self.flash_job = None
            self.root.title(self.base_title)
        self.alerts = 0
