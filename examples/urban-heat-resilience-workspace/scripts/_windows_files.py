#!/usr/bin/env python3
"""No-follow Windows handles for bounded local reads and pinned captures."""

from __future__ import annotations

import os
import re
import stat
import sys
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from _evidence_authority import EvidenceInvalid
from _evidence_revision import observation
from _record_artifacts import artifact_path
from _workspace_module_loader import load_workspace_module

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_TYPE_DISK = 1
_CLOCK_OWNER = None


def change_time(descriptor: int, fallback: int) -> int:
    """Read the native change clock independently of Python's stat conventions."""
    global _CLOCK_OWNER
    if sys.platform != "win32":
        return fallback
    if _CLOCK_OWNER is None:
        _CLOCK_OWNER = load_workspace_module(Path(__file__).resolve().parent, "_windows_fs").filesystem
    return _CLOCK_OWNER.change_time(descriptor)


def checked_file_observation(path: Path, descriptor: int, expected: tuple[int, ...]) -> tuple[int, ...]:
    """Match the pinned path and descriptor without mixing their clock meanings."""
    opened = observation(os.fstat(descriptor))
    # Windows path stat adds extension-based execute bits and reports creation
    # time as ctime; fstat has neither convention. Retain device/inode, type,
    # read/write modes, links, size and mtime; recheck each API's own full tuple.
    comparable = (*opened[:2], opened[2] & ~0o111, *opened[3:6])
    wanted = (*expected[:2], expected[2] & ~0o111, *expected[3:6])
    native_changed = change_time(descriptor, opened[6])
    if (comparable != wanted or observation(path.lstat()) != expected[:7]
            or len(expected) > 7 and native_changed != expected[7]):
        raise EvidenceInvalid("usage_workspace_changed")
    return (*opened, native_changed)


def validate_relative_path(relative: str) -> None:
    """Permit hidden control files while rejecting traversal and Windows aliases."""
    if not isinstance(relative, str) or not relative or len(relative) > 4096:
        raise EvidenceInvalid("unsafe_usage_workspace_file")
    for part in relative.split("/"):
        if (part in {"", ".", ".."} or part.endswith((".", " "))
                or any(ord(character) < 32 or ord(character) == 127 or character in '\\:<>"|?*' for character in part)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)):
            raise EvidenceInvalid("unsafe_usage_workspace_file")


@lru_cache(maxsize=1)
def file_api():
    """Bind immutable Win32 entry points once; keep filesystem observations live."""
    import ctypes
    from ctypes import wintypes

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
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel, FileInformation


@contextmanager
def open_observed_file(root: Path, relative: str, expected: tuple[int, ...], limit: int):
    """Pin one regular file and its ancestors until the caller releases the context.

    Open reparse points themselves, reject them, and retain directory handles
    without write/delete sharing. A workspace capture pins every selected file
    before reading any bytes; a single-file pin alone is not a workspace epoch.
    """
    if sys.platform != "win32":
        raise OSError("Windows file handles are unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    validate_relative_path(relative)
    path = root.absolute() / relative
    if not path.drive or path.drive.startswith("\\\\") or ".." in path.parts:
        raise EvidenceInvalid("unsafe_usage_workspace_file")

    kernel, FileInformation = file_api()
    handles = []
    descriptor = None
    try:
        if kernel.GetDriveTypeW(path.anchor) not in {2, 3, 5, 6}:
            raise EvidenceInvalid("unsafe_usage_workspace_file")
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
        yield descriptor
        if checked_file_observation(path, descriptor, expected) != opened:
            raise EvidenceInvalid("usage_workspace_changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def read_file(root: Path, relative: str, expected: tuple[int, ...], limit: int) -> bytes:
    """Read bounded binary bytes through a pinned no-follow handle."""
    with open_observed_file(root, relative, expected, limit) as descriptor:
        chunks, size = [], 0
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise EvidenceInvalid("usage_workspace_bound_exceeded")
        if size != expected[4]:
            raise EvidenceInvalid("usage_workspace_changed")
        return b"".join(chunks)


def read_legacy_file(root: Path, relative: str, expected: tuple[int, ...], limit: int) -> bytes:
    """Retain the stricter evidence-artifact path policy for legacy usage reads."""
    artifact_path(relative)
    return read_file(root, relative, expected, limit)
