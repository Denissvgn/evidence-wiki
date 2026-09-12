#!/usr/bin/env python3
"""Bounded Windows file reads for legacy workspace declaration inspection."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from _evidence_authority import EvidenceInvalid
from _evidence_revision import observation
from _record_artifacts import artifact_path

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_TYPE_DISK = 1


def checked_file_observation(path: Path, descriptor: int, expected: tuple[int, ...]) -> tuple[int, ...]:
    """Match the pinned path and descriptor without mixing their clock meanings."""
    opened = observation(os.fstat(descriptor))
    # Python 3.12's Windows stat reports creation time as ctime, while fstat
    # reports change time. Compare each clock only with the same API's snapshot.
    if opened[:6] != expected[:6] or observation(path.lstat()) != expected:
        raise EvidenceInvalid("usage_workspace_changed")
    return opened


def read_legacy_file(root: Path, relative: str, expected: tuple[int, ...], limit: int) -> bytes:
    """Read one regular file while holding its ancestors against replacement.

    This does not provide a coherent workspace capture or host-store authority.
    Open reparse points themselves, reject them, and retain directory handles
    without write/delete sharing until the bounded binary read has finished.
    """
    if sys.platform != "win32":
        raise OSError("Windows file handles are unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    artifact_path(relative)
    path = root.absolute() / relative
    if not path.drive or path.drive.startswith("\\\\") or ".." in path.parts:
        raise EvidenceInvalid("unsafe_usage_workspace_file")

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = []
    descriptor = None
    try:
        # Opening each component separately prevents a junction in an ancestor
        # from being followed before its own reparse attributes are inspected.
        for component in [*reversed(path.parents), path]:
            leaf = component == path
            handle = kernel.CreateFileW(
                str(component), GENERIC_READ if leaf else 0, FILE_SHARE_READ, None,
                OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS, None,
            )
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            info = FileInformation()
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or kernel.GetFileType(handle) != FILE_TYPE_DISK:
                raise EvidenceInvalid("unsafe_usage_workspace_file")
            directory = bool(info.attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
            if directory == leaf or leaf and (info.links != 1 or (info.size_high << 32 | info.size_low) > limit):
                raise EvidenceInvalid("unsafe_usage_workspace_file")
        descriptor = msvcrt.open_osfhandle(handles[-1], os.O_RDONLY | os.O_BINARY)
        handles.pop()  # The CRT descriptor now owns the leaf handle.
        opened = checked_file_observation(path, descriptor, expected)
        chunks = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise EvidenceInvalid("usage_workspace_bound_exceeded")
        if checked_file_observation(path, descriptor, expected) != opened or size != expected[4]:
            raise EvidenceInvalid("usage_workspace_changed")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for handle in reversed(handles):
            kernel.CloseHandle(handle)
