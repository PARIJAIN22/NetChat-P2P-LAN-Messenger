"""
osmonitor.py - what this NetChat process costs the operating system (standard library only).

  CPU %        process CPU time (user + kernel, from os.times()) used since the last sample,
               divided by the wall-clock time and the number of CPU cores: the share of the
               WHOLE machine, like the Windows Task Manager shows it.
  RAM          resident memory: the Working Set on Windows (GetProcessMemoryInfo via ctypes),
               the resident set from /proc/self/statm on Linux, peak RSS elsewhere.
  threads      threading.active_count(): every thread of this program (GUI, accept, readers ...).
  connections  open TCP connections of the Peer (dialed by us + accepted from others).
"""

import os
import sys
import threading
import time


def _ram_windows():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_info = kernel32.K32GetProcessMemoryInfo
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return counters.WorkingSetSize


def process_ram():
    """Resident memory of this process in bytes, or None if the OS does not tell us."""
    try:
        if os.name == "nt":
            return _ram_windows()
        if os.path.exists("/proc/self/statm"):
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        import resource                                   # macOS / BSD: peak, not current
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def connection_count(peer):
    """(dialed by us, accepted from others) open TCP connections of a Peer."""
    with peer.conn_lock:
        outgoing = sum(1 for c in peer.connections.values() if not c.closed)
        incoming = sum(1 for c in peer.incoming if not c.closed)
    return outgoing, incoming


class ProcessMonitor:
    """Call sample() regularly (e.g. once a second); CPU % is measured between two calls."""

    def __init__(self):
        self.cores = os.cpu_count() or 1
        self.last_cpu, self.last_wall = self._cpu_seconds(), time.perf_counter()

    @staticmethod
    def _cpu_seconds():
        t = os.times()
        return t.user + t.system

    def sample(self, peer=None):
        cpu, wall = self._cpu_seconds(), time.perf_counter()
        elapsed = wall - self.last_wall
        percent = max(0.0, (cpu - self.last_cpu) / elapsed / self.cores * 100) if elapsed > 0 else 0.0
        self.last_cpu, self.last_wall = cpu, wall
        outgoing, incoming = connection_count(peer) if peer is not None else (0, 0)
        return {"cpu": min(percent, 100.0), "ram": process_ram(), "threads": threading.active_count(),
                "outgoing": outgoing, "incoming": incoming, "pid": os.getpid(), "cores": self.cores}
