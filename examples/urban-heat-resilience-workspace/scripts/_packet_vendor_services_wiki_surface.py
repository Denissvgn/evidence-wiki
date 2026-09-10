# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.wiki_surface.

Original source SHA-256: 4ea989fc497743b6d08115d31ee3d4db0870fd94dd2b8f29aa842274b6ec744f
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

from enum import Enum
from typing import Optional
from typing import Union
from dataclasses import dataclass
from _packet_vendor_services_validation import is_portable_path_component
from urllib.parse import quote
import re
from urllib.parse import unquote


RESOURCE_SCHEME = "llm-wiki"


_PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.()-]+$")


class WikiSurfaceError(ValueError):
    """Raised for invalid wiki surface lookups."""


class PageKind(str, Enum):
    INDEX = "index"
    LOG = "log"
    ENTITIES = "entities"
    MODULES = "modules"
    WORKFLOWS = "workflows"
    GUIDES = "guides"
    FLOWS = "flows"
    INFRASTRUCTURE = "infrastructure"
    API_CONTRACTS = "api-contracts"
    DEPENDENCIES = "dependencies"
    LOAD_ORDER = "load-order"


class SurfaceRole(str, Enum):
    GENERATED = "generated"
    SEMANTIC = "semantic"
    MIXED = "mixed"


@dataclass(frozen=True)
class WikiSurfaceKind:
    kind: PageKind
    label: str
    path_pattern: str
    mcp_uri_kind: str
    obsidian_mirror_dir: Optional[str]
    role: SurfaceRole

    @property
    def requires_page_id(self) -> bool:
        return "{page_id}" in self.path_pattern

    @property
    def directory(self) -> Optional[str]:
        if not self.requires_page_id:
            return None
        return self.path_pattern.split("/", 1)[0]


_PAGE_KINDS = (
    WikiSurfaceKind(
        kind=PageKind.INDEX,
        label="Index",
        path_pattern="index.md",
        mcp_uri_kind="index",
        obsidian_mirror_dir=None,
        role=SurfaceRole.MIXED,
    ),
    WikiSurfaceKind(
        kind=PageKind.LOG,
        label="Log",
        path_pattern="log.md",
        mcp_uri_kind="log",
        obsidian_mirror_dir=None,
        role=SurfaceRole.GENERATED,
    ),
    WikiSurfaceKind(
        kind=PageKind.ENTITIES,
        label="Entities",
        path_pattern="entities/{page_id}.md",
        mcp_uri_kind="entities",
        obsidian_mirror_dir="Entities",
        role=SurfaceRole.SEMANTIC,
    ),
    WikiSurfaceKind(
        kind=PageKind.MODULES,
        label="Modules",
        path_pattern="modules/{page_id}.md",
        mcp_uri_kind="modules",
        obsidian_mirror_dir="Modules",
        role=SurfaceRole.SEMANTIC,
    ),
    WikiSurfaceKind(
        kind=PageKind.WORKFLOWS,
        label="Workflows",
        path_pattern="workflows/{page_id}.md",
        mcp_uri_kind="workflows",
        obsidian_mirror_dir="Workflows",
        role=SurfaceRole.MIXED,
    ),
    WikiSurfaceKind(
        kind=PageKind.GUIDES,
        label="Guides",
        path_pattern="guides/{page_id}.md",
        mcp_uri_kind="guides",
        obsidian_mirror_dir="Guides",
        role=SurfaceRole.SEMANTIC,
    ),
    WikiSurfaceKind(
        kind=PageKind.FLOWS,
        label="User flows",
        path_pattern="flows/{page_id}.md",
        mcp_uri_kind="flows",
        obsidian_mirror_dir="Flows",
        role=SurfaceRole.MIXED,
    ),
    WikiSurfaceKind(
        kind=PageKind.INFRASTRUCTURE,
        label="Infrastructure",
        path_pattern="infrastructure/{page_id}.md",
        mcp_uri_kind="infrastructure",
        obsidian_mirror_dir="Infrastructure",
        role=SurfaceRole.MIXED,
    ),
    WikiSurfaceKind(
        kind=PageKind.API_CONTRACTS,
        label="API contracts",
        path_pattern="api-contracts.md",
        mcp_uri_kind="api-contracts",
        obsidian_mirror_dir=None,
        role=SurfaceRole.MIXED,
    ),
    WikiSurfaceKind(
        kind=PageKind.DEPENDENCIES,
        label="Dependencies",
        path_pattern="dependencies.md",
        mcp_uri_kind="dependencies",
        obsidian_mirror_dir=None,
        role=SurfaceRole.MIXED,
    ),
    WikiSurfaceKind(
        kind=PageKind.LOAD_ORDER,
        label="Load order",
        path_pattern="load-order.md",
        mcp_uri_kind="load-order",
        obsidian_mirror_dir=None,
        role=SurfaceRole.MIXED,
    ),
)


