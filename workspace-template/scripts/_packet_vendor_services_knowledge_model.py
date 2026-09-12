#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.knowledge_model.

Original source SHA-256: 6ecb9bd0fb10e3f9c59ccbe1bc75fc79eadb01d0d5e037ac079e4faa8d360fdf
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
from typing import Mapping
from types import MappingProxyType
from _packet_vendor_services_wiki_surface import PageKind
from typing import Union


REPOSITORY_IDENTITY_PATTERN = (
    r"^(?!.*[\u0000-\u001F])(?:unknown|[a-z0-9][a-z0-9._-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+)$"
)


class KnowledgeModelError(ValueError):
    """Raised when a knowledge payload violates the v1 contract."""

    def __init__(
        self,
        field: str,
        message: str,
        *,
        code: str | None = None,
    ):
        self.field = field
        self.reason = message
        self.code = code
        super().__init__(f"{field}: {message}")


class ConceptKind(str, Enum):
    """Versioned domain taxonomy independent of the current page layout."""

    SOURCE_MODULE = "source-module"
    CODE_ENTITY = "code-entity"
    WORKFLOW = "workflow"
    GUIDE = "guide"
    USER_FLOW = "user-flow"
    INFRASTRUCTURE_RESOURCE = "infrastructure-resource"
    API_CONTRACT = "api-contract"
    DEPENDENCY_VIEW = "dependency-view"
    NAVIGATION_DOCUMENT = "navigation-document"
    CHANGE_LOG_DOCUMENT = "change-log-document"

    @property
    def is_document_only(self) -> bool:
        return self in {
            ConceptKind.NAVIGATION_DOCUMENT,
            ConceptKind.CHANGE_LOG_DOCUMENT,
        }


class Origin(str, Enum):
    UNKNOWN = "unknown"
    EXTRACTED = "extracted"
    AUTHORED = "authored"
    INFERRED = "inferred"
    IMPORTED = "imported"
    MARKDOWN = "markdown"
    GOVERNANCE = "governance"


class EvidenceState(str, Enum):
    UNKNOWN = "unknown"
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"
    NOT_APPLICABLE = "not-applicable"


class Resolution(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


class TargetClass(str, Enum):
    """Classification of a relationship target, separate from resolution."""

    UNKNOWN = "unknown"
    CONCEPT = "concept"
    SOURCE = "source"
    EXTERNAL = "external"
    MAIL = "mail"
    ANCHOR = "anchor"
    ASSET = "asset"
    MALFORMED = "malformed"


class Verification(str, Enum):
    UNTRACKED = "untracked"
    UNVERIFIED = "unverified"
    MACHINE_CHECKED = "machine-checked"
    HUMAN_REVIEWED = "human-reviewed"
    FAILED = "failed"
    EXPIRED = "expired"


class Lifecycle(str, Enum):
    UNKNOWN = "unknown"
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class ComputedFreshness(str, Enum):
    """Live comparison outcomes; never serialized in the knowledge index."""

    UNKNOWN = "unknown"
    CURRENT = "current"
    NONSEMANTIC_SOURCE_CHANGE = "nonsemantic-source-change"
    SOURCE_CHANGED = "source-changed"
    BASIS_INCOMPATIBLE = "basis-incompatible"
    SOURCE_MISSING = "source-missing"


PAGE_KIND_TO_CONCEPT_KIND: Mapping[PageKind, ConceptKind] = MappingProxyType(
    {
        PageKind.INDEX: ConceptKind.NAVIGATION_DOCUMENT,
        PageKind.LOG: ConceptKind.CHANGE_LOG_DOCUMENT,
        PageKind.ENTITIES: ConceptKind.CODE_ENTITY,
        PageKind.MODULES: ConceptKind.SOURCE_MODULE,
        PageKind.WORKFLOWS: ConceptKind.WORKFLOW,
        PageKind.GUIDES: ConceptKind.GUIDE,
        PageKind.FLOWS: ConceptKind.USER_FLOW,
        PageKind.INFRASTRUCTURE: ConceptKind.INFRASTRUCTURE_RESOURCE,
        PageKind.API_CONTRACTS: ConceptKind.API_CONTRACT,
        PageKind.DEPENDENCIES: ConceptKind.DEPENDENCY_VIEW,
        PageKind.LOAD_ORDER: ConceptKind.DEPENDENCY_VIEW,
    }
)


def concept_kind_for_page_kind(value: Union[PageKind, str]) -> ConceptKind:
    """Map a presentation page kind to the v1 domain/document taxonomy."""

    try:
        page_kind = value if isinstance(value, PageKind) else PageKind(value)
    except (TypeError, ValueError) as exc:
        raise KnowledgeModelError(
            "page_kind", f"unsupported page kind {value!r}"
        ) from exc
    return PAGE_KIND_TO_CONCEPT_KIND[page_kind]
