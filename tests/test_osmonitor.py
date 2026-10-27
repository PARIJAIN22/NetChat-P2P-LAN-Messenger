"""
The OS monitoring panel's numbers: CPU %, RAM, threads, TCP connections (no window needed).
"""

import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netchat import osmonitor


class ProcessMonitorTests(unittest.TestCase):
    def test_ram_is_reported_and_plausible(self):
        ram = osmonitor.process_ram()
        self.assertIsNotNone(ram)
        self.assertGreater(ram, 5 * 1024 ** 2)                  # a Python process uses more than 5 MB
        self.assertLess(ram, 64 * 1024 ** 3)

    def test_cpu_rises_while_busy_and_stays_in_range(self):
        monitor = osmonitor.ProcessMonitor()
        end = time.perf_counter() + 0.3
        while time.perf_counter() < end:                         # keep one core busy
            pass
        busy = monitor.sample()["cpu"]
        self.assertGreater(busy, 50.0 / monitor.cores)
        time.sleep(0.3)
        idle = monitor.sample()["cpu"]
        self.assertLess(idle, busy)
        self.assertTrue(0.0 <= idle <= 100.0)

    def test_thread_count_follows_new_threads(self):
        monitor, stop = osmonitor.ProcessMonitor(), threading.Event()
        before = monitor.sample()["threads"]
        worker = threading.Thread(target=stop.wait)
        worker.start()
        try:
            self.assertEqual(monitor.sample()["threads"], before + 1)
        finally:
            stop.set()
            worker.join()

    def test_only_open_connections_are_counted(self):
        open_, closed = types.SimpleNamespace(closed=False), types.SimpleNamespace(closed=True)
        peer = types.SimpleNamespace(conn_lock=threading.Lock(), connections={"Bob": open_, "Eve": closed},
                                     incoming=[open_, open_, closed])
        sample = osmonitor.ProcessMonitor().sample(peer)
        self.assertEqual((sample["outgoing"], sample["incoming"]), (1, 2))

    def test_panel_text(self):
        from netchat.gui_dialogs import SystemMonitorDialog
        text = SystemMonitorDialog.format({"pid": 42, "cpu": 3.25, "ram": 50 * 1024 ** 2, "threads": 7,
                                           "outgoing": 1, "incoming": 2})
        for expected in ("42", "3.2 %", "50.0 MB", "Threads             7", "3  (1 dialed, 2 accepted)"):
            self.assertIn(expected, text)
        self.assertIn("unknown", SystemMonitorDialog.format({"pid": 1, "cpu": 0, "ram": None, "threads": 1,
                                                             "outgoing": 0, "incoming": 0}))


if __name__ == "__main__":
    unittest.main()
