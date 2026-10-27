"""
logger.py - records socket events with timestamps.

Every important network event (HELLO sent, TCP connection accepted, message
sent, BYE ...) goes through EventLog.log(). Lines are appended to netchat.log
and also handed to "listeners" (the GUI log panel, or the console in --verbose
mode).
"""

import threading
import time


class EventLog:
    def __init__(self, label, filename=None):
        # label tells apart lines from different instances that share one log file
        self.label = label
        self.filename = filename          # None = do not write a file (used by tests)
        self.listeners = []
        # Many threads log at once; the lock keeps each line in one piece.
        self.lock = threading.Lock()

    def add_listener(self, func):
        """func(line) will be called for every new log line (from any thread!)."""
        self.listeners.append(func)

    def log(self, text):
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.label, text)
        with self.lock:
            if self.filename:
                try:
                    with open(self.filename, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass                  # a broken log file must never crash the chat
            for func in self.listeners:
                func(line)
