"""
gui_settings.py - Settings, the Network info window, the emoji picker and restarting.
"""

import os
import subprocess
import sys
from tkinter import messagebox, simpledialog

from . import settings
from .gui_dialogs import EmojiPicker, NetworkInfoDialog, SettingsDialog, SystemMonitorDialog


def restart_arguments(argv):
    """The command-line options to keep when NetChat restarts itself: everything except the options
    whose values were just changed in Settings (they would win over settings.json)."""
    drop = ("--name", "--tcp-port", "--udp-port", "--passphrase")
    kept, skip_next = [], False
    for argument in argv:
        if skip_next:
            skip_next = False
        elif argument in drop:
            skip_next = True                     # "--name Asha": drop the option AND its value
        elif argument.split("=")[0] in drop:
            pass                                 # "--name=Asha"
        else:
            kept.append(argument)
    return kept


class SettingsMixin:
    """Settings, the Network info window, the emoji picker and restarting. (mixed into ChatApp in gui.py)"""

    def _save_settings(self, **changes):
        """Remember a setting in settings.json (a failing disk must not break the window)."""
        self.settings.update(changes)
        try:
            settings.save(self.settings_path, self.settings)
        except OSError as e:
            self._show_notice("Could not save settings: %s" % e)

    def _open_network_info(self):
        if self.net_dialog is not None and self.net_dialog.winfo_exists():
            self.net_dialog.lift()
            return
        self.net_dialog = NetworkInfoDialog(self.root, self.t, self)
        self.net_dialog.show()

    def _open_system_monitor(self):
        if self.sys_dialog is not None and self.sys_dialog.winfo_exists():
            self.sys_dialog.lift()
            return
        self.sys_dialog = SystemMonitorDialog(self.root, self.t, self)
        self.sys_dialog.show()

    def _open_settings(self):
        current = dict(self.settings)
        current.update(username=self.my_name, tcp_port=self.peer.tcp_port, udp_port=self.peer.udp_port,
                       passphrase=self.peer.passphrase)
        new = SettingsDialog(self.root, self.t, current, settings.default_download_dir()).ask()
        if new is None:
            return
        restart_needed = any(new[key] != current[key] for key in ("username", "tcp_port", "udp_port"))
        if new["username"] != self.my_name:
            self.db.rename_owner(new["username"])        # history belongs to the person, not to the old name
        self._save_settings(**new)
        # These can change while the program runs:
        self.sound = new["sound"]
        self.peer.download_dir = new["download_dir"] or settings.default_download_dir()
        if new["passphrase"] != self.peer.passphrase:
            self.peer.set_passphrase(new["passphrase"])
            self._show_notice("Passphrase changed: connections are made again with the new one.")
        if new["dark_mode"] != self.dark_var.get():
            self.dark_var.set(new["dark_mode"])
            self.apply_theme("dark" if new["dark_mode"] else "light")
        if restart_needed and messagebox.askyesno(
                "Restart", "The new username / ports are used after a restart.\nRestart NetChat now?", parent=self.root):
            self._restart()
        elif restart_needed:
            self._show_notice("Username / port changes will apply the next time you start NetChat.")

    def _restart(self):
        """Close this window and start a new NetChat. Command-line options that would override the
        saved username / ports / passphrase are left out, so the new settings are used."""
        command = [sys.executable, os.path.abspath(sys.argv[0])] + restart_arguments(sys.argv[1:])
        self._on_close()
        subprocess.Popen(command, cwd=os.getcwd())

    def _connect_by_ip(self):
        """When UDP discovery cannot work (guest Wi-Fi, different subnets): type the other computer's address."""
        text = simpledialog.askstring(
            "Connect by IP", "IP address (or IP:port) of the other NetChat.\nIt is shown in the status bar of "
            "its window, e.g. 192.168.1.7:5000", parent=self.root)
        if text and text.strip():
            self._show_notice("Connecting to %s..." % text.strip())
            self.peer.connect_async(text)            # the result arrives as a status message

    def _pick_emoji(self):
        EmojiPicker(self.root, self.t, self.input.insert_text, self.emoji_button)
