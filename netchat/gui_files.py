"""
gui_files.py - Sending and receiving files: the paperclip, the file bubbles' buttons and the transfer events.
"""

import os
import threading
import uuid
from tkinter import filedialog

from .gui_widgets import open_folder
from .gui_common import user_chat


class FilesMixin:
    """Sending and receiving files: the paperclip, the file bubbles' buttons and the transfer events. (mixed into ChatApp in gui.py)"""

    def _pe_file_offer(self, info):
        """Somebody wants to send us a file: show it in the chat with Accept / Decline."""
        chat = user_chat(info["peer"])
        row = self._new_row(chat, "in", info["peer"], info["name"], kind="file", message_id=info["fid"],
                            extra={"fid": info["fid"], "name": info["name"], "size": info["size"],
                                   "sha256": info["sha256"], "state": "offered"})
        if row is None:
            return
        self._show_new_row(row)
        if chat != self.current or self.root.focus_displayof() is None:
            self._beep()
            self._start_flash()

    def _pe_file_state(self, info):
        row = self.db.get_message(info["fid"])
        if row is None:
            return
        extra = row["extra"]
        extra["state"] = info["state"]
        extra["reason"] = info.get("reason", "")
        if info.get("path"):
            extra["path"] = info["path"]
        self.db.update_message(row["id"], extra=extra)
        row["extra"] = extra
        if info["state"] in ("verified", "failed", "declined", "cancelled"):
            self.progress.pop(info["fid"], None)
        if row["chat"] == self.current:
            self.chat.update_item(row)
        if info["state"] == "failed":
            self._show_notice("File transfer failed: %s" % info.get("reason", ""))
        elif info["state"] == "verified" and info["direction"] == "in":
            self._show_notice("%s received and verified (SHA-256). Saved in %s" % (info["name"], self.peer.download_dir))
        self._refresh_list()

    def _pe_file_progress(self, info):
        self.progress[info["fid"]] = (info["done"], info["speed"])
        self.chat.update_progress(info["fid"])

    def _attach(self):
        """The paperclip: choose a file and offer it to the person in the open chat."""
        peer = self._peer_of(self.current)
        if peer is None:
            self._show_notice("Open a chat with one person first - files go to one person at a time.")
            return
        path = filedialog.askopenfilename(parent=self.root, title="Send a file to %s" % peer)
        if path:
            self._send_file(peer, path)

    def _send_file(self, peer, path):
        try:
            size = os.path.getsize(path)
        except OSError as e:
            self._show_notice("Cannot read that file: %s" % e)
            return
        fid = uuid.uuid4().hex                       # the file id doubles as the message id
        name = os.path.basename(path)
        row = self._new_row(user_chat(peer), "out", self.my_name, name, kind="file", message_id=fid,
                            extra={"fid": fid, "name": name, "size": size, "path": path, "state": "hashing"})
        self._show_new_row(row)
        self.peer.files.offer(peer, path, fid)       # hashing + sending run in their own threads

    def _file_action(self, action, item):
        """A button inside a file bubble was clicked."""
        fid = item["extra"].get("fid", item["id"])
        work = {"accept": self.peer.files.accept, "decline": self.peer.files.decline,
                "cancel": self.peer.files.cancel}.get(action)
        if work is not None:
            # These send a frame, and during a transfer the connection may be busy with a 64 KB chunk
            # to a slow peer: run them in a thread so the window never freezes. Results come back as events.
            threading.Thread(target=work, args=(fid,), daemon=True).start()
        elif action == "open" and item["extra"].get("path"):
            error = open_folder(item["extra"]["path"])
            if error:
                self._show_notice("Could not open the folder: %s" % error)
