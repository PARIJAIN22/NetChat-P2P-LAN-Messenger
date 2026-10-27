"""
gui.py - the tkinter window: the heart of it (events from the network, messages, the sidebar).

IMPORTANT RULE: tkinter is NOT thread-safe. Our network threads must never
touch widgets. Instead they put small "events" into a queue.Queue, and the GUI
thread empties that queue every 100 ms using root.after(). That is the only
bridge between the network threads and the window.

Layers:   gui*.py (windows)  ->  peer.py (network, no widgets)  +  storage.py (sqlite)
The window never touches a socket; the network layer never touches a widget.

To keep every file short, ChatApp is built from "mixins", one file per topic:
    gui_layout.py    building the window, colour themes
    gui_status.py    status bar, network log panel, sound, blinking title
    gui_files.py     file transfer (paperclip, progress, Accept / Decline)
    gui_groups.py    group chats and the fingerprint
    gui_settings.py  settings, network info, emoji picker, restart
and the drawing code (bubbles, peer rows, dialogs) is in gui_chatview.py, gui_dialogs.py, gui_widgets.py.
"""

import ctypes
import os
import queue
import threading
import time
import tkinter as tk
import uuid
from tkinter import messagebox

from . import config, netinfo, settings
from .gui_chatview import PeerRow
from .gui_common import EMPTY_PEERS_TEXT, EVERYONE, HISTORY_LIMIT, group_chat, user_chat
from .gui_files import FilesMixin
from .gui_groups import GroupsMixin
from .gui_layout import LayoutMixin
from .gui_settings import SettingsMixin, restart_arguments       # noqa: F401  (restart_arguments is re-exported)
from .gui_status import StatusMixin
from .gui_widgets import (
    FONT_BODY, LIGHT, SIDEBAR_WIDTH, NameDialog, apply_ttk_styles, make_app_icon, px, set_scale)
from .logger import EventLog
from .peer import Peer
from .storage import Database

POLL_BUDGET = 0.05                       # seconds of event handling per GUI tick (keeps the window responsive)


