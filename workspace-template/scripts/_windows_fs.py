"""Windows handle-relative operations for the shared filesystem owners.

NtCreateFile and NtSetInformationFile operate on held directory handles. Each
component is opened without reparse traversal; native errors are propagated.
The adapter is local to its callers and never patches the standard os module.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

ULONG = ctypes.c_uint32
NTSTATUS = ctypes.c_int32
HANDLE = ctypes.c_void_p
USHORT = ctypes.c_uint16
BOOLEAN = ctypes.c_ubyte


class UnicodeString(ctypes.Structure):
    _fields_ = [("length", USHORT), ("maximum", USHORT), ("buffer", ctypes.c_wchar_p)]


class ObjectAttributes(ctypes.Structure):
    _fields_ = [("length", ULONG), ("root", HANDLE), ("name", ctypes.POINTER(UnicodeString)),
                ("attributes", ULONG), ("security", HANDLE), ("quality", HANDLE)]


class IoStatus(ctypes.Structure):
    _fields_ = [("status", HANDLE), ("information", ctypes.c_size_t)]


class RenameInformation(ctypes.Structure):
    _fields_ = [("replace", BOOLEAN), ("root", HANDLE), ("length", ULONG), ("name", USHORT * 1)]


class AclSize(ctypes.Structure):
    _fields_ = [("count", ULONG), ("used", ULONG), ("free", ULONG)]


class FileBasicInformation(ctypes.Structure):
    _fields_ = [("created", ctypes.c_int64), ("accessed", ctypes.c_int64),
                ("written", ctypes.c_int64), ("changed", ctypes.c_int64), ("attributes", ULONG)]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", ULONG), ("descriptor", HANDLE), ("inherit", ctypes.c_int)]


class NativeStat:
    """Retain native identity and an independently checked private-ACL predicate."""

    def __init__(self, observed, private, change_time):
        self._observed = observed
        self.native_private = private
        self.native_change_time_ns = change_time
        # Match Windows path-stat conventions without confusing this projection
        # with the raw change-time observations used by the capture owner.
        self.st_ctime = getattr(observed, "st_birthtime", observed.st_ctime)
        self.st_ctime_ns = getattr(observed, "st_birthtime_ns", observed.st_ctime_ns)

    def __getattr__(self, name):
        return getattr(self._observed, name)

    def __eq__(self, other):
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        return (isinstance(other, NativeStat) and self.native_private == other.native_private
                and self.native_change_time_ns == other.native_change_time_ns
                and all(getattr(self._observed, field) == getattr(other._observed, field) for field in fields))


class WindowsFilesystem:
    native_windows = True
    O_DIRECTORY = 0x10000000
    O_NOFOLLOW = 0x20000000
    O_NONBLOCK = 0x40000000

    def __init__(self):
        self._initialized = False
        self.supports_dir_fd = {self.open, self.stat, self.mkdir, self.unlink, self.rmdir, self.rename, self.replace, self.link}
        self.supports_fd = {*os.supports_fd, self.stat, self.listdir, self.scandir}

    def __getattr__(self, name):
        return getattr(os, name)

    def _api(self):
        if self._initialized:
            return
        if os.name != "nt":
            raise OSError(errno.ENOSYS, "Native Windows filesystem operations are unavailable")
        import msvcrt

        self.crt = msvcrt
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.native = ctypes.WinDLL("ntdll", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        declarations = (
            (self.native, "NtCreateFile", NTSTATUS, [ctypes.POINTER(HANDLE), ULONG, ctypes.POINTER(ObjectAttributes),
                ctypes.POINTER(IoStatus), HANDLE, ULONG, ULONG, ULONG, ULONG, HANDLE, ULONG]),
            (self.native, "NtSetInformationFile", NTSTATUS, [HANDLE, ctypes.POINTER(IoStatus), HANDLE, ULONG, ULONG]),
            (self.native, "NtQueryDirectoryFile", NTSTATUS, [HANDLE, HANDLE, HANDLE, HANDLE, ctypes.POINTER(IoStatus),
                HANDLE, ULONG, ULONG, BOOLEAN, ctypes.POINTER(UnicodeString), BOOLEAN]),
            (self.native, "NtFlushBuffersFile", NTSTATUS, [HANDLE, ctypes.POINTER(IoStatus)]),
            (self.native, "RtlNtStatusToDosError", ULONG, [NTSTATUS]),
            (self.kernel, "GetFileInformationByHandleEx", ctypes.c_int, [HANDLE, ctypes.c_int, HANDLE, ULONG]),
            (self.kernel, "GetFileType", ULONG, [HANDLE]),
            (self.kernel, "GetDriveTypeW", ULONG, [ctypes.c_wchar_p]),
            (self.kernel, "GetFinalPathNameByHandleW", ULONG, [HANDLE, ctypes.c_wchar_p, ULONG, ULONG]),
            (self.kernel, "ReOpenFile", HANDLE, [HANDLE, ULONG, ULONG, ULONG]),
            (self.kernel, "CloseHandle", ctypes.c_int, [HANDLE]),
            (self.kernel, "LocalFree", HANDLE, [HANDLE]),
            (self.kernel, "GetCurrentProcess", HANDLE, []),
            (self.kernel, "DuplicateHandle", ctypes.c_int, [HANDLE, HANDLE, HANDLE, ctypes.POINTER(HANDLE), ULONG, ctypes.c_int, ULONG]),
            (self.kernel, "CreateMutexW", HANDLE, [ctypes.POINTER(SecurityAttributes), ctypes.c_int, ctypes.c_wchar_p]),
            (self.kernel, "WaitForSingleObject", ULONG, [HANDLE, ULONG]),
            (self.kernel, "ReleaseMutex", ctypes.c_int, [HANDLE]),
            (self.security, "OpenProcessToken", ctypes.c_int, [HANDLE, ULONG, ctypes.POINTER(HANDLE)]),
            (self.security, "GetTokenInformation", ctypes.c_int, [HANDLE, ctypes.c_int, HANDLE, ULONG, ctypes.POINTER(ULONG)]),
            (self.security, "ConvertSidToStringSidW", ctypes.c_int, [HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]),
            (self.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", ctypes.c_int,
                [ctypes.c_wchar_p, ULONG, ctypes.POINTER(HANDLE), ctypes.POINTER(ULONG)]),
            (self.security, "GetSecurityInfo", ULONG, [HANDLE, ctypes.c_int, ULONG, ctypes.POINTER(HANDLE),
                ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE)]),
            (self.security, "GetAclInformation", ctypes.c_int, [HANDLE, HANDLE, ULONG, ctypes.c_int]),
            (self.security, "GetAce", ctypes.c_int, [HANDLE, ULONG, ctypes.POINTER(HANDLE)]),
            (self.security, "GetSecurityDescriptorDacl", ctypes.c_int,
                [HANDLE, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(HANDLE), ctypes.POINTER(ctypes.c_int)]),
            (self.security, "SetSecurityInfo", ULONG, [HANDLE, ctypes.c_int, ULONG, HANDLE, HANDLE, HANDLE, HANDLE]),
        )
        for library, name, result, arguments in declarations:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
        self._initialized = True

    def _check(self, status):
        if status < 0:
            raise ctypes.WinError(self.native.RtlNtStatusToDosError(status))

    def _handle(self, descriptor):
        self._api()
        return self.crt.get_osfhandle(descriptor)

    def _descriptor(self, handle, flags):
        try:
            return self.crt.open_osfhandle(handle, flags)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def path_for_fd(self, descriptor):
        self._api()
        buffer = ctypes.create_unicode_buffer(32768)
        size = self.kernel.GetFinalPathNameByHandleW(self._handle(descriptor), buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        value = buffer.value
        if value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def _sid_text(self, sid):
        result = ctypes.c_wchar_p()
        if not self.security.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return result.value
        finally:
            self.kernel.LocalFree(ctypes.cast(result, HANDLE))

    def _user_sid(self):
        token, required = HANDLE(), ULONG()
        if not self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
            buffer = ctypes.create_string_buffer(required.value)
            if not self.security.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            return self._sid_text(ctypes.cast(buffer, ctypes.POINTER(HANDLE))[0])
        finally:
            self.kernel.CloseHandle(token)

    @contextmanager
    def _private_security(self, private):
        descriptor = HANDLE()
        if private:
            sid = self._user_sid()
            text = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"
            if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, 1, ctypes.byref(descriptor), None):
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield descriptor
        finally:
            if descriptor:
                self.kernel.LocalFree(descriptor)

    def private_handle(self, handle):
        """Require current-user ownership and no other unprivileged allow ACEs."""
        owner, acl, descriptor = HANDLE(), HANDLE(), HANDLE()
        error = self.security.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
            ctypes.byref(acl), None, ctypes.byref(descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            sid = self._user_sid()
            if not acl or self._sid_text(owner) != sid:
                return False
            size = AclSize()
            if not self.security.GetAclInformation(acl, ctypes.byref(size), ctypes.sizeof(size), 2):
                raise ctypes.WinError(ctypes.get_last_error())
            for index in range(size.count):
                ace = HANDLE()
                if not self.security.GetAce(acl, index, ctypes.byref(ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                kind, _flags, length = struct.unpack("<BBH", ctypes.string_at(ace, 4))
                if kind == 1:  # Denied rights cannot broaden access.
                    continue
                if kind != 0 or length < 12 or self._sid_text(ace.value + 8) not in {sid, "S-1-5-18", "S-1-5-32-544"}:
                    return False
            return True
        finally:
            self.kernel.LocalFree(descriptor)

    def _create(self, name, root, *, directory, access, disposition=1, private=False):
        self._api()
        if (not isinstance(name, str) or not name and root is None
                or "\0" in name or len(name.encode("utf-16-le")) > 65532):
            raise OSError(errno.EINVAL, "Invalid native filename")
        buffer = ctypes.create_unicode_buffer(name)
        text = UnicodeString(len(name.encode("utf-16-le")), ctypes.sizeof(buffer), ctypes.cast(buffer, ctypes.c_wchar_p))
        handle, result = HANDLE(), IoStatus()
        options = 0x00200020  # OPEN_REPARSE_POINT | SYNCHRONOUS_IO_NONALERT
        options |= 1 if directory is True else 0x40 if directory is False else 0
        with self._private_security(private) as security:
            attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), root, ctypes.pointer(text), 0x40, security, None)
            self._check(self.native.NtCreateFile(ctypes.byref(handle), access | 0x00120080, ctypes.byref(attributes),
                ctypes.byref(result), None, 0x80, 7, disposition, options, None, 0))
        try:
            attributes = (ULONG * 2)()
            if not self.kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes[0] & 0x400 or self.kernel.GetFileType(handle) != 1:
                raise OSError(errno.ELOOP, "Native filesystem operation refuses reparse points and devices")
            return handle.value
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    @contextmanager
    def _parent(self, path, descriptor=None):
        """Resolve each component through handles; never resolve an untrusted link."""
        text = os.fsdecode(path)
        if descriptor is not None:
            if Path(text).is_absolute() or Path(text).drive or "\\" in text or "\0" in text:
                raise OSError(errno.EINVAL, "A descriptor-relative path must use relative components")
            pieces = text.split("/")
            current = os.dup(descriptor)
        else:
            absolute = Path(text).absolute()
            if len(absolute.drive) != 2 or absolute.drive[1] != ":":
                raise OSError(errno.ENOTSUP, "Native publication requires a local drive")
            if self.kernel.GetDriveTypeW(absolute.anchor) not in {2, 3, 5, 6}:
                raise OSError(errno.ENOTSUP, "Native filesystem operations require a local drive")
            pieces = list(absolute.parts[1:]) or ["."]
            handle = self._create("\\??\\" + absolute.anchor, None, directory=True, access=0x21)
            current = self._descriptor(handle, os.O_RDONLY | os.O_BINARY)
        try:
            for component in pieces[:-1]:
                if component in {"", "."}:
                    continue
                handle = self._create(component, self._handle(current), directory=True, access=0x21)
                nested = self._descriptor(handle, os.O_RDONLY | os.O_BINARY)
                os.close(current)
                current = nested
            if not pieces[-1] or ":" in pieces[-1] or "\0" in pieces[-1]:
                raise OSError(errno.EINVAL, "Invalid native filename")
            yield current, pieces[-1]
        finally:
            os.close(current)

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        if not flags & (self.O_DIRECTORY | self.O_NOFOLLOW) and dir_fd is None:
            return os.open(path, flags, mode)
        self._api()
        directory = bool(flags & self.O_DIRECTORY)
        access = 0x21 if directory else 0x3 if flags & os.O_RDWR else 0x2 if flags & os.O_WRONLY else 0x1
        if flags & os.O_APPEND:
            access |= 4
        disposition = 2 if flags & os.O_CREAT and flags & os.O_EXCL else 3 if flags & os.O_CREAT else 1
        with self._parent(path, dir_fd) as (parent, name):
            if name == "." and directory and not flags & (os.O_CREAT | os.O_TRUNC):
                return os.dup(parent)
            handle = self._create(name, self._handle(parent), directory=directory, access=access,
                disposition=disposition, private=bool(flags & os.O_CREAT) and not mode & 0o077)
        descriptor = self._descriptor(handle, (flags & (os.O_RDWR | os.O_WRONLY | os.O_APPEND)) | os.O_BINARY)
        try:
            if flags & os.O_TRUNC:
                os.ftruncate(descriptor, 0)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def mkdir(self, path, mode=0o777, *, dir_fd=None):
        self._api()
        with self._parent(path, dir_fd) as (parent, name):
            handle = self._create(name, self._handle(parent), directory=True, access=0x21,
                disposition=2, private=not mode & 0o077)
            self.kernel.CloseHandle(handle)

    def fstat(self, descriptor):
        handle = self._handle(descriptor)
        return NativeStat(os.fstat(descriptor), self.private_handle(handle), self.change_time(descriptor))

    def change_time(self, descriptor):
        self._api()
        information = FileBasicInformation()
        if not self.kernel.GetFileInformationByHandleEx(self._handle(descriptor), 0,
                ctypes.byref(information), ctypes.sizeof(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (information.changed - 116444736000000000) * 100

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if isinstance(path, int):
            return self.fstat(path)
        if dir_fd is None and follow_symlinks:
            return os.stat(path)
        with self._parent(path, dir_fd) as (parent, name):
            if name == ".":
                return self.fstat(parent)
            handle = self._create(name, self._handle(parent), directory=None, access=0)
        descriptor = self._descriptor(handle, os.O_RDONLY | os.O_BINARY)
        try:
            return self.fstat(descriptor)
        finally:
            os.close(descriptor)

    def listdir(self, path="."):
        if not isinstance(path, int):
            return os.listdir(path)
        return list(self._names(path))

    def _names(self, descriptor):
        """Stream native directory pages so caller entry bounds can stop early."""
        self._api()
        handle = self._create("", self._handle(descriptor), directory=True, access=0x21)
        restart = True
        try:
            while True:
                buffer, result = ctypes.create_string_buffer(65536), IoStatus()
                status = self.native.NtQueryDirectoryFile(handle, None, None, None, ctypes.byref(result),
                    buffer, len(buffer), 12, False, None, restart)
                if status & 0xffffffff == 0x80000006:  # STATUS_NO_MORE_FILES
                    return
                self._check(status)
                restart, offset = False, 0
                while True:
                    next_offset, _index, length = struct.unpack_from("<III", buffer, offset)
                    if length % 2 or offset + 12 + length > result.information:
                        raise OSError(errno.EIO, "Invalid native directory response")
                    name = bytes(buffer[offset + 12:offset + 12 + length]).decode("utf-16-le")
                    if name not in {".", ".."}:
                        yield name
                    if not next_offset:
                        break
                    offset += next_offset
        finally:
            self.kernel.CloseHandle(handle)

    def scandir(self, path="."):
        if not isinstance(path, int):
            return os.scandir(path)
        return NativeScandir(self, path)

    def _move(self, source, destination, source_fd, destination_fd, *, replace, link=False):
        self._api()
        with self._parent(source, source_fd) as (parent, name), self._parent(destination, destination_fd) as (target, leaf):
            handle = self._create(name, self._handle(parent), directory=None, access=0x00010100)
            try:
                encoded = leaf.encode("utf-16-le")
                buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInformation) + len(encoded))
                info = ctypes.cast(buffer, ctypes.POINTER(RenameInformation)).contents
                info.replace, info.root, info.length = replace, self._handle(target), len(encoded)
                ctypes.memmove(ctypes.addressof(buffer) + RenameInformation.name.offset, encoded, len(encoded))
                result = IoStatus()
                self._check(self.native.NtSetInformationFile(handle, ctypes.byref(result), buffer,
                    len(buffer), 11 if link else 10))
            finally:
                self.kernel.CloseHandle(handle)

    def rename(self, source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        return self._move(source, destination, src_dir_fd, dst_dir_fd, replace=True)

    replace = rename

    def link(self, source, destination, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        return self._move(source, destination, src_dir_fd, dst_dir_fd, replace=False, link=True)

    def unlink(self, path, *, dir_fd=None):
        return self._delete(path, dir_fd, directory=False)

    def rmdir(self, path, *, dir_fd=None):
        return self._delete(path, dir_fd, directory=True)

    def _delete(self, path, descriptor, *, directory):
        self._api()
        with self._parent(path, descriptor) as (parent, name):
            handle = self._create(name, self._handle(parent), directory=directory, access=0x00010000)
            try:
                result, deleted = IoStatus(), BOOLEAN(True)
                self._check(self.native.NtSetInformationFile(handle, ctypes.byref(result), ctypes.byref(deleted), 1, 13))
            finally:
                self.kernel.CloseHandle(handle)

    def fsync(self, descriptor):
        self._api()
        result = IoStatus()
        handle = self._handle(descriptor)
        status = self.native.NtFlushBuffersFile(handle, ctypes.byref(result))
        if status & 0xffffffff != 0xc0000022:  # STATUS_ACCESS_DENIED
            self._check(status)
            return
        # Windows flush rights differ from POSIX fsync on a read descriptor.
        # Reopen the same object with write rights, never a mutable pathname.
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            # ReOpenFile rejects GENERIC_WRITE for directories. A native empty
            # relative name reopens the held directory object with the precise
            # FILE_ADD_FILE / FILE_WRITE_DATA right required by a flush.
            writable = self._create("", handle, directory=True, access=0x2)
        else:
            writable = self.kernel.ReOpenFile(handle, 0x40000000, 7, 0x02200000)
            if writable == HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            self._check(self.native.NtFlushBuffersFile(writable, ctypes.byref(result)))
        finally:
            self.kernel.CloseHandle(writable)

    def chmod(self, path, mode, *, dir_fd=None, follow_symlinks=True):
        """Apply owner-controlled Windows ACLs, including explicit public-read modes."""
        self._api()
        sid = self._user_sid()
        broad = "(A;OICI;FR;;;WD)" if mode & 0o077 else ""
        descriptor = HANDLE()
        if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY){broad}", 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            present, defaulted, acl = ctypes.c_int(), ctypes.c_int(), HANDLE()
            if not self.security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
                raise ctypes.WinError(ctypes.get_last_error())
            with self._parent(path, dir_fd) as (parent, name):
                handle = self._create(name, self._handle(parent), directory=None, access=0x00040000)
            try:
                error = self.security.SetSecurityInfo(handle, 1, 0x80000004, None, None, acl, None)
                if error:
                    raise ctypes.WinError(error)
            finally:
                self.kernel.CloseHandle(handle)
        finally:
            self.kernel.LocalFree(descriptor)

    @contextmanager
    def lock(self, descriptor, *, timeout_seconds=10.0):
        """Own a process-shared, owner-death-safe lock without locking file bytes."""
        self._api()
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, "A native lock requires one regular file")
        # A coordination handle needs no write access while held. Narrow the
        # caller's descriptor so a coherent read can pin the empty lock file.
        process, duplicate = self.kernel.GetCurrentProcess(), HANDLE()
        if not self.kernel.DuplicateHandle(process, self._handle(descriptor), process,
                ctypes.byref(duplicate), 0x00120081, False, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        readonly = self._descriptor(duplicate.value, os.O_RDONLY | os.O_BINARY)
        try:
            os.dup2(readonly, descriptor)
        finally:
            os.close(readonly)
        identity = f"{info.st_dev}:{info.st_ino}"
        name = "Global\\EvidenceWiki-" + hashlib.sha256(identity.encode()).hexdigest()
        holder = ModuleType("_evidence_wiki_windows_mutexes")
        holder.guard, holder.held = threading.Lock(), set()
        registry = sys.modules.setdefault(holder.__name__, holder)
        with self._private_security(True) as security:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), security, False)
            mutex = self.kernel.CreateMutexW(ctypes.byref(attributes), False, name)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                with registry.guard:
                    if name not in registry.held:
                        outcome = self.kernel.WaitForSingleObject(mutex, 0)
                        if outcome in (0, 0x80):  # Acquired, including a terminated owner.
                            registry.held.add(name)
                            acquired = True
                        elif outcome != 0x102:
                            raise ctypes.WinError(ctypes.get_last_error())
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise BlockingIOError(errno.EAGAIN, "The native lock is held by another writer")
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
            yield
        finally:
            try:
                if acquired:
                    with registry.guard:
                        if not self.kernel.ReleaseMutex(mutex):
                            raise ctypes.WinError(ctypes.get_last_error())
                        registry.held.remove(name)
            finally:
                self.kernel.CloseHandle(mutex)


class NativeEntry:
    def __init__(self, filesystem, descriptor, name):
        self._filesystem, self._descriptor, self.name, self.path = filesystem, descriptor, name, name

    def stat(self, *, follow_symlinks=True):
        return self._filesystem.stat(self.name, dir_fd=self._descriptor, follow_symlinks=follow_symlinks)

    def is_dir(self, *, follow_symlinks=True):
        return stat.S_ISDIR(self.stat(follow_symlinks=follow_symlinks).st_mode)

    def is_file(self, *, follow_symlinks=True):
        return stat.S_ISREG(self.stat(follow_symlinks=follow_symlinks).st_mode)

    def is_symlink(self):
        return stat.S_ISLNK(self.stat(follow_symlinks=False).st_mode)


class NativeScandir:
    def __init__(self, filesystem, descriptor):
        self._descriptor = os.dup(descriptor)
        self._filesystem, self._entry_descriptor = filesystem, descriptor
        self._names = None
        try:
            self._names = filesystem._names(self._descriptor)
        except BaseException:
            self.close()
            raise

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return NativeEntry(self._filesystem, self._entry_descriptor, next(self._names))
        except StopIteration:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if self._descriptor is not None:
            try:
                if self._names is not None:
                    self._names.close()
            finally:
                os.close(self._descriptor)
                self._descriptor = None


_source = Path(__file__).resolve()
_identity = hashlib.sha256(str(_source).encode() + b"\0" + _source.read_bytes()).hexdigest()
_holder = ModuleType("_evidence_wiki_windows_filesystem_" + _identity)
_holder.filesystem = WindowsFilesystem()
# Isolated script imports of one installed generation must share their native
# owner. Different copied roots and changed implementations remain independent.
filesystem = sys.modules.setdefault(_holder.__name__, _holder).filesystem
