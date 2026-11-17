"""Small .env loader for local Windows setup without extra dependencies."""

import os
from pathlib import Path


# Telegram User API collection is intentionally split into a short, bounded
# history pass and a permanent realtime listener.  Keep all tuning knobs here
# so a future diagnostics screen can expose facts rather than magic numbers.
STARTUP_HISTORY_WINDOW_MINUTES = 60
STARTUP_HISTORY_LIMIT_PER_SOURCE = 100
RECOVERY_MAX_WINDOW_MINUTES = 60
RECOVERY_OVERLAP_MINUTES = 2
HISTORY_WORKERS = 6
PROCESSING_WORKERS = 3
REALTIME_QUEUE_MAXSIZE = 200
HISTORY_QUEUE_MAXSIZE = 500
REALTIME_QUEUE_PAUSE_HISTORY_PERCENT = 80
REALTIME_QUEUE_RESUME_HISTORY_PERCENT = 50
REALTIME_ENQUEUE_TIMEOUT_SECONDS = 0.25
REALTIME_OVERFLOW_MAX_MESSAGES = 2_000
PROCESSING_REALTIME_BURST = 5
DELIVERY_BARRIER_MAX_SECONDS = 30
MANUAL_HISTORY_COOLDOWN_SECONDS = 30

# History workers share this limiter.  It protects one Telegram account from
# request bursts even though six workers may be waiting for source work.
TELEGRAM_USER_SYNC_CONCURRENCY = 3


def load_local_env(path: str = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