class ChatApp(LayoutMixin, StatusMixin, FilesMixin, GroupsMixin, SettingsMixin):
    def __init__(self, root, name, args):
        self.root = root
        self.my_name = name
        self.events = queue.Queue()          # network threads -> GUI thread
        self.unread = {}                     # chat key -> number of unread messages
        self.known = set()                   # peers that were online at the last refresh
        self.ips = {}                        # peer name -> last known IP
        self.typing = {}                     # peer name -> time when "is typing" expires
        self.secure = {}                     # peer name -> (encrypted, fingerprint)
        self.listed = []                     # chat keys currently shown in the sidebar, in order
        self.current = None                  # chat key that is open
        self.theme_name = "light"
        self.themed = []                     # (widget, {option: colour key}) re-coloured on theme change
        self.dot_error = False               # status dot: False = green, True = red
        self.notice = ("", 0)                # (temporary status text, time it expires)
        self.alerts = 0                      # unseen messages that make the title blink
        self.flash_job = None
        self.log_grew = 0                    # pixels the window grew when the log panel opened
        self.search_open = False
        self.progress = {}                   # file id -> (bytes done, bytes per second)
        self.net_info = None                 # subnet facts, filled in by a background thread
        self.net_dialog = None
        self.base_title = "NetChat - %s" % name
        self.manual_peers = getattr(args, "peer", None) or []
        self.trust_asked = set()             # (peer, fingerprint) already asked about, so we ask only once

        data_dir = getattr(args, "data_dir", ".")
        os.makedirs(data_dir, exist_ok=True)
        self.settings_path = getattr(args, "settings_path", os.path.join(data_dir, config.SETTINGS_FILE))
        self.settings = getattr(args, "settings", None) or settings.load(self.settings_path)
        self.sound = self.settings["sound"]
        self.db = Database(os.path.join(data_dir, config.DB_FILE), name)
        self.summaries = self.db.chat_summaries()    # chat key -> {'ts', 'preview'} for the sidebar
        self.groups = {g["id"]: g for g in self.db.groups()}    # named groups (Everyone is separate)

        self.log = EventLog("%s:%d" % (name, args.tcp_port), args.log_file)
        self.log.add_listener(lambda line: self.events.put(("log", line)))
        # These callbacks run in worker threads, so they only drop events in the queue.
        self.peer = Peer(
            name, args.tcp_port, args.udp_port, self.log,
            on_peers_changed=lambda: self.events.put(("peers",)),
            on_status=lambda text: self.events.put(("status", text)),
            on_event=lambda kind, info: self.events.put(("peer", kind, info)),
            db=self.db, download_dir=getattr(args, "download_dir", None),
            passphrase=getattr(args, "passphrase", None) or "",
            identity_path=os.path.join(data_dir, "identity.key"),
            encrypt=not getattr(args, "no_encryption", False))

        self._build_widgets(name, args)

    # ==================================================================
    # Start / stop
    # ==================================================================
    def start(self):
        try:
            self.peer.start()
        except OSError as e:
            self.dot_error = True
            self._paint_dot()
            messagebox.showerror(
                "NetChat",
                "Could not start: %s\n\nIs another NetChat already using TCP port %d "
                "or UDP port %d?\nTry --tcp-port / --udp-port." % (e, self.peer.tcp_port, self.peer.udp_port))
            return False
        for address in self.manual_peers:           # --peer IP:PORT on the command line
            self.peer.connect_async(address)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)
        self.input.focus_input()
        # Asking Windows for the subnet mask takes a second: do it in a thread, result via the queue.
        threading.Thread(target=lambda: self.events.put(("netinfo", netinfo.get_network_info(self.peer.my_ip))),
                         daemon=True).start()
        return True

    def _on_close(self):
        self.peer.stop()                     # broadcasts BYE and closes sockets
        self.db.close()
        self.root.destroy()

    # ==================================================================
    # Events coming from the network threads
    # ==================================================================
    def _poll_events(self):
        """Runs in the GUI thread every 100 ms and handles what the network threads queued.

        At most POLL_BUDGET seconds per call, so a flood of events (log lines during a big transfer)
        cannot freeze the window; the rest is handled 10 ms later. One failing handler is logged
        and skipped: before, an exception here stopped the loop and the window never updated again."""
        deadline = time.time() + POLL_BUDGET
        more = False
        try:
            while True:
                if time.time() > deadline:
                    more = True
                    break
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(event)
                except Exception as e:
                    self.log.log("GUI error while handling %r: %r" % (event[:2], e))
        finally:
            self.root.after(10 if more else 100, self._poll_events)

    def _handle_event(self, event):
        kind = event[0]
        if kind == "log":
            self._append_log(event[1])
        elif kind == "status":
            self._on_status(event[1])
        elif kind == "peers":
            self._on_peers_changed()
        elif kind == "netinfo":
            self.net_info = event[1]
        elif kind == "peer":
            # events from peer.py: message, msg_status, typing, secure, rtt ...
            handler = getattr(self, "_pe_" + event[1], None)
            if handler is not None:
                handler(event[2])

    def _on_status(self, text):
        if text.startswith("Broadcast failed"):          # discovery is broken: turn the dot red
            self.dot_error = True
            self._paint_dot()
        self._show_notice(text)

    def _on_peers_changed(self):
        online = set(self.peer.peer_names())
        for name in sorted(online - self.known):
            self._add_system(user_chat(name), "%s joined" % name)
        for name in sorted(self.known - online):
            self._add_system(user_chat(name), "%s left" % name)
        self.known = online
        self._refresh_list()
        self._update_chat_header()
        self._update_status_bar()

    def _pe_message(self, info):
        """A chat message arrived (already ACKed by the network layer)."""
        sender, chat = info["peer"], user_chat(info["peer"])
        if info.get("group"):
            chat = self._group_chat_for(info["group"], sender)
        row = self._new_row(chat, "in", sender, info["text"], shown=info["ts"], message_id=info["id"],
                            extra={"group": info.get("group")})
        if row is None:
            return                                           # a duplicate we already stored
        self.typing.pop(sender, None)
        self._show_new_row(row)
        # Alert unless the user is already looking at this very chat.
        if chat != self.current or self.root.focus_displayof() is None:
            self._beep()
            self._start_flash()

    def _pe_msg_status(self, info):
        """The network layer tells us what happened to a message we sent (queued / ACKed / failed)."""
        row = self.db.get_message(info["id"])
        if row is None or row["direction"] != "out":
            return
        states = row["extra"].get("states", {})
        peer = info["peer"]
        if peer not in states or states[peer] == "delivered":
            return
        states[peer] = info["status"]
        values = list(states.values())
        acked = values.count("delivered")
        if acked == len(values):
            status = "delivered"
        elif "failed" in values:
            status = "failed"
        elif "queued" in values:
            status = "queued"
        else:
            status = "sending"
        row["extra"]["states"] = states
        row.update(status=status, acked=acked, total=len(values))
        self.db.update_message(row["id"], status=status, acked=acked, total=len(values), extra=row["extra"])
        if row["chat"] == self.current:
            self.chat.update_item(row)
        if info["status"] == "failed":
            self._show_notice("Message to %s was not acknowledged. Click 'Failed' on it to retry." % peer)

    def _pe_typing(self, info):
        self.typing[info["peer"]] = time.time() + config.TYPING_EXPIRE
        self._update_chat_header()
        self._refresh_list()
        self.root.after(int(config.TYPING_EXPIRE * 1000) + 100, self._expire_typing)

    def _expire_typing(self):
        now = time.time()
        for name in [n for n, until in self.typing.items() if until <= now]:
            del self.typing[name]
        self._update_chat_header()
        self._refresh_list()

    # ---- files -------------------------------------------------------------------

    def _pe_secure(self, info):
        self.secure[info["peer"]] = (info["encrypted"], info["fingerprint"])
        self._update_chat_header()

    def _pe_identity(self, info):
        """A peer presented a different identity key than the one remembered for its name."""
        key = (info["peer"], info["presented"])
        if key in self.trust_asked:
            return
        self.trust_asked.add(key)
        # after_idle: show the dialog outside the event loop, so events keep flowing meanwhile
        self.root.after_idle(lambda: self._ask_trust(info))

    def _ask_trust(self, info):
        trust = messagebox.askyesno(
            "Identity changed",
            "%s now presents a DIFFERENT key:\n\nnew          %s\nremembered   %s\n\n"
            "Either %s reinstalled NetChat, or another computer is pretending to be %s.\n"
            "Ask %s to read their fingerprint (click the lock in their window) and compare.\n\n"
            "Trust the new key?" % (info["peer"], info["presented"], info["known"],
                                    info["peer"], info["peer"], info["peer"]),
            icon="warning", parent=self.root)
        if trust:
            self.peer.trust_identity(info["peer"], info["ident"])
            self._add_system(user_chat(info["peer"]), "You trusted a new key for %s (%s)" % (info["peer"], info["presented"]))
            self.peer.ensure_connection(info["peer"])

    def _pe_rtt(self, info):
        pass                                                 # shown by the Network info panel (later tier)

    # ==================================================================
    # Messages: storing, showing, deleting
    # ==================================================================
    def _new_row(self, chat, direction, sender, text, shown=None, status="delivered", kind="text",
                 extra=None, total=1, message_id=None):
        """Store a message in the database and return its dict (None if that id already exists)."""
        row = dict(id=message_id or uuid.uuid4().hex, chat=chat, direction=direction, sender=sender,
                   text=text, ts=time.time(), shown=shown or time.strftime("%H:%M:%S"), status=status,
                   kind=kind, extra=extra or {}, acked=0, total=total)
        if not self.db.add_message(row):
            return None
        if direction != "system":
            self._note_activity(chat, row)
        return row

    def _note_activity(self, chat, row):
        preview = row["text"] if row["kind"] == "text" else "File: " + row["extra"].get("name", "")
        if row["direction"] == "out":
            prefix = "You: "
        else:
            prefix = row["sender"] + ": " if chat.startswith("g:") else ""
        self.summaries[chat] = {"ts": row["ts"], "preview": prefix + preview}

    def _show_new_row(self, row):
        """Show a freshly stored message: draw it if its chat is open, else count it as unread."""
        if row["chat"] == self.current:
            self.chat.add_item(row)
            self._update_search_count()
        elif row["direction"] == "in":
            self.unread[row["chat"]] = self.unread.get(row["chat"], 0) + 1
        self._refresh_list()

    def _add_system(self, chat, text):
        row = self._new_row(chat, "system", "", text)
        if row is not None and chat == self.current:
            self.chat.add_item(row)

    def _delete_message(self, item):
        """Delete one message from THIS computer only (the other side keeps its copy)."""
        self.db.delete_message(item["id"])
        self.chat.remove_item(item["id"])
        self.summaries = self.db.chat_summaries()
        self._refresh_list()

    def _clear_chat(self):
        if self.current is None:
            return
        if not messagebox.askyesno("Clear chat", "Delete all messages of this chat from this computer?\n"
                                   "The other person keeps their copy.", parent=self.root):
            return
        self.db.clear_chat(self.current)
        self.summaries = self.db.chat_summaries()
        self._render_chat()
        self._refresh_list()

    def _render_chat(self, keep_scroll=True):
        """Draw the whole open chat again from the database (open chat, theme change, search)."""
        self.chat.clear()
        if self.current is None:
            self._show_chat_placeholder()
            return
        query = self.search_var.get().strip() if self.search_open else ""
        rows = self.db.messages(self.current, HISTORY_LIMIT, search=query or None)
        if not any(r["direction"] != "system" for r in rows):
            self.chat.show_hint("No messages match your search." if query else "No messages yet - say hello!")
        for row in rows:
            self.chat.add_item(row, scroll=False)
        self.chat.scroll_to_bottom()
        self.search_count.configure(text=("%d found" % len(rows)) if query else "")

    def _update_search_count(self):
        if self.search_open and self.search_var.get().strip():
            self._render_chat()

    def _toggle_search(self):
        if self.current is None:
            return
        self.search_open = not self.search_open
        if self.search_open:
            self.search_bar.grid()
            self.search_entry.focus_set()
        else:
            self.search_bar.grid_remove()
            self.search_var.set("")
            self.input.focus_input()
        self._render_chat()

    def _show_chat_placeholder(self):
        self.chat_title.configure(text="NetChat")
        self.chat_status.configure(text="")
        self.chat.show_hint("Select a peer on the left to start chatting.")

    # ==================================================================
    # Sidebar and chat header
    # ==================================================================
    def _peer_of(self, chat):
        return chat[2:] if chat and chat.startswith("u:") else None

    def _update_chat_header(self):
        group = self._group_of(self.current)
        if group is not None:
            self.chat_title.configure(text=group["name"])
            if group is EVERYONE:
                text = "everyone who is online (%d)" % len(self.peer.peer_names())
            else:
                text = "%d members: %s" % (len(group["members"]), ", ".join(group["members"]))
            self.chat_status.configure(text=text, fg=self.t["muted"])
            self.chat_lock.configure(text="")
            return
        peer = self._peer_of(self.current)
        if peer is None:
            return
        online = peer in self.peer.peer_names()
        self.chat_title.configure(text=peer)
        if self.typing.get(peer, 0) > time.time():
            self.chat_status.configure(text="%s is typing..." % peer, fg=self.t["accent"])
        else:
            self.chat_status.configure(text="● online" if online else "● offline",
                                       fg=self.t["ok"] if online else self.t["muted"])
        # Lock: closed = AES-GCM encrypted. The number is the fingerprint of THEIR identity key.
        encrypted, fingerprint = self.secure.get(peer, (None, ""))
        if not online or encrypted is None:
            self.chat_lock.configure(text="")
        elif encrypted:
            self.chat_lock.configure(text="\U0001F512 encrypted   fingerprint %s" % fingerprint, fg=self.t["ok"])
        else:
            self.chat_lock.configure(text="\U0001F513 not encrypted", fg=self.t["err"])

    def _chat_entries(self):
        """One dict per sidebar row, newest activity first."""
        online = set(self.peer.peer_names())
        names = online | {c[2:] for c in self.db.chats_with_messages() if c.startswith("u:")}
        now = time.time()
        entries = []
        for name in names:
            key = user_chat(name)
            summary = self.summaries.get(key)
            typing = self.typing.get(name, 0) > now
            sub = "typing..." if typing else (summary["preview"] if summary else self.ips.get(name, ""))
            entries.append(dict(key=key, name=name, kind="user", online=name in online, typing=typing,
                                unread=self.unread.get(key, 0), sub=sub, ts=summary["ts"] if summary else 0))
        for group in [EVERYONE] + sorted(self.groups.values(), key=lambda g: g["name"].lower()):
            key = group_chat(group["id"])
            summary = self.summaries.get(key)
            default = "everyone online" if group is EVERYONE else "%d members" % len(group["members"])
            entries.append(dict(key=key, name=group["name"], kind="group", online=True, typing=False,
                                unread=self.unread.get(key, 0), sub=summary["preview"] if summary else default,
                                ts=summary["ts"] if summary else 0, pinned=group is EVERYONE))
        # "Everyone" stays on top; everything else: most recent activity first
        entries.sort(key=lambda e: (not e.get("pinned"), -e["ts"], e["name"].lower()))
        return entries

    def _refresh_list(self):
        online = self.peer.peer_names()
        for name in online:
            address = self.peer.table.get(name)
            if address:
                self.ips[name] = address[0]
        entries = self._chat_entries()
        self.listed = [e["key"] for e in entries]
        self.peer_count.configure(text=str(len(online)))

        for child in self.peer_list.inner.winfo_children():
            child.destroy()
        if not entries:
            tk.Label(self.peer_list.inner, text=EMPTY_PEERS_TEXT, font=FONT_BODY, justify="left",
                     bg=self.t["side_bg"], fg=self.t["side_muted"],
                     wraplength=px(SIDEBAR_WIDTH - 50)).pack(padx=px(16), pady=px(20), anchor="w")
        for entry in entries:
            PeerRow(self.peer_list.inner, self.t, entry, entry["key"] == self.current,
                    self._select).pack(fill="x")
        self._update_status_bar()

    def _select(self, chat):
        self.current = chat
        self.unread.pop(chat, None)
        self._stop_flash()
        if self.search_open:
            self._toggle_search()
        self._update_chat_header()
        self._render_chat()
        self._refresh_list()
        self._update_send_state()
        self.input.focus_input()
        peer = self._peer_of(chat)
        if peer:
            self.peer.ensure_connection(peer)        # connect now, so the connection is ready to use

    def _move_selection(self, step):
        """Ctrl+Up / Ctrl+Down: jump to the previous / next chat in the list."""
        if not self.listed:
            return
        if self.current in self.listed:
            index = (self.listed.index(self.current) + step) % len(self.listed)
        else:
            index = 0 if step > 0 else len(self.listed) - 1
        self._select(self.listed[index])

    # ==================================================================
    # Sending
    # ==================================================================
    def _update_send_state(self):
        if self.current is None:
            self.send_button.state(["disabled"])
            self.send_button.configure(cursor="arrow")
        else:
            self.send_button.state(["!disabled"])
            self.send_button.configure(cursor="hand2")

    def _typing_notify(self):
        """The user is typing: tell the other side (peer.py sends at most one frame per 2 s)."""
        peer = self._peer_of(self.current)
        if peer and peer in self.peer.peer_names():
            self.peer.send_typing(peer)

    def _recipients(self, chat):
        """Who gets a message sent to this chat."""
        peer = self._peer_of(chat)
        if peer:
            return [peer]
        group = self._group_of(chat)
        if group is EVERYONE:
            return self.peer.peer_names()                    # everybody online right now
        if group is not None:
            # named group: every member except me. Offline members get it from the offline queue.
            return [m for m in group["members"] if m != self.my_name]
        return []

    def _send(self):
        text = self.input.get_text().strip()
        if text == "":
            return
        if self.current is None:
            self._show_notice("Select a peer on the left first.")
            return
        if len(text) > config.MAX_TEXT_LEN:
            # Keep the text in the box so the user can shorten it instead of retyping it.
            self._show_notice("Message too long (%d of %d characters)." % (len(text), config.MAX_TEXT_LEN))
            return
        recipients = self._recipients(self.current)
        if not recipients:
            self._show_notice("Nobody else is online, so nobody would receive that." if self.current == group_chat("everyone")
                              else "Nobody to send to.")
            return
        self.input.clear()
        extra = {"states": {r: "sending" for r in recipients}}
        group = self._group_of(self.current)
        if group is not None:
            # Every message carries the group id, name and members, so a computer that has never heard
            # of the group can create it from the first message it receives.
            extra["group"] = {"id": group["id"], "name": group["name"], "members": group["members"]}
        row = self._new_row(self.current, "out", self.my_name, text, status="sending", total=len(recipients),
                            extra=extra)
        self._show_new_row(row)
        self._deliver_in_background(row, recipients)

    def _deliver_in_background(self, row, recipients):
        """Connecting can take a few seconds, so do it in a thread to keep the window responsive.
        The outcome comes back as 'msg_status' events through the queue."""
        fields = {"text": row["text"], "ts": row["shown"]}
        if row["extra"].get("group"):
            fields["group"] = row["extra"]["group"]

        for recipient in recipients:
            # One ordered worker per recipient (peer.py): a quick second message cannot overtake the first.
            self.peer.deliver_async(recipient, row["id"], fields)

    def _retry(self, message_id):
        """The user clicked 'Failed - retry' on a message."""
        row = self.db.get_message(message_id)
        if row is None or row["direction"] != "out" or row["kind"] != "text":
            return
        states = row["extra"].get("states", {})
        failed = [peer for peer, state in states.items() if state == "failed"]
        if not failed:
            return                       # a late ACK arrived just before the click: nothing to retry
        for peer in failed:
            states[peer] = "sending"
        row["extra"]["states"] = states
        row["status"] = "sending"
        self.db.update_message(message_id, status="sending", extra=row["extra"])
        if row["chat"] == self.current:
            self.chat.update_item(row)
        self._deliver_in_background(row, failed)


def _prepare_windows():
    """Ask Windows for crisp text on high-DPI screens and a proper taskbar icon.
    Must run BEFORE the first Tk window is created."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass                             # not Windows, or an old Windows: the window is just a bit blurry
    try:
        # Without an app id, Windows groups us under "python.exe" and shows its icon.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NetChat.App")
    except Exception:
        pass


def run_gui(args):
    _prepare_windows()
    root = tk.Tk()
    root.withdraw()                          # hide the empty window while asking the name
    set_scale(root)
    apply_ttk_styles(root, LIGHT)
    name = args.name
    if not name:
        root.iconphoto(True, make_app_icon())
        name = NameDialog(root, config.MAX_NAME_LEN).ask()
    name = (name or "").strip()[:config.MAX_NAME_LEN]
    if not name:
        root.destroy()
        return 1
    app = ChatApp(root, name, args)
    if not app.start():
        root.destroy()
        return 1
    root.deiconify()
    root.mainloop()
    return 0
