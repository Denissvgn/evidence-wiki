#!/usr/bin/env python3
"""Select native anchored filesystem operations without changing Python's os module."""

import os as _os

if _os.name == "nt":
    from _windows_fs import filesystem as os
else:
    os = _os
