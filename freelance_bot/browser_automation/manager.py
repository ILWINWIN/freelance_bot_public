"""Memory-aware browser session manager.

The manager deliberately bounds heavy browser work. Fifty saved accounts do not
mean fifty running browsers; a session is opened only for the current job.
"""

import os
import sys
import ctypes
from contextlib import contextmanager
from threading import BoundedSemaphore


class MemoryGuard:
    def __init__(self, soft_limit_mb: int = 4096, hard_limit_mb: int = 5120) -> None:
        self.soft_limit_mb = soft_limit_mb
        self.hard_limit_mb = hard_limit_mb

    def rss_mb(self) -> float:
        # Windows exposes the current process working set through psapi. This
        # keeps the safety brake functional on the target desktop platform.
        if sys.platform == "win32":
            class Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("page_fault_count", ctypes.c_ulong),
                            ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t)]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return counters.working_set / 1024 / 1024
        try:
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return value / 1024 / 1024 if sys.platform != "darwin" else value / 1024 / 1024
        except (ImportError, AttributeError):
            return 0.0

    def allow_heavy_task(self) -> bool:
        return self.rss_mb() < self.soft_limit_mb


class BrowserSessionManager:
    def __init__(self, max_workers: int = 1, memory_guard: MemoryGuard | None = None) -> None:
        self.memory_guard = memory_guard or MemoryGuard()
        self._slots = BoundedSemaphore(max_workers)

    @contextmanager
    def slot(self):
        if not self.memory_guard.allow_heavy_task():
            raise MemoryError("Browser work paused because the memory soft limit was reached")
        acquired = self._slots.acquire(timeout=1)
        if not acquired:
            raise RuntimeError("No browser worker is currently available")
        try:
            yield
        finally:
            self._slots.release()
