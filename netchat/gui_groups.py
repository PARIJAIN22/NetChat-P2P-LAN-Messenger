"""
gui_groups.py - Group chats (Everyone room + named groups) and the key fingerprint.
"""

import uuid
from tkinter import messagebox

from .gui_dialogs import NewGroupDialog
from .gui_common import EVERYONE, group_chat


class GroupsMixin:
    """Group chats (Everyone room + named groups) and the key fingerprint. (mixed into ChatApp in gui.py)"""

    def _group_chat_for(self, group, sender):
        """Chat key for a group message that arrived. If we never heard of this group, the message
        itself tells us everything (id, name, members), so we create it here: that is how a group
        shows up on the computers of people who did not create it."""
        gid = group["id"]
        if gid != "everyone" and gid not in self.groups:
            self.groups[gid] = group
            self.db.add_group(gid, group["name"], group["members"])
            self._add_system(group_chat(gid), "%s added you to the group %s" % (sender, group["name"]))
        return group_chat(gid)

    def _new_group(self):
        """Ask for a name and members, then create the group (it appears on the other computers
        with the first message sent to it)."""
        candidates = [e["name"] for e in self._chat_entries() if e["kind"] == "user"]
        result = NewGroupDialog(self.root, self.t, candidates).ask()
        if result is None:
            return
        name, members = result
        group = {"id": uuid.uuid4().hex[:12], "name": name, "members": [self.my_name] + members}
        self.groups[group["id"]] = group
        self.db.add_group(group["id"], name, group["members"])
        self._add_system(group_chat(group["id"]), "You created the group %s with %s" % (name, ", ".join(members)))
        self._select(group_chat(group["id"]))

    def _group_of(self, chat):
        """The group dict of a group chat key, or None for a one-to-one chat."""
        if not chat or not chat.startswith("g:"):
            return None
        return EVERYONE if chat == group_chat("everyone") else self.groups.get(chat[2:])

    def _show_my_fingerprint(self):
        if not self.peer.fingerprint:
            messagebox.showinfo("My fingerprint", "Encryption is not active (the 'cryptography' package is "
                                "missing or encryption is switched off), so there is no key fingerprint.", parent=self.root)
            return
        messagebox.showinfo("My fingerprint", "Your key fingerprint:\n\n%s\n\nRead it to the other person. In THEIR chat "
                            "window with you, the number next to the lock must be exactly this one. If it is not, "
                            "somebody is sitting between you." % self.peer.fingerprint, parent=self.root)
