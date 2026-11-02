"""Keep one process's UTC readings ordered across small host clock corrections."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

# Virtualized hosts step the wall clock back by milliseconds when they resynchronize. A larger regression passes
# through unchanged, so clock-regression refusals still see a real host clock fault.
ALLOWANCE = timedelta(seconds=1)

_lock = threading.Lock()
_last: datetime | None = None


def ordered(reading: datetime) -> datetime:
    """Return ``reading``, or the previous result when the clock stepped back by at most ``ALLOWANCE``."""
    global _last
    with _lock:
        if _last is not None and _last - ALLOWANCE <= reading < _last:
            return _last
        _last = reading
        return reading
