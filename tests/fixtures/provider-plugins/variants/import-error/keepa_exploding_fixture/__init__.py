"""A registered distribution whose module raises on import.

CR-5 §2.4: a registration that fails to import is recorded as an invalid registration with
its reason. It must never crash enumeration and must never silently disappear, so the
fixture that proves it has to fail at the earliest possible moment — module import, before
any attribute of the declaration can be read.
"""

from __future__ import annotations

IMPORT_FAILURE_MESSAGE = "keepa-exploding-fixture cannot start: synthetic import failure for CR-5 tests"

raise RuntimeError(IMPORT_FAILURE_MESSAGE)
