"""
settings.py - the user's settings in settings.json (next to netchat.db, see --data-dir).

Priority when the program starts:   command-line option  >  settings.json  >  config.py default
"""

import json
import os

from . import config

DEFAULTS = {
    "username": "",                       # empty = ask in the start-up dialog
    "tcp_port": config.DEFAULT_TCP_PORT,
    "udp_port": config.DEFAULT_UDP_PORT,
    "download_dir": "",                   # empty = Downloads\NetChat in the user's home folder
    "sound": True,                        # beep when a message arrives in a chat you are not looking at
    "dark_mode": False,
    "passphrase": "",                     # shared room passphrase; empty = none
}


def default_download_dir():
    return os.path.join(os.path.expanduser("~"), *config.DOWNLOAD_SUBDIR)


def load(path):
    """Read the settings file. A missing or damaged file gives the defaults (never a crash), and
    every value must have the right type, otherwise the default is used for it."""
    settings = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key, default in DEFAULTS.items():
            value = stored.get(key)
            if not isinstance(value, type(default)):
                continue
            # In Python True is also an int, so a port stored as `true` must be refused explicitly.
            if isinstance(value, bool) and not isinstance(default, bool):
                continue
            settings[key] = value
    return settings


def save(path, settings):
    """Write the settings. The file is written to a temporary name first and then renamed, so a crash
    in the middle can never leave a half-written settings.json behind."""
    data = {key: settings.get(key, default) for key, default in DEFAULTS.items()}
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(temporary, 0o600)        # it may contain the passphrase: owner only (best effort on Windows)
    except OSError:
        pass
    os.replace(temporary, path)


def valid_port(text):
    """Return the port number if `text` is a usable port (1024-65535), else None."""
    try:
        port = int(str(text).strip())
    except ValueError:
        return None
    return port if 1024 <= port <= 65535 else None
