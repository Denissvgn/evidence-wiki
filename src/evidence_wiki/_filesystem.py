"""Use the same native filesystem owner as standalone workspace scripts."""

import os as _os

from ._platform import require_supported

require_supported(_os.name)
os = _os


def metadata_fstat(descriptor):
    """Observe identity and change metadata without granting private-ACL status."""
    if getattr(os, "native_windows", False):
        return os.fstat(descriptor, check_private=False)
    return os.fstat(descriptor)


def metadata_stat(path, *, dir_fd=None, follow_symlinks=True):
    """Read current metadata; private custody checks must use the full stat owner."""
    if getattr(os, "native_windows", False):
        return os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks, check_private=False)
    return os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
