"""Bounded inert pack/catalog reads with stable content and filesystem identity."""

from __future__ import annotations

import hashlib
import json
import math
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from ._filesystem import os
from .errors import UsageError

MAX_FILE = 1_048_576
MAX_TREE = 8_388_608
MAX_FILES = 256
MAX_ENTRIES = 512
MAX_OUTPUT = 1_048_576


def refuse(reason: str, code: str = "ONBOARDING_INVALID") -> None:
    raise UsageError(code, "Pack discovery request refused.",
                     remediation="Use an explicit origin and a current bounded pack/catalog; retain unresolved scope.",
                     details={"field": reason})


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def identity(path: Path | int) -> dict:
    value = os.fstat(path) if isinstance(path, int) else path.stat()
    return {"device": str(value.st_dev), "inode": str(value.st_ino)}


def signature(value):
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 512 or Path(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(c) < 32 or c in '\\:*?"<>|' for c in value)):
        refuse("pack_relative_path")
    return value


def bounded(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > 32768 or depth > 24:
            refuse("pack_document_bound")
        if type(item) is dict:
            if len(item) > 512 or any(type(key) is not str for key in item):
                refuse("pack_document_shape")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            if len(item) > 1024:
                refuse("pack_document_bound")
            pending.extend((part, depth + 1) for part in item)
        elif type(item) is str:
            if len(item) > 65536 or any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                refuse("pack_text_bound")
        elif type(item) is float and not math.isfinite(item):
            refuse("pack_nonfinite_value")
        elif type(item) not in {str, int, float, bool, type(None)}:
            refuse("pack_non_json_value")
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            refuse("pack_duplicate_key")
        result[key] = value
    return result


def json_document(raw: bytes):
    if not 0 < len(raw) <= MAX_FILE:
        refuse("pack_document_bound")
    try:
        return bounded(json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                                  parse_constant=lambda _: refuse("pack_nonfinite_value")))
    except (ValueError, UnicodeError, RecursionError):
        refuse("pack_json_invalid")


def yaml_document(raw: bytes):
    if not 0 < len(raw) <= MAX_FILE:
        refuse("pack_document_bound")
    class Loader(yaml.SafeLoader):
        pass
    def mapping(loader, node):
        loader.flatten_mapping(node)
        return unique((loader.construct_object(key), loader.construct_object(value)) for key, value in node.value)
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        events, depth = 0, 0
        for event in yaml.parse(raw):
            events += 1
            if isinstance(event, yaml.AliasEvent):
                refuse("pack_yaml_alias_unsupported")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
            if events > 32768 or depth > 24:
                refuse("pack_yaml_bound")
        return bounded(yaml.load(raw, Loader=Loader))  # noqa: S506 -- SafeLoader subclass only rejects duplicate keys.
    except (yaml.YAMLError, ValueError, TypeError, UnicodeError, RecursionError):
        refuse("pack_yaml_invalid")


def _windows_reader():
    from ._script_host import load_packaged_script, shared_assets_root

    assets = shared_assets_root()
    return (load_packaged_script(assets, "_windows_files"),
            load_packaged_script(assets, "_evidence_revision"))


@contextmanager
def file_reader():
    """Bind one reader generation for a read-only batch, with a closing recheck."""
    native = _windows_reader() if os.name == "nt" else None

    def read(root, relative, maximum=MAX_FILE, expected=None):
        return _read_file(root, relative, maximum, expected, native=native)

    yield read
    if native is not None and _windows_reader() != native:
        refuse("pack_reader_changed_during_read")


def read_file(root: Path, relative: str, maximum=MAX_FILE, expected=None) -> bytes:
    return _read_file(root, relative, maximum, expected)


def _read_file(root, relative, maximum, expected, *, native=None) -> bytes:
    relative_path(relative)
    try:
        if os.name == "nt" and type(root) is not int:
            module, revision = native if native is not None else _windows_reader()
            observed = (root / relative).lstat()
            if expected is not None and signature(observed) != expected:
                refuse("pack_changed_during_read")
            value = module.read_file(root, relative, revision.observation(observed), maximum)
        elif os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.dup(root) if type(root) is int else os.open(root, flags)
            try:
                parts = relative.split("/")
                for part in parts[:-1]:
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(leaf, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if expected is not None and signature(before) != expected:
                        refuse("pack_changed_during_read")
                    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                        refuse("pack_file_unsafe_or_large")
                    value = stream.read(maximum + 1)
                    if signature(before) != signature(os.fstat(stream.fileno())):
                        refuse("pack_changed_during_read")
            finally:
                os.close(descriptor)
        else:
            refuse("pack_read_platform_unsupported", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        if len(value) > maximum:
            refuse("pack_file_bound")
        return value
    except OSError:
        refuse("pack_file_unavailable")


@dataclass
class PackSnapshot:
    root: Path
    files: dict[str, bytes]
    observations: dict[str, tuple]

    @property
    def tree_sha256(self):
        digest = hashlib.sha256()
        for name, value in sorted(self.files.items()):
            digest.update(f"{name}\0{hashlib.sha256(value).hexdigest()}\n".encode())
        return digest.hexdigest()


def capture_pack(path: str | Path) -> PackSnapshot:
    from .domain_pack_validator import (
        ALLOWED_PACK_FILE_SUFFIXES,
        FORBIDDEN_PACK_PATH_CHARACTERS,
        WINDOWS_RESERVED_PACK_NAMES,
    )

    root = Path(path).expanduser().absolute()
    files, observations, pending = {}, {}, [(root, "", 0)]
    total, entries = 0, 0
    try:
        while pending:
            current, relative, depth = pending.pop()
            observed = current.lstat()
            entries += 1
            if entries > MAX_ENTRIES or depth > 8 or current.is_symlink() or getattr(observed, "st_file_attributes", 0) & 0x400:
                refuse("pack_tree_unsafe_or_large")
            if (any(ord(char) < 32 or char in FORBIDDEN_PACK_PATH_CHARACTERS for char in current.name)
                    or current.name.endswith((" ", ".")) or current.name.split(".", 1)[0].casefold() in WINDOWS_RESERVED_PACK_NAMES):
                refuse("pack_member_name_invalid")
            observations[relative] = signature(observed)
            if stat.S_ISDIR(observed.st_mode):
                with os.scandir(current) as children:
                    names = []
                    for child in children:
                        names.append(child.name)
                        if entries + len(names) + len(pending) > MAX_ENTRIES:
                            refuse("pack_tree_bound")
                pending.extend((current / name, f"{relative}/{name}".lstrip("/"), depth + 1) for name in sorted(names, reverse=True))
            elif (stat.S_ISREG(observed.st_mode) and Path(relative).suffix.lower() in ALLOWED_PACK_FILE_SUFFIXES
                  and not observed.st_mode & 0o111):
                if len(files) >= MAX_FILES:
                    refuse("pack_file_count_bound")
                content = read_file(root, relative, expected=signature(observed))
                total += len(content)
                if total > MAX_TREE:
                    refuse("pack_tree_bytes_bound")
                content.decode("utf-8")
                if b"\0" in content:
                    refuse("pack_binary_content")
                files[relative] = content
            else:
                refuse("pack_member_unsafe")
        if not root.is_dir() or "research.overlay.yml" not in files:
            refuse("pack_overlay_missing")
        for relative, observed in observations.items():
            if signature((root / relative).lstat()) != observed:
                refuse("pack_changed_during_read")
        return PackSnapshot(root, files, observations)
    except (OSError, UnicodeError):
        refuse("pack_tree_unavailable")