_KINDS_BY_KIND = {entry.kind: entry for entry in _PAGE_KINDS}


def iter_page_kinds() -> tuple[WikiSurfaceKind, ...]:
    """Return all canonical page kinds in display/collection order."""
    return _PAGE_KINDS


def is_safe_page_id(page_id: str) -> bool:
    """Return True when a page id can safely map to one Markdown filename."""
    return (
        isinstance(page_id, str)
        and bool(page_id)
        and not page_id.startswith(".")
        and ".." not in page_id
        and bool(_PAGE_ID_RE.fullmatch(page_id))
        and is_portable_path_component(page_id)
    )


def canonical_path(kind: Union[PageKind, str], page_id: Optional[str] = None) -> str:
    """Return the canonical POSIX relative path for a wiki page."""
    entry = _entry_for(kind)
    if entry.requires_page_id:
        page_id = _validate_page_id(page_id, required=True)
        return entry.path_pattern.format(page_id=page_id)
    if page_id is not None:
        raise WikiSurfaceError(f"{entry.kind.value} does not accept a page id.")
    return entry.path_pattern


def mcp_uri(kind: Union[PageKind, str], page_id: Optional[str] = None) -> str:
    """Return the canonical MCP URI for a wiki page."""
    entry = _entry_for(kind)
    if entry.requires_page_id:
        page_id = _validate_page_id(page_id, required=True)
        return f"{RESOURCE_SCHEME}://{entry.mcp_uri_kind}/{quote(page_id, safe='._-')}"
    if page_id is not None:
        raise WikiSurfaceError(f"{entry.kind.value} does not accept a page id.")
    return f"{RESOURCE_SCHEME}://{entry.mcp_uri_kind}"


def validate_exact_page_coordinate(value: object) -> str:
    """Validate and return one canonical wiki path or MCP URI coordinate."""
    if not isinstance(value, str) or not value.strip():
        raise WikiSurfaceError("Exact wiki page coordinate must be a non-empty string.")

    coordinate = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in coordinate):
        raise WikiSurfaceError("Invalid exact wiki page coordinate.")

    for entry in _PAGE_KINDS:
        if entry.requires_page_id:
            if _matches_directory_path(coordinate, entry) or _matches_directory_uri(
                coordinate, entry
            ):
                return coordinate
            continue
        if coordinate in {
            canonical_path(entry.kind),
            mcp_uri(entry.kind),
        }:
            return coordinate

    raise WikiSurfaceError("Invalid exact wiki page coordinate.")


def _entry_for(kind: Union[PageKind, str]) -> WikiSurfaceKind:
    try:
        page_kind = kind if isinstance(kind, PageKind) else PageKind(kind)
    except ValueError as exc:
        raise WikiSurfaceError(f"Unknown wiki page kind: {kind}") from exc
    return _KINDS_BY_KIND[page_kind]


def _validate_page_id(page_id: Optional[str], *, required: bool) -> str:
    if page_id is None:
        if required:
            raise WikiSurfaceError("page id is required for directory-backed pages.")
        raise WikiSurfaceError("page id is not supported for this page kind.")
    if not is_safe_page_id(page_id):
        raise WikiSurfaceError(f"Unsafe wiki page id: {page_id}")
    return page_id


def _matches_directory_path(coordinate: str, entry: WikiSurfaceKind) -> bool:
    directory = entry.directory
    if directory is None:
        return False
    prefix = f"{directory}/"
    if not coordinate.startswith(prefix) or not coordinate.endswith(".md"):
        return False
    page_id = coordinate[len(prefix) : -len(".md")]
    return bool(
        is_safe_page_id(page_id) and coordinate == canonical_path(entry.kind, page_id)
    )


def _matches_directory_uri(coordinate: str, entry: WikiSurfaceKind) -> bool:
    prefix = f"{RESOURCE_SCHEME}://{entry.mcp_uri_kind}/"
    if not coordinate.startswith(prefix):
        return False
    encoded_page_id = coordinate[len(prefix) :]
    if (
        not encoded_page_id
        or re.search(r"%(?![0-9A-Fa-f]{2})", encoded_page_id)
        or "/" in encoded_page_id
    ):
        return False
    try:
        page_id = unquote(encoded_page_id, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return bool(is_safe_page_id(page_id) and coordinate == mcp_uri(entry.kind, page_id))
