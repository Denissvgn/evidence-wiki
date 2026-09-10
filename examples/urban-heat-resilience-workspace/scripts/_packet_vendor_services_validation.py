# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.validation.

Original source SHA-256: 3396418deeeb840ce03038b63f57eaa00681e1f8fdad51e0160c7b2c45144204
Only the validation dependency closure is included; source discovery, producer
execution, persistence, plugins and live reconciliation are excluded.

MIT License

Copyright (c) 2026 Denis Sivagin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import MutableMapping
from pathlib import PurePosixPath
import os
import posixpath
import re
import unicodedata


_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")


_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
    | {f"com{number}" for number in ("¹", "²", "³")}
    | {f"lpt{number}" for number in ("¹", "²", "³")}
)


_WINDOWS_FORBIDDEN_PATH_CHARS = frozenset('<>:"|?*')


class SharedValidationError(ValueError):
    """Raised when a caller uses a shared validator without a domain adapter."""


def _default_path_error(value: object) -> SharedValidationError:
    return SharedValidationError(
        f"Path must be a non-empty portable relative path: {value!r}"
    )


def require_portable_path_component(
    component: str,
    *,
    context: str | None = None,
    defer_non_nfc_error: bool = False,
    reject_delete_character: bool = True,
    utf8_error: Exception | None = None,
    control_error: Exception | None = None,
    non_nfc_error: Exception | None = None,
    nonportable_error: Exception | None = None,
    reserved_error: Exception | None = None,
) -> str:
    """Return a portable path component or raise a caller-owned exception.

    ``defer_non_nfc_error`` exists only for compatibility preflights that must
    detect a collection-level collision before strict per-path validation.
    Callers using it must subsequently validate the returned path strictly.
    """

    rendered = component if context is None else context
    try:
        component.encode("utf-8")
    except UnicodeEncodeError:
        raise utf8_error or nonportable_error or SharedValidationError(
            f"Path is not valid UTF-8 text: {rendered!r}"
        ) from None
    if (
        not defer_non_nfc_error
        and component != unicodedata.normalize("NFC", component)
    ):
        raise non_nfc_error or SharedValidationError(
            f"Path is not NFC-normalized: {rendered!r}"
        )
    if any(
        ord(character) < 32
        or (reject_delete_character and ord(character) == 127)
        for character in component
    ):
        raise control_error or nonportable_error or SharedValidationError(
            f"Path is not portable across supported systems: {rendered!r}"
        )
    if component.endswith((" ", ".")) or any(
        character in _WINDOWS_FORBIDDEN_PATH_CHARS
        for character in component
    ):
        raise nonportable_error or SharedValidationError(
            f"Path is not portable across supported systems: {rendered!r}"
        )
    if component.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        raise reserved_error or SharedValidationError(
            f"Path uses a reserved Windows name: {rendered!r}"
        )
    return component


def is_portable_path_component(component: str) -> bool:
    """Return whether *component* is portable on every supported filesystem."""

    try:
        require_portable_path_component(component)
    except (SharedValidationError, TypeError):
        return False
    return True


