"""Keep one process's UTC readings ordered across small host clock corrections."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

# Virtualized hosts step the wall clock back by milliseconds when they resynchronize. A larger regression passes
# through unchanged, so clock-regression refusals still see a real host clock fault.
ALLOWANCE = timedelta(seconds=1)

_lock = threading.Lock()
_last: datetime | None = None


def ordered(reading: datetime) -> datetime:
    """Return ``reading``, or wait out a step back of at most ``ALLOWANCE`` and return the previous result.

    Waiting until the clock is back at the previous result keeps every result at or behind the raw clock. A workspace
    script, a host executor or another process that checks a stamp against its own raw clock then never sees it
    ahead.
    """
    global _last
    with _lock:
        if _last is None or not _last - ALLOWANCE <= reading < _last:
            _last = reading
            return reading
        held = _last
    # Outside the lock, so concurrent callers wait out the same step together rather than one after another.
    time.sleep((held - reading).total_seconds())
    return held