def require_portable_relative_path(
    value: object,
    *,
    normalize_backslashes: bool = False,
    normalize_posix_spelling: bool = False,
    required_suffix: str | None = None,
    defer_non_nfc_error: bool = False,
    reject_delete_character: bool = True,
    text_error: Exception | None = None,
    relative_error: Exception | None = None,
    escape_error: Exception | None = None,
    traversal_error: Exception | None = None,
    separator_error: Exception | None = None,
    utf8_error: Exception | None = None,
    control_error: Exception | None = None,
    non_nfc_error: Exception | None = None,
    nonportable_error: Exception | None = None,
    reserved_error: Exception | None = None,
    collision_seen: MutableMapping[str, str] | None = None,
    collision_error: Callable[[str, str], Exception] | None = None,
) -> str:
    """Return a canonical portable relative path.

    The strict default accepts only canonical ``/`` separators.  A filesystem
    API that intentionally accepts native Windows spelling can opt into
    backslash normalization without weakening any other check.
    ``normalize_posix_spelling`` is an explicit compatibility mode for legacy
    observational inputs: it collapses redundant ``/`` and ``.`` spelling,
    but still rejects traversal and every cross-platform hazard.
    ``defer_non_nfc_error`` is reserved for a compatibility preflight whose
    result is subsequently passed through this helper again in strict mode.
    """

    if not isinstance(value, (str, os.PathLike)):
        raise text_error or _default_path_error(value) from None
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise text_error or _default_path_error(value)
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise utf8_error or nonportable_error or relative_error or (
            _default_path_error(raw)
        ) from None
    if "\\" in raw and not normalize_backslashes:
        raise separator_error or relative_error or _default_path_error(raw)
    normalized = raw.replace("\\", "/") if normalize_backslashes else raw
    path = PurePosixPath(normalized)
    if path.is_absolute() or _WINDOWS_ABSOLUTE_RE.match(raw):
        raise escape_error or relative_error or _default_path_error(raw)
    if ".." in path.parts:
        raise (
            traversal_error
            or escape_error
            or relative_error
            or _default_path_error(raw)
        )
    canonical = path.as_posix()
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized != normalized.strip()
        or "." in path.parts
        or (not normalize_posix_spelling and canonical != normalized)
        or (
            required_suffix is not None
            and not canonical.casefold().endswith(required_suffix.casefold())
        )
    ):
        raise relative_error or _default_path_error(raw)
    for component in path.parts:
        require_portable_path_component(
            component,
            context=canonical,
            defer_non_nfc_error=defer_non_nfc_error,
            reject_delete_character=reject_delete_character,
            utf8_error=utf8_error,
            control_error=control_error,
            non_nfc_error=non_nfc_error,
            nonportable_error=nonportable_error,
            reserved_error=reserved_error,
        )
    if collision_seen is not None:
        key = portable_path_key(canonical)
        previous = collision_seen.setdefault(key, canonical)
        if previous != canonical:
            if collision_error is None:
                raise SharedValidationError(
                    f"Paths collide across supported filesystems: "
                    f"{previous!r} and {canonical!r}"
                )
            raise collision_error(previous, canonical)
    return canonical


def require_repository_relative_path(
    value: object,
    *,
    text_error: Exception,
    posix_error: Exception,
    normalized_error: Exception,
    absolute_error: Exception | None = None,
    separator_error: Exception | None = None,
    control_error: Exception | None = None,
    reject_delete_character: bool = False,
    control_after_normalization: bool = False,
    leading_backslash_is_absolute: bool = False,
    normalize_posix_spelling: bool = False,
    portability_error: Exception | None = None,
) -> str:
    """Return one strict repository-relative path with tiered diagnostics.

    The three required exceptions preserve the established distinction between
    missing/non-text input, non-POSIX spelling, and non-normalized paths.
    Cross-platform hazards that the legacy repository validators accepted are
    rejected as normalization failures unless the caller supplies a dedicated
    portability diagnostic.
    """

    if not isinstance(value, str) or not value:
        raise text_error
    if value != value.strip():
        raise posix_error
    has_control_character = any(
        ord(character) < 0x20
        or (reject_delete_character and ord(character) == 0x7F)
        for character in value
    )
    if has_control_character and not control_after_normalization:
        raise control_error or posix_error
    if (
        value.startswith("/")
        or (leading_backslash_is_absolute and value.startswith("\\"))
        or _WINDOWS_DRIVE_PREFIX_RE.match(value)
    ):
        raise absolute_error or posix_error
    if "\\" in value:
        raise separator_error or posix_error
    parts = value.split("/")
    if normalize_posix_spelling:
        if ".." in PurePosixPath(value).parts:
            raise normalized_error
    elif (
        any(part in {"", ".", ".."} for part in parts)
        or posixpath.normpath(value) != value
    ):
        raise normalized_error
    if has_control_character:
        raise control_error or posix_error
    strict_error = portability_error or normalized_error
    return require_portable_relative_path(
        value,
        normalize_posix_spelling=normalize_posix_spelling,
        text_error=text_error,
        relative_error=normalized_error,
        escape_error=absolute_error or posix_error,
        traversal_error=normalized_error,
        separator_error=separator_error or posix_error,
        utf8_error=strict_error,
        control_error=control_error or strict_error,
        non_nfc_error=strict_error,
        nonportable_error=strict_error,
        reserved_error=strict_error,
    )


def portable_path_key(value: str) -> str:
    """Return the collision key used by normalizing/case-insensitive filesystems."""

    return unicodedata.normalize("NFC", value).casefold()
