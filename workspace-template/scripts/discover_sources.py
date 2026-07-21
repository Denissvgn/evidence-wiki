#!/usr/bin/env python3
"""Bounded source discovery and candidate lifecycle command surface.

Discovery proposes *candidate* sources; it never downloads, scrapes, clones, or
ingests the candidate contents (see docs/source-discovery.md). Provider-backed
routes enforce the disabled-by-default discovery gate and an explicit concrete
provider allow-list before transport. Offline candidate review and jurisdiction
inspection remain available without network authorization.

Implemented routes include request-backed arXiv/OpenAlex paper discovery,
general search, GitHub repository search, legal query planning/ranking, author
publication expansion, companion discovery, and fixture-backed standards
metadata. A discovered candidate is not evidence until it is explicitly
selected and acquired into ``raw/`` with provenance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc

from urllib.request import Request, urlopen

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_INVALID = 2
DISCOVERY_COMMANDS = ("academic", "search", "legal", "github", "authors", "companions", "standards")

# Trust tiers, ordered best (rank 0) to worst, from docs/source-discovery.md. The
# ordering is the policy authority for "outranks": a lower rank is more trusted.
TRUST_TIERS = (
    "official_primary",
    "primary_non_official",
    "secondary_reputable",
    "secondary_unknown",
    "unsafe_or_unusable",
)
TIER_RANK = {tier: index for index, tier in enumerate(TRUST_TIERS)}
OEH_TRUST_TIERS = (
    "official_primary",
    "official_secondary",
    "academic_primary",
    "vendor_primary",
    "implementation_primary",
    "aggregator",
    "unknown",
    "rejected",
)
TIER_RANK.update(
    {
        "official_secondary": 1,
        "academic_primary": 1,
        "vendor_primary": 1,
        "implementation_primary": 1,
        "aggregator": 2,
        "unknown": 3,
        "rejected": 4,
    }
)

# The durable candidate store (see docs/source-discovery.md). Candidates live
# under sources/discovery/, never directly under raw/.
CANDIDATE_STORE_RELATIVE = ("sources", "discovery", "candidates.jsonl")
WINDOWS_REPLACE_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25, 0.25, 0.25)
WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})

# --- Candidate lifecycle -----------------------------------------------------
# ``status`` remains the coarse legacy compatibility field consumed by older
# status/run surfaces. ``lifecycle_state`` is the authoritative state machine.
# Legacy new/selected/rejected/fetched records are mapped explicitly and never
# inferred to have passed through review.
CANDIDATE_STATUSES = ("new", "selected", "rejected", "fetched")
DEFAULT_CANDIDATE_STATUS = "new"
CANDIDATE_LIFECYCLE_VERSION = "2.0"
CANDIDATE_LIFECYCLE_STATES = (
    "proposed",
    "reviewed",
    "selected",
    "rejected",
    "deferred",
    "fetched",
    "failed",
    "superseded",
)
DEFAULT_CANDIDATE_LIFECYCLE_STATE = "proposed"
LEGACY_CANDIDATE_STATE_MAP = {
    "new": "proposed",
    "selected": "selected",
    "rejected": "rejected",
    "fetched": "fetched",
}
CANDIDATE_STATE_TRANSITIONS = {
    "proposed": ("reviewed", "selected", "rejected", "deferred", "superseded"),
    "reviewed": ("selected", "rejected", "deferred", "superseded"),
    "selected": ("rejected", "deferred", "fetched", "failed", "superseded"),
    "rejected": (),
    "deferred": ("reviewed", "selected", "rejected", "superseded"),
    "fetched": (),
    "failed": ("selected", "rejected", "deferred", "superseded"),
    "superseded": (),
}
CANDIDATE_STATE_TO_LEGACY_STATUS = {
    "proposed": "new",
    "reviewed": "new",
    "selected": "selected",
    "rejected": "rejected",
    "deferred": "new",
    "fetched": "fetched",
    "failed": "selected",
    "superseded": "rejected",
}
SELECTION_STATUSES = ("selected", "rejected", "duplicate", "obsolete", "needs_manual_review", "pending")
DEFAULT_SELECTION_STATUS = "pending"
FETCH_STATUSES = ("not_planned", "pending_manual_delivery", "planned", "fetched", "failed", "not_fetchable")
DEFAULT_FETCH_STATUS = "not_planned"
CANDIDATES_ACTOR = "discover_sources.py/candidates"
# Append-only audit trail of lifecycle events, alongside the candidate store.
CANDIDATE_AUDIT_RELATIVE = ("sources", "discovery", "audit.jsonl")
# Stable lock file that survives candidate-store temp-file replacement, so
# concurrent select/reject writers serialize instead of clobbering each other
# (mirrors the per-question workspace lock in question_claim.py).
CANDIDATE_LOCK_RELATIVE = ("sources", "discovery", ".locks", "candidates.lock")
# Academic provider calls consume a per-run budget before transport.  The
# append-only ledger is intentionally stored with the run-controller artifacts:
# candidates cannot account for zero-result/error calls, and acquisition
# provenance describes downloads rather than discovery requests.
ACADEMIC_PROVIDER_REQUESTS_FILENAME = "academic-provider-requests.jsonl"
ACADEMIC_PROVIDER_REQUESTS_LOCK_FILENAME = "academic-provider-requests.lock"
ACADEMIC_PROVIDER_ACCOUNTING_FIELD = "academic_provider_request_accounting"
ACADEMIC_PROVIDER_ACCOUNTING_SCHEMA_VERSION = "1.0"
ACADEMIC_PROVIDER_ACCOUNTING_FRESH_RUN_REMEDIATION = (
    "This active run predates durable academic provider accounting. Preserve it for audit, "
    "start a fresh run with `python3 scripts/run_controller.py start --run-id <new-run-id> "
    "--agent-id <agent-id>`, and retry discovery with the new run ID. Do not create the marker "
    "or ledger by hand."
)
DEFAULT_MAX_ACADEMIC_PROVIDER_REQUESTS_PER_RUN = 25
RUN_STATES = frozenset(
    {
        "initialized",
        "planned",
        "discovering",
        "candidates_ready",
        "fetch_planned",
        "fetching",
        "evidence_ready",
        "answering",
        "verifying",
        "complete",
        "blocked_on_sources",
        "no_ship",
        "failed",
    }
)
RUN_TERMINAL_STATES = frozenset({"complete", "blocked_on_sources", "no_ship", "failed"})
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Map a candidate source_type to a source-request kind when minting a request.
SOURCE_TYPE_TO_REQUEST_KIND = {
    "paper": "paper",
    "code_repository": "code",
    "dataset": "dataset",
    "project_page": "web",
    "publisher_page": "web",
    "supplemental_material": "web",
    "official_legal": "web",
    "standards_registry_entry": "web",
    "harmonised_standard_reference": "web",
    "product_requirement_guidance": "web",
    "geospatial_standard_register_entry": "web",
    "web_page": "web",
}
CANDIDATE_POLICY_DEFAULTS = {
    "paper": {
        "evidence_path": "academic_method_existence",
        "source_policy": "academic_indexed",
        "freshness_policy": "publication_identity",
        "identity_policy": "citation_id_resolves",
    },
    "code_repository": {
        "evidence_path": "github_implementation",
        "source_policy": "canonical_repository",
        "freshness_policy": "release_snapshot",
        "identity_policy": "repo_ref_resolves",
    },
    "official_legal": {
        "evidence_path": "legal_current_figure",
        "source_policy": "official_primary",
        "freshness_policy": "current_legal_figure",
        "identity_policy": "official_domain_match",
    },
    "standards_registry_entry": {
        "evidence_path": "standards_registry_reference",
        "source_policy": "official_standards_registry",
        "freshness_policy": "current_standard_reference",
        "identity_policy": "standard_designation_matches_registry",
    },
    "harmonised_standard_reference": {
        "evidence_path": "standards_registry_reference",
        "source_policy": "official_standards_registry",
        "freshness_policy": "current_product_requirement",
        "identity_policy": "registry_entry_matches_product_requirement",
    },
    "product_requirement_guidance": {
        "evidence_path": "product_requirement_profile",
        "source_policy": "official_primary",
        "freshness_policy": "current_product_requirement",
        "identity_policy": "official_domain_match",
    },
    "geospatial_standard_register_entry": {
        "evidence_path": "standards_registry_reference",
        "source_policy": "official_standards_registry",
        "freshness_policy": "current_standard_reference",
        "identity_policy": "standard_designation_matches_registry",
    },
    "dataset": {
        "evidence_path": "academic_method_existence",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "publisher_page": {
        "evidence_path": "academic_method_existence",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "supplemental_material": {
        "evidence_path": "academic_method_existence",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "project_page": {
        "evidence_path": "vendor_product_spec",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "web_page": {
        "evidence_path": "vendor_product_spec",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
}
DEFAULT_CANDIDATE_POLICY = CANDIDATE_POLICY_DEFAULTS["web_page"]

# --- General search discovery provider (E33-T01) -----------------------------
# A provider-neutral search interface: a configured backend (fixture, command, or
# HTTP) returns raw results, which are normalized into source_candidate records.
# No commercial API is hard-coded; an HTTP backend requires an explicit endpoint.
# Outputs are candidates for review, never raw provider dumps, and search never
# fetches or ingests a result it proposes.
SEARCH_DISCOVERED_BY = "discover_sources.py/search"
SEARCH_PROVIDERS = ("fixture", "command", "http")
SEARCH_COMMAND_TIMEOUT_SECONDS = 30.0
SEARCH_HTTP_TIMEOUT_SECONDS = 30.0
SEARCH_HTTP_MAX_ATTEMPTS = 3
SEARCH_USER_AGENT = "evidence-wiki discover_sources.py/1.0; general search discovery"
# Injected query placeholder for the command provider; substituted when present,
# otherwise the query is appended as the final argument.
SEARCH_QUERY_PLACEHOLDER = "{query}"
# Transport seam so tests exercise the HTTP adapter without real network I/O.
SEARCH_HTTP_TRANSPORT = None

# --- Reasoned search query planner (E33-T02) ---------------------------------
# A research need is expanded into a small, bounded set of explained queries
# before any backend is contacted. Planning is the default (read-only, no
# network); execution runs the planned queries only when the caller passes
# --execute. The intent selects which query templates apply.
SEARCH_INTENTS = ("paper", "code", "dataset", "legal", "web")
# Source-request kinds that map to a specific non-legal intent. Other kinds fall
# through to a jurisdiction-driven legal intent or the general 'web' intent.
REQUEST_KIND_TO_INTENT = {"paper": "paper", "code": "code", "dataset": "dataset"}
# Per-intent query templates: (query suffix, expected source type, rationale).
# An empty suffix means the bare research need.
SEARCH_INTENT_TEMPLATES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "paper": (
        ("", "paper", "Locate the paper or report directly by title or topic."),
        ("preprint", "paper", "Find an open-access preprint (for example arXiv) of the work."),
        ("doi", "paper", "Find the DOI or publisher landing page for the work."),
    ),
    "code": (
        ("", "code_repository", "Find the canonical project or repository by name."),
        ("github", "code_repository", "Find a GitHub repository that implements or releases the work."),
        ("source code", "code_repository", "Find a source-code or replication package."),
    ),
    "dataset": (
        ("dataset", "dataset", "Find a dataset matching the research need."),
        ("data download", "dataset", "Find a downloadable data distribution for the topic."),
    ),
    "web": (
        ("", "web_page", "Direct general web query for the research need."),
    ),
}
# Legal query terms (E33-T02). Each becomes an official-source-first query.
# Profile-driven official domain enumeration is layered on by E34.
SEARCH_LEGAL_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("statute", "Find the controlling statute or law text."),
    ("code", "Find the relevant legal code section."),
    ("regulation", "Find the implementing regulation."),
    ("administrative rule", "Find the administrative rule."),
    ("agency guidance", "Find official agency guidance."),
    ("court opinion", "Find a controlling court opinion."),
    ("official gazette", "Find the official gazette notice."),
)

# --- Legal discovery query planning (E34-T02) --------------------------------
# `legal --jurisdiction --topic` expands a (jurisdiction, topic) into an
# official-source-first query plan. Unlike the general `search` legal intent, it
# is profile-driven: the matched jurisdiction profile (E34-T01) supplies the
# official_domains allowlist, blocked_domains, and the per-category entry-point
# roots below. Each category is planned as one explained query so the plan
# distinguishes statutes, regulations, agency guidance, court opinions, official
# forms, and gazette/legislative-history notices. Planning is read-only and
# contacts no backend; legal candidate ranking (E34-T03) layers on once a backend
# executes the plan.
# (legal_category, query term, profile root field, rationale)
LEGAL_QUERY_CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    ("statute", "statute", "legislature_urls", "Find the controlling statute or law text."),
    ("regulation", "regulation", "regulator_urls", "Find the implementing administrative rule or regulation."),
    ("agency_guidance", "agency guidance", "regulator_urls", "Find official agency guidance interpreting the rule."),
    ("court_opinion", "court opinion", "court_urls", "Find a controlling court opinion."),
    ("official_form", "official form", "regulator_urls", "Find an official government form for the topic."),
    ("gazette_notice", "official gazette notice", "gazette_urls", "Find a legislative-history record or official gazette notice."),
)

# --- Search result trust ranking (E33-T03) -----------------------------------
# Normalized search results are ranked by the documented trust-tier policy
# (docs/source-discovery.md), never by the provider's own ordering. Provider rank
# is a weak relevance input only. classify_search_result applies the per-result
# policy; apply_search_trust_rejection runs the cross-result duplicate pass.
# Conservative official-source TLD heuristic: a .gov/.mil host is treated as an
# official_primary authority. Profile-driven official domains (E34) and the
# optional integrations.discovery.search.official_domains list layer on more.
SEARCH_OFFICIAL_TLDS = (".gov", ".mil")
# Direct-executable / installer / script downloads are suspicious for a generic
# web result (we cannot confirm what they run): rejected unsafe_or_unusable with
# a suspicious_download flag.
SEARCH_SUSPICIOUS_DOWNLOAD_EXTENSIONS = (
    ".exe", ".msi", ".msu", ".dmg", ".pkg", ".deb", ".rpm", ".apk", ".iso",
    ".jar", ".bat", ".cmd", ".sh", ".ps1", ".scr", ".vbs",
)
# Archive extensions are suspicious only for web/official_legal results: a
# dataset or code intent legitimately resolves to an archive, so the planned
# query's expected_source_type gates this signal.
SEARCH_ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tgz", ".7z", ".rar", ".gz", ".bz2")
# Archive extensions are acceptable (not suspicious) when the query plan was
# looking for one of these source types.
SEARCH_ARCHIVE_OK_SOURCE_TYPES = ("dataset", "code_repository", "supplemental_material")
# Mirror / scraped-copy / cache telltales. A host/path/title hit raises a
# possible_mirror flag (or unsafe_or_unusable when the title signals an
# unauthorized/prohibited copy). Same-title duplicates of an official source are
# handled separately by apply_search_trust_rejection.
SEARCH_MIRROR_HOST_TOKENS = ("mirror", "webcache", "scrape", "scraped")
SEARCH_MIRROR_PATH_TOKENS = ("/mirror/", "/mirrors/", "/cache/", "/cached/", "/scrap")
SEARCH_MIRROR_TITLE_TOKENS = ("mirror", "cached copy", "full-text mirror")
SEARCH_TERMS_PROHIBITED_TITLE_TOKENS = ("unauthorized", "pirated", "piracy", "prohibited copy")
# Risk-flag vocabulary used by search trust reasoning (mirrors the documented set
# in docs/source-discovery.md plus suspicious_download and stale_source).
SEARCH_RISK_SUSPICIOUS_DOWNLOAD = "suspicious_download"
SEARCH_RISK_POSSIBLE_MIRROR = "possible_mirror"
SEARCH_RISK_TERMS_PROHIBITED = "terms_prohibited"
SEARCH_RISK_UNKNOWN_OFFICIALNESS = "unknown_officialness"
SEARCH_RISK_LICENSE_UNCERTAIN = "license_uncertain"
SEARCH_RISK_TERMS_UNCERTAIN = "terms_uncertain"
SEARCH_RISK_STALE_SOURCE = "stale_source"
SEARCH_RISK_DUPLICATE_OF_OFFICIAL = "duplicate_of_official"
# A published date older than this raises a stale_source risk flag.
SEARCH_STALE_YEARS_THRESHOLD = 10
# Valid source_type values a provider hint may carry (mirrors the schema in
# docs/source-discovery.md).
ALLOWED_SEARCH_SOURCE_TYPES = (
    "paper", "code_repository", "dataset", "project_page", "supplemental_material",
    "publisher_page", "official_legal", "web_page",
)
# Token-overlap fraction at which a non-official result is treated as a
# lower-trust duplicate of an official result sharing the same run.
SEARCH_DUPLICATE_TITLE_OVERLAP = 0.6
SEARCH_TIER_TRUST_BASE = {
    "official_primary": 0.9,
    "primary_non_official": 0.7,
    "secondary_reputable": 0.6,
    "secondary_unknown": 0.4,
    "unsafe_or_unusable": 0.05,
}

# --- Legal candidate ranking (E34-T03) ---------------------------------------
# Recognized secondary legal databases and aggregators: reputable and widely
# cited, but NOT the official primary authority for a jurisdiction. They are
# retained as secondary_reputable (never silently dropped) and marked supplemental
# when an official source is available in the same run. Matched by host suffix, so
# subdomains (for example supreme.justia.com) match too.
LEGAL_SECONDARY_DB_HOSTS = (
    "law.cornell.edu", "justia.com", "findlaw.com", "casetext.com",
    "courtlistener.com", "leagle.com", "vlex.com", "ravel.com",
    "westlaw.com", "lexis.com", "lexisnexis.com", "heinonline.org",
    "openjurist.org", "oyez.org", "law.com",
)
# Title/snippet tokens that signal a superseded, repealed, or historical legal
# page (a stale-authority risk distinct from the date-based stale_source flag).
LEGAL_SUPERSEDED_TOKENS = (
    "repealed", "superseded", "historical version", "no longer in effect",
    "former version", "prior version", "obsolete", "rescinded",
)
LEGAL_RISK_SUPERSEDED = "superseded_or_historical"
# A non-official legal source kept only as supplemental because an official source
# for the jurisdiction is available in the same run.
LEGAL_RISK_SECONDARY_WHEN_OFFICIAL = "secondary_when_official_available"
LEGAL_DISCOVERED_BY = "discover_sources.py/legal"

# --- Author extraction (E35-T01) ---------------------------------------------
# `authors --source-id` reads a normalized paper source (and any provider author
# metadata captured on the manifest record) and emits a bounded author seed list
# with provenance and confidence. It is the read-only preparation path for author
# and publication expansion (E35-T02); it never infers personal data and uses only
# metadata already present in the source or provider response.
AUTHORS_DISCOVERED_BY = "discover_sources.py/authors"
AUTHOR_CONFIDENCE_TIERS = ("high", "medium", "low")
AUTHOR_CONFIDENCE_RANK = {tier: index for index, tier in enumerate(AUTHOR_CONFIDENCE_TIERS)}
# ORCID iD: four groups of four; the final character may be a check digit X.
# Matched anywhere so a bare id or an https://orcid.org/<id> URL both resolve.
ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])")

# --- Author publication discovery (E35-T02) ----------------------------------
# `authors --discover-publications` resolves each extracted author to an OpenAlex
# identity (by ORCID when present, otherwise by name plus context) and lists that
# author's works as `source_candidate` records of source_type `paper`. It proposes
# candidates for review and never downloads them. OpenAlex is a free, open
# scholarly index; no commercial API is enabled by default. OPENALEX_API_KEY is
# read from the environment only and is never written to output, the candidate
# store, or logs (only whether a key was used is reported).
# Request-backed academic discovery uses the same bounded scholarly APIs as
# author expansion, but starts from an open source request rather than an
# already-normalized seed paper.
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_ABS_URL = "https://arxiv.org/abs/{id}"
ARXIV_PDF_URL = "https://arxiv.org/pdf/{id}"
ARXIV_TERMS_URL = "https://info.arxiv.org/help/api/tou.html"
ARXIV_TIMEOUT_SECONDS = 30.0
ARXIV_MAX_ATTEMPTS = 3
ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
# Metadata responses are intentionally small and bounded.  The default
# transport reads at most limit + 1 bytes so an oversized response can be
# rejected without buffering the remainder; the post-transport check applies
# the same contract to injected/test transports.
ARXIV_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ARXIV_USER_AGENT = "evidence-wiki discover_sources.py/1.0; request-backed academic discovery"
ARXIV_TRANSPORT = None
ARXIV_SLEEP = time.sleep
ARXIV_CLOCK = time.monotonic
ARXIV_LAST_REQUEST_AT: float | None = None
ARXIV_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

OPENALEX_API_URL = "https://api.openalex.org"
OPENALEX_WORKS_PATH = "/works"
OPENALEX_AUTHORS_PATH = "/authors"
OPENALEX_TERMS_URL = "https://developers.openalex.org/api-reference/works"
OPENALEX_TIMEOUT_SECONDS = 30.0
OPENALEX_MAX_ATTEMPTS = 3
OPENALEX_REQUEST_INTERVAL_SECONDS = 1.0
OPENALEX_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
OPENALEX_USER_AGENT = (
    "evidence-wiki discover_sources.py/1.0; "
    "OpenAlex author-publication discovery; set OPENALEX_API_KEY for higher usage limits"
)
OPENALEX_DISCOVERED_BY = "discover_sources.py/authors/publications"
# Hard bounds so discovery never resembles crawling: at most this many seed
# authors expanded per run, this many works fetched per author, and this many
# candidates proposed overall (--max-results caps the final list below this).
OPENALEX_DISCOVERY_MAX_AUTHORS = 10
OPENALEX_DISCOVERY_PER_AUTHOR = 25
OPENALEX_AUTHOR_SEARCH_PER_PAGE = 5
OPENALEX_DISCOVERY_MAX_RESULTS_CAP = 50
OPENALEX_TRANSPORT = None
OPENALEX_SLEEP = time.sleep
OPENALEX_CLOCK = time.monotonic
OPENALEX_LAST_REQUEST_AT: float | None = None
# Identity-match vocabulary recorded per author and embedded in candidate
# reasoning. `orcid_exact` is a certain match; `name_resolved` is an inferred
# single best match (carries identity_inferred); `ambiguous`/`no_match` emit no
# publication candidates for that author and record a review-required warning.
AUTHOR_IDENTITY_ORCID_EXACT = "orcid_exact"
AUTHOR_IDENTITY_NAME_RESOLVED = "name_resolved"
AUTHOR_IDENTITY_AMBIGUOUS = "ambiguous"
AUTHOR_IDENTITY_NO_MATCH = "no_match"
AUTHOR_IDENTITY_CONTEXT_MISSING = "context_missing"
# A name-search result must clear this token-overlap threshold against the seed
# author name to count as a resolved single match; otherwise it is ambiguous.
AUTHOR_NAME_RESOLVE_OVERLAP = 0.6
# Publication-candidate risk-flag vocabulary (mirrors the documented set in
# docs/source-discovery.md plus publication-specific signals).
PUBLICATION_RISK_IDENTITY_INFERRED = "identity_inferred"
PUBLICATION_RISK_AMBIGUOUS_IDENTITY = "ambiguous_identity"
PUBLICATION_RISK_OUT_OF_SCOPE = "out_of_scope"
PUBLICATION_RISK_NOT_OPEN_ACCESS = "not_open_access"
# Canonical-id parsers: an OpenAlex work id is W<digits>, an author id is A<digits>
# (OpenAlex returns them as https://openalex.org/<id> URLs). A DOI is 10.<prefix>/<suffix>.
OPENALEX_WORK_ID_RE = re.compile(r"(W\d+)", re.IGNORECASE)
OPENALEX_AUTHOR_ID_RE = re.compile(r"(A\d+)", re.IGNORECASE)
DOI_RE = re.compile(r"^10\.\S+/.+", re.IGNORECASE)

# --- Companion artifact discovery (E35-T03) ----------------------------------
# `companions --source-id` finds a paper's companion repositories, datasets,
# project pages, supplemental material, and publisher pages. It prefers links
# already present in the paper body/frontmatter or its provider metadata (highest
# trust, because the paper itself cites them), then falls back to GitHub repository
# discovery (E32-T02) and the configured general search provider (E33) for breadth.
# It proposes candidates for review and never fetches or executes anything; a
# paper-centered composite of the other discovery providers.
COMPANIONS_DISCOVERED_BY = "discover_sources.py/companions"
COMPANION_DISCOVERY_MAX_RESULTS_CAP = 50
# Per-phase bounds so companion discovery stays bounded (no crawling).
COMPANION_GITHUB_MAX_RESULTS = 10
COMPANION_SEARCH_MAX_RESULTS = 10
# Inline URL extraction from the normalized paper body (precedent: source_inventory
# URL_EXTRACT_RE). The class strips trailing punctuation that is not part of a URL.
COMPANION_URL_RE = re.compile(r"https?://[^\s<>'\")\]]+")
# Where a companion candidate came from. Paper-inline links and provider metadata
# are highest trust (the paper cites them); github_search and search are
# progressively lower-trust fallbacks. Origin is the major ranking input.
COMPANION_ORIGIN_PAPER_INLINE = "paper_inline"
COMPANION_ORIGIN_PROVIDER_METADATA = "provider_metadata"
COMPANION_ORIGIN_GITHUB_SEARCH = "github_search"
COMPANION_ORIGIN_SEARCH = "search"
COMPANION_ORIGIN_RANK = {
    COMPANION_ORIGIN_PAPER_INLINE: 0,
    COMPANION_ORIGIN_PROVIDER_METADATA: 1,
    COMPANION_ORIGIN_GITHUB_SEARCH: 2,
    COMPANION_ORIGIN_SEARCH: 3,
}
COMPANION_REPOSITORY_ORIGIN_CONFIDENCE = {
    COMPANION_ORIGIN_PAPER_INLINE: "paper_linked",
    COMPANION_ORIGIN_PROVIDER_METADATA: "provider_metadata",
    COMPANION_ORIGIN_GITHUB_SEARCH: "search_only",
    COMPANION_ORIGIN_SEARCH: "search_only",
}
# Host -> source_type classification for companion links (matched by host suffix
# so subdomains classify too). Recognized dataset/repository/preprint hosts are
# primary_non_official; a DOI/publisher landing page is the canonical publisher
# (official_primary); a generic host defaults to project_page.
COMPANION_REPOSITORY_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
COMPANION_DATASET_HOSTS = (
    "zenodo.org", "figshare.com", "huggingface.co", "dryad.org",
    "datadryad.org", "osf.io", "kaggle.com", "openneuro.org",
)
COMPANION_PUBLISHER_HOSTS = ("doi.org", "dx.doi.org")
COMPANION_PREPRINT_HOSTS = ("arxiv.org", "biorxiv.org", "medrxiv.org", "chemrxiv.org")
# Companion search is driven by a small, explainable query plan rather than the
# title alone (E35-T03 instructions: title, author names, DOI/arXiv id, project
# names). Bounds keep it from crawling: at most this many queries per network
# phase, a few lead-author surnames, and a short pre-colon title segment as the
# candidate project/system name (the common "SystemName: subtitle" convention).
COMPANION_MAX_QUERIES_PER_PHASE = 3
COMPANION_MAX_AUTHOR_SEEDS = 5
COMPANION_MAX_AUTHOR_SURNAMES = 2
COMPANION_PROJECT_NAME_MAX_WORDS = 4
COMPANION_PROJECT_NAME_MAX_LEN = 40

# --- Jurisdiction profiles (E34-T01) -----------------------------------------
# A workspace-local, user-editable file (default sources/jurisdictions.yml) lists
# jurisdiction profiles: the official domains and legislature/regulator/court/
# gazette roots for a country or state. Profiles drive official-source-first
# legal discovery (E34-T02/T03). They are NOT a shipped universal legal database;
# each workspace curates its own, validated by the `jurisdictions` subcommand.
JURISDICTION_PROFILE_SCHEMA_VERSION = "1.0"
JURISDICTIONS_DEFAULT_RELATIVE = ("sources", "jurisdictions.yml")
# jurisdiction_id must be a lowercase slug (matches the ids legal discovery and
# the candidate jurisdiction field already use, e.g. us-federal, us-ca).
JURISDICTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Fields of a profile that each contribute official source roots. A profile is
# invalid unless at least one is non-empty.
JURISDICTION_OFFICIAL_ROOT_FIELDS = (
    "official_domains",
    "legislature_urls",
    "regulator_urls",
    "court_urls",
    "gazette_urls",
)
# URL-list fields validated as http(s) URLs; domain-list fields normalized.
JURISDICTION_URL_FIELDS = JURISDICTION_OFFICIAL_ROOT_FIELDS[1:]
JURISDICTION_DOMAIN_FIELDS = ("official_domains", "blocked_domains")
JURISDICTION_DISCOVERED_BY = "discover_sources.py/jurisdictions"

# --- GitHub discovery provider (E32-T02) -------------------------------------
# Discovery searches GitHub repository metadata through a transport-injected
# adapter. It never clones a repository, downloads an archive, or reads file
# contents (see docs/source-discovery.md and docs/acquisition.md). GITHUB_TOKEN
# is read from the environment only and is never written to output, the candidate
# store, or logs.
GITHUB_API_URL = "https://api.github.com"
GITHUB_SEARCH_REPOSITORIES_PATH = "/search/repositories"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_TERMS_URL = "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service"
GITHUB_TIMEOUT_SECONDS = 30.0
GITHUB_MAX_ATTEMPTS = 3
GITHUB_REQUEST_INTERVAL_SECONDS = 2.0
# GitHub caps search results at 100 per page; discovery stays well under that to
# keep queries bounded and avoid anything resembling crawling.
GITHUB_MAX_RESULTS_CAP = 50
GITHUB_USER_AGENT = (
    "evidence-wiki discover_sources.py/1.0; "
    "GitHub repository discovery; set GITHUB_TOKEN for higher rate limits"
)
GITHUB_DISCOVERED_BY = "discover_sources.py/github"
GITHUB_TRANSPORT = None
GITHUB_SLEEP = time.sleep
GITHUB_CLOCK = time.monotonic
GITHUB_LAST_REQUEST_AT: float | None = None
# Tokenizer used for query-term overlap scoring (not related to GITHUB_TOKEN).
_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+")

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _provider_registry import (
    DISCOVERY_ACCEPTED_IDS,
    STANDARDS_DISCOVERY_PROVIDER_IDS,
    ProviderListError,
    provider_is_allowed,
    validate_provider_ids,
)
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError, workspace_lock

STANDARDS_PROVIDER_IDS = STANDARDS_DISCOVERY_PROVIDER_IDS
DISCOVERY_PROVIDER_REGISTRY = DISCOVERY_ACCEPTED_IDS


class DiscoverSourcesError(Exception):
    """Structured discovery failure with a stable machine error code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        recoverable: bool = True,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.recoverable = recoverable
        self.remediation = remediation
        self.details = details


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Propose candidate sources (read-only). Discovery emits ranked, "
            "explained candidates and never downloads, scrapes, clones, or "
            "ingests them. Provider-backed commands perform bounded network I/O "
            "only when their concrete provider is explicitly enabled."
        ),
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("text", "json"),
        default="text",
        help="Report format for command output and fatal errors. Defaults to text.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    academic = commands.add_parser(
        "academic",
        help="Search explicitly enabled scholarly providers for one source request; never fetch evidence.",
    )
    academic.add_argument("--request-id", required=True, help="Open source-request id that defines the research need.")
    academic.add_argument(
        "--provider",
        action="append",
        choices=("arxiv", "openalex"),
        required=True,
        help="Scholarly discovery provider. Repeat to search both arxiv and openalex.",
    )
    academic.add_argument(
        "--query",
        help="Optional refined query. Defaults to the source request query_or_identifier value.",
    )
    academic.add_argument("--max-results", type=positive_int, default=15, help="Maximum deduplicated candidates.")
    academic.add_argument(
        "--run-id",
        default=None,
        help=(
            "Active run-controller id used for restart-stable academic provider-call budgets. "
            "When omitted, the sole active run is selected automatically."
        ),
    )
    academic.add_argument(
        "--format",
        dest="output_format",
        choices=("text", "json"),
        default=argparse.SUPPRESS,
        help="Report format; accepted here as well as before the subcommand for copy-paste workflows.",
    )

    search = commands.add_parser(
        "search",
        help="Propose candidate sources through a configured search provider (read-only).",
    )
    search.add_argument("--query", required=True, help="Search query text.")
    search.add_argument(
        "--request-id",
        help="Optional source-request id to link discovered candidates to. Omit for an exploratory query.",
    )
    search.add_argument("--max-results", type=positive_int, default=10, help="Maximum candidates to propose.")
    search.add_argument(
        "--domain-allow",
        action="append",
        dest="domain_allowlist",
        default=None,
        metavar="DOMAIN",
        help="Only keep results from this domain (or its subdomains). Repeatable.",
    )
    search.add_argument(
        "--domain-block",
        action="append",
        dest="domain_blocklist",
        default=None,
        metavar="DOMAIN",
        help="Drop results from this domain (or its subdomains). Repeatable.",
    )
    search.add_argument(
        "--jurisdiction",
        help="Optional jurisdiction id for jurisdiction-aware discovery, for example us-federal.",
    )
    search.add_argument(
        "--intent",
        choices=SEARCH_INTENTS,
        help="Override the inferred research intent used to plan queries (else from --request-id kind or --jurisdiction).",
    )
    search.add_argument(
        "--execute",
        action="store_true",
        help="Execute the planned queries through the configured search provider. Without it, search only plans (read-only, no network).",
    )

    legal = commands.add_parser(
        "legal",
        help="Plan official-source-first legal/regulatory queries; --execute ranks candidates.",
    )
    legal.add_argument("--jurisdiction", required=True, help="Jurisdiction id or name, for example us-federal.")
    legal.add_argument("--topic", required=True, help="Legal/regulatory topic to discover sources for.")
    legal.add_argument("--max-results", type=positive_int, default=10, help="Maximum candidates to propose.")
    legal.add_argument(
        "--execute",
        action="store_true",
        help="Run the planned queries through the configured search backend and rank legal candidates by officialness. Without it, planning is read-only and contacts no backend.",
    )

    github = commands.add_parser(
        "github",
        help="Propose GitHub repository candidates (read-only discovery; never clones or downloads).",
    )
    github.add_argument("--query", required=True, help="Repository or code search query text.")
    github.add_argument("--max-results", type=positive_int, default=10, help="Maximum candidates to propose.")
    github.add_argument(
        "--request-id",
        help="Optional source-request id to link discovered candidates to. Omit for an exploratory query.",
    )

    authors = commands.add_parser(
        "authors",
        help="Extract an author seed list from a seed source; with --discover-publications, propose related publications.",
    )
    authors.add_argument("--source-id", required=True, help="Manifest source id to expand from, for example paper:2601.00001v1.")
    authors.add_argument("--max-results", type=positive_int, default=10, help="Maximum authors (default) or publication candidates (with --discover-publications) to propose.")
    authors.add_argument(
        "--discover-publications",
        action="store_true",
        help=(
            "Query OpenAlex for each author's works and propose related publication candidates "
            "(network I/O). Without it, the command is read-only author extraction only."
        ),
    )
    authors.add_argument(
        "--run-id",
        default=None,
        help=(
            "Active run-controller id used for restart-stable OpenAlex provider-call budgets. "
            "Used only with --discover-publications; the sole active run is selected automatically when omitted."
        ),
    )

    companions = commands.add_parser(
        "companions",
        help="Propose a paper's companion repositories, datasets, project pages, and publisher pages.",
    )
    companions.add_argument("--source-id", required=True, help="Manifest paper source id to expand from.")
    companions.add_argument("--max-results", type=positive_int, default=10, help="Maximum companion candidates to propose.")
    companions.add_argument(
        "--request-id",
        help="Optional source-request id to link discovered candidates to. Omit for an exploratory run.",
    )
    companions.add_argument(
        "--no-github",
        action="store_true",
        help="Skip the GitHub repository discovery phase (inline + search phases still run).",
    )
    companions.add_argument(
        "--no-search",
        action="store_true",
        help="Skip the general-search phase (inline + GitHub phases still run).",
    )

    standards = commands.add_parser(
        "standards",
        help="Propose standards-registry candidates from explicit fixture snapshots.",
    )
    standards_commands = standards.add_subparsers(dest="standards_provider", required=True)

    iso = standards_commands.add_parser("iso-open-data", help="Read ISO Open Data deliverable metadata from a fixture.")
    iso_lookup = iso.add_mutually_exclusive_group()
    iso_lookup.add_argument("--designation", help="Exact ISO designation to match, for example 'ISO 19131:2022'.")
    iso_lookup.add_argument("--query", help="Title/designation query used for review candidates.")
    iso.add_argument("--fixture", help="JSONL fixture containing ISO Open Data deliverable records.")
    iso.add_argument("--ics-fixture", help="Optional CSV fixture mapping ICS code to title.")
    iso.add_argument("--attribution-fixture", help="Optional JSON fixture with ISO Open Data terms and attribution metadata.")
    iso.add_argument("--max-results", type=positive_int, default=10, help="Maximum standards candidates to propose.")

    eu = standards_commands.add_parser(
        "eu-product-requirements",
        help="Read EU product guidance, harmonised standard, and OJEU fixture records.",
    )
    eu.add_argument("--query", required=True, help="Product category or compliance query.")
    eu.add_argument("--guidance-fixture", help="HTML fixture for Your Europe or Commission guidance.")
    eu.add_argument("--harmonised-fixture", help="HTML fixture for Commission harmonised-standard rows.")
    eu.add_argument("--ojeu-fixture", help="JSON fixture for OJEU/EUR-Lex legal authority metadata.")
    eu.add_argument("--max-results", type=positive_int, default=10, help="Maximum standards candidates to propose.")

    uk = standards_commands.add_parser(
        "uk-geospatial-register",
        help="Read GOV.UK geospatial standards register rows from a fixture.",
    )
    uk.add_argument("--query", required=True, help="Geospatial register search query.")
    uk.add_argument("--fixture", help="HTML fixture for the GOV.UK geospatial standards register.")
    uk.add_argument("--max-results", type=positive_int, default=10, help="Maximum standards candidates to propose.")

    nist = standards_commands.add_parser(
        "nist",
        help="Read NIST standards guidance and concrete publication records from fixtures.",
    )
    nist.add_argument("--query", required=True, help="NIST standards query, for example 'FIPS 140-3'.")
    nist.add_argument("--guidance-fixture", help="HTML fixture for NIST SIC or Standards.gov guidance.")
    nist.add_argument("--publication-fixture", help="JSON fixture for a concrete NIST CSRC/FIPS/SP publication.")
    nist.add_argument("--max-results", type=positive_int, default=10, help="Maximum standards candidates to propose.")

    candidates = commands.add_parser(
        "candidates",
        help="Review and select discovered candidates (read/write, never network).",
    )
    candidate_commands = candidates.add_subparsers(dest="candidates_command", required=True)

    cand_list = candidate_commands.add_parser(
        "list",
        help="List candidates from the durable store, optionally filtered by lifecycle status.",
    )
    cand_list.add_argument(
        "--status",
        action="append",
        choices=CANDIDATE_STATUSES,
        default=None,
        help="Only include candidates with this lifecycle status. Repeatable.",
    )
    cand_list.add_argument(
        "--request-id",
        help="Only include candidates proposed for or selected for this source request.",
    )
    cand_list.add_argument(
        "--state",
        action="append",
        choices=CANDIDATE_LIFECYCLE_STATES,
        default=None,
        help="Only include candidates in this canonical lifecycle state. Repeatable.",
    )

    cand_select = candidate_commands.add_parser(
        "select",
        help="Select a candidate and link it to a source request. Never fetches.",
    )
    cand_select.add_argument("--candidate-id", required=True, help="Candidate id to select.")
    cand_select.add_argument(
        "--request-id",
        help="Existing source request to link the candidate to. Omit when using --create-request.",
    )
    cand_select.add_argument(
        "--create-request",
        action="store_true",
        help="Mint a new source request derived from the candidate and link it.",
    )
    cand_select.add_argument(
        "--priority",
        choices=("high", "medium", "low"),
        default="medium",
        help="Priority for a request created with --create-request. Defaults to medium.",
    )
    cand_select.add_argument(
        "--question-slug",
        action="append",
        dest="question_slugs",
        default=None,
        help="Question slug a created request unblocks. Repeatable; only used with --create-request.",
    )
    cand_select.add_argument(
        "--reason",
        help="Selection rationale; autonomous runs should cite the candidate trust_tier and evidence fit.",
    )
    cand_select.add_argument("--expected-state", choices=CANDIDATE_LIFECYCLE_STATES)
    cand_select.add_argument("--actor", default=CANDIDATES_ACTOR, help="Actor recorded in the lifecycle audit event.")
    cand_select.add_argument("--run-id", help="Optional run correlation recorded in the lifecycle audit event.")

    cand_reject = candidate_commands.add_parser(
        "reject",
        help="Reject a candidate with a recorded reason. Never fetches.",
    )
    cand_reject.add_argument("--candidate-id", required=True, help="Candidate id to reject.")
    cand_reject.add_argument("--reason", required=True, help="Why the candidate is rejected.")
    cand_reject.add_argument("--expected-state", choices=CANDIDATE_LIFECYCLE_STATES)
    cand_reject.add_argument("--actor", default=CANDIDATES_ACTOR, help="Actor recorded in the lifecycle audit event.")
    cand_reject.add_argument("--run-id", help="Optional run correlation recorded in the lifecycle audit event.")

    cand_transition = candidate_commands.add_parser(
        "transition",
        help="Apply an explicit candidate lifecycle transition. Never fetches or contacts a provider.",
    )
    cand_transition.add_argument("--candidate-id", required=True)
    cand_transition.add_argument("--expected-state", required=True, choices=CANDIDATE_LIFECYCLE_STATES)
    cand_transition.add_argument("--to-state", required=True, choices=CANDIDATE_LIFECYCLE_STATES)
    cand_transition.add_argument("--reason", required=True)
    cand_transition.add_argument("--actor", default=CANDIDATES_ACTOR)
    cand_transition.add_argument("--run-id")
    cand_transition.add_argument("--request-id", help="Required when transitioning to selected.")
    cand_transition.add_argument("--source-id", help="Required when transitioning to fetched.")
    cand_transition.add_argument(
        "--superseded-by-candidate-id",
        help="Required when transitioning to superseded.",
    )

    jurisdictions = commands.add_parser(
        "jurisdictions",
        help="Validate and inspect jurisdiction profiles (read-only, never network).",
    )
    jurisdiction_commands = jurisdictions.add_subparsers(dest="jurisdictions_command", required=True)

    jurisdiction_commands.add_parser(
        "validate",
        help="Validate the workspace jurisdiction profile file (sources/jurisdictions.yml).",
    )
    jurisdiction_commands.add_parser(
        "list",
        help="List configured jurisdiction profiles.",
    )
    jurisdiction_show = jurisdiction_commands.add_parser(
        "show",
        help="Show one jurisdiction profile.",
    )
    jurisdiction_show.add_argument("--jurisdiction", required=True, help="Jurisdiction id, for example us-federal.")

    return parser.parse_args(argv)


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "research.yml"
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit(f"Invalid config: {config_path}")
    return config


def integrations_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("integrations")
    return value if isinstance(value, dict) else {}


def require_non_empty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiscoverSourcesError(
            "VALUE_INVALID",
            f"{label} must be a non-empty value.",
            remediation="Pass non-empty option values.",
        )
    return value.strip()


def validate_command_arguments(args: argparse.Namespace) -> None:
    if args.command == "academic":
        require_non_empty(args.request_id, "--request-id")
        if args.run_id is not None:
            require_non_empty(args.run_id, "--run-id")
        if args.query is not None:
            require_non_empty(args.query, "--query")
        duplicates = sorted({provider for provider in args.provider if args.provider.count(provider) > 1})
        if duplicates:
            raise DiscoverSourcesError(
                "VALUE_INVALID",
                f"--provider has duplicate provider(s): {', '.join(duplicates)}.",
                remediation="Pass each academic provider at most once.",
                details={"command": "academic", "network_io_executed": False},
            )
    elif args.command == "search":
        require_non_empty(args.query, "--query")
        if getattr(args, "request_id", None) is not None:
            require_non_empty(args.request_id, "--request-id")
    elif args.command == "legal":
        require_non_empty(args.jurisdiction, "--jurisdiction")
        require_non_empty(args.topic, "--topic")
    elif args.command == "github":
        require_non_empty(args.query, "--query")
        if getattr(args, "request_id", None) is not None:
            require_non_empty(args.request_id, "--request-id")
    elif args.command == "authors":
        require_non_empty(args.source_id, "--source-id")
        if args.run_id is not None:
            require_non_empty(args.run_id, "--run-id")
    elif args.command == "companions":
        require_non_empty(args.source_id, "--source-id")
        if getattr(args, "request_id", None) is not None:
            require_non_empty(args.request_id, "--request-id")
    elif args.command == "standards":
        validate_standards_arguments(args)
    elif args.command == "candidates":
        validate_candidates_arguments(args)
    elif args.command == "jurisdictions":
        if args.jurisdictions_command == "show":
            require_non_empty(args.jurisdiction, "--jurisdiction")


def validate_standards_arguments(args: argparse.Namespace) -> None:
    provider = args.standards_provider
    if provider == "iso-open-data":
        if getattr(args, "designation", None) is not None:
            require_non_empty(args.designation, "--designation")
        if getattr(args, "query", None) is not None:
            require_non_empty(args.query, "--query")
        if args.designation is None and args.query is None:
            raise DiscoverSourcesError(
                "VALUE_INVALID",
                "Pass --designation or --query for standards iso-open-data.",
                remediation="Use --designation for exact standard lookups, or --query for broader fixture review.",
            )
    elif provider in {"eu-product-requirements", "uk-geospatial-register", "nist"}:
        require_non_empty(args.query, "--query")


def validate_candidates_arguments(args: argparse.Namespace) -> None:
    sub = args.candidates_command
    if sub == "select":
        require_non_empty(args.candidate_id, "--candidate-id")
        require_non_empty(args.actor, "--actor")
        if args.run_id is not None:
            require_non_empty(args.run_id, "--run-id")
        if args.reason is not None:
            require_non_empty(args.reason, "--reason")
        request_id = getattr(args, "request_id", None)
        has_request = isinstance(request_id, str) and request_id.strip() != ""
        if request_id is not None and not has_request:
            raise DiscoverSourcesError(
                "VALUE_INVALID",
                "--request-id must be a non-empty value.",
                remediation="Pass a real source-request id, or use --create-request instead.",
            )
        if bool(args.create_request) == has_request:
            raise DiscoverSourcesError(
                "VALUE_INVALID",
                "Pass exactly one of --request-id or --create-request.",
                remediation=(
                    "Link the candidate to an existing source request with --request-id, "
                    "or mint a new one with --create-request."
                ),
            )
    elif sub == "reject":
        require_non_empty(args.candidate_id, "--candidate-id")
        require_non_empty(args.reason, "--reason")
        require_non_empty(args.actor, "--actor")
        if args.run_id is not None:
            require_non_empty(args.run_id, "--run-id")
    elif sub == "transition":
        require_non_empty(args.candidate_id, "--candidate-id")
        require_non_empty(args.reason, "--reason")
        require_non_empty(args.actor, "--actor")
        if args.run_id is not None:
            require_non_empty(args.run_id, "--run-id")
        if args.to_state == "selected":
            require_non_empty(args.request_id, "--request-id")
        if args.to_state == "fetched":
            require_non_empty(args.source_id, "--source-id")
        if args.to_state == "superseded":
            require_non_empty(args.superseded_by_candidate_id, "--superseded-by-candidate-id")


def discovery_disabled(command: str, message: str) -> DiscoverSourcesError:
    return DiscoverSourcesError(
        "DISCOVERY_DISABLED",
        message,
        remediation=(
            "Set integrations.discovery.enabled: true and list the concrete provider in "
            "integrations.discovery.providers before retrying."
        ),
        details={"command": command, "network_io_executed": False},
    )


def require_discovery_enabled(config: dict[str, Any], command: str) -> dict[str, Any]:
    """Enforce the disabled-by-default discovery gate.

    Mirrors the acquisition gate: a missing or `enabled: false`
    `integrations.discovery` block refuses with `DISCOVERY_DISABLED`. Like
    acquisition, discovery stays disabled until a workspace explicitly opts in.
    """
    discovery = integrations_config(config).get("discovery")
    if discovery is None:
        raise discovery_disabled(
            command,
            "Discovery is disabled: missing integrations.discovery in research.yml.",
        )
    if not isinstance(discovery, dict):
        raise SystemExit("research.yml integrations.discovery must be a mapping")
    enabled = discovery.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SystemExit("research.yml integrations.discovery.enabled must be a boolean")
    if not enabled:
        raise discovery_disabled(
            command,
            "Discovery is disabled: integrations.discovery.enabled is not true.",
        )
    validate_discovery_provider_list(discovery.get("providers", []), "integrations.discovery.providers")
    return discovery


def validate_discovery_provider_list(value: Any, label: str) -> list[str]:
    try:
        validated = validate_provider_ids(value, phase="discovery")
    except ProviderListError as exc:
        raise SystemExit(f"research.yml {label} {exc}") from exc
    return list(validated.configured)


def require_discovery_provider_allowed(command: str, discovery: dict[str, Any], provider_ids: tuple[str, ...]) -> None:
    try:
        providers = validate_provider_ids(discovery.get("providers", []), phase="discovery")
    except ProviderListError as exc:
        raise SystemExit(f"research.yml integrations.discovery.providers {exc}") from exc
    if any(provider_is_allowed(providers, provider_id) for provider_id in provider_ids):
        return
    expected = " or ".join(provider_ids)
    raise DiscoverSourcesError(
        "DISCOVERY_PROVIDER_DISABLED",
        f"Discovery provider {expected!r} is not listed in integrations.discovery.providers.",
        remediation=(
            "Add an explicit provider entry to integrations.discovery.providers before running this discovery route. "
            "Use 'standards' for all standards fixture routes, or a route-specific value such as "
            "'standards:iso-open-data'."
        ),
        details={"command": command, "providers": list(provider_ids), "network_io_executed": False},
    )


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def relative_label(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def academic_provider_requests_path(project_root: Path, run_id: str) -> Path:
    return project_root / "runs" / run_id / ACADEMIC_PROVIDER_REQUESTS_FILENAME


def academic_provider_requests_lock_path(project_root: Path, run_id: str) -> Path:
    return project_root / "runs" / run_id / ".locks" / ACADEMIC_PROVIDER_REQUESTS_LOCK_FILENAME


def expected_academic_provider_accounting(run_id: str) -> dict[str, str]:
    return {
        "schema_version": ACADEMIC_PROVIDER_ACCOUNTING_SCHEMA_VERSION,
        "ledger_path": f"runs/{run_id}/{ACADEMIC_PROVIDER_REQUESTS_FILENAME}",
    }


def validate_academic_provider_accounting(
    project_root: Path,
    run_id: str,
    *,
    run_state: dict[str, Any] | None = None,
) -> Path:
    """Validate the versioned run-state marker and its run-owned ledger."""

    if run_state is None:
        state_path = project_root / "runs" / run_id / "run-state.json"
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DiscoverSourcesError(
                "ACADEMIC_PROVIDER_ACCOUNTING_INVALID",
                f"Cannot validate academic provider accounting for run {run_id}: {exc}",
                recoverable=False,
                remediation=(
                    "Preserve the affected run for audit, restore its verified run state and accounting "
                    "artifacts from a trusted backup, or start a fresh run."
                ),
                details={"command": "academic", "run_id": run_id, "network_io_executed": False},
            ) from exc
        run_state = loaded if isinstance(loaded, dict) else None

    marker = run_state.get(ACADEMIC_PROVIDER_ACCOUNTING_FIELD) if isinstance(run_state, dict) else None
    if marker is None:
        raise DiscoverSourcesError(
            "ACADEMIC_PROVIDER_ACCOUNTING_UNINITIALIZED",
            f"Run {run_id} has no durable academic provider accounting marker.",
            recoverable=False,
            remediation=ACADEMIC_PROVIDER_ACCOUNTING_FRESH_RUN_REMEDIATION,
            details={"command": "academic", "run_id": run_id, "network_io_executed": False},
        )

    expected = expected_academic_provider_accounting(run_id)
    if not isinstance(marker, dict) or marker != expected:
        raise DiscoverSourcesError(
            "ACADEMIC_PROVIDER_ACCOUNTING_INVALID",
            f"Run {run_id} has an invalid academic provider accounting marker.",
            recoverable=False,
            remediation=(
                "Preserve the affected run for audit, restore the exact verified marker and ledger from a "
                "trusted backup, or start a fresh run. Do not reconstruct accounting by hand."
            ),
            details={
                "command": "academic",
                "run_id": run_id,
                "expected": expected,
                "network_io_executed": False,
            },
        )

    path = academic_provider_requests_path(project_root, run_id)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DiscoverSourcesError(
            "ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID",
            f"Cannot inspect academic provider-call ledger {relative_label(project_root, path)}: {exc}",
            recoverable=False,
            remediation=(
                "Preserve the affected run for audit, restore its verified provider-call ledger from a trusted "
                "backup, or start a fresh run. Do not create an empty replacement by hand."
            ),
            details={"command": "academic", "run_id": run_id, "network_io_executed": False},
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or int(getattr(metadata, "st_nlink", 1) or 1) != 1
    ):
        raise DiscoverSourcesError(
            "ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID",
            (
                "Academic provider-call ledger must be a singly linked regular file: "
                f"{relative_label(project_root, path)}"
            ),
            recoverable=False,
            remediation=(
                "Preserve the affected run for audit, restore its verified provider-call ledger from a trusted "
                "backup, or start a fresh run. Do not replace it with a symlink or hard link."
            ),
            details={"command": "academic", "run_id": run_id, "network_io_executed": False},
        )
    return path


def _load_academic_run_state(path: Path, *, requested_run_id: str | None) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoverSourcesError(
            "DISCOVERY_RUN_STATE_INVALID",
            f"Cannot read retained discovery run state {path}: {exc}",
            recoverable=False,
            remediation="Repair or recover the retained run-state artifact before contacting an academic provider.",
            details={"command": "academic", "network_io_executed": False},
        ) from exc
    state = document.get("state") if isinstance(document, dict) and isinstance(document.get("state"), dict) else {}
    run_id = document.get("run_id") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or not isinstance(run_id, str)
        or run_id != path.parent.name
        or state.get("current") not in RUN_STATES
        or not isinstance(document.get("started_at"), str)
        or not document["started_at"].strip()
    ):
        raise DiscoverSourcesError(
            "DISCOVERY_RUN_STATE_INVALID",
            f"Retained discovery run state has an invalid shape: {path}",
            recoverable=False,
            remediation="Repair or recover the retained run-state artifact before contacting an academic provider.",
            details={"command": "academic", "network_io_executed": False},
        )
    if document.get("_pending_event") is not None:
        raise DiscoverSourcesError(
            "DISCOVERY_RUN_RECOVERY_REQUIRED",
            f"Run {run_id} has an interrupted mutation and cannot reserve an academic provider call.",
            remediation=f"Run run_controller.py recover --run-id {run_id} before retrying discovery.",
            details={"command": "academic", "run_id": run_id, "network_io_executed": False},
        )
    return {
        "run_id": run_id,
        "state": state["current"],
        "path": path,
        "requested": requested_run_id is not None,
        "document": document,
    }


def resolve_academic_discovery_run(project_root: Path, requested_run_id: str | None) -> dict[str, Any] | None:
    runs_root = project_root / "runs"
    if requested_run_id is not None:
        run_id = requested_run_id.strip()
        if not RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."} or ".." in run_id:
            raise DiscoverSourcesError(
                "DISCOVERY_RUN_ID_INVALID",
                f"Invalid discovery run id: {requested_run_id!r}",
                recoverable=False,
                remediation="Use a filename-safe active run id from runs/<run-id>/run-state.json.",
                details={"command": "academic", "network_io_executed": False},
            )
        state_path = runs_root / run_id / "run-state.json"
        if not state_path.is_file():
            raise DiscoverSourcesError(
                "DISCOVERY_RUN_UNKNOWN",
                f"No retained run state exists for discovery run {run_id}.",
                remediation="Start the run with run_controller.py or pass an existing active --run-id.",
                details={"command": "academic", "run_id": run_id, "network_io_executed": False},
            )
        selected = _load_academic_run_state(state_path, requested_run_id=run_id)
        if selected["state"] in RUN_TERMINAL_STATES:
            raise DiscoverSourcesError(
                "DISCOVERY_RUN_TERMINAL",
                f"Discovery run {run_id} is already terminal: {selected['state']}.",
                recoverable=False,
                remediation="Start a new run before contacting additional academic providers.",
                details={"command": "academic", "run_id": run_id, "network_io_executed": False},
            )
        validate_academic_provider_accounting(project_root, run_id, run_state=selected["document"])
        return selected

    if not runs_root.is_dir():
        return None
    active: list[dict[str, Any]] = []
    for state_path in sorted(runs_root.glob("*/run-state.json")):
        selected = _load_academic_run_state(state_path, requested_run_id=None)
        if selected["state"] not in RUN_TERMINAL_STATES:
            active.append(selected)
    if not active:
        return None
    if len(active) > 1:
        raise DiscoverSourcesError(
            "DISCOVERY_RUN_ID_REQUIRED",
            "Multiple active runs exist; academic provider-call budget ownership is ambiguous.",
            remediation="Pass --run-id for the active run that owns this discovery call.",
            details={"command": "academic", "network_io_executed": False},
        )
    selected = active[0]
    validate_academic_provider_accounting(
        project_root,
        selected["run_id"],
        run_state=selected["document"],
    )
    return selected


def max_academic_provider_requests_per_run(config: dict[str, Any]) -> int:
    run = config.get("run") if isinstance(config.get("run"), dict) else {}
    value = run.get(
        "max_academic_provider_requests_per_run",
        DEFAULT_MAX_ACADEMIC_PROVIDER_REQUESTS_PER_RUN,
    )
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SystemExit("research.yml run.max_academic_provider_requests_per_run must be a positive integer")
    return value


def load_academic_provider_request_events(
    project_root: Path,
    run_id: str,
    *,
    strict: bool = True,
) -> list[dict[str, Any]]:
    path = validate_academic_provider_accounting(project_root, run_id)
    records: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        if not strict:
            return []
        raise DiscoverSourcesError(
            "ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID",
            f"Cannot read academic provider-call ledger {relative_label(project_root, path)}: {exc}",
            recoverable=False,
            remediation="Repair or restore the run-bound provider-call ledger before retrying discovery.",
            details={"command": "academic", "run_id": run_id, "network_io_executed": False},
        ) from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if not strict:
                continue
            raise DiscoverSourcesError(
                "ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID",
                (
                    f"Invalid JSONL in {relative_label(project_root, path)} "
                    f"at line {line_number}: {exc}"
                ),
                recoverable=False,
                remediation="Repair or restore the run-bound provider-call ledger before retrying discovery.",
                details={"command": "academic", "run_id": run_id, "network_io_executed": False},
            ) from exc
        valid = (
            isinstance(record, dict)
            and record.get("schema_version") == SCHEMA_VERSION
            and record.get("event_type") == "academic_provider_request"
            and record.get("run_id") == run_id
            and record.get("provider") in {"arxiv", "openalex"}
            and isinstance(record.get("call_id"), str)
            and bool(record["call_id"].strip())
            and isinstance(record.get("reserved_at"), str)
        )
        if not valid:
            if not strict:
                continue
            raise DiscoverSourcesError(
                "ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID",
                f"Invalid provider-call record in {relative_label(project_root, path)} at line {line_number}.",
                recoverable=False,
                remediation="Repair or restore the run-bound provider-call ledger before retrying discovery.",
                details={"command": "academic", "run_id": run_id, "network_io_executed": False},
            )
        call_id = record["call_id"].strip()
        if call_id in seen_call_ids:
            if not strict:
                continue
            raise DiscoverSourcesError(
                "ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID",
                (
                    f"Duplicate provider call_id {call_id!r} in "
                    f"{relative_label(project_root, path)} at line {line_number}."
                ),
                recoverable=False,
                remediation=(
                    "Preserve the affected run for audit, restore its provider-call ledger from a trusted "
                    "backup, or start a fresh run. Do not deduplicate or reset accounting by hand."
                ),
                details={"command": "academic", "run_id": run_id, "network_io_executed": False},
            )
        seen_call_ids.add(call_id)
        records.append(record)
    return records


def academic_provider_request_count(project_root: Path, run_id: str, *, strict: bool = True) -> int:
    return len(load_academic_provider_request_events(project_root, run_id, strict=strict))


def academic_provider_budget_context(
    project_root: Path,
    config: dict[str, Any],
    requested_run_id: str | None,
    *,
    command: str,
    scope_id: str,
) -> dict[str, Any] | None:
    run = resolve_academic_discovery_run(project_root, requested_run_id)
    if run is None:
        return None
    return {
        "project_root": project_root,
        "run_id": run["run_id"],
        "limit": max_academic_provider_requests_per_run(config),
        "command": command,
        "scope_id": scope_id,
        "network_io_executed": False,
    }


def reserve_academic_provider_request(
    context: dict[str, Any] | None,
    *,
    provider: str,
    attempt: int,
) -> dict[str, Any] | None:
    """Durably consume one provider-call slot immediately before transport."""
    if context is None:
        return None
    project_root = context["project_root"]
    run_id = context["run_id"]
    limit = int(context["limit"])
    ledger_path = academic_provider_requests_path(project_root, run_id)
    lock_path = academic_provider_requests_lock_path(project_root, run_id)
    with workspace_lock(lock_path, purpose=f"academic provider-call budget for {run_id}"):
        used = academic_provider_request_count(project_root, run_id)
        if used >= limit:
            raise DiscoverSourcesError(
                "ACADEMIC_PROVIDER_REQUEST_BUDGET_EXCEEDED",
                (
                    f"Run {run_id} already reserved {used} academic provider request(s); "
                    f"the next {provider} call would exceed max_academic_provider_requests_per_run={limit}."
                ),
                remediation="Start a new run or raise the reviewed academic provider request budget.",
                details={
                    "command": context["command"],
                    "provider": provider,
                    "run_id": run_id,
                    "used": used,
                    "limit": limit,
                    "network_io_executed": bool(context.get("network_io_executed")),
                },
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "event_type": "academic_provider_request",
            "call_id": f"academic-call-{uuid.uuid4().hex}",
            "run_id": run_id,
            "command": context["command"],
            "scope_id": context["scope_id"],
            "provider": provider,
            "attempt": attempt,
            "reserved_at": timestamp_utc(),
            "budget_consumed": True,
        }
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(compact_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise DiscoverSourcesError(
                "ACADEMIC_PROVIDER_REQUEST_LEDGER_WRITE_FAILED",
                f"Cannot persist the academic provider-call reservation for run {run_id}: {exc}",
                recoverable=False,
                remediation="Restore workspace write access before retrying; no provider transport was invoked.",
                details={
                    "command": context["command"],
                    "provider": provider,
                    "run_id": run_id,
                    "network_io_executed": False,
                },
            ) from exc
        return record


def configured_candidate_store_relative(config: dict[str, Any] | None) -> str:
    default = "/".join(CANDIDATE_STORE_RELATIVE)
    if not isinstance(config, dict):
        return default
    discovery = integrations_config(config).get("discovery")
    if not isinstance(discovery, dict):
        return default
    value = discovery.get("candidate_store_path", default)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit("research.yml integrations.discovery.candidate_store_path must be a non-empty path")
    text = value.strip()
    if "\\" in text or "://" in text or text.startswith("~"):
        raise SystemExit(
            "research.yml integrations.discovery.candidate_store_path must be a portable workspace-relative path"
        )
    relative = PurePosixPath(text)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise SystemExit(
            "research.yml integrations.discovery.candidate_store_path must be a workspace-relative path without '..'"
        )
    if len(relative.parts) < 2 or relative.parts[0] != "sources":
        raise SystemExit("research.yml integrations.discovery.candidate_store_path must stay under sources/")
    if relative.suffix.lower() != ".jsonl":
        raise SystemExit("research.yml integrations.discovery.candidate_store_path must use the .jsonl extension")
    return relative.as_posix()


def candidate_store_path(project_root: Path, config: dict[str, Any] | None = None) -> Path:
    relative = configured_candidate_store_relative(config)
    path = project_root.joinpath(*PurePosixPath(relative).parts)
    resolved_root = project_root.resolve()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise SystemExit(f"Cannot resolve integrations.discovery.candidate_store_path: {exc}") from exc
    if not resolved.is_relative_to(resolved_root):
        raise SystemExit("research.yml integrations.discovery.candidate_store_path escapes the workspace")
    return path


def selected_request_id(candidate: dict[str, Any]) -> str | None:
    """Return canonical selected request id, accepting the legacy alias."""
    for key in ("source_request_id", "selected_for_request_id", "selected_request_id", "request_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_matches_request(candidate: dict[str, Any], request_id: str) -> bool:
    return selected_request_id(candidate) == request_id


def candidate_lifecycle_state(candidate: dict[str, Any]) -> str:
    value = candidate.get("lifecycle_state")
    if isinstance(value, str) and value in CANDIDATE_LIFECYCLE_STATES:
        return value
    legacy = candidate.get("status")
    if legacy is None:
        return DEFAULT_CANDIDATE_LIFECYCLE_STATE
    if isinstance(legacy, str):
        if legacy in LEGACY_CANDIDATE_STATE_MAP:
            return LEGACY_CANDIDATE_STATE_MAP[legacy]
        if legacy in CANDIDATE_LIFECYCLE_STATES:
            return legacy
    raise DiscoverSourcesError(
        "CANDIDATE_STATE_INVALID",
        f"Candidate {candidate.get('candidate_id')!r} has an unknown lifecycle state.",
        recoverable=False,
        remediation="Repair the candidate lifecycle_state/status using a documented canonical state.",
        details={
            "candidate_id": candidate.get("candidate_id"),
            "lifecycle_state": value,
            "status": legacy,
            "network_io_executed": False,
        },
    )


def candidate_selection_status(candidate: dict[str, Any]) -> str:
    value = candidate.get("selection_status")
    if isinstance(value, str) and value in SELECTION_STATUSES:
        return value
    state = candidate_lifecycle_state(candidate)
    if state in {"selected", "fetched", "failed"}:
        return "selected"
    if state == "superseded":
        return "obsolete"
    if state == "rejected":
        reason = candidate.get("rejection_reason")
        if isinstance(reason, str):
            lowered = reason.lower()
            if "duplicate" in lowered:
                return "duplicate"
            if "obsolete" in lowered or "superseded" in lowered or "historical" in lowered:
                return "obsolete"
        return "rejected"
    if state in {"reviewed", "deferred"}:
        return "pending"
    if candidate.get("recommended_action") == "review":
        return "needs_manual_review"
    return DEFAULT_SELECTION_STATUS


def candidate_fetch_status(candidate: dict[str, Any]) -> str:
    value = candidate.get("fetch_status")
    if isinstance(value, str) and value in FETCH_STATUSES:
        return value
    state = candidate_lifecycle_state(candidate)
    if state == "fetched":
        return "fetched"
    if state == "failed":
        return "failed"
    if state in {"rejected", "superseded"}:
        return "not_fetchable"
    if state == "selected":
        source_type = candidate.get("source_type")
        if source_type in {"official_legal", "web_page", "publisher_page", "project_page", "dataset", "supplemental_material"}:
            return "pending_manual_delivery"
        return "planned"
    if state in {"reviewed", "deferred"}:
        return "not_planned"
    if candidate.get("recommended_action") == "reject":
        return "not_fetchable"
    return DEFAULT_FETCH_STATUS


def candidate_policy_defaults(source_type: Any) -> dict[str, str]:
    if isinstance(source_type, str):
        return CANDIDATE_POLICY_DEFAULTS.get(source_type, DEFAULT_CANDIDATE_POLICY)
    return DEFAULT_CANDIDATE_POLICY


def apply_candidate_schema_defaults(
    candidate: dict[str, Any],
    *,
    new_record: bool = False,
) -> dict[str, Any]:
    """Add candidate policy/lifecycle fields without discarding provider data."""
    raw_state = candidate.get("lifecycle_state")
    raw_status = candidate.get("status")
    raw_version = candidate.get("lifecycle_schema_version")
    state = candidate_lifecycle_state(candidate)
    expected_status = CANDIDATE_STATE_TO_LEGACY_STATUS[state]
    if isinstance(raw_state, str) and raw_state in CANDIDATE_LIFECYCLE_STATES:
        if raw_version not in {None, CANDIDATE_LIFECYCLE_VERSION}:
            raise DiscoverSourcesError(
                "CANDIDATE_STATE_INVALID",
                f"Candidate {candidate.get('candidate_id')!r} uses unsupported lifecycle schema {raw_version!r}.",
                recoverable=False,
                remediation="Migrate the candidate with a supported lifecycle schema before mutation.",
            )
        if raw_status is not None and raw_status != expected_status:
            raise DiscoverSourcesError(
                "CANDIDATE_STATE_INVALID",
                (
                    f"Candidate {candidate.get('candidate_id')!r} lifecycle_state {state!r} "
                    f"conflicts with compatibility status {raw_status!r}."
                ),
                recoverable=False,
                remediation="Repair the conflicting lifecycle fields before candidate mutation.",
                details={
                    "candidate_id": candidate.get("candidate_id"),
                    "lifecycle_state": state,
                    "status": raw_status,
                    "expected_status": expected_status,
                    "network_io_executed": False,
                },
            )
    else:
        candidate["lifecycle_state"] = state
        if not new_record:
            candidate["lifecycle_migration"] = {
                "legacy_status": raw_status if raw_status is not None else "implicit_new",
                "mapped_state": state,
                "review_state_inferred": False,
            }
    candidate["lifecycle_schema_version"] = CANDIDATE_LIFECYCLE_VERSION
    candidate["status"] = expected_status
    for field, value in candidate_policy_defaults(candidate.get("source_type")).items():
        existing = candidate.get(field)
        if not isinstance(existing, str) or not existing.strip():
            candidate[field] = value
    request_id = selected_request_id(candidate)
    if "source_request_id" not in candidate:
        candidate["source_request_id"] = request_id
    if "selected_for_request_id" not in candidate:
        candidate["selected_for_request_id"] = request_id
    if "selected_at" not in candidate:
        candidate["selected_at"] = None
    if "selection_status" not in candidate:
        candidate["selection_status"] = candidate_selection_status(candidate)
    if "fetch_status" not in candidate:
        candidate["fetch_status"] = candidate_fetch_status(candidate)
    if "evidence_areas" not in candidate:
        evidence_path = candidate.get("evidence_path")
        candidate["evidence_areas"] = [evidence_path] if isinstance(evidence_path, str) and evidence_path else []
    return candidate


def existing_candidate_ids(path: Path) -> set[str]:
    """Read candidate ids already present in the durable store, for dedup."""
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            # A malformed existing line is not this command's concern; skip it so
            # discovery still appends new candidates rather than crashing.
            continue
        candidate_id = record.get("candidate_id")
        if isinstance(candidate_id, str):
            ids.add(candidate_id)
    return ids


def append_candidates(path: Path, candidates: list[dict[str, Any]]) -> list[str]:
    """Append new candidate records, skipping ids already in the store.

    Discovery is append-oriented and idempotent: re-running the same query does
    not duplicate candidates. The same stable store lock used by lifecycle
    transitions prevents discovery appends from racing a state rewrite.
    """
    lock_path = path.parent / ".locks" / f"{path.stem}.lock"
    with workspace_lock(lock_path, purpose="discovery candidate store"):
        existing = existing_candidate_ids(path)
        written: list[str] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for candidate in candidates:
                apply_candidate_schema_defaults(candidate, new_record=True)
                candidate_id = candidate["candidate_id"]
                if candidate_id in existing:
                    continue
                handle.write(compact_json(candidate) + "\n")
                existing.add(candidate_id)
                written.append(candidate_id)
        return written


# --- Candidate lifecycle: review and selection -------------------------------


def candidate_audit_path(project_root: Path, config: dict[str, Any] | None = None) -> Path:
    return candidate_store_path(project_root, config).with_name("audit.jsonl")


def candidate_lock_path(project_root: Path, config: dict[str, Any] | None = None) -> Path:
    store = candidate_store_path(project_root, config)
    return store.parent / ".locks" / f"{store.stem}.lock"


def candidate_store_lock(project_root: Path, config: dict[str, Any] | None = None):
    """Hold a stable lock for candidate-store read-modify-write.

    The lock file is never renamed, so concurrent select/reject writers serialize
    on it even though each write replaces candidates.jsonl via temp-file rename
    (the stale-inode-safe pattern from question_claim.py).
    """
    return workspace_lock(candidate_lock_path(project_root, config), purpose="discovery candidate store")


def load_all_candidates(path: Path) -> list[dict[str, Any]]:
    """Load every candidate record. Unlike discovery's dedup read, a malformed
    line is fatal here: silently dropping it would lose the record on rewrite."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DiscoverSourcesError(
                "WORKSPACE_UNREADABLE",
                f"Invalid JSONL in the configured candidate store at line {line_number}: {exc}",
                recoverable=False,
                remediation="Repair or restore the configured discovery candidate store before review.",
            ) from exc
        if not isinstance(record, dict):
            raise DiscoverSourcesError(
                "WORKSPACE_UNREADABLE",
                f"Candidate record on line {line_number} is not a JSON object.",
                recoverable=False,
                remediation="Repair or restore the configured discovery candidate store before review.",
            )
        records.append(apply_candidate_schema_defaults(record))
    return records


def replace_candidate_store(tmp_path: Path, path: Path) -> None:
    """Atomically replace the store, retrying transient Windows sharing holds."""
    for attempt in range(len(WINDOWS_REPLACE_RETRY_DELAYS_SECONDS) + 1):
        try:
            tmp_path.replace(path)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror not in WINDOWS_TRANSIENT_REPLACE_ERRORS or attempt >= len(
                WINDOWS_REPLACE_RETRY_DELAYS_SECONDS
            ):
                raise
            time.sleep(WINDOWS_REPLACE_RETRY_DELAYS_SECONDS[attempt])


def rewrite_candidates(path: Path, records: list[dict[str, Any]]) -> None:
    """Rewrite the candidate store atomically (write-temp-rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(compact_json(apply_candidate_schema_defaults(record)) + "\n" for record in records)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    try:
        replace_candidate_store(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def append_audit_event(audit_path: Path, event: dict[str, Any]) -> None:
    """Append one lifecycle audit event. Called while holding the store lock."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(compact_json(event) + "\n")


def candidate_status(record: dict[str, Any]) -> str:
    return CANDIDATE_STATE_TO_LEGACY_STATUS[candidate_lifecycle_state(record)]


def find_candidate(records: list[dict[str, Any]], candidate_id: str) -> dict[str, Any]:
    for record in records:
        if record.get("candidate_id") == candidate_id:
            return record
    raise DiscoverSourcesError(
        "CANDIDATE_UNKNOWN",
        f"Unknown candidate id: {candidate_id} (no record in sources/discovery/candidates.jsonl).",
        remediation="List candidates with discover_sources.py candidates list and choose an existing candidate_id.",
        details={"candidate_id": candidate_id, "network_io_executed": False},
    )


def require_expected_candidate_state(candidate: dict[str, Any], expected_state: str | None) -> str:
    current_state = candidate_lifecycle_state(candidate)
    if expected_state is not None and current_state != expected_state:
        raise DiscoverSourcesError(
            "CANDIDATE_STATE_STALE",
            (
                f"Candidate {candidate.get('candidate_id')!r} is in state {current_state!r}, "
                f"not the expected state {expected_state!r}."
            ),
            remediation="Reload the candidate and retry only if the new state still permits the intended transition.",
            details={
                "candidate_id": candidate.get("candidate_id"),
                "expected_state": expected_state,
                "actual_state": current_state,
                "network_io_executed": False,
            },
        )
    return current_state


def require_candidate_transition(candidate: dict[str, Any], new_state: str, expected_state: str | None) -> str:
    prior_state = require_expected_candidate_state(candidate, expected_state)
    if prior_state == new_state:
        return prior_state
    if new_state not in CANDIDATE_STATE_TRANSITIONS[prior_state]:
        raise DiscoverSourcesError(
            "CANDIDATE_TRANSITION_INVALID",
            f"Candidate {candidate.get('candidate_id')!r} cannot transition from {prior_state!r} to {new_state!r}.",
            remediation="Use a transition listed in the candidate lifecycle table; terminal states cannot be reopened.",
            details={
                "candidate_id": candidate.get("candidate_id"),
                "prior_state": prior_state,
                "requested_state": new_state,
                "allowed_states": list(CANDIDATE_STATE_TRANSITIONS[prior_state]),
                "network_io_executed": False,
            },
        )
    return prior_state


def candidate_correlation_conflict(candidate: dict[str, Any], field: str, existing: Any, requested: Any) -> None:
    raise DiscoverSourcesError(
        "CANDIDATE_CORRELATION_CONFLICT",
        (
            f"Candidate {candidate.get('candidate_id')!r} already has {field}={existing!r}; "
            f"the idempotent repeat requested {requested!r}."
        ),
        remediation="Reload the candidate and preserve its existing correlation, or choose another candidate.",
        details={
            "candidate_id": candidate.get("candidate_id"),
            "field": field,
            "existing": existing,
            "requested": requested,
            "network_io_executed": False,
        },
    )


def apply_common_candidate_state(
    candidate: dict[str, Any],
    state: str,
    now: str,
    actor: str,
    reason: str,
) -> None:
    candidate["lifecycle_schema_version"] = CANDIDATE_LIFECYCLE_VERSION
    candidate["lifecycle_state"] = state
    candidate["status"] = CANDIDATE_STATE_TO_LEGACY_STATUS[state]
    candidate["lifecycle_updated_at"] = now
    candidate["lifecycle_updated_by"] = actor
    candidate["lifecycle_reason"] = reason


def apply_selection(
    candidate: dict[str, Any],
    request_id: str,
    now: str,
    reason: str,
    actor: str,
) -> None:
    apply_common_candidate_state(candidate, "selected", now, actor, reason)
    candidate["status"] = "selected"
    candidate["selected_for_request_id"] = request_id
    candidate["selected_request_id"] = request_id
    candidate["source_request_id"] = request_id
    candidate["selection_status"] = "selected"
    source_type = candidate.get("source_type")
    if source_type in {"official_legal", "web_page", "publisher_page", "project_page", "dataset", "supplemental_material"}:
        candidate["fetch_status"] = "pending_manual_delivery"
    else:
        candidate["fetch_status"] = "planned"
    candidate["selected_at"] = now
    candidate["selected_by"] = actor
    candidate["selection_reason"] = reason
    for key in ("failure_reason", "failed_at", "failed_by"):
        candidate.pop(key, None)


def apply_rejection(candidate: dict[str, Any], reason: str, now: str, actor: str) -> None:
    apply_common_candidate_state(candidate, "rejected", now, actor, reason)
    candidate["status"] = "rejected"
    lowered = reason.lower()
    if "duplicate" in lowered:
        candidate["selection_status"] = "duplicate"
    elif "obsolete" in lowered or "superseded" in lowered or "historical" in lowered:
        candidate["selection_status"] = "obsolete"
    else:
        candidate["selection_status"] = "rejected"
    candidate["fetch_status"] = "not_fetchable"
    candidate["rejection_reason"] = reason
    candidate["rejected_at"] = now
    candidate["rejected_by"] = actor
    for key in ("selected_request_id", "source_request_id", "selected_by"):
        candidate.pop(key, None)
    candidate["selected_for_request_id"] = None
    candidate["selected_at"] = None


def apply_candidate_transition(
    candidate: dict[str, Any],
    *,
    new_state: str,
    now: str,
    actor: str,
    reason: str,
    request_id: str | None = None,
    source_id: str | None = None,
    superseded_by_candidate_id: str | None = None,
) -> None:
    if new_state == "selected":
        if request_id is None:  # guarded by CLI validation; keeps direct callers safe.
            raise DiscoverSourcesError("VALUE_INVALID", "selected requires request_id.")
        apply_selection(candidate, request_id, now, reason, actor)
        return
    if new_state == "rejected":
        apply_rejection(candidate, reason, now, actor)
        return

    apply_common_candidate_state(candidate, new_state, now, actor, reason)
    if new_state == "reviewed":
        candidate["selection_status"] = "pending"
        candidate["fetch_status"] = "not_planned"
        candidate["reviewed_at"] = now
        candidate["reviewed_by"] = actor
    elif new_state == "deferred":
        candidate["selection_status"] = "pending"
        candidate["fetch_status"] = "not_planned"
        candidate["deferred_at"] = now
        candidate["deferred_by"] = actor
        candidate["defer_reason"] = reason
    elif new_state == "fetched":
        candidate["selection_status"] = "selected"
        candidate["fetch_status"] = "fetched"
        candidate["fetched_at"] = now
        candidate["fetched_by"] = actor
        candidate["fetched_source_id"] = source_id
    elif new_state == "failed":
        candidate["selection_status"] = "selected"
        candidate["fetch_status"] = "failed"
        candidate["failed_at"] = now
        candidate["failed_by"] = actor
        candidate["failure_reason"] = reason
    elif new_state == "superseded":
        candidate["selection_status"] = "obsolete"
        candidate["fetch_status"] = "not_fetchable"
        candidate["superseded_at"] = now
        candidate["superseded_by"] = actor
        candidate["superseded_by_candidate_id"] = superseded_by_candidate_id


def candidate_audit_event(
    candidate: dict[str, Any],
    *,
    action: str,
    prior_state: str,
    new_state: str,
    actor: str,
    reason: str,
    at: str,
    request_id: str | None,
    run_id: str | None,
    source_id: str | None = None,
    superseded_by_candidate_id: str | None = None,
    created_request: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "lifecycle_schema_version": CANDIDATE_LIFECYCLE_VERSION,
        "event_id": f"candidate-event-{uuid.uuid4().hex}",
        "event": action,
        "event_type": "candidate_transition",
        "candidate_id": candidate.get("candidate_id"),
        "prior_state": prior_state,
        "new_state": new_state,
        "status": CANDIDATE_STATE_TO_LEGACY_STATUS[new_state],
        "actor": actor,
        "by": actor,
        "reason": reason,
        "request_id": request_id,
        "run_id": run_id,
        "source_id": source_id,
        "superseded_by_candidate_id": superseded_by_candidate_id,
        "created_request": created_request,
        "at": at,
    }


def ensure_request_exists(project_root: Path, config: dict[str, Any], request_id: str) -> None:
    import source_requests

    path = source_requests.requests_path(project_root, config)
    records = source_requests.load_requests(path)
    if not any(record.get("request_id") == request_id for record in records):
        raise DiscoverSourcesError(
            "REQUEST_UNKNOWN",
            f"Unknown request id: {request_id} (no record in {relative_label(project_root, path)}).",
            remediation=(
                "List requests with source_requests.py list and pass an existing id, "
                "or use --create-request to mint one."
            ),
            details={"request_id": request_id, "network_io_executed": False},
        )


def ensure_source_exists(project_root: Path, config: dict[str, Any], source_id: str) -> None:
    import source_requests

    if source_id not in source_requests.manifest_source_ids(project_root, config):
        raise DiscoverSourcesError(
            "SOURCE_UNKNOWN",
            f"Unknown source id: {source_id} (no record in the configured source manifest).",
            remediation="Inventory the delivered source first, then transition the selected candidate to fetched.",
            details={"source_id": source_id, "network_io_executed": False},
        )


def create_source_request_from_candidate(
    project_root: Path,
    config: dict[str, Any],
    candidate: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Mint (or reuse an open) source request derived from the candidate.

    Reuses source_requests.py so the request schema and id generation stay
    authoritative in one place. Re-running select --create-request reuses the
    open request from the first run instead of duplicating it.
    """
    import source_requests

    path = source_requests.requests_path(project_root, config)
    records = source_requests.load_requests(path)
    kind = SOURCE_TYPE_TO_REQUEST_KIND.get(candidate.get("source_type"), "other")
    query = candidate.get("url") or candidate.get("title") or candidate.get("candidate_id")
    if not isinstance(query, str) or not query.strip():
        raise DiscoverSourcesError(
            "VALUE_INVALID",
            "Candidate has no url or title to derive a source request from.",
            remediation="Pass --request-id to link an existing request instead of --create-request.",
        )
    query = query.strip()
    duplicate = source_requests.find_open_duplicate(records, kind, query)
    if duplicate is not None:
        return {"request_id": duplicate["request_id"], "created": False}

    question_slugs = source_requests.validate_question_slugs(project_root, config, args.question_slugs or [])
    now = source_requests.timestamp_utc()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": source_requests.generate_request_id(kind, query, now, len(records)),
        "kind": kind,
        "query_or_identifier": query,
        "rationale": f"Selected from discovery candidate {candidate.get('candidate_id')}: {candidate.get('title')}",
        "priority": args.priority,
        "question_slugs": question_slugs,
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "source_id": None,
    }
    source_requests.append_request(path, record)
    return {"request_id": record["request_id"], "created": True}


def run_candidates_list(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    store_path = candidate_store_path(project_root, config)
    records = load_all_candidates(store_path)
    statuses = list(dict.fromkeys(args.status)) if args.status else None
    states = list(dict.fromkeys(args.state)) if args.state else None
    request_id = args.request_id.strip() if isinstance(args.request_id, str) and args.request_id.strip() else None
    selected = [
        record
        for record in records
        if (statuses is None or candidate_status(record) in statuses)
        and (states is None or candidate_lifecycle_state(record) in states)
        and (request_id is None or candidate_matches_request(record, request_id))
    ]
    selected.sort(key=lambda record: (str(record.get("discovered_at", "")), str(record.get("candidate_id", ""))))
    counts = {status: 0 for status in CANDIDATE_STATUSES}
    state_counts = {state: 0 for state in CANDIDATE_LIFECYCLE_STATES}
    for record in records:
        counts[candidate_status(record)] += 1
        state_counts[candidate_lifecycle_state(record)] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "candidates",
        "candidates_command": "list",
        "generated_at": timestamp_utc(),
        "candidates_path": relative_label(project_root, store_path),
        "filter_statuses": statuses,
        "filter_states": states,
        "filter_request_id": request_id,
        "counts": {"total": len(records), **counts},
        "state_counts": {"total": len(records), **state_counts},
        "count": len(selected),
        "candidates": selected,
        "network_io_executed": False,
    }


def run_candidates_select(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    candidate_id = require_non_empty(args.candidate_id, "--candidate-id")
    reason = args.reason.strip() if isinstance(args.reason, str) and args.reason.strip() else "candidate selected"
    actor = require_non_empty(args.actor, "--actor")
    store_path = candidate_store_path(project_root, config)
    with candidate_store_lock(project_root, config):
        records = load_all_candidates(store_path)
        target = find_candidate(records, candidate_id)
        prior_state = require_candidate_transition(target, "selected", args.expected_state)
        current_request_id = selected_request_id(target)
        run_id = args.run_id or target.get("discovery_run_id")
        if prior_state == "selected":
            if args.create_request:
                if current_request_id is None:
                    candidate_correlation_conflict(target, "request_id", None, "--create-request")
                request_id = current_request_id
            else:
                request_id = args.request_id.strip()
                if current_request_id != request_id:
                    candidate_correlation_conflict(target, "request_id", current_request_id, request_id)
            existing_reason = target.get("selection_reason") or target.get("lifecycle_reason")
            if isinstance(existing_reason, str) and existing_reason != reason:
                candidate_correlation_conflict(target, "selection_reason", existing_reason, reason)
            existing_actor = target.get("selected_by") or target.get("lifecycle_updated_by")
            if isinstance(existing_actor, str) and existing_actor != actor:
                candidate_correlation_conflict(target, "actor", existing_actor, actor)
            created_request = False
        elif args.create_request:
            if current_request_id is not None:
                ensure_request_exists(project_root, config, current_request_id)
                request_id = current_request_id
                created_request = False
            else:
                creation = create_source_request_from_candidate(project_root, config, target, args)
                request_id = creation["request_id"]
                created_request = creation["created"]
        else:
            request_id = args.request_id.strip()
            if current_request_id is not None and current_request_id != request_id:
                candidate_correlation_conflict(target, "request_id", current_request_id, request_id)
            ensure_request_exists(project_root, config, request_id)
            created_request = False

        already_selected = prior_state == "selected"
        now = timestamp_utc()
        if not already_selected:
            apply_selection(target, request_id, now, reason, actor)
            target["lifecycle_run_id"] = run_id
            rewrite_candidates(store_path, records)
            append_audit_event(
                candidate_audit_path(project_root, config),
                candidate_audit_event(
                    target,
                    action="select",
                    prior_state=prior_state,
                    new_state="selected",
                    actor=actor,
                    reason=reason,
                    at=now,
                    request_id=request_id,
                    run_id=run_id,
                    created_request=created_request,
                ),
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "command": "candidates",
            "candidates_command": "select",
            "candidate_id": candidate_id,
            "request_id": request_id,
            "created_request": created_request,
            "updated": not already_selected,
            "status": "selected",
            "lifecycle_state": "selected",
            "reason": reason,
            "candidate": target,
            "candidates_path": relative_label(project_root, store_path),
            "audit_path": relative_label(project_root, candidate_audit_path(project_root, config)),
            "network_io_executed": False,
        }


def run_candidates_reject(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    candidate_id = require_non_empty(args.candidate_id, "--candidate-id")
    reason = require_non_empty(args.reason, "--reason")
    actor = require_non_empty(args.actor, "--actor")
    store_path = candidate_store_path(project_root, config)
    with candidate_store_lock(project_root, config):
        records = load_all_candidates(store_path)
        target = find_candidate(records, candidate_id)
        prior_state = require_candidate_transition(target, "rejected", args.expected_state)
        already_rejected = prior_state == "rejected"
        if already_rejected and target.get("rejection_reason") != reason:
            candidate_correlation_conflict(
                target,
                "rejection_reason",
                target.get("rejection_reason"),
                reason,
            )
        if already_rejected:
            existing_actor = target.get("rejected_by") or target.get("lifecycle_updated_by")
            if isinstance(existing_actor, str) and existing_actor != actor:
                candidate_correlation_conflict(target, "actor", existing_actor, actor)
        request_id = selected_request_id(target)
        run_id = args.run_id or target.get("discovery_run_id")
        now = timestamp_utc()
        if not already_rejected:
            apply_rejection(target, reason, now, actor)
            target["lifecycle_run_id"] = run_id
            rewrite_candidates(store_path, records)
            append_audit_event(
                candidate_audit_path(project_root, config),
                candidate_audit_event(
                    target,
                    action="reject",
                    prior_state=prior_state,
                    new_state="rejected",
                    actor=actor,
                    reason=reason,
                    at=now,
                    request_id=request_id,
                    run_id=run_id,
                ),
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "command": "candidates",
            "candidates_command": "reject",
            "candidate_id": candidate_id,
            "reason": reason,
            "updated": not already_rejected,
            "status": "rejected",
            "lifecycle_state": "rejected",
            "candidate": target,
            "candidates_path": relative_label(project_root, store_path),
            "audit_path": relative_label(project_root, candidate_audit_path(project_root, config)),
            "network_io_executed": False,
        }


def run_candidates_transition(
    project_root: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    candidate_id = require_non_empty(args.candidate_id, "--candidate-id")
    reason = require_non_empty(args.reason, "--reason")
    actor = require_non_empty(args.actor, "--actor")
    new_state = args.to_state
    store_path = candidate_store_path(project_root, config)
    with candidate_store_lock(project_root, config):
        records = load_all_candidates(store_path)
        target = find_candidate(records, candidate_id)
        prior_state = require_candidate_transition(target, new_state, args.expected_state)
        run_id = args.run_id or target.get("discovery_run_id")

        request_id = selected_request_id(target)
        source_id = None
        superseded_by = None
        if new_state == "selected":
            requested_id = require_non_empty(args.request_id, "--request-id")
            if request_id is not None and request_id != requested_id:
                candidate_correlation_conflict(target, "request_id", request_id, requested_id)
            if prior_state != "selected":
                ensure_request_exists(project_root, config, requested_id)
            request_id = requested_id
        elif new_state == "fetched":
            source_id = require_non_empty(args.source_id, "--source-id")
            if prior_state == "fetched":
                if target.get("fetched_source_id") != source_id:
                    candidate_correlation_conflict(
                        target,
                        "fetched_source_id",
                        target.get("fetched_source_id"),
                        source_id,
                    )
            else:
                ensure_source_exists(project_root, config, source_id)
        elif new_state == "superseded":
            superseded_by = require_non_empty(
                args.superseded_by_candidate_id,
                "--superseded-by-candidate-id",
            )
            if prior_state == "superseded":
                if target.get("superseded_by_candidate_id") != superseded_by:
                    candidate_correlation_conflict(
                        target,
                        "superseded_by_candidate_id",
                        target.get("superseded_by_candidate_id"),
                        superseded_by,
                    )
            elif superseded_by == candidate_id:
                raise DiscoverSourcesError(
                    "CANDIDATE_CORRELATION_CONFLICT",
                    "A candidate cannot supersede itself.",
                    remediation="Choose the distinct replacement candidate id.",
                )
            else:
                replacement = find_candidate(records, superseded_by)
                if candidate_lifecycle_state(replacement) in {"rejected", "superseded"}:
                    raise DiscoverSourcesError(
                        "CANDIDATE_CORRELATION_CONFLICT",
                        f"Replacement candidate {superseded_by!r} is not active.",
                        remediation="Choose a proposed, reviewed, selected, deferred, fetched, or failed replacement.",
                    )

        already_applied = prior_state == new_state
        if already_applied:
            existing_reason = target.get("lifecycle_reason")
            if isinstance(existing_reason, str) and existing_reason != reason:
                candidate_correlation_conflict(target, "lifecycle_reason", existing_reason, reason)
            existing_actor = target.get("lifecycle_updated_by")
            if isinstance(existing_actor, str) and existing_actor != actor:
                candidate_correlation_conflict(target, "actor", existing_actor, actor)
            existing_run_id = target.get("lifecycle_run_id")
            if isinstance(existing_run_id, str) and existing_run_id != run_id:
                candidate_correlation_conflict(target, "run_id", existing_run_id, run_id)
        else:
            now = timestamp_utc()
            apply_candidate_transition(
                target,
                new_state=new_state,
                now=now,
                actor=actor,
                reason=reason,
                request_id=request_id,
                source_id=source_id,
                superseded_by_candidate_id=superseded_by,
            )
            target["lifecycle_run_id"] = run_id
            rewrite_candidates(store_path, records)
            append_audit_event(
                candidate_audit_path(project_root, config),
                candidate_audit_event(
                    target,
                    action="transition",
                    prior_state=prior_state,
                    new_state=new_state,
                    actor=actor,
                    reason=reason,
                    at=now,
                    request_id=request_id,
                    run_id=run_id,
                    source_id=source_id,
                    superseded_by_candidate_id=superseded_by,
                ),
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "command": "candidates",
            "candidates_command": "transition",
            "candidate_id": candidate_id,
            "prior_state": prior_state,
            "lifecycle_state": new_state,
            "updated": not already_applied,
            "candidate": target,
            "candidates_path": relative_label(project_root, store_path),
            "audit_path": relative_label(project_root, candidate_audit_path(project_root, config)),
            "network_io_executed": False,
        }


def run_candidates_command(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.candidates_command == "list":
        return run_candidates_list(project_root, config, args)
    if args.candidates_command == "select":
        return run_candidates_select(project_root, config, args)
    if args.candidates_command == "reject":
        return run_candidates_reject(project_root, config, args)
    if args.candidates_command == "transition":
        return run_candidates_transition(project_root, config, args)
    raise DiscoverSourcesError(
        "NOT_IMPLEMENTED",
        f"candidates {args.candidates_command} is not implemented.",
        remediation="Choose candidates list, candidates select, candidates reject, or candidates transition.",
    )


# --- Standards registry discovery ------------------------------------------

STANDARDS_DISCOVERED_BY = "discover_sources.py/standards"


def standards_error_details(provider: str) -> dict[str, Any]:
    return {"command": "standards", "standards_provider": provider, "network_io_executed": False}


def standards_optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_fixture_arg(project_root: Path, value: str | None, label: str, provider: str) -> Path:
    if value is None:
        raise DiscoverSourcesError(
            "PROVIDER_FAILED",
            f"standards {provider} requires {label} for offline discovery.",
            remediation=(
                "Pass an explicit fixture snapshot. Live standards-registry discovery is intentionally "
                "not enabled by this command."
            ),
            details=standards_error_details(provider),
        )
    text = require_non_empty(value, label)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = project_root / path
    if not path.is_file():
        raise DiscoverSourcesError(
            "PROVIDER_FAILED",
            f"standards {provider} fixture does not exist: {text}",
            remediation="Pass a readable local fixture path.",
            details=standards_error_details(provider),
        )
    return path


def optional_fixture_arg(project_root: Path, value: str | None, label: str, provider: str) -> Path | None:
    if value is None:
        return None
    return resolve_fixture_arg(project_root, value, label, provider)


def load_json_fixture(path: Path, provider: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"Invalid JSON standards fixture: {path}",
            remediation="Fix the fixture JSON and retry.",
            details=standards_error_details(provider),
        ) from exc
    if not isinstance(document, dict):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"Standards fixture must contain a JSON object: {path}",
            remediation="Use a mapping/object fixture.",
            details=standards_error_details(provider),
        )
    return document


def load_jsonl_fixture(path: Path, provider: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DiscoverSourcesError(
                "DISCOVERY_RESPONSE_INVALID",
                f"Invalid JSONL standards fixture at {path}:{line_number}",
                remediation="Fix the fixture JSONL and retry.",
                details=standards_error_details(provider),
            ) from exc
        if isinstance(record, dict):
            records.append(record)
    return records


def load_ics_titles(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {
            str(row.get("code", "")).strip(): str(row.get("title", "")).strip()
            for row in csv.DictReader(handle)
            if str(row.get("code", "")).strip()
        }


def standards_candidate_id(provider: str, key: str) -> str:
    digest = hashlib.sha1(f"standards:{provider}:{key.strip().lower()}".encode(), usedforsecurity=False).hexdigest()[:10]
    return f"cand-standards-{digest}"


def standards_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def standards_base_candidate(
    *,
    provider: str,
    url: str,
    title: str,
    source_type: str,
    trust_tier: str,
    relevance_score: float,
    trust_score: float,
    official_source: bool | None,
    terms_url: str | None,
    rationale: str,
    recommended_action: str,
    standards: dict[str, Any],
    reasoning: dict[str, Any],
    discovered_at: str,
    discovery_id: str,
) -> dict[str, Any]:
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": standards_candidate_id(provider, f"{url}:{standards.get('designation') or title}"),
        "request_id": None,
        "seed_source_id": None,
        "discovery_run_id": discovery_id,
        "discovered_at": discovered_at,
        "discovered_by": STANDARDS_DISCOVERED_BY,
        "provider": provider,
        "url": url,
        "title": title,
        "source_type": source_type,
        "trust_tier": trust_tier,
        "relevance_score": relevance_score,
        "trust_score": trust_score,
        "official_source": official_source,
        "jurisdiction": None,
        "license": None,
        "terms_url": terms_url,
        "rationale": rationale,
        "recommended_action": recommended_action,
        "network_io_executed": False,
        "standards": standards,
        "reasoning": reasoning,
    }
    return apply_candidate_schema_defaults(candidate, new_record=True)


def standards_status_action(status: str | None, *, exact: bool, terms_known: bool) -> tuple[str, list[str]]:
    normalized = (status or "").strip().casefold()
    flags: list[str] = []
    if normalized == "withdrawn":
        return "review", ["standard_status_withdrawn"]
    if normalized in {"replaced", "superseded", "historical"}:
        return "reject", ["standard_status_superseded"]
    if "draft" in normalized or normalized.startswith("dis"):
        return "reject", ["standard_status_draft"]
    if not normalized:
        flags.append("standard_status_missing")
    if not terms_known:
        flags.append("registry_terms_unknown")
    if exact and normalized in {"published", "current", "active"} and terms_known:
        return "fetch", flags
    return "review", flags


def iso_record_matches(record: dict[str, Any], designation: str | None, query: str | None) -> bool:
    record_designation = standards_optional_text(record.get("designation")) or ""
    if designation is not None:
        return record_designation.casefold() == designation.casefold()
    if query is None:
        return True
    haystack = " ".join(
        value
        for value in (
            record_designation,
            standards_optional_text(record.get("title")) or "",
        )
        if value
    ).casefold()
    return all(part in haystack for part in query.casefold().split())


def build_iso_candidates(project_root: Path, args: argparse.Namespace, discovered_at: str, discovery_id: str) -> list[dict[str, Any]]:
    provider = "iso-open-data"
    fixture = resolve_fixture_arg(project_root, args.fixture, "--fixture", provider)
    records = load_jsonl_fixture(fixture, provider)
    ics_titles = load_ics_titles(optional_fixture_arg(project_root, args.ics_fixture, "--ics-fixture", provider))
    attribution_path = optional_fixture_arg(project_root, args.attribution_fixture, "--attribution-fixture", provider)
    attribution = load_json_fixture(attribution_path, provider) if attribution_path is not None else {}
    designation = standards_optional_text(getattr(args, "designation", None))
    query = standards_optional_text(getattr(args, "query", None))
    terms_url = standards_optional_text(attribution.get("terms_url"))
    terms_known = bool(terms_url or attribution.get("dataset_license"))
    candidates: list[dict[str, Any]] = []
    for record in records:
        if not iso_record_matches(record, designation, query):
            continue
        record_designation = standards_optional_text(record.get("designation")) or "ISO standard"
        title = standards_optional_text(record.get("title")) or record_designation
        status = standards_optional_text(record.get("status"))
        exact = designation is not None and record_designation.casefold() == designation.casefold()
        action, flags = standards_status_action(status, exact=exact, terms_known=terms_known)
        ics_codes = [str(item).strip() for item in standards_list(record.get("ics_codes")) if str(item).strip()]
        standards = {
            "registry_provider": provider,
            "standards_body": "ISO",
            "designation": record_designation,
            "title": title,
            "edition": record.get("edition"),
            "publication_date": record.get("publication_date"),
            "status": status,
            "current_stage": record.get("current_stage"),
            "ics_codes": ics_codes,
            "ics_titles": {code: ics_titles[code] for code in ics_codes if code in ics_titles},
            "owner_committee": record.get("owner_committee"),
            "replaces": standards_list(record.get("replaces")),
            "replaced_by": standards_list(record.get("replaced_by")),
            "registry_url": standards_optional_text(record.get("registry_url")) or "",
            "dataset_license": attribution.get("dataset_license"),
            "attribution_required": bool(attribution.get("attribution_required", False)),
            "attribution": attribution.get("attribution"),
        }
        url = standards_optional_text(record.get("registry_url")) or f"https://www.iso.org/search.html?q={record_designation}"
        candidates.append(
            standards_base_candidate(
                provider=provider,
                url=url,
                title=f"{record_designation} {title}",
                source_type="standards_registry_entry",
                trust_tier="official_primary",
                relevance_score=0.95 if exact else 0.72,
                trust_score=0.98,
                official_source=True,
                terms_url=terms_url,
                rationale=(
                    "ISO Open Data metadata identifies the standards registry entry. "
                    "The candidate records metadata only and does not acquire full standards text."
                ),
                recommended_action=action,
                standards=standards,
                reasoning={
                    "matched_query_terms": [designation or query or record_designation],
                    "authority_reason": "ISO Open Data and ISO catalogue URLs are official registry metadata.",
                    "freshness_reason": f"Registry status is {status or 'missing'}.",
                    "scope_reason": "Candidate was matched from a bounded ISO fixture snapshot.",
                    "risk_flags": flags,
                },
                discovered_at=discovered_at,
                discovery_id=discovery_id,
            )
        )
    return candidates[: args.max_results]


ATTR_RE = re.compile(r'data-([a-zA-Z0-9_-]+)="([^"]*)"')


def html_data_objects(text: str, tag: str) -> list[dict[str, str]]:
    objects: list[dict[str, str]] = []
    for match in re.finditer(rf"<{tag}\b([^>]*)>", text, flags=re.IGNORECASE):
        attrs = {key.replace("-", "_"): value for key, value in ATTR_RE.findall(match.group(1))}
        if attrs:
            objects.append(attrs)
    return objects


def first_href(text: str) -> str | None:
    match = re.search(r'href="([^"]+)"', text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def build_eu_candidates(project_root: Path, args: argparse.Namespace, discovered_at: str, discovery_id: str) -> list[dict[str, Any]]:
    provider = "eu-product-requirements"
    guidance_path = resolve_fixture_arg(project_root, args.guidance_fixture, "--guidance-fixture", provider)
    harmonised_path = resolve_fixture_arg(project_root, args.harmonised_fixture, "--harmonised-fixture", provider)
    ojeu_path = resolve_fixture_arg(project_root, args.ojeu_fixture, "--ojeu-fixture", provider)
    guidance_html = guidance_path.read_text(encoding="utf-8")
    harmonised_html = harmonised_path.read_text(encoding="utf-8")
    legal = load_json_fixture(ojeu_path, provider)
    candidates: list[dict[str, Any]] = []
    guidance_attrs = html_data_objects(guidance_html, "article")[0]
    guidance_url = first_href(guidance_html) or "https://europa.eu/youreurope/"
    candidates.append(
        standards_base_candidate(
            provider="your-europe",
            url=guidance_url,
            title="Your Europe product requirement guidance",
            source_type="product_requirement_guidance",
            trust_tier="official_primary",
            relevance_score=0.76,
            trust_score=0.86,
            official_source=True,
            terms_url=None,
            rationale="Official explanatory guidance for product requirements; supplemental for mandatory legal authority.",
            recommended_action="review",
            standards={
                "registry_provider": "your-europe",
                "product_category": guidance_attrs.get("product_category"),
                "legal_act": guidance_attrs.get("legal_act"),
                "registry_url": guidance_url,
            },
            reasoning={
                "matched_query_terms": [args.query],
                "authority_reason": "Your Europe is official explanatory guidance.",
                "freshness_reason": "Guidance needs currentness metadata during acquisition.",
                "scope_reason": "Fixture product category matches the query.",
                "risk_flags": ["product_requirement_guidance_not_legal_authority"],
            },
            discovered_at=discovered_at,
            discovery_id=discovery_id,
        )
    )
    for row in html_data_objects(harmonised_html, "tr"):
        designation = row.get("designation", "harmonised standard")
        url = first_href(harmonised_html) or "https://single-market-economy.ec.europa.eu/"
        candidates.append(
            standards_base_candidate(
                provider="eu-harmonised-standards",
                url=url,
                title=f"{designation} harmonised standard reference",
                source_type="harmonised_standard_reference",
                trust_tier="official_primary",
                relevance_score=0.9,
                trust_score=0.94,
                official_source=True,
                terms_url=None,
                rationale="European Commission harmonised-standard reference with OJEU linkage.",
                recommended_action="fetch",
                standards={
                    "registry_provider": "eu-harmonised-standards",
                    "designation": designation,
                    "title": row.get("title"),
                    "status": "harmonised",
                    "product_category": row.get("product_category"),
                    "legal_act": row.get("legal_act"),
                    "ojeu_reference": row.get("ojeu_reference"),
                    "registry_url": url,
                },
                reasoning={
                    "matched_query_terms": [args.query, designation],
                    "authority_reason": "Commission harmonised-standard references are official registry evidence.",
                    "freshness_reason": "OJEU reference is present for product requirement linkage.",
                    "scope_reason": "Fixture row links the standard to the product category and legal act.",
                    "risk_flags": [],
                },
                discovered_at=discovered_at,
                discovery_id=discovery_id,
            )
        )
    legal_url = standards_optional_text(legal.get("eurlex_url")) or "https://eur-lex.europa.eu/"
    candidates.append(
        standards_base_candidate(
            provider="eur-lex",
            url=legal_url,
            title=standards_optional_text(legal.get("title")) or "EU legal authority",
            source_type="official_legal",
            trust_tier="official_primary",
            relevance_score=0.96,
            trust_score=0.98,
            official_source=True,
            terms_url=None,
            rationale="EUR-Lex/OJEU legal authority for the mandatory product requirement.",
            recommended_action="fetch",
            standards={
                "registry_provider": "eur-lex",
                "legal_act": legal.get("legal_act"),
                "ojeu_reference": legal.get("ojeu_reference"),
                "publication_date": legal.get("publication_date"),
                "product_category": legal.get("product_category"),
                "registry_url": legal_url,
            },
            reasoning={
                "matched_query_terms": [args.query, str(legal.get("legal_act", ""))],
                "authority_reason": "EUR-Lex/OJEU is controlling legal authority for mandatory requirements.",
                "freshness_reason": "Fixture includes publication and OJEU reference metadata.",
                "scope_reason": "Legal act matches the product-requirement fixture.",
                "risk_flags": [],
            },
            discovered_at=discovered_at,
            discovery_id=discovery_id,
        )
    )
    return candidates[: args.max_results]


def build_uk_geospatial_candidates(project_root: Path, args: argparse.Namespace, discovered_at: str, discovery_id: str) -> list[dict[str, Any]]:
    provider = "uk-geospatial-register"
    fixture = resolve_fixture_arg(project_root, args.fixture, "--fixture", provider)
    html = fixture.read_text(encoding="utf-8")
    section = html_data_objects(html, "section")[0]
    candidates: list[dict[str, Any]] = []
    for row in html_data_objects(html, "article"):
        designation = row.get("designation", "geospatial standard")
        owner = row.get("owner")
        url = row.get("url") or "https://www.gov.uk/"
        candidates.append(
            standards_base_candidate(
                provider=provider,
                url=url,
                title=f"{designation} in UK Geospatial Data Standards Register",
                source_type="geospatial_standard_register_entry",
                trust_tier="official_primary",
                relevance_score=0.86,
                trust_score=0.92,
                official_source=True,
                terms_url=None,
                rationale="GOV.UK register entry is official guidance for inclusion and recommendation.",
                recommended_action="fetch",
                standards={
                    "registry_provider": provider,
                    "designation": designation,
                    "register_category": row.get("category"),
                    "product_category": row.get("category"),
                    "owner": owner,
                    "scope": row.get("scope"),
                    "register_owner": section.get("register_owner"),
                    "register_manager": section.get("register_manager"),
                    "control_body": section.get("control_body"),
                    "registry_url": url,
                    "linked_owner_references": [{"owner": owner, "url": url}] if owner else [],
                },
                reasoning={
                    "matched_query_terms": [args.query, designation],
                    "authority_reason": "GOV.UK register is official for UK geospatial standards guidance.",
                    "freshness_reason": "Register metadata must be acquired with retrieval date before policy evaluation.",
                    "scope_reason": "Fixture row is a bounded register entry.",
                    "risk_flags": ["underlying_standard_identity_requires_owner_record"],
                },
                discovered_at=discovered_at,
                discovery_id=discovery_id,
            )
        )
    return candidates[: args.max_results]


def build_nist_candidates(project_root: Path, args: argparse.Namespace, discovered_at: str, discovery_id: str) -> list[dict[str, Any]]:
    provider = "nist"
    guidance_path = resolve_fixture_arg(project_root, args.guidance_fixture, "--guidance-fixture", provider)
    publication_path = resolve_fixture_arg(project_root, args.publication_fixture, "--publication-fixture", provider)
    guidance_html = guidance_path.read_text(encoding="utf-8")
    publication = load_json_fixture(publication_path, provider)
    guidance_url = first_href(guidance_html) or "https://www.nist.gov/standardsgov/"
    candidates = [
        standards_base_candidate(
            provider="nist-standards-info",
            url=guidance_url,
            title="NIST Standards Information Center",
            source_type="product_requirement_guidance",
            trust_tier="official_primary",
            relevance_score=0.78,
            trust_score=0.88,
            official_source=True,
            terms_url=None,
            rationale="NIST SIC is official standards navigation and referral guidance, not concrete publication identity.",
            recommended_action="review",
            standards={
                "registry_provider": "nist-standards-info",
                "standards_body": "NIST",
                "registry_url": guidance_url,
            },
            reasoning={
                "matched_query_terms": [args.query],
                "authority_reason": "NIST SIC is an official U.S. standards guidance portal.",
                "freshness_reason": "Guidance is supplemental and requires concrete publication currentness for document claims.",
                "scope_reason": "Fixture guidance page is relevant to standards navigation.",
                "risk_flags": ["nist_guidance_not_publication_identity"],
            },
            discovered_at=discovered_at,
            discovery_id=discovery_id,
        )
    ]
    designation = standards_optional_text(publication.get("designation")) or args.query
    registry_url = standards_optional_text(publication.get("registry_url")) or "https://csrc.nist.gov/"
    candidates.append(
        standards_base_candidate(
            provider="nist-csrc",
            url=registry_url,
            title=standards_optional_text(publication.get("title")) or designation,
            source_type="standards_registry_entry",
            trust_tier="official_primary",
            relevance_score=0.94,
            trust_score=0.97,
            official_source=True,
            terms_url=None,
            rationale="Concrete NIST publication record from the owning NIST publication surface.",
            recommended_action="fetch",
            standards={
                "registry_provider": "nist-csrc",
                "standards_body": publication.get("standards_body") or "NIST",
                "designation": designation,
                "title": publication.get("title"),
                "publication_date": publication.get("publication_date"),
                "status": publication.get("status"),
                "registry_url": registry_url,
            },
            reasoning={
                "matched_query_terms": [args.query, designation],
                "authority_reason": "CSRC/NIST publication record owns the concrete FIPS/SP identity.",
                "freshness_reason": "Publication record includes status and publication date metadata.",
                "scope_reason": "Fixture designation matches the standards query.",
                "risk_flags": [],
            },
            discovered_at=discovered_at,
            discovery_id=discovery_id,
        )
    )
    return candidates[: args.max_results]


def run_standards_discovery(
    project_root: Path,
    config: dict[str, Any],
    discovery: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    discovered_at = timestamp_utc()
    provider = args.standards_provider
    require_discovery_provider_allowed("standards", discovery, ("standards", f"standards:{provider}"))
    query_parts = [
        provider,
        standards_optional_text(getattr(args, "designation", None)) or standards_optional_text(getattr(args, "query", None)) or "",
    ]
    discovery_id = discovery_run_id("standards", query_parts)
    if provider == "iso-open-data":
        candidates = build_iso_candidates(project_root, args, discovered_at, discovery_id)
    elif provider == "eu-product-requirements":
        candidates = build_eu_candidates(project_root, args, discovered_at, discovery_id)
    elif provider == "uk-geospatial-register":
        candidates = build_uk_geospatial_candidates(project_root, args, discovered_at, discovery_id)
    elif provider == "nist":
        candidates = build_nist_candidates(project_root, args, discovered_at, discovery_id)
    else:  # pragma: no cover - argparse prevents this
        candidates = []
    candidates.sort(
        key=lambda item: (
            TIER_RANK.get(str(item.get("trust_tier")), 99),
            -float(item.get("relevance_score", 0.0)),
            str(item.get("title", "")),
        )
    )
    store_path = candidate_store_path(project_root, config)
    written = append_candidates(store_path, candidates)
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "standards",
        "command": "standards",
        "standards_provider": provider,
        "generated_at": discovered_at,
        "discovered_by": STANDARDS_DISCOVERED_BY,
        "discovery_run_id": discovery_id,
        "network_io_executed": False,
        "max_results": getattr(args, "max_results", len(candidates)),
        "count": len(candidates),
        "candidates": candidates,
        "candidates_path": relative_label(project_root, store_path),
        "written": len(written),
        "warnings": [],
    }


# --- GitHub transport --------------------------------------------------------


def github_token() -> str | None:
    """Read GITHUB_TOKEN from the environment only. Never persisted or emitted."""
    value = os.environ.get("GITHUB_TOKEN")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": GITHUB_USER_AGENT,
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def urllib_github_transport(url: str, timeout: float, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)  # noqa: S310 - URL is built by GitHub provider helpers
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - provider transport is HTTPS-only upstream
        return response.read()


def active_github_transport():
    return GITHUB_TRANSPORT or urllib_github_transport


def github_wait_for_rate_limit() -> None:
    global GITHUB_LAST_REQUEST_AT
    now = float(GITHUB_CLOCK())
    if GITHUB_LAST_REQUEST_AT is not None:
        elapsed = now - GITHUB_LAST_REQUEST_AT
        if elapsed < GITHUB_REQUEST_INTERVAL_SECONDS:
            GITHUB_SLEEP(GITHUB_REQUEST_INTERVAL_SECONDS - elapsed)
            now = float(GITHUB_CLOCK())
    GITHUB_LAST_REQUEST_AT = now


def is_retryable_http_status(code: int) -> bool:
    return code == 429 or 500 <= code <= 599


def close_http_error(exc: HTTPError) -> None:
    """Close provider error response handles before wrapping them."""
    try:
        exc.close()
    except Exception:  # pragma: no cover - defensive cleanup only
        return


def github_error_details() -> dict[str, Any]:
    # A network attempt was made before this error surfaced, so record it.
    return {"command": "github", "network_io_executed": True}


def github_fetch_url(url: str) -> bytes:
    transport = active_github_transport()
    last_error: BaseException | None = None
    for attempt in range(1, GITHUB_MAX_ATTEMPTS + 1):
        github_wait_for_rate_limit()
        try:
            payload = transport(url, GITHUB_TIMEOUT_SECONDS, github_headers())
            if not isinstance(payload, bytes):
                raise DiscoverSourcesError(
                    "DISCOVERY_RESPONSE_INVALID",
                    "GitHub transport returned a non-byte response.",
                    remediation="Fix the discovery transport adapter and retry.",
                    details=github_error_details(),
                )
            if not payload:
                raise DiscoverSourcesError(
                    "DISCOVERY_RESPONSE_INVALID",
                    f"GitHub returned an empty response for {url}.",
                    remediation="Retry later or inspect the provider response outside the workspace.",
                    details=github_error_details(),
                )
            return payload
        except DiscoverSourcesError:
            raise
        except HTTPError as exc:
            last_error = exc
            status = exc.code
            close_http_error(exc)
            if status == 401:
                raise DiscoverSourcesError(
                    "GITHUB_AUTH_REQUIRED",
                    f"GitHub request failed with HTTP 401: {url}",
                    remediation=(
                        "Set a valid GITHUB_TOKEN in the process environment and rerun, "
                        "or unset an invalid token to use unauthenticated discovery."
                    ),
                    details=github_error_details(),
                ) from exc
            if status in {403, 429}:
                raise DiscoverSourcesError(
                    "GITHUB_RATE_LIMITED",
                    f"GitHub rate-limited the request with HTTP {status}: {url}",
                    remediation=(
                        "Retry later, lower --max-results, or set GITHUB_TOKEN in the "
                        "process environment for a higher rate limit."
                    ),
                    details=github_error_details(),
                ) from exc
            if attempt < GITHUB_MAX_ATTEMPTS and is_retryable_http_status(status):
                continue
            raise DiscoverSourcesError(
                "DISCOVERY_NETWORK_ERROR",
                f"GitHub request failed with HTTP {status}: {url}",
                remediation="Retry later, check network access, or lower request volume.",
                details=github_error_details(),
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < GITHUB_MAX_ATTEMPTS:
                continue
    raise DiscoverSourcesError(
        "DISCOVERY_NETWORK_ERROR",
        f"GitHub request failed after {GITHUB_MAX_ATTEMPTS} attempt(s): {last_error}",
        remediation="Retry later, check network access, or lower request volume.",
        details=github_error_details(),
    )


def github_json_object(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"GitHub returned invalid JSON: {exc}",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details=github_error_details(),
        ) from exc
    if not isinstance(document, dict):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            "GitHub returned JSON that was not an object.",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details=github_error_details(),
        )
    return document


# --- GitHub candidate reasoning ----------------------------------------------


def query_tokens(text: str) -> list[str]:
    return _QUERY_TOKEN_RE.findall(text.lower())


def github_license_key(repo: dict[str, Any]) -> str | None:
    """Return the detected SPDX license key, treating NOASSERTION as unknown."""
    license_obj = repo.get("license")
    if not isinstance(license_obj, dict):
        return None
    for key in ("spdx_id", "key"):
        value = license_obj.get(key)
        if isinstance(value, str) and value.strip() and value.strip().upper() != "NOASSERTION":
            return value.strip()
    return None


def github_candidate_id(full_name: str) -> str:
    digest = hashlib.sha1(f"github:{full_name.lower()}".encode(), usedforsecurity=False).hexdigest()[:10]
    return f"cand-{digest}"


def discovery_run_id(provider: str, query_parts: list[str]) -> str:
    normalized_parts = [str(part).strip().lower() for part in query_parts if str(part).strip()]
    payload = json.dumps(
        {
            "provider": provider.strip().lower(),
            "query_parts": normalized_parts,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"disc-{digest}"


def clamp_unit(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 2)


def github_relevance(query: str, terms: list[str], full_name: str, name: str, repo_terms: set[str]) -> tuple[float, list[str]]:
    """Score topical relevance and report which query terms matched.

    Exact owner/repo or repo-name matches are identifier matches and outrank
    fuzzy lexical overlap (Ranking Rule 2 in docs/source-discovery.md).
    """
    normalized_query = query.strip().lower()
    if normalized_query and normalized_query == full_name.lower():
        return 0.97, [full_name.lower()]
    if normalized_query and normalized_query == name.lower():
        return 0.9, [name.lower()]
    matched = [term for term in terms if term in repo_terms]
    ratio = (len(matched) / len(terms)) if terms else 0.0
    return clamp_unit(0.4 + 0.5 * ratio), matched


def build_github_candidate(
    repo: dict[str, Any],
    *,
    query: str,
    terms: list[str],
    request_id: str | None,
    discovery_id: str | None,
    discovered_at: str,
) -> dict[str, Any] | None:
    full_name = repo.get("full_name")
    html_url = repo.get("html_url")
    owner_obj = repo.get("owner")
    owner = owner_obj.get("login") if isinstance(owner_obj, dict) else None
    name = repo.get("name")
    if not isinstance(full_name, str) or not full_name.strip():
        return None
    if not isinstance(html_url, str) or not html_url.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        name = full_name.split("/", 1)[-1]

    description = repo.get("description") if isinstance(repo.get("description"), str) else None
    default_branch = repo.get("default_branch") if isinstance(repo.get("default_branch"), str) else None
    license_key = github_license_key(repo)
    archived = bool(repo.get("archived"))
    is_fork = bool(repo.get("fork"))
    stars = repo.get("stargazers_count") if isinstance(repo.get("stargazers_count"), int) else 0
    forks = repo.get("forks_count") if isinstance(repo.get("forks_count"), int) else 0
    pushed_at = repo.get("pushed_at") if isinstance(repo.get("pushed_at"), str) else None

    repo_text = " ".join(part for part in (full_name, description or "") if part)
    relevance, matched_terms = github_relevance(query, terms, full_name, name, set(query_tokens(repo_text)))
    if not matched_terms:
        # Always record at least the originating query so the reasoning surface is
        # never empty (the contract requires a non-empty matched_query_terms list).
        matched_terms = [query.strip()]

    # A code repository is a primary, non-official source. Canonical ownership
    # cannot be verified from a search result, so official_source stays unknown
    # and the candidate requires review before any fetch (Ranking Rule 3).
    risk_flags: list[str] = ["unknown_officialness"]
    if is_fork:
        trust_tier = "secondary_unknown"
        risk_flags.append("possible_mirror")
    else:
        trust_tier = "primary_non_official"
    if license_key is None:
        risk_flags.append("license_uncertain")
    if archived:
        risk_flags.append("archived")

    base_score = 0.6 if trust_tier == "primary_non_official" else 0.4
    if license_key:
        base_score += 0.1
    if relevance >= 0.95:
        base_score += 0.05
    if archived:
        base_score -= 0.1
    trust_score = clamp_unit(base_score)

    authority_reason = (
        f"GitHub repository {full_name}; a code repository host is a primary, non-official "
        "source. Canonical project ownership is not verified from search results, so "
        "official_source is unknown and the candidate requires review before fetch."
    )
    if is_fork:
        authority_reason += " This repository is a fork (a possible mirror of an upstream repository), lowering it to secondary_unknown."
    if archived:
        freshness_reason = (
            f"Repository is archived (read-only) and may be stale or unmaintained; last pushed {pushed_at or 'unknown'}."
        )
    else:
        freshness_reason = f"Repository is active; last pushed {pushed_at or 'unknown'}."
    license_phrase = (
        f"License detected as {license_key}." if license_key else "No license detected; license is uncertain."
    )
    scope_reason = (
        f"Matched query {query!r} against the repository name and description. "
        f"Stars/forks ({stars}/{forks}) are weak popularity signals only, not trust or license proof."
    )
    rationale = (
        f"GitHub repository {full_name} matched query {query!r} and is classified {trust_tier} "
        f"with unverified canonical ownership, so it is recommended for review before fetch. {license_phrase}"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": github_candidate_id(full_name),
        "request_id": request_id,
        "seed_source_id": None,
        "discovery_run_id": discovery_id,
        "discovered_at": discovered_at,
        "discovered_by": GITHUB_DISCOVERED_BY,
        "provider": "github",
        "url": html_url,
        "title": full_name,
        "source_type": "code_repository",
        "trust_tier": trust_tier,
        "relevance_score": relevance,
        "trust_score": trust_score,
        "official_source": None,
        "jurisdiction": None,
        "license": license_key,
        "terms_url": GITHUB_TERMS_URL,
        "rationale": rationale,
        "recommended_action": "review",
        "network_io_executed": True,
        "reasoning": {
            "matched_query_terms": matched_terms,
            "authority_reason": authority_reason,
            "freshness_reason": freshness_reason,
            "scope_reason": scope_reason,
            "risk_flags": risk_flags,
        },
        "github": {
            "owner": owner,
            "repo": name,
            "full_name": full_name,
            "default_branch": default_branch,
            "description": description,
            "license_key": license_key,
            "stars": stars,
            "forks": forks,
            "archived": archived,
            "is_fork": is_fork,
            "pushed_at": pushed_at,
            "api_url": repo.get("url") if isinstance(repo.get("url"), str) else None,
            # Canonical latest-release pointer for review only. Existence is not
            # verified during discovery; acquisition (E32-T03) confirms it.
            "latest_release_url": f"{html_url.rstrip('/')}/releases/latest",
        },
    }


def github_candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, float, float, str]:
    return (
        TIER_RANK[candidate["trust_tier"]],
        -candidate["trust_score"],
        -candidate["relevance_score"],
        candidate["title"].lower(),
    )


def github_search_url(query: str, per_page: int) -> str:
    params = {"q": query, "per_page": str(per_page)}
    return f"{GITHUB_API_URL}{GITHUB_SEARCH_REPOSITORIES_PATH}?{urlencode(params)}"


def run_github_discovery(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    query = require_non_empty(args.query, "--query")
    request_id_arg = getattr(args, "request_id", None)
    request_id = request_id_arg.strip() if isinstance(request_id_arg, str) else None
    discovery_id = None if request_id is not None else discovery_run_id("github", [query])
    per_page = min(args.max_results, GITHUB_MAX_RESULTS_CAP)

    document = github_json_object(github_fetch_url(github_search_url(query, per_page)))
    items = document.get("items")
    if not isinstance(items, list):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            "GitHub search response did not contain an items list.",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details=github_error_details(),
        )

    discovered_at = timestamp_utc()
    terms = query_tokens(query)
    candidates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = build_github_candidate(
            item,
            query=query,
            terms=terms,
            request_id=request_id,
            discovery_id=discovery_id,
            discovered_at=discovered_at,
        )
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= args.max_results:
            break

    candidates.sort(key=github_candidate_sort_key)
    store_path = candidate_store_path(project_root, config)
    written = append_candidates(store_path, candidates)

    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "github",
        "command": "github",
        "query": query,
        "request_id": request_id,
        "discovery_run_id": discovery_id,
        "max_results": args.max_results,
        "network_io_executed": True,
        # Only whether a token was used, never the token value itself.
        "token_used": github_token() is not None,
        "count": len(candidates),
        "written": len(written),
        "candidates_path": relative_label(project_root, store_path),
        "candidates": candidates,
    }


# --- General search discovery (E33-T01) --------------------------------------


def http_url(value: Any) -> str | None:
    """Return a normalized http/https URL, or None when the value is not one."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def result_host(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    return host.removeprefix("www.")


def normalize_domains(values: Any) -> list[str]:
    if not values:
        return []
    domains: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            domains.append(value.strip().lower().removeprefix("www."))
    return domains


def domain_matches(host: str, domains: list[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def validate_search_relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"research.yml {label} must be a non-empty workspace-relative path")
    normalized = value.strip().replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not a URL: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path without '..': {value}")
    return path


def search_provider_config(discovery: dict[str, Any]) -> dict[str, Any]:
    value = discovery.get("search")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit("research.yml integrations.discovery.search must be a mapping")
    return value


def selected_search_provider(search_cfg: dict[str, Any]) -> str | None:
    provider = search_cfg.get("provider")
    if provider is None:
        return None
    if not isinstance(provider, str):
        raise SystemExit("research.yml integrations.discovery.search.provider must be a string")
    cleaned = provider.strip().lower()
    if not cleaned or cleaned == "none":
        return None
    if cleaned not in SEARCH_PROVIDERS:
        allowed = ", ".join(SEARCH_PROVIDERS)
        raise SystemExit(
            f"research.yml integrations.discovery.search.provider must be one of: {allowed} (got {provider!r})"
        )
    return cleaned


def search_official_domains(search_cfg: dict[str, Any]) -> list[str]:
    """Read the optional official_domains list (E33-T03).

    Workspace-curated official domains raise a result to official_primary,
    complementing the conservative TLD heuristic. Forward-compatible with E34
    jurisdiction profiles, which will populate official domains from a profile.
    """
    raw = search_cfg.get("official_domains")
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise SystemExit(
            "research.yml integrations.discovery.search.official_domains must be a list of domain strings"
        )
    return normalize_domains(raw)


def search_provider_disabled() -> DiscoverSourcesError:
    return DiscoverSourcesError(
        "SEARCH_PROVIDER_DISABLED",
        "No search provider is configured: integrations.discovery.search.provider is unset or 'none'.",
        remediation=(
            "Configure integrations.discovery.search with a fixture, command, or http provider, "
            "then rerun. No commercial search API is enabled by default."
        ),
        details={"command": "search", "network_io_executed": False},
    )


def search_provider_failed(message: str) -> DiscoverSourcesError:
    return DiscoverSourcesError(
        "SEARCH_PROVIDER_FAILED",
        message,
        remediation="Check the configured search provider command/fixture and rerun.",
        details={"command": "search", "network_io_executed": False},
    )


def coerce_search_results(payload: Any) -> list[dict[str, Any]]:
    """Accept a provider payload as a results list or a {'results': [...]} object."""
    if isinstance(payload, dict):
        payload = payload.get("results")
    if not isinstance(payload, list):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            "Search provider did not return a results list.",
            remediation="Return a JSON array of results or an object with a 'results' array.",
            details={"command": "search", "network_io_executed": False},
        )
    return [item for item in payload if isinstance(item, dict)]


# --- Search adapters: fixture, command, http ---------------------------------


def fixture_search_results(project_root: Path, search_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rel = validate_search_relative_path(
        search_cfg.get("fixture_path"), "integrations.discovery.search.fixture_path"
    )
    path = project_root / rel
    if not path.is_file():
        raise search_provider_failed(f"Search fixture file not found: {rel.as_posix()}")
    results: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DiscoverSourcesError(
                "DISCOVERY_RESPONSE_INVALID",
                f"Invalid JSON in search fixture {rel.as_posix()}:{line_number}: {exc}",
                remediation="Fix the fixture so each line is one JSON result object.",
                details={"command": "search", "network_io_executed": False},
            ) from exc
        if isinstance(record, dict):
            results.append(record)
    return results


def build_search_command(command: Any, query: str) -> list[str]:
    if isinstance(command, str):
        argv = shlex.split(command)
    elif isinstance(command, list) and all(isinstance(part, str) for part in command):
        argv = list(command)
    else:
        raise SystemExit(
            "research.yml integrations.discovery.search.command must be a string or a list of strings"
        )
    if not argv:
        raise SystemExit("research.yml integrations.discovery.search.command must not be empty")
    if any(SEARCH_QUERY_PLACEHOLDER in part for part in argv):
        return [part.replace(SEARCH_QUERY_PLACEHOLDER, query) for part in argv]
    return [*argv, query]


def command_search_results(search_cfg: dict[str, Any], query: str) -> list[dict[str, Any]]:
    argv = build_search_command(search_cfg.get("command"), query)
    try:
        completed = subprocess.run(  # noqa: S603 - command is explicit operator configuration, shell=False
            argv,
            capture_output=True,
            text=True,
            timeout=SEARCH_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise search_provider_failed(f"Search command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise search_provider_failed(f"Search command timed out after {SEARCH_COMMAND_TIMEOUT_SECONDS:g}s.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else "no stderr"
        raise search_provider_failed(f"Search command exited with code {completed.returncode}: {tail}")
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"Search command did not emit valid JSON: {exc}",
            remediation="The command must print a JSON results array (or {'results': [...]}) to stdout.",
            details={"command": "search", "network_io_executed": False},
        ) from exc
    return coerce_search_results(payload)


def active_search_http_transport():
    return SEARCH_HTTP_TRANSPORT or urllib_github_transport


def http_search_url(search_cfg: dict[str, Any], request: dict[str, Any]) -> str:
    endpoint = http_url(search_cfg.get("endpoint"))
    if endpoint is None:
        raise SystemExit(
            "research.yml integrations.discovery.search.endpoint must be an explicit http(s) URL for the http provider"
        )
    query_param = search_cfg.get("query_param", "q")
    if not isinstance(query_param, str) or not query_param.strip():
        raise SystemExit("research.yml integrations.discovery.search.query_param must be a non-empty string")
    params: dict[str, str] = {query_param.strip(): request["query"]}
    extra = search_cfg.get("params")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and isinstance(value, (str, int)):
                params[key] = str(value)
    separator = "&" if urlparse(endpoint).query else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


def http_search_results(search_cfg: dict[str, Any], request: dict[str, Any]) -> list[dict[str, Any]]:
    url = http_search_url(search_cfg, request)
    headers = {"Accept": "application/json", "User-Agent": SEARCH_USER_AGENT}
    transport = active_search_http_transport()
    last_error: BaseException | None = None
    payload: bytes | None = None
    for attempt in range(1, SEARCH_HTTP_MAX_ATTEMPTS + 1):
        try:
            payload = transport(url, SEARCH_HTTP_TIMEOUT_SECONDS, headers)
            break
        except HTTPError as exc:
            last_error = exc
            status = exc.code
            close_http_error(exc)
            if attempt < SEARCH_HTTP_MAX_ATTEMPTS and is_retryable_http_status(status):
                continue
            raise DiscoverSourcesError(
                "DISCOVERY_NETWORK_ERROR",
                f"Search provider request failed with HTTP {status}: {url}",
                remediation="Retry later, check the endpoint, or lower request volume.",
                details={"command": "search", "network_io_executed": True},
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < SEARCH_HTTP_MAX_ATTEMPTS:
                continue
    if payload is None:
        raise DiscoverSourcesError(
            "DISCOVERY_NETWORK_ERROR",
            f"Search provider request failed after {SEARCH_HTTP_MAX_ATTEMPTS} attempt(s): {last_error}",
            remediation="Retry later, check network access, or lower request volume.",
            details={"command": "search", "network_io_executed": True},
        )
    if not isinstance(payload, bytes):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            "Search HTTP transport returned a non-byte response.",
            remediation="Fix the search transport adapter and retry.",
            details={"command": "search", "network_io_executed": True},
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"Search provider returned invalid JSON: {exc}",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details={"command": "search", "network_io_executed": True},
        ) from exc
    results_key = search_cfg.get("results_path", "results")
    if isinstance(document, dict) and isinstance(results_key, str):
        document = document.get(results_key)
    return coerce_search_results(document)


def gather_search_results(
    project_root: Path,
    provider: str,
    search_cfg: dict[str, Any],
    request: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Dispatch to the selected adapter. Returns (raw results, network_io_executed)."""
    if provider == "fixture":
        return fixture_search_results(project_root, search_cfg), False
    if provider == "command":
        return command_search_results(search_cfg, request["query"]), False
    if provider == "http":
        return http_search_results(search_cfg, request), True
    raise SystemExit(f"research.yml integrations.discovery.search.provider is not supported: {provider}")


# --- Search result normalization ---------------------------------------------


def search_candidate_id(url: str) -> str:
    digest = hashlib.sha1(f"search:{url.strip().lower()}".encode(), usedforsecurity=False).hexdigest()[:10]
    return f"cand-{digest}"


def _published_year(published: str | None) -> int | None:
    """Best-effort 4-digit year extraction from a publication timestamp."""
    if not published:
        return None
    match = re.search(r"\b(18|19|20)\d{2}\b", published)
    return int(match.group(0)) if match else None


def _download_extension(url: str, extensions: tuple[str, ...]) -> str | None:
    path = urlparse(url).path.lower()
    if not path:
        return None
    for ext in extensions:
        if path.endswith(ext):
            return ext
    return None


def is_suspicious_download_url(url: str, expected_source_type: str | None) -> bool:
    """A direct executable/installer/script download is suspicious; an archive is
    suspicious only when the query was not looking for a dataset/code archive."""
    if _download_extension(url, SEARCH_SUSPICIOUS_DOWNLOAD_EXTENSIONS) is not None:
        return True
    if expected_source_type in SEARCH_ARCHIVE_OK_SOURCE_TYPES:
        return False
    return _download_extension(url, SEARCH_ARCHIVE_EXTENSIONS) is not None


def search_mirror_signal(host: str, url: str, title: str) -> tuple[bool, bool]:
    """Return (mirror_telltale_hit, terms_prohibited_title_hit)."""
    path = urlparse(url).path.lower()
    title_lower = title.lower()
    mirror_hit = (
        any(token in host for token in SEARCH_MIRROR_HOST_TOKENS)
        or any(token in path for token in SEARCH_MIRROR_PATH_TOKENS)
        or any(token in title_lower for token in SEARCH_MIRROR_TITLE_TOKENS)
    )
    terms_prohibited = any(token in title_lower for token in SEARCH_TERMS_PROHIBITED_TITLE_TOKENS)
    return mirror_hit, terms_prohibited


def _title_token_set(title: str) -> set[str]:
    return set(query_tokens(title))


def _title_overlap(a: set[str], b: set[str]) -> float:
    """Jaccard-like overlap bounded to [0, 1]; 0 when either side is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def classify_search_result(
    *,
    host: str,
    url: str,
    title: str,
    snippet: str | None,
    published: str | None,
    query: str,
    terms: list[str],
    position: int,
    prefer_official: bool,
    expected_source_type: str | None,
    official_domains: list[str],
    domain_allowlist: list[str],
    result_hint_official: bool | None,
    result_hint_tier: str | None,
    result_hint_source_type: str | None,
) -> dict[str, Any]:
    """Apply the trust-tier policy (docs/source-discovery.md) to one result.

    Provider rank is an input feature only; the tier, scores, officialness, risk
    flags, and recommended action are derived here from policy signals, not from
    the backend's ordering. Returns a classification consumed by
    build_search_candidate.
    """
    text_pool = f"{title} {snippet or ''}"
    text_tokens = set(query_tokens(text_pool))
    matched = [term for term in terms if term in text_tokens]
    query_norm = query.strip().lower()
    exact_phrase = bool(query_norm) and query_norm in text_pool.lower()
    if exact_phrase and query_norm not in matched:
        matched = [query_norm, *matched]
    if not matched:
        matched = [query_norm or (query_tokens(title)[:1] or ["result"])]

    # Relevance: exact phrase (Ranking Rule 2 analog) beats token overlap;
    # provider position is a weak input feature, never the authority.
    overlap = (len([term for term in terms if term in text_tokens]) / len(terms)) if terms else 0.0
    relevance = clamp_unit(0.78 - 0.04 * position + 0.10 * overlap + (0.12 if exact_phrase else 0.0))

    # --- Official detection (combination; E34 jurisdiction profiles layer on) ---
    official: bool | None = None
    official_signal: str | None = None
    if official_domains and domain_matches(host, official_domains):
        official, official_signal = True, "the integrations.discovery.search.official_domains allowlist"
    elif prefer_official and domain_allowlist and domain_matches(host, domain_allowlist):
        official, official_signal = True, "the --domain-allow list for this official/legal query"
    elif any(host.endswith(tld) for tld in SEARCH_OFFICIAL_TLDS):
        tld = next(tld for tld in SEARCH_OFFICIAL_TLDS if host.endswith(tld))
        official, official_signal = True, f"the official {tld} top-level domain"
    elif result_hint_official is True:
        official, official_signal = True, "the provider's official hint"

    suspicious = is_suspicious_download_url(url, expected_source_type)
    mirror_hit, terms_prohibited = search_mirror_signal(host, url, title)

    risk_flags: list[str] = []
    if suspicious:
        trust_tier = "unsafe_or_unusable"
        risk_flags.append(SEARCH_RISK_SUSPICIOUS_DOWNLOAD)
        authority_reason = (
            "URL points to a direct executable/archive download; a generic web search result cannot "
            "verify what such a file runs, so it is unsafe_or_unusable."
        )
    elif mirror_hit and official is not True:
        risk_flags.append(SEARCH_RISK_POSSIBLE_MIRROR)
        if terms_prohibited:
            trust_tier = "unsafe_or_unusable"
            risk_flags.append(SEARCH_RISK_TERMS_PROHIBITED)
            authority_reason = (
                f"{host} carries mirror/scraped-copy signals and the title suggests an unauthorized or "
                "prohibited copy, so it is unsafe_or_unusable."
            )
        else:
            trust_tier = "secondary_unknown"
            authority_reason = (
                f"{host} carries mirror/scraped-copy signals (a possible non-authoritative copy of an "
                "upstream source), so it is demoted to secondary_unknown."
            )
    elif official is True:
        trust_tier = "official_primary"
        authority_reason = (
            f"{host} is recognized as an official authority via {official_signal}; treated as "
            "official_primary for this topic. A jurisdiction profile (E34) refines official-domain matching."
        )
    elif result_hint_tier in TIER_RANK and result_hint_tier != "official_primary":
        trust_tier = result_hint_tier
        authority_reason = (
            f"Provider hinted trust_tier {trust_tier}; provenance is still unverified, so officialness "
            "is recorded as unknown and must be reviewed before fetch."
        )
    else:
        trust_tier = "secondary_unknown"
        authority_reason = (
            f"Generic search result from {host}; provenance and officialness are unverified, so it is "
            "classified secondary_unknown and must be reviewed before any fetch."
        )

    if trust_tier == "official_primary":
        official_source: bool | None = True
    elif trust_tier == "unsafe_or_unusable" or mirror_hit:
        official_source = False
    else:
        official_source = None
        risk_flags.append(SEARCH_RISK_UNKNOWN_OFFICIALNESS)

    if result_hint_source_type in ALLOWED_SEARCH_SOURCE_TYPES:
        source_type = result_hint_source_type
    elif prefer_official and trust_tier == "official_primary":
        source_type = "official_legal"
    else:
        source_type = "web_page"

    year = _published_year(published)
    if year is not None:
        if datetime.now(timezone.utc).year - year >= SEARCH_STALE_YEARS_THRESHOLD:
            risk_flags.append(SEARCH_RISK_STALE_SOURCE)
            freshness_reason = (
                f"Published {year}; over {SEARCH_STALE_YEARS_THRESHOLD} years old, may be stale or superseded."
            )
        else:
            freshness_reason = f"Search provider reported publication date {published}."
    elif published:
        freshness_reason = f"Search provider reported publication date {published}."
    else:
        freshness_reason = "No freshness signal reported by the search provider."

    if exact_phrase:
        phrase_note = "exact phrase match"
    elif matched:
        phrase_note = "token overlap"
    else:
        phrase_note = "no lexical overlap"
    scope_reason = (
        f"Matched query {query!r} against the result title and snippet ({phrase_note}) "
        f"at provider position {position + 1}."
    )

    base = SEARCH_TIER_TRUST_BASE.get(trust_tier, 0.4)
    trust_score = clamp_unit(base - 0.05 * len(risk_flags) + (0.03 if (year is not None and SEARCH_RISK_STALE_SOURCE not in risk_flags) else 0.0))

    uncertain_terms = {SEARCH_RISK_LICENSE_UNCERTAIN, SEARCH_RISK_TERMS_UNCERTAIN}
    if trust_tier == "unsafe_or_unusable":
        recommended_action = "reject"
    elif official_source is None:
        recommended_action = "review"
    elif set(risk_flags) & uncertain_terms:
        recommended_action = "review"
    elif trust_tier in ("official_primary", "primary_non_official") and not risk_flags:
        recommended_action = "fetch"
    else:
        recommended_action = "review"

    return {
        "trust_tier": trust_tier,
        "official_source": official_source,
        "source_type": source_type,
        "relevance_score": relevance,
        "trust_score": trust_score,
        "recommended_action": recommended_action,
        "matched_query_terms": matched,
        "authority_reason": authority_reason,
        "freshness_reason": freshness_reason,
        "scope_reason": scope_reason,
        "risk_flags": risk_flags,
        "exact_phrase": exact_phrase,
    }


def apply_search_trust_rejection(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cross-result pass: mark non-official mirrors and lower-trust duplicates of
    an official source as ``reject`` (recorded with rationale, never silently
    dropped). Per-result suspicious/unsafe classification already happened in
    classify_search_result; this only adjusts recommended_action for duplicates
    of an official_primary candidate in the same run.
    """
    officials = [
        candidate
        for candidate in candidates
        if candidate["trust_tier"] == "official_primary" and candidate.get("official_source") is True
    ]
    if not officials:
        return candidates
    official_title_keys = [_title_token_set(candidate["title"]) for candidate in officials]
    for candidate in candidates:
        if candidate["trust_tier"] == "official_primary":
            continue
        if candidate["recommended_action"] == "reject":
            continue
        is_mirror = SEARCH_RISK_POSSIBLE_MIRROR in candidate["reasoning"]["risk_flags"]
        overlap = max(
            (_title_overlap(_title_token_set(candidate["title"]), key) for key in official_title_keys),
            default=0.0,
        )
        if is_mirror or overlap >= SEARCH_DUPLICATE_TITLE_OVERLAP:
            reason = (
                "non-official mirror/scraped copy of an official source in the same run"
                if is_mirror and overlap < SEARCH_DUPLICATE_TITLE_OVERLAP
                else "lower-trust duplicate of an official source in the same run"
            )
            candidate["recommended_action"] = "reject"
            if SEARCH_RISK_DUPLICATE_OF_OFFICIAL not in candidate["reasoning"]["risk_flags"]:
                candidate["reasoning"]["risk_flags"].append(SEARCH_RISK_DUPLICATE_OF_OFFICIAL)
            candidate["rationale"] = candidate["rationale"].rstrip(".") + f". Rejected as a {reason}."
    return candidates


# --- Legal candidate ranking (E34-T03) ---------------------------------------


def is_legal_secondary_db(host: str) -> bool:
    """A recognized reputable secondary legal database/aggregator (Justia,
    FindLaw, CourtListener, ...): not the official primary authority."""
    return domain_matches(host, list(LEGAL_SECONDARY_DB_HOSTS))


def _legal_superseded_signal(candidate: dict[str, Any]) -> bool:
    pool = f"{candidate.get('title', '')} {candidate['search'].get('snippet') or ''}".lower()
    return any(token in pool for token in LEGAL_SUPERSEDED_TOKENS)


def refine_legal_candidates(
    candidates: list[dict[str, Any]],
    *,
    official_domains: list[str],
    jurisdiction: str | None,
) -> list[dict[str, Any]]:
    """Apply the E34-T03 legal ranking rules on top of the E33-T03 classification.

    The profile's official domains were already passed to the classifier as the
    major trust signal, so a host match is `official_primary`. This pass then:

    - records *why* a source is official (profile official-domain match) or why
      officialness is unknown, so every candidate is review-ready;
    - keeps official gazette/legislature/regulator/court sources ahead of
      aggregators (the sort key already ranks `official_primary` first);
    - retains recognized secondary legal databases as `secondary_reputable`
      (never silently dropped) but, when an official source is available in the
      same run, marks them supplemental-only rather than primary authority;
    - rejects non-official mirrors/scraped copies and lower-trust duplicates of an
      official source available in the run (a reputable secondary DB is exempt);
    - rejects superseded/repealed/historical pages while retaining them as
      auditable negative candidates with a stable risk flag.
    """
    # Pass 1: legal-specific re-tiering and flags.
    for candidate in candidates:
        reasoning = candidate["reasoning"]
        risk_flags = reasoning["risk_flags"]
        host = candidate["search"]["host"]

        if _legal_superseded_signal(candidate) and LEGAL_RISK_SUPERSEDED not in risk_flags:
            risk_flags.append(LEGAL_RISK_SUPERSEDED)
            reasoning["freshness_reason"] = (
                reasoning["freshness_reason"].rstrip(".")
                + ". Title/snippet signals a superseded, repealed, or historical version."
            )
            candidate["recommended_action"] = "reject"

        if candidate["trust_tier"] == "official_primary":
            continue

        # Recognized reputable secondary legal databases: retain as
        # secondary_reputable, never drop. Officialness becomes known (False).
        if candidate["trust_tier"] != "unsafe_or_unusable" and is_legal_secondary_db(host):
            candidate["trust_tier"] = "secondary_reputable"
            candidate["official_source"] = False
            if SEARCH_RISK_UNKNOWN_OFFICIALNESS in risk_flags:
                risk_flags.remove(SEARCH_RISK_UNKNOWN_OFFICIALNESS)
            reasoning["authority_reason"] = (
                f"{host} is a recognized secondary legal database (reputable, widely cited, but not "
                "the official primary authority for this jurisdiction)."
            )

    official_present = any(
        c["trust_tier"] == "official_primary" and c.get("official_source") is True for c in candidates
    )
    official_title_keys = [
        _title_token_set(c["title"]) for c in candidates if c["trust_tier"] == "official_primary"
    ]

    # Pass 2: officialness rationale, supplemental marking, and rejection of
    # mirrors/duplicates of an official source available in the run.
    for candidate in candidates:
        reasoning = candidate["reasoning"]
        risk_flags = reasoning["risk_flags"]
        host = candidate["search"]["host"]
        tier = candidate["trust_tier"]

        if tier == "official_primary":
            if domain_matches(host, official_domains):
                reason = f"{host} matches an official domain in the {jurisdiction} jurisdiction profile"
            else:
                reason = reasoning["authority_reason"].rstrip(".")
            officialness = (
                f"Official source: {reason} -- the major trust signal for legal research, so it "
                "outranks secondary legal databases and aggregators."
            )
        elif tier == "unsafe_or_unusable":
            officialness = "Not a usable legal source; " + reasoning["authority_reason"].rstrip(".") + "."
        else:
            is_secondary_db = tier == "secondary_reputable" and is_legal_secondary_db(host)
            is_mirror = SEARCH_RISK_POSSIBLE_MIRROR in risk_flags
            overlap = max(
                (_title_overlap(_title_token_set(candidate["title"]), key) for key in official_title_keys),
                default=0.0,
            )
            if official_present and not is_secondary_db and (is_mirror or overlap >= SEARCH_DUPLICATE_TITLE_OVERLAP):
                candidate["recommended_action"] = "reject"
                if SEARCH_RISK_DUPLICATE_OF_OFFICIAL not in risk_flags:
                    risk_flags.append(SEARCH_RISK_DUPLICATE_OF_OFFICIAL)
                officialness = (
                    "Not an official source and mirrors/duplicates an official source available in "
                    "this run; rejected in favor of the official source."
                )
            elif official_present:
                if LEGAL_RISK_SECONDARY_WHEN_OFFICIAL not in risk_flags:
                    risk_flags.append(LEGAL_RISK_SECONDARY_WHEN_OFFICIAL)
                if candidate["recommended_action"] == "fetch":
                    candidate["recommended_action"] = "review"
                label = "a recognized secondary legal database" if is_secondary_db else "of unknown officialness"
                officialness = (
                    f"This source is {label}; an official source for jurisdiction {jurisdiction} is "
                    "available in this run, so use it only as supplemental authority, not primary."
                )
            elif is_secondary_db:
                officialness = (
                    "Recognized secondary legal database (reputable but not official); no official "
                    "source was found in this run, so review before relying on it as primary authority."
                )
            else:
                officialness = (
                    f"Officialness unknown: {host} is not in the {jurisdiction} jurisdiction's official "
                    "domains. Review before relying on it as legal authority."
                )

        # Rebuild the rationale from the final tier/officialness/action (the base
        # search rationale was written before this legal re-tiering), so the record
        # a reviewer reads is self-consistent.
        candidate["rationale"] = (
            f"Legal candidate {candidate['url']} classified {candidate['trust_tier']} "
            f"(official_source={candidate['official_source']}); recommended_action "
            f"{candidate['recommended_action']} before any fetch. {officialness}"
        )
    return candidates


def build_search_candidate(
    result: dict[str, Any],
    *,
    request: dict[str, Any],
    terms: list[str],
    request_id: str | None,
    discovery_id: str | None,
    network_io: bool,
    position: int,
    discovered_at: str,
    provider: str = "search",
    discovered_by: str = SEARCH_DISCOVERED_BY,
) -> dict[str, Any] | None:
    url = http_url(result.get("url")) or http_url(result.get("link"))
    if url is None:
        return None
    host = result_host(url)
    title_value = result.get("title") or result.get("name")
    title = title_value.strip() if isinstance(title_value, str) and title_value.strip() else url
    snippet_value = result.get("snippet") or result.get("description")
    snippet = snippet_value.strip() if isinstance(snippet_value, str) and snippet_value.strip() else None
    published = result.get("published") if isinstance(result.get("published"), str) else None
    license_value = result.get("license") if isinstance(result.get("license"), str) and result.get("license").strip() else None
    terms_url_value = http_url(result.get("terms_url"))
    result_hint_official = result.get("official") if isinstance(result.get("official"), bool) else None
    result_hint_tier = result.get("trust_tier") if isinstance(result.get("trust_tier"), str) else None
    result_hint_source_type = result.get("source_type") if isinstance(result.get("source_type"), str) else None

    # Rank with the trust-tier policy (E33-T03), not the provider's ordering.
    classification = classify_search_result(
        host=host,
        url=url,
        title=title,
        snippet=snippet,
        published=published,
        query=request["query"],
        terms=terms,
        position=position,
        prefer_official=bool(request.get("prefer_official", False)),
        expected_source_type=request.get("expected_source_type"),
        official_domains=request.get("official_domains", []),
        domain_allowlist=request["domain_allowlist"],
        result_hint_official=result_hint_official,
        result_hint_tier=result_hint_tier,
        result_hint_source_type=result_hint_source_type,
    )
    trust_tier = classification["trust_tier"]
    official_source = classification["official_source"]
    recommended_action = classification["recommended_action"]
    risk_flags = classification["risk_flags"]

    rationale = (
        f"Search candidate {url} matched query {request['query']!r} and is classified {trust_tier} "
        f"(official_source={official_source}); recommended_action {recommended_action} before any fetch."
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": search_candidate_id(url),
        "request_id": request_id,
        "seed_source_id": None,
        "discovery_run_id": discovery_id,
        "discovered_at": discovered_at,
        "discovered_by": discovered_by,
        "provider": provider,
        "url": url,
        "title": title,
        "source_type": classification["source_type"],
        "trust_tier": trust_tier,
        "relevance_score": classification["relevance_score"],
        "trust_score": classification["trust_score"],
        "official_source": official_source,
        "jurisdiction": request["jurisdiction"],
        "license": license_value,
        "terms_url": terms_url_value,
        "rationale": rationale,
        "recommended_action": recommended_action,
        "network_io_executed": network_io,
        "reasoning": {
            "matched_query_terms": classification["matched_query_terms"],
            "authority_reason": classification["authority_reason"],
            "freshness_reason": classification["freshness_reason"],
            "scope_reason": classification["scope_reason"],
            "risk_flags": risk_flags,
        },
        "search": {
            "host": host,
            "snippet": snippet,
            "provider_rank": position + 1,
            "exact_phrase": classification["exact_phrase"],
        },
    }


def search_candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, float, float, str]:
    # Mirrors the documented policy key: trust tier dominates, a known official
    # source breaks ties, then trust and relevance scores. Provider rank is not
    # an input to ordering.
    return (
        TIER_RANK[candidate["trust_tier"]],
        0 if candidate.get("official_source") is True else 1,
        -candidate["trust_score"],
        -candidate["relevance_score"],
        candidate["title"].lower(),
    )


def normalize_search_candidates(
    results: list[dict[str, Any]],
    *,
    request: dict[str, Any],
    request_id: str | None,
    discovery_id: str | None,
    network_io: bool,
    discovered_at: str,
    provider: str = "search",
    discovered_by: str = SEARCH_DISCOVERED_BY,
) -> list[dict[str, Any]]:
    allowlist = request["domain_allowlist"]
    blocklist = request["domain_blocklist"]
    terms = query_tokens(request["query"])
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    position = 0
    for result in results:
        candidate = build_search_candidate(
            result,
            request=request,
            terms=terms,
            request_id=request_id,
            discovery_id=discovery_id,
            network_io=network_io,
            position=position,
            discovered_at=discovered_at,
            provider=provider,
            discovered_by=discovered_by,
        )
        if candidate is None:
            continue
        host = candidate["search"]["host"]
        if allowlist and not domain_matches(host, allowlist):
            continue
        if blocklist and domain_matches(host, blocklist):
            continue
        normalized_url = candidate["url"].strip().lower()
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        position += 1
        candidates.append(candidate)
        if len(candidates) >= request["max_results"]:
            break
    return candidates


# --- Query planning (E33-T02) ------------------------------------------------


def lookup_request_kind(project_root: Path, config: dict[str, Any], request_id: str) -> str | None:
    """Return the kind of a matching source request to infer search intent.

    Lenient by design (mirrors github discovery's unvalidated --request-id): a
    request_id with no matching record simply yields no kind, so the candidate is
    still linked to it while intent falls back to jurisdiction or the general web
    intent. The link is recorded regardless.
    """
    import source_requests

    path = source_requests.requests_path(project_root, config)
    records = source_requests.load_requests(path)
    for record in records:
        if record.get("request_id") == request_id:
            kind = record.get("kind")
            return kind if isinstance(kind, str) else None
    return None


def resolve_search_intent(explicit_intent: str | None, request_kind: str | None, jurisdiction: str | None) -> str:
    """Pick the planning intent. Explicit wins; then a specific request kind;
    then a jurisdiction implies legal; otherwise a general web query."""
    if explicit_intent:
        return explicit_intent
    mapped = REQUEST_KIND_TO_INTENT.get(request_kind or "")
    if mapped:
        return mapped
    if jurisdiction:
        return "legal"
    return "web"


def planned_query(
    need: str,
    suffix: str,
    expected_source_type: str,
    rationale: str,
    domain_allowlist: list[str],
    domain_blocklist: list[str],
    *,
    prefer_official: bool = False,
    jurisdiction: str | None = None,
) -> dict[str, Any]:
    text = need if not suffix else f"{need} {suffix}"
    record: dict[str, Any] = {
        "query": text,
        "expected_source_type": expected_source_type,
        "domain_allowlist": domain_allowlist,
        "domain_blocklist": domain_blocklist,
        "rationale": rationale,
    }
    if prefer_official:
        record["prefer_official"] = True
    if jurisdiction:
        record["jurisdiction"] = jurisdiction
    return record


def build_search_query_plan(
    need: str,
    intent: str,
    jurisdiction: str | None,
    domain_allowlist: list[str],
    domain_blocklist: list[str],
) -> list[dict[str, Any]]:
    """Expand a research need into a small, bounded set of explained queries."""
    if intent == "legal":
        suffix_note = f" for jurisdiction {jurisdiction}" if jurisdiction else ""
        return [
            planned_query(
                need,
                term,
                "official_legal",
                (
                    f"{rationale}{suffix_note} "
                    "Prefer official government, legislature, regulator, court, or gazette sources over aggregators."
                ),
                domain_allowlist,
                domain_blocklist,
                prefer_official=True,
                jurisdiction=jurisdiction,
            )
            for term, rationale in SEARCH_LEGAL_TEMPLATES
        ]
    return [
        planned_query(need, suffix, expected_source_type, rationale, domain_allowlist, domain_blocklist)
        for suffix, expected_source_type, rationale in SEARCH_INTENT_TEMPLATES[intent]
    ]


def execute_query_plan(
    project_root: Path,
    config: dict[str, Any],
    discovery: dict[str, Any],
    planned_queries: list[dict[str, Any]],
    *,
    base_request: dict[str, Any],
    request_id: str | None,
    discovery_id: str | None,
    official_domains: list[str] | None = None,
    use_query_allowlist: bool = True,
    candidate_provider: str = "search",
    discovered_by: str = SEARCH_DISCOVERED_BY,
    refine: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run each planned query through the configured backend, aggregating
    deduplicated candidates ranked by the trust-tier policy. Provider order across
    queries is not authoritative.

    By default a cross-result pass rejects non-official mirrors and lower-trust
    duplicates of an official source in the same run. Legal discovery (E34-T03)
    overrides two things: it sets ``use_query_allowlist=False`` so a profile's
    official domains act as a major trust *signal* (raising matches to
    official_primary) rather than a hard filter that would drop the secondary
    sources legal review needs to see, and it passes a ``refine`` hook that applies
    the legal-specific ranking rules instead of the generic rejection pass.
    """
    search_cfg = search_provider_config(discovery)
    provider = selected_search_provider(search_cfg)
    if provider is None:
        raise search_provider_disabled()

    discovered_at = timestamp_utc()
    network_io_any = False
    aggregated: dict[str, dict[str, Any]] = {}
    for planned in planned_queries:
        sub_request = {
            "query": planned["query"],
            "request_id": request_id,
            "jurisdiction": base_request["jurisdiction"],
            "domain_allowlist": planned["domain_allowlist"] if use_query_allowlist else [],
            "domain_blocklist": planned["domain_blocklist"],
            "max_results": base_request["max_results"],
            # Thread trust-ranking signals (E33-T03) into normalization.
            "prefer_official": bool(planned.get("prefer_official", False)),
            "expected_source_type": planned.get("expected_source_type"),
            "official_domains": official_domains or [],
        }
        raw_results, network_io = gather_search_results(project_root, provider, search_cfg, sub_request)
        network_io_any = network_io_any or network_io
        for candidate in normalize_search_candidates(
            raw_results,
            request=sub_request,
            request_id=request_id,
            discovery_id=discovery_id,
            network_io=network_io,
            discovered_at=discovered_at,
            provider=candidate_provider,
            discovered_by=discovered_by,
        ):
            aggregated.setdefault(candidate["candidate_id"], candidate)

    candidates = list(aggregated.values())
    candidates = refine(candidates) if refine is not None else apply_search_trust_rejection(candidates)
    candidates.sort(key=search_candidate_sort_key)
    candidates = candidates[: base_request["max_results"]]
    store_path = candidate_store_path(project_root, config)
    written = append_candidates(store_path, candidates)
    return {
        "search_provider": provider,
        "network_io_executed": network_io_any,
        "count": len(candidates),
        "written": len(written),
        "candidates_path": relative_label(project_root, store_path),
        "candidates": candidates,
    }


def run_search_discovery(
    project_root: Path,
    config: dict[str, Any],
    discovery: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    need = require_non_empty(args.query, "--query")
    request_id_arg = getattr(args, "request_id", None)
    request_id = request_id_arg.strip() if isinstance(request_id_arg, str) and request_id_arg.strip() else None
    jurisdiction_arg = getattr(args, "jurisdiction", None)
    jurisdiction = jurisdiction_arg.strip() if isinstance(jurisdiction_arg, str) and jurisdiction_arg.strip() else None
    domain_allowlist = normalize_domains(args.domain_allowlist)
    domain_blocklist = normalize_domains(args.domain_blocklist)

    request_kind = lookup_request_kind(project_root, config, request_id) if request_id is not None else None
    intent = resolve_search_intent(getattr(args, "intent", None), request_kind, jurisdiction)
    planned_queries = build_search_query_plan(need, intent, jurisdiction, domain_allowlist, domain_blocklist)
    discovery_id = None if request_id is not None else discovery_run_id("search", [need, intent])
    official_domains = search_official_domains(search_provider_config(discovery))

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provider": "search",
        "command": "search",
        "mode": "execute" if args.execute else "plan",
        "research_need": need,
        "request_id": request_id,
        "request_kind": request_kind,
        "intent": intent,
        "jurisdiction": jurisdiction,
        "discovery_run_id": discovery_id,
        "domain_allowlist": domain_allowlist,
        "domain_blocklist": domain_blocklist,
        "official_domains": official_domains,
        "max_results": args.max_results,
        "planned_query_count": len(planned_queries),
        "planned_queries": planned_queries,
    }
    if not args.execute:
        # Planning is read-only and contacts no backend, so a plan is explainable
        # before any provider is configured or any network I/O happens.
        report["network_io_executed"] = False
        return report

    base_request = {
        "query": need,
        "jurisdiction": jurisdiction,
        "max_results": args.max_results,
    }
    report.update(
        execute_query_plan(
            project_root,
            config,
            discovery,
            planned_queries,
            base_request=base_request,
            request_id=request_id,
            discovery_id=discovery_id,
            official_domains=official_domains,
        )
    )
    return report


def run_jurisdictions_command(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.jurisdictions_command == "validate":
        return run_jurisdictions_validate(project_root, config)
    if args.jurisdictions_command == "list":
        return run_jurisdictions_list(project_root, config)
    if args.jurisdictions_command == "show":
        return run_jurisdictions_show(project_root, config, args)
    raise DiscoverSourcesError(
        "NOT_IMPLEMENTED",
        f"jurisdictions {args.jurisdictions_command} is not implemented.",
        remediation="Choose jurisdictions validate, list, or show.",
    )


# --- Jurisdiction profile schema, loader, and validation (E34-T01) ------------


def _jurisdiction_invalid(message: str, *, remediation: str | None = None) -> DiscoverSourcesError:
    return DiscoverSourcesError(
        "JURISDICTION_INVALID",
        message,
        remediation=remediation or "Fix the profile in sources/jurisdictions.yml and rerun jurisdictions validate.",
        details={"command": "jurisdictions", "network_io_executed": False},
    )


def jurisdictions_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the integrations.discovery mapping (may be empty). Jurisdiction
    profiles are an offline concern, so this is read leniently even when
    discovery is disabled."""
    discovery = integrations_config(config).get("discovery")
    return discovery if isinstance(discovery, dict) else {}


def jurisdictions_path(project_root: Path, config: dict[str, Any]) -> Path:
    """Resolve the workspace-local jurisdictions file. Pinned under sources/ via
    the shared path validator (mirrors sources.manifest_path)."""
    import source_requests

    default = "/".join(JURISDICTIONS_DEFAULT_RELATIVE)
    value = jurisdictions_config(config).get("jurisdictions_path")
    relative = source_requests.validate_generated_sources_path(
        value if isinstance(value, str) and value.strip() else default,
        "integrations.discovery.jurisdictions_path",
    )
    return project_root / relative


def validate_jurisdiction_profile(profile: Any, seen_ids: set[str]) -> dict[str, Any]:
    """Validate and normalize one jurisdiction profile. Raises JURISDICTION_INVALID
    on any schema violation. Returns a normalized profile dict."""
    if not isinstance(profile, dict):
        raise _jurisdiction_invalid("A jurisdiction profile must be a mapping.")

    raw_id = profile.get("jurisdiction_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise _jurisdiction_invalid("jurisdiction_id is required and must be a non-empty string.")
    jurisdiction_id = raw_id.strip().lower()
    if not JURISDICTION_ID_RE.match(jurisdiction_id):
        raise _jurisdiction_invalid(
            f"jurisdiction_id {raw_id!r} must be a lowercase slug of letters, digits, and hyphens "
            "(for example us-federal, us-ca)."
        )
    if jurisdiction_id in seen_ids:
        raise _jurisdiction_invalid(f"jurisdiction_id {jurisdiction_id!r} appears more than once.")
    seen_ids.add(jurisdiction_id)
    scope = f"profile {jurisdiction_id!r}"

    name = profile.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _jurisdiction_invalid(f"{scope}: name is required and must be a non-empty string.")

    country = profile.get("country")
    if not isinstance(country, str) or not country.strip():
        raise _jurisdiction_invalid(f"{scope}: country is required (ISO 3166 alpha-2, e.g. US).")
    country = country.strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise _jurisdiction_invalid(f"{scope}: country {country!r} must be a 2-letter ISO alpha-2 code.")

    state_or_region_raw = profile.get("state_or_region")
    if state_or_region_raw is None:
        state_or_region: str | None = None
    elif not isinstance(state_or_region_raw, str) or not state_or_region_raw.strip():
        raise _jurisdiction_invalid(f"{scope}: state_or_region must be a non-empty string when present.")
    else:
        state_or_region = state_or_region_raw.strip()

    normalized: dict[str, Any] = {
        "schema_version": JURISDICTION_PROFILE_SCHEMA_VERSION,
        "jurisdiction_id": jurisdiction_id,
        "name": name.strip(),
        "country": country,
        "state_or_region": state_or_region,
        "official_domains": [],
        "legislature_urls": [],
        "regulator_urls": [],
        "court_urls": [],
        "gazette_urls": [],
        "blocked_domains": [],
        "notes": None,
    }

    for field in JURISDICTION_DOMAIN_FIELDS:
        value = profile.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise _jurisdiction_invalid(f"{scope}: {field} must be a list of domain strings.")
        normalized[field] = normalize_domains(value)

    for field in JURISDICTION_URL_FIELDS:
        value = profile.get(field)
        if value is None:
            continue
        if not isinstance(value, list):
            raise _jurisdiction_invalid(f"{scope}: {field} must be a list of http(s) URL strings.")
        urls: list[str] = []
        for item in value:
            url = http_url(item)
            if url is None:
                raise _jurisdiction_invalid(f"{scope}: {field} entry {item!r} must be an http(s) URL.")
            urls.append(url)
        normalized[field] = urls

    notes = profile.get("notes")
    if notes is not None:
        if not isinstance(notes, str) or not notes.strip():
            raise _jurisdiction_invalid(f"{scope}: notes must be a non-empty string when present.")
        normalized["notes"] = notes.strip()

    if not any(normalized[field] for field in JURISDICTION_OFFICIAL_ROOT_FIELDS):
        raise _jurisdiction_invalid(
            f"{scope}: at least one official source root is required "
            "(official_domains, legislature_urls, regulator_urls, court_urls, or gazette_urls)."
        )
    return normalized


def validate_jurisdictions_document(document: Any, source_label: str) -> list[dict[str, Any]]:
    """Validate the parsed jurisdictions file. Returns a list of normalized
    profiles (empty when jurisdiction_profiles is absent)."""
    if not isinstance(document, dict):
        raise _jurisdiction_invalid(f"{source_label}: top level must be a mapping with a jurisdiction_profiles list.")
    profiles = document.get("jurisdiction_profiles")
    if profiles is None:
        return []
    if not isinstance(profiles, list):
        raise _jurisdiction_invalid(f"{source_label}: jurisdiction_profiles must be a list.")
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, profile in enumerate(profiles, start=1):
        try:
            validated.append(validate_jurisdiction_profile(profile, seen_ids))
        except DiscoverSourcesError as exc:
            raise DiscoverSourcesError(
                exc.error_code,
                f"{source_label}: profile #{index}: {exc.message}",
                remediation=exc.remediation,
                details=exc.details,
            ) from exc
    return validated


def load_jurisdiction_profiles(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Read and validate the workspace jurisdiction profile file. Returns an empty
    list when the file is absent (no profiles configured). Raises
    JURISDICTION_INVALID on malformed YAML or a schema violation."""
    path = jurisdictions_path(project_root, config)
    if not path.is_file():
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DiscoverSourcesError(
            "JURISDICTION_INVALID",
            f"{relative_label(project_root, path)}: invalid YAML: {exc}",
            remediation="Fix the YAML syntax and rerun jurisdictions validate.",
            details={"command": "jurisdictions", "network_io_executed": False},
        ) from exc
    if document is None:
        return []
    return validate_jurisdictions_document(document, relative_label(project_root, path))


def find_jurisdiction(profiles: list[dict[str, Any]], jurisdiction_id: str) -> dict[str, Any] | None:
    for profile in profiles:
        if profile["jurisdiction_id"] == jurisdiction_id:
            return profile
    return None


def require_jurisdiction(profiles: list[dict[str, Any]], jurisdiction_id: str) -> dict[str, Any]:
    profile = find_jurisdiction(profiles, jurisdiction_id)
    if profile is None:
        raise DiscoverSourcesError(
            "JURISDICTION_UNKNOWN",
            f"Unknown jurisdiction id: {jurisdiction_id!r} (no matching profile in sources/jurisdictions.yml).",
            remediation="Run discover_sources.py jurisdictions list to see configured profiles, or add the profile.",
            details={"jurisdiction_id": jurisdiction_id, "network_io_executed": False},
        )
    return profile


def profile_official_domains(profile: dict[str, Any]) -> list[str]:
    """Flatten a profile's official_domains list (the canonical input E34-T02/T03
    feed into official-source-first legal discovery and the search ranker)."""
    return list(profile.get("official_domains", []))


def profile_matches_host(profile: dict[str, Any], host: str) -> bool:
    return domain_matches(host, profile_official_domains(profile))


def run_jurisdictions_validate(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    path = jurisdictions_path(project_root, config)
    profiles = load_jurisdiction_profiles(project_root, config)
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "jurisdictions",
        "jurisdictions_command": "validate",
        "generated_at": timestamp_utc(),
        "jurisdictions_path": relative_label(project_root, path),
        "jurisdictions_path_exists": path.is_file(),
        "count": len(profiles),
        "jurisdiction_ids": [profile["jurisdiction_id"] for profile in profiles],
        "network_io_executed": False,
    }


def run_jurisdictions_list(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    profiles = load_jurisdiction_profiles(project_root, config)
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "jurisdictions",
        "jurisdictions_command": "list",
        "generated_at": timestamp_utc(),
        "count": len(profiles),
        "jurisdictions": [
            {
                "jurisdiction_id": profile["jurisdiction_id"],
                "name": profile["name"],
                "country": profile["country"],
                "state_or_region": profile["state_or_region"],
                "official_root_count": sum(1 for field in JURISDICTION_OFFICIAL_ROOT_FIELDS if profile[field]),
            }
            for profile in profiles
        ],
        "network_io_executed": False,
    }


def run_jurisdictions_show(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    jurisdiction_id = require_non_empty(args.jurisdiction, "--jurisdiction")
    profiles = load_jurisdiction_profiles(project_root, config)
    profile = require_jurisdiction(profiles, jurisdiction_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "jurisdictions",
        "jurisdictions_command": "show",
        "generated_at": timestamp_utc(),
        "jurisdiction": profile,
        "network_io_executed": False,
    }


# --- Legal discovery query planning (E34-T02) --------------------------------


def find_jurisdiction_by_id_or_name(
    profiles: list[dict[str, Any]], value: str
) -> dict[str, Any] | None:
    """Resolve a --jurisdiction value to a profile, or None. An exact
    jurisdiction_id wins; otherwise a case-insensitive display-name match. The
    `legal` command accepts either (for example us-federal or "United States
    (Federal)")."""
    normalized = value.strip().lower()
    for profile in profiles:
        if profile["jurisdiction_id"] == normalized:
            return profile
    for profile in profiles:
        if profile["name"].strip().lower() == normalized:
            return profile
    return None


def build_legal_query_plan(
    topic: str,
    *,
    profile: dict[str, Any] | None,
    jurisdiction_label: str,
    official_domains: list[str],
    blocked_domains: list[str],
) -> list[dict[str, Any]]:
    """Expand a legal topic into one official-source-first query per legal
    category. Each query carries the profile's official_domains allowlist, its
    blocked_domains, and the category's entry-point roots from the profile."""
    plan: list[dict[str, Any]] = []
    for category, term, root_field, rationale in LEGAL_QUERY_CATEGORIES:
        profile_roots = list(profile.get(root_field, [])) if profile else []
        plan.append(
            {
                "query": f"{topic} {term}",
                "legal_category": category,
                "expected_source_type": "official_legal",
                "domain_allowlist": list(official_domains),
                "domain_blocklist": list(blocked_domains),
                "profile_roots": profile_roots,
                "prefer_official": True,
                "jurisdiction": jurisdiction_label,
                "rationale": (
                    f"{rationale} Prefer official {jurisdiction_label} government, legislature, "
                    "regulator, court, or gazette sources over aggregators."
                ),
            }
        )
    return plan


def run_legal_discovery(
    project_root: Path, config: dict[str, Any], discovery: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    """Plan official-source-first legal/regulatory queries for a jurisdiction and
    topic, and with --execute run them through the search backend and rank the
    results by officialness (E34-T03).

    Planning loads the jurisdiction profile (E34-T01) and emits an explained plan
    without contacting a backend. A missing profile or a profile without official
    domains is a warning, not an error — the plan is still produced, just without
    official-domain prioritization. Execution threads the profile's official
    domains in as the major trust signal and applies the legal ranking rules
    (refine_legal_candidates): official sources outrank aggregators, recognized
    secondary legal databases are retained as supplemental, and mirrors/duplicates
    of an official source are rejected."""
    topic = require_non_empty(args.topic, "--topic")
    jurisdiction_input = require_non_empty(args.jurisdiction, "--jurisdiction")
    profiles = load_jurisdiction_profiles(project_root, config)
    profile = find_jurisdiction_by_id_or_name(profiles, jurisdiction_input)

    warnings: list[dict[str, str]] = []
    if profile is None:
        official_domains: list[str] = []
        blocked_domains: list[str] = []
        jurisdiction_label = jurisdiction_input
        warnings.append(
            {
                "code": "no_jurisdiction_profile",
                "message": (
                    f"No jurisdiction profile matches {jurisdiction_input!r}; planning generic "
                    "legal queries without official-domain prioritization. Add a profile to "
                    "sources/jurisdictions.yml (see discover_sources.py jurisdictions list)."
                ),
            }
        )
    else:
        official_domains = profile_official_domains(profile)
        blocked_domains = list(profile.get("blocked_domains", []))
        jurisdiction_label = profile["jurisdiction_id"]
        if not official_domains:
            warnings.append(
                {
                    "code": "no_official_domains",
                    "message": (
                        f"Jurisdiction {profile['jurisdiction_id']!r} declares no official_domains; "
                        "official-domain filtering will not apply. Add official_domains to the "
                        "profile for stronger official-source ranking."
                    ),
                }
            )

    planned_queries = build_legal_query_plan(
        topic,
        profile=profile,
        jurisdiction_label=jurisdiction_label,
        official_domains=official_domains,
        blocked_domains=blocked_domains,
    )
    execute = bool(getattr(args, "execute", False))
    report = {
        "schema_version": SCHEMA_VERSION,
        "provider": "legal",
        "command": "legal",
        "mode": "execute" if execute else "plan",
        "research_need": topic,
        "topic": topic,
        "jurisdiction_input": jurisdiction_input,
        "jurisdiction": jurisdiction_label,
        "jurisdiction_resolved": profile is not None,
        "jurisdiction_name": profile["name"] if profile else None,
        "country": profile["country"] if profile else None,
        "state_or_region": profile["state_or_region"] if profile else None,
        "official_domains": official_domains,
        "blocked_domains": blocked_domains,
        "max_results": args.max_results,
        "warnings": warnings,
        "planned_query_count": len(planned_queries),
        "planned_queries": planned_queries,
    }
    if not execute:
        # Planning is read-only and contacts no backend, so the official-source
        # plan is explainable before any provider is configured (E34-T02).
        report["network_io_executed"] = False
        return report

    discovery_id = discovery_run_id("legal", [topic, jurisdiction_label])
    base_request = {"jurisdiction": jurisdiction_label, "max_results": args.max_results}
    report.update(
        execute_query_plan(
            project_root,
            config,
            discovery,
            planned_queries,
            base_request=base_request,
            request_id=None,
            discovery_id=discovery_id,
            official_domains=official_domains,
            # Profile official domains are a major trust *signal*, not a hard
            # filter: secondary legal sources are retained for ranking/review.
            use_query_allowlist=False,
            candidate_provider="legal",
            discovered_by=LEGAL_DISCOVERED_BY,
            refine=lambda cands: refine_legal_candidates(
                cands, official_domains=official_domains, jurisdiction=jurisdiction_label
            ),
        )
    )
    report["discovery_run_id"] = discovery_id
    return report


# --- Author extraction (E35-T01) ---------------------------------------------


def clean_author_text(value: Any) -> str | None:
    """Whitespace-normalize a name or affiliation string. Returns None for
    non-strings or empties. Deliberately conservative: it never strips or infers
    content beyond collapsing whitespace and trimming surrounding punctuation."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip().strip(",;")
    return text or None


def normalize_orcid(value: Any) -> str | None:
    """Extract a canonical ORCID iD (0000-0000-0000-000X) from a bare id or an
    orcid.org URL. Returns None when no well-formed ORCID is present (never
    fabricated)."""
    if not isinstance(value, str) or not value.strip():
        return None
    match = ORCID_RE.search(value.strip().upper())
    return match.group(1) if match else None


def author_seed(
    name: Any,
    *,
    source: str,
    confidence: str,
    orcid: Any = None,
    affiliation: Any = None,
) -> dict[str, Any] | None:
    """Build one author seed record, or None when there is no usable name. Only
    metadata actually supplied is recorded; missing fields stay null."""
    clean_name = clean_author_text(name)
    if clean_name is None:
        return None
    tier = confidence if confidence in AUTHOR_CONFIDENCE_TIERS else "medium"
    return {
        "name": clean_name,
        "orcid": normalize_orcid(orcid),
        "affiliation": clean_author_text(affiliation),
        "source": source,
        "confidence": tier,
    }


def extract_authors_from_frontmatter(frontmatter: dict[str, Any]) -> list[dict[str, Any]]:
    """Author names from a normalized source's `authors` frontmatter list. The
    record's overall `confidence` (extraction confidence) is carried per author."""
    authors = frontmatter.get("authors")
    if not isinstance(authors, list):
        return []
    confidence = frontmatter.get("confidence")
    confidence = confidence if confidence in AUTHOR_CONFIDENCE_TIERS else "medium"
    seeds: list[dict[str, Any]] = []
    for entry in authors:
        if isinstance(entry, str):
            seed = author_seed(entry, source="normalized_frontmatter", confidence=confidence)
        elif isinstance(entry, dict):
            seed = author_seed(
                entry.get("name"),
                source="normalized_frontmatter",
                confidence=confidence,
                orcid=entry.get("orcid"),
                affiliation=entry.get("affiliation"),
            )
        else:
            seed = None
        if seed is not None:
            seeds.append(seed)
    return seeds


def extract_authors_from_arxiv(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Authors from an arXiv-style metadata record (an `authors` list of names or
    {name, affiliation} objects). Provider-supplied, so confidence is high."""
    authors = metadata.get("authors")
    if not isinstance(authors, list):
        return []
    seeds: list[dict[str, Any]] = []
    for entry in authors:
        if isinstance(entry, str):
            seed = author_seed(entry, source="arxiv", confidence="high")
        elif isinstance(entry, dict):
            seed = author_seed(
                entry.get("name"),
                source="arxiv",
                confidence="high",
                orcid=entry.get("orcid"),
                affiliation=entry.get("affiliation"),
            )
        else:
            seed = None
        if seed is not None:
            seeds.append(seed)
    return seeds


def extract_authors_from_openalex(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Authors from an OpenAlex-style `authorships` list: author.display_name and
    author.orcid, with affiliation from the first institution or raw string."""
    authorships = metadata.get("authorships")
    if not isinstance(authorships, list):
        return []
    seeds: list[dict[str, Any]] = []
    for entry in authorships:
        if not isinstance(entry, dict):
            continue
        author = entry.get("author") if isinstance(entry.get("author"), dict) else {}
        affiliation: Any = None
        institutions = entry.get("institutions")
        if isinstance(institutions, list):
            affiliation = next(
                (inst.get("display_name") for inst in institutions
                 if isinstance(inst, dict) and clean_author_text(inst.get("display_name"))),
                None,
            )
        if affiliation is None:
            raw = entry.get("raw_affiliation_strings")
            if isinstance(raw, list) and raw:
                affiliation = raw[0]
        seed = author_seed(
            author.get("display_name"),
            source="openalex",
            confidence="high",
            orcid=author.get("orcid"),
            affiliation=affiliation,
        )
        if seed is not None:
            seeds.append(seed)
    return seeds


def extract_authors_from_provider_metadata(metadata: Any) -> list[dict[str, Any]]:
    """Detect a provider metadata shape (OpenAlex authorships or arXiv authors)
    and extract authors. Returns [] for shapes that carry no author data."""
    if not isinstance(metadata, dict):
        return []
    if isinstance(metadata.get("authorships"), list):
        return extract_authors_from_openalex(metadata)
    if isinstance(metadata.get("authors"), list):
        return extract_authors_from_arxiv(metadata)
    return []


def merge_author_seeds(*seed_lists: list[dict[str, Any]], max_results: int) -> list[dict[str, Any]]:
    """Combine author seeds, de-duplicating by case-folded name. The first
    occurrence wins (so richer provider seeds should be passed before frontmatter
    seeds); later occurrences only fill missing orcid/affiliation and improve
    confidence. The result is capped to a bounded length."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for seeds in seed_lists:
        for seed in seeds:
            key = seed["name"].casefold()
            if key not in merged:
                merged[key] = dict(seed)
                order.append(key)
                continue
            existing = merged[key]
            for field in ("orcid", "affiliation"):
                if not existing.get(field) and seed.get(field):
                    existing[field] = seed[field]
            if AUTHOR_CONFIDENCE_RANK.get(seed["confidence"], 1) < AUTHOR_CONFIDENCE_RANK.get(existing["confidence"], 1):
                existing["confidence"] = seed["confidence"]
    return [merged[key] for key in order][:max_results]


def extract_author_seeds(
    *,
    frontmatter: dict[str, Any] | None = None,
    provider_metadata: Any = None,
    max_results: int,
) -> list[dict[str, Any]]:
    """Emit a bounded author seed list from a normalized record's frontmatter and
    any provider author metadata. Provider seeds (richer: orcid/affiliation) take
    precedence over frontmatter name-only seeds when both name the same author."""
    provider_seeds = extract_authors_from_provider_metadata(provider_metadata)
    frontmatter_seeds = extract_authors_from_frontmatter(frontmatter) if isinstance(frontmatter, dict) else []
    return merge_author_seeds(provider_seeds, frontmatter_seeds, max_results=max_results)


def load_manifest_records(path: Path) -> list[dict[str, Any]]:
    """Read manifest records leniently (skip blank/malformed lines). Read-only;
    author extraction never rewrites the manifest."""
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


# --- Author publication discovery (E35-T02) ----------------------------------


def openalex_api_key() -> str | None:
    """Read OPENALEX_API_KEY from the environment only. Never persisted or emitted."""
    value = os.environ.get("OPENALEX_API_KEY")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def openalex_headers() -> dict[str, str]:
    return {"Accept": "application/json", "User-Agent": OPENALEX_USER_AGENT}


def redact_openalex_diagnostic(value: Any) -> str:
    """Remove OpenAlex credentials from URLs and exception diagnostics.

    OpenAlex accepts its API key as a query parameter, so HTTPError URLs and
    transport exception strings can otherwise echo the operator credential.
    Redact both the configured key (raw or URL encoded) and any ``api_key``
    query value, including diagnostics produced by custom transports.
    """
    text = str(value)
    api_key = openalex_api_key()
    if api_key:
        encoded_key = urlencode({"api_key": api_key}).partition("=")[2]
        for secret in sorted({api_key, encoded_key}, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)(\bapi_key=)[^&#\s]+",
        r"\1[REDACTED]",
        text,
    )


def urllib_openalex_transport(url: str, timeout: float, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)  # noqa: S310 - URL is built by OpenAlex provider helpers
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - provider transport is HTTPS-only upstream
        return response.read(OPENALEX_MAX_RESPONSE_BYTES + 1)


def active_openalex_transport():
    return OPENALEX_TRANSPORT or urllib_openalex_transport


def openalex_wait_for_rate_limit() -> None:
    global OPENALEX_LAST_REQUEST_AT
    now = float(OPENALEX_CLOCK())
    if OPENALEX_LAST_REQUEST_AT is not None:
        elapsed = now - OPENALEX_LAST_REQUEST_AT
        if elapsed < OPENALEX_REQUEST_INTERVAL_SECONDS:
            OPENALEX_SLEEP(OPENALEX_REQUEST_INTERVAL_SECONDS - elapsed)
            now = float(OPENALEX_CLOCK())
    OPENALEX_LAST_REQUEST_AT = now


def openalex_error_details(command: str = "authors") -> dict[str, Any]:
    # A network attempt was made before this error surfaced, so record it.
    return {"command": command, "provider": "openalex", "network_io_executed": True}


def openalex_build_url(path: str, params: dict[str, Any] | None = None) -> str:
    query: dict[str, str] = {
        key: str(value) for key, value in (params or {}).items() if value is not None
    }
    api_key = openalex_api_key()
    if api_key:
        query["api_key"] = api_key
    encoded = urlencode(query)
    return f"{OPENALEX_API_URL}{path}" + (f"?{encoded}" if encoded else "")


def openalex_fetch_url(
    url: str,
    *,
    command: str = "authors",
    budget_context: dict[str, Any] | None = None,
) -> bytes:
    transport = active_openalex_transport()
    last_error: BaseException | None = None
    safe_url = redact_openalex_diagnostic(url)
    for attempt in range(1, OPENALEX_MAX_ATTEMPTS + 1):
        openalex_wait_for_rate_limit()
        reserve_academic_provider_request(budget_context, provider="openalex", attempt=attempt)
        if budget_context is not None:
            budget_context["network_io_executed"] = True
        try:
            payload = transport(url, OPENALEX_TIMEOUT_SECONDS, openalex_headers())
            if not isinstance(payload, bytes):
                raise DiscoverSourcesError(
                    "DISCOVERY_RESPONSE_INVALID",
                    "OpenAlex transport returned a non-byte response.",
                    remediation="Fix the discovery transport adapter and retry.",
                    details=openalex_error_details(command),
                )
            if not payload:
                raise DiscoverSourcesError(
                    "DISCOVERY_RESPONSE_INVALID",
                    f"OpenAlex returned an empty response for {safe_url}.",
                    remediation="Retry later or inspect the provider response outside the workspace.",
                    details=openalex_error_details(command),
                )
            if len(payload) > OPENALEX_MAX_RESPONSE_BYTES:
                raise DiscoverSourcesError(
                    "DISCOVERY_RESPONSE_INVALID",
                    (
                        "OpenAlex response exceeded the fixed "
                        f"{OPENALEX_MAX_RESPONSE_BYTES}-byte limit."
                    ),
                    remediation="Narrow the query or retry after the provider response is within the limit.",
                    details={
                        **openalex_error_details(command),
                        "response_limit_bytes": OPENALEX_MAX_RESPONSE_BYTES,
                    },
                )
            return payload
        except DiscoverSourcesError:
            raise
        except HTTPError as exc:
            last_error = exc
            status = exc.code
            close_http_error(exc)
            if status in {401, 403}:
                raise DiscoverSourcesError(
                    "OPENALEX_AUTH_REQUIRED",
                    f"OpenAlex request failed with HTTP {status}: {safe_url}",
                    remediation=(
                        "Set OPENALEX_API_KEY in the process environment, verify the key, "
                        "and rerun the discovery command."
                    ),
                    details=openalex_error_details(command),
                ) from exc
            if status == 429:
                raise DiscoverSourcesError(
                    "OPENALEX_RATE_LIMITED",
                    f"OpenAlex request was rate limited with HTTP 429: {safe_url}",
                    remediation="Retry later, reduce request volume, or set OPENALEX_API_KEY for a larger usage budget.",
                    details=openalex_error_details(command),
                ) from exc
            if attempt < OPENALEX_MAX_ATTEMPTS and is_retryable_http_status(status):
                continue
            raise DiscoverSourcesError(
                "DISCOVERY_NETWORK_ERROR",
                f"OpenAlex request failed with HTTP {status}: {safe_url}",
                remediation="Retry later, check network access, or lower request volume.",
                details=openalex_error_details(command),
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < OPENALEX_MAX_ATTEMPTS:
                continue
    raise DiscoverSourcesError(
        "DISCOVERY_NETWORK_ERROR",
        (
            f"OpenAlex request failed after {OPENALEX_MAX_ATTEMPTS} attempt(s): "
            f"{redact_openalex_diagnostic(last_error)}"
        ),
        remediation="Retry later, check network access, or lower request volume.",
        details=openalex_error_details(command),
    )


def openalex_json_response(payload: bytes, *, command: str = "authors") -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"OpenAlex returned invalid JSON: {exc}",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details=openalex_error_details(command),
        ) from exc
    if not isinstance(document, dict):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            "OpenAlex returned JSON that was not an object.",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details=openalex_error_details(command),
        )
    return document


def openalex_results_list(
    document: dict[str, Any],
    *,
    endpoint: str,
    command: str = "authors",
) -> list[dict[str, Any]]:
    results = document.get("results")
    if not isinstance(results, list):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"OpenAlex {endpoint} response did not contain a results list.",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details=openalex_error_details(command),
        )
    return [item for item in results if isinstance(item, dict)]


def openalex_works_by_filter(
    filter_key: str,
    filter_value: str,
    per_page: int,
    *,
    budget_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch an author's works via an OpenAlex works filter (author.orcid or
    author.id), most recent first. Bounded by per_page; never paginated."""
    params = {
        "filter": f"{filter_key}:{filter_value}",
        "per_page": min(per_page, OPENALEX_DISCOVERY_PER_AUTHOR),
        "sort": "publication_year:desc",
    }
    url = openalex_build_url(OPENALEX_WORKS_PATH, params)
    document = openalex_json_response(openalex_fetch_url(url, budget_context=budget_context))
    return openalex_results_list(document, endpoint="works")


def openalex_author_search(
    name: str,
    *,
    budget_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    url = openalex_build_url(
        OPENALEX_AUTHORS_PATH,
        {"search": name, "per_page": OPENALEX_AUTHOR_SEARCH_PER_PAGE},
    )
    document = openalex_json_response(openalex_fetch_url(url, budget_context=budget_context))
    return openalex_results_list(document, endpoint="authors")


def orcid_url(orcid: str) -> str:
    return f"https://orcid.org/{orcid}"


def _openalex_work_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = OPENALEX_WORK_ID_RE.search(value.strip())
    return match.group(1).upper() if match else None


def _openalex_author_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = OPENALEX_AUTHOR_ID_RE.search(value.strip())
    return match.group(1).upper() if match else None


def _clean_doi(value: Any) -> str | None:
    """Canonical lowercase DOI (10.<prefix>/<suffix>) from a bare DOI or URL."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().lower()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text)
    text = text.removeprefix("doi:")
    return text if DOI_RE.match(text) else None


def _frontmatter_year(frontmatter: dict[str, Any]) -> int | None:
    year = frontmatter.get("year")
    if isinstance(year, int):
        return year
    return _published_year(frontmatter.get("date") if isinstance(frontmatter.get("date"), str) else None)


def _year_freshness(year: int, seed_year: int | None) -> float:
    """0..1 freshness signal: 1.0 at the reference year, decaying over ~15 years."""
    reference = seed_year if seed_year is not None else datetime.now(timezone.utc).year
    delta = abs(reference - year)
    return clamp_unit(1.0 - delta / 15.0)


def author_context_signals(seed: dict[str, Any], seed_context: dict[str, Any]) -> list[str]:
    """Context signals that make a name-only author expansion auditable.

    ORCID is handled separately as an exact identity. For name-only seeds, require
    at least one seed-paper context signal before listing works so a same-name
    author is never silently promoted without paper or affiliation context.
    """
    signals: list[str] = []
    if seed_context.get("title_tokens"):
        signals.append("seed_title_context")
    if seed.get("affiliation"):
        signals.append("affiliation_context")
    return signals


def author_identity_quality_gate(
    identity: str,
    *,
    seed: dict[str, Any],
    seed_context: dict[str, Any],
) -> dict[str, Any]:
    if identity == AUTHOR_IDENTITY_ORCID_EXACT:
        return {
            "status": "passed",
            "identity_match": identity,
            "signals": ["orcid"],
            "review_required": False,
            "reason": "Seed author carries an exact ORCID iD; works are filtered by author.orcid.",
        }

    signals = author_context_signals(seed, seed_context)
    if identity == AUTHOR_IDENTITY_NAME_RESOLVED:
        return {
            "status": "review_required",
            "identity_match": identity,
            "signals": signals,
            "review_required": True,
            "reason": (
                "Author identity was inferred from a single OpenAlex name match plus seed-paper "
                "context; review is required before treating related works as primary evidence."
            ),
        }

    reason = {
        AUTHOR_IDENTITY_CONTEXT_MISSING: (
            "Name-only author seed lacks seed-title or affiliation context; no publication "
            "candidates are proposed."
        ),
        AUTHOR_IDENTITY_AMBIGUOUS: (
            "OpenAlex returned multiple plausible same-name authors; no publication candidates "
            "are proposed."
        ),
        AUTHOR_IDENTITY_NO_MATCH: "OpenAlex returned no author match; no publication candidates are proposed.",
    }.get(identity, "Author identity did not pass expansion quality gates.")
    return {
        "status": "blocked",
        "identity_match": identity,
        "signals": signals,
        "review_required": True,
        "reason": reason,
    }


def _work_location_urls(work: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Best-effort (landing_page_url, pdf_url, license) from the work's OA/primary
    locations. Read-only normalization of provider metadata already present."""
    landing: str | None = None
    pdf: str | None = None
    license_key: str | None = None
    for loc_key in ("best_oa_location", "primary_location"):
        location = work.get(loc_key)
        if not isinstance(location, dict):
            continue
        if landing is None:
            landing = http_url(location.get("landing_page_url"))
        if pdf is None:
            pdf = http_url(location.get("pdf_url"))
        if license_key is None:
            for license_field in ("license", "license_id"):
                value = location.get(license_field)
                if isinstance(value, str) and value.strip():
                    license_key = value.strip()
                    break
        if landing and pdf and license_key:
            break
    return landing, pdf, license_key


def _work_author_names(work: dict[str, Any]) -> list[str]:
    authorships = work.get("authorships")
    if not isinstance(authorships, list):
        return []
    names: list[str] = []
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        if not isinstance(author, dict):
            continue
        name = clean_author_text(author.get("display_name"))
        if name and name not in names:
            names.append(name)
    return names


def openalex_paper_metadata(work: dict[str, Any], title: str) -> dict[str, Any]:
    work_id = _openalex_work_id(work.get("id"))
    doi = _clean_doi(work.get("doi"))
    year = work.get("publication_year") if isinstance(work.get("publication_year"), int) else None
    open_access = work.get("open_access") if isinstance(work.get("open_access"), dict) else {}
    is_oa = open_access.get("is_oa") if isinstance(open_access.get("is_oa"), bool) else None
    oa_status = open_access.get("oa_status") if isinstance(open_access.get("oa_status"), str) else None
    landing_url, pdf_url, license_key = _work_location_urls(work)
    if work_id is None and doi is None:
        resolution_status = "uncertain"
    elif is_oa is False:
        resolution_status = "metadata_only"
    else:
        resolution_status = "resolved"
    return {
        "provider_ids": {"arxiv": None, "openalex": work_id, "doi": doi},
        "title": title,
        "authors": _work_author_names(work),
        "publication_year": year,
        "doi": doi,
        "arxiv_id": None,
        "open_access": is_oa,
        "oa_status": oa_status,
        "license": license_key,
        "landing_page_url": landing_url,
        "pdf_url": pdf_url,
        "resolution_status": resolution_status,
    }


# --- Request-backed academic discovery --------------------------------------


def _academic_request(project_root: Path, config: dict[str, Any], request_id: str) -> dict[str, Any]:
    import source_requests

    path = source_requests.requests_path(project_root, config)
    records = source_requests.load_requests(path)
    request_record = next((record for record in records if record.get("request_id") == request_id), None)
    if request_record is None:
        raise DiscoverSourcesError(
            "REQUEST_UNKNOWN",
            f"Unknown request id: {request_id} (no record in {relative_label(project_root, path)}).",
            remediation="List source requests and pass an existing request id.",
            details={"command": "academic", "request_id": request_id, "network_io_executed": False},
        )
    status = request_record.get("status")
    if status != "open":
        raise DiscoverSourcesError(
            "REQUEST_NOT_OPEN",
            f"Source request {request_id} is not open (status: {status!r}).",
            remediation="Pass an open source request, or create a new request for the remaining evidence gap.",
            details={
                "command": "academic",
                "request_id": request_id,
                "request_status": status,
                "network_io_executed": False,
            },
        )
    return request_record


def _arxiv_headers() -> dict[str, str]:
    return {"Accept": "application/atom+xml", "User-Agent": ARXIV_USER_AGENT}


def _urllib_arxiv_transport(url: str, timeout: float, headers: dict[str, str]) -> bytes:
    request = Request(url, headers=headers)  # noqa: S310 - fixed HTTPS arXiv endpoint
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed provider endpoint
        return response.read(ARXIV_MAX_RESPONSE_BYTES + 1)


def _arxiv_wait_for_rate_limit() -> None:
    global ARXIV_LAST_REQUEST_AT
    now = float(ARXIV_CLOCK())
    if ARXIV_LAST_REQUEST_AT is not None:
        elapsed = now - ARXIV_LAST_REQUEST_AT
        if elapsed < ARXIV_REQUEST_INTERVAL_SECONDS:
            ARXIV_SLEEP(ARXIV_REQUEST_INTERVAL_SECONDS - elapsed)
            now = float(ARXIV_CLOCK())
    ARXIV_LAST_REQUEST_AT = now


def _arxiv_fetch_url(
    url: str,
    *,
    budget_context: dict[str, Any] | None = None,
) -> bytes:
    transport = ARXIV_TRANSPORT or _urllib_arxiv_transport
    last_error: BaseException | None = None
    details = {"command": "academic", "provider": "arxiv", "network_io_executed": True}
    for attempt in range(1, ARXIV_MAX_ATTEMPTS + 1):
        _arxiv_wait_for_rate_limit()
        reserve_academic_provider_request(budget_context, provider="arxiv", attempt=attempt)
        if budget_context is not None:
            budget_context["network_io_executed"] = True
        try:
            payload = transport(url, ARXIV_TIMEOUT_SECONDS, _arxiv_headers())
            if not isinstance(payload, bytes) or not payload:
                raise DiscoverSourcesError(
                    "DISCOVERY_RESPONSE_INVALID",
                    "arXiv returned an empty or non-byte response.",
                    remediation="Retry later or fix the injected provider transport.",
                    details=details,
                )
            if len(payload) > ARXIV_MAX_RESPONSE_BYTES:
                raise DiscoverSourcesError(
                    "DISCOVERY_RESPONSE_INVALID",
                    f"arXiv response exceeded the fixed {ARXIV_MAX_RESPONSE_BYTES}-byte limit.",
                    remediation="Narrow the query or retry after the provider response is within the limit.",
                    details={
                        **details,
                        "response_limit_bytes": ARXIV_MAX_RESPONSE_BYTES,
                    },
                )
            return payload
        except DiscoverSourcesError:
            raise
        except HTTPError as exc:
            last_error = exc
            status = exc.code
            close_http_error(exc)
            if status == 429:
                raise DiscoverSourcesError(
                    "ARXIV_RATE_LIMITED",
                    "arXiv academic discovery was rate limited with HTTP 429.",
                    remediation="Retry later or lower --max-results.",
                    details=details,
                ) from exc
            if attempt < ARXIV_MAX_ATTEMPTS and is_retryable_http_status(status):
                continue
            raise DiscoverSourcesError(
                "DISCOVERY_NETWORK_ERROR",
                f"arXiv academic discovery failed with HTTP {status}.",
                remediation="Retry later or check network access.",
                details=details,
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < ARXIV_MAX_ATTEMPTS:
                continue
    raise DiscoverSourcesError(
        "DISCOVERY_NETWORK_ERROR",
        f"arXiv academic discovery failed after {ARXIV_MAX_ATTEMPTS} attempt(s): {last_error}",
        remediation="Retry later or check network access.",
        details=details,
    )


def _arxiv_text(element: ET.Element, path: str) -> str | None:
    found = element.find(path, ARXIV_ATOM_NS)
    if found is None or found.text is None:
        return None
    text = " ".join(found.text.split())
    return text or None


def _arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    path = urlparse(value).path
    candidate = (path.rsplit("/", 1)[-1] if path else value.rsplit("/", 1)[-1]).removesuffix(".pdf")
    candidate = candidate.strip()
    return candidate or None


def _parse_arxiv_results(payload: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)  # noqa: S314 - stdlib Atom parser; no external entities
    except ET.ParseError as exc:
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            f"arXiv returned invalid Atom XML: {exc}",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details={"command": "academic", "provider": "arxiv", "network_io_executed": True},
        ) from exc
    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ARXIV_ATOM_NS):
        identifier = _arxiv_id(_arxiv_text(entry, "atom:id"))
        if identifier is None:
            continue
        links = entry.findall("atom:link", ARXIV_ATOM_NS)
        abs_url = next(
            (
                link.get("href")
                for link in links
                if link.get("rel") == "alternate" and isinstance(link.get("href"), str)
            ),
            ARXIV_ABS_URL.format(id=identifier),
        )
        pdf_url = next(
            (
                link.get("href")
                for link in links
                if link.get("title") == "pdf" and isinstance(link.get("href"), str)
            ),
            ARXIV_PDF_URL.format(id=identifier),
        )
        results.append(
            {
                "id": identifier,
                "title": _arxiv_text(entry, "atom:title") or "(untitled arXiv paper)",
                "summary": _arxiv_text(entry, "atom:summary"),
                "authors": [
                    name
                    for author in entry.findall("atom:author", ARXIV_ATOM_NS)
                    for name in [_arxiv_text(author, "atom:name")]
                    if name
                ],
                "published": _arxiv_text(entry, "atom:published"),
                "updated": _arxiv_text(entry, "atom:updated"),
                "doi": _clean_doi(_arxiv_text(entry, "arxiv:doi")),
                "abs_url": abs_url,
                "pdf_url": pdf_url,
            }
        )
    return results


def _academic_candidate_id(paper: dict[str, Any]) -> str:
    provider_ids = paper.get("provider_ids") if isinstance(paper.get("provider_ids"), dict) else {}
    doi = _clean_doi(provider_ids.get("doi") or paper.get("doi"))
    arxiv_id = provider_ids.get("arxiv") or paper.get("arxiv_id")
    openalex_id = provider_ids.get("openalex")
    title = clean_author_text(paper.get("title")) or "untitled"
    year = paper.get("publication_year")
    identity = doi or arxiv_id or openalex_id or f"{title.casefold()}:{year or ''}"
    digest = hashlib.sha1(f"academic:{identity}".encode(), usedforsecurity=False).hexdigest()[:10]
    return f"cand-{digest}"


def _academic_reasoning(query: str, title: str, *, authority: str) -> dict[str, Any]:
    matched = sorted(set(query_tokens(query)) & set(query_tokens(title)))
    if not matched:
        matched = query_tokens(query)[:1] or ["academic"]
    return {
        "matched_query_terms": matched,
        "authority_reason": authority,
        "freshness_reason": "Publication date is provider metadata and must be reviewed for the research question.",
        "scope_reason": "Returned by a bounded scholarly-provider search for the linked source request.",
        "risk_flags": ["license_uncertain"],
    }


def _arxiv_academic_candidate(
    record: dict[str, Any],
    *,
    request_id: str,
    query: str,
    discovered_at: str,
    max_results: int,
) -> dict[str, Any]:
    identifier = record["id"]
    published = record.get("published")
    year = _published_year(published if isinstance(published, str) else None)
    paper = {
        "provider_ids": {"arxiv": identifier, "openalex": None, "doi": record.get("doi")},
        "title": record["title"],
        "authors": record.get("authors", []),
        "publication_year": year,
        "doi": record.get("doi"),
        "arxiv_id": identifier,
        "open_access": True,
        "oa_status": "preprint",
        "license": None,
        "landing_page_url": record["abs_url"],
        "pdf_url": record["pdf_url"],
        "resolution_status": "resolved",
    }
    overlap = len(set(query_tokens(query)) & set(query_tokens(record["title"])))
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": _academic_candidate_id(paper),
        "request_id": request_id,
        "seed_source_id": None,
        "discovery_run_id": None,
        "discovered_at": discovered_at,
        "discovered_by": "discover_sources.py/academic",
        "provider": "arxiv",
        "discovery_providers": ["arxiv"],
        "url": record["abs_url"],
        "title": record["title"],
        "source_type": "paper",
        "trust_tier": "academic_primary",
        "relevance_score": clamp_unit(0.55 + min(0.35, overlap * 0.08)),
        "trust_score": 0.8,
        "official_source": None,
        "jurisdiction": None,
        "license": None,
        "terms_url": ARXIV_TERMS_URL,
        "rationale": "arXiv preprint candidate linked to the source request; review scope and publication status before selection.",
        "recommended_action": "review",
        "network_io_executed": True,
        "paper": paper,
        "provider_budget": {
            "provider": "arxiv",
            "network_io_executed": True,
            "max_results": max_results,
            "max_results_cap": OPENALEX_DISCOVERY_MAX_RESULTS_CAP,
        },
        "reasoning": _academic_reasoning(
            query,
            record["title"],
            authority="arXiv hosts the identified preprint; peer review is not inferred.",
        ),
        "provider_records": {"arxiv": record},
    }
    return candidate


def _openalex_arxiv_id(work: dict[str, Any]) -> str | None:
    for key in ("best_oa_location", "primary_location"):
        location = work.get(key)
        if not isinstance(location, dict):
            continue
        for field in ("landing_page_url", "pdf_url"):
            value = location.get(field)
            if isinstance(value, str) and result_host(value) == "arxiv.org":
                return _arxiv_id(value)
    return None


def _openalex_academic_candidate(
    work: dict[str, Any],
    *,
    request_id: str,
    query: str,
    discovered_at: str,
    max_results: int,
) -> dict[str, Any]:
    title = clean_author_text(work.get("display_name")) or "(untitled OpenAlex work)"
    paper = openalex_paper_metadata(work, title)
    arxiv_id = _openalex_arxiv_id(work)
    paper["arxiv_id"] = arxiv_id
    paper["provider_ids"]["arxiv"] = arxiv_id
    url = paper.get("landing_page_url") or (
        f"https://doi.org/{paper['doi']}" if paper.get("doi") else work.get("id")
    )
    cited_by = work.get("cited_by_count") if isinstance(work.get("cited_by_count"), int) else 0
    overlap = len(set(query_tokens(query)) & set(query_tokens(title)))
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": _academic_candidate_id(paper),
        "request_id": request_id,
        "seed_source_id": None,
        "discovery_run_id": None,
        "discovered_at": discovered_at,
        "discovered_by": "discover_sources.py/academic",
        "provider": "openalex",
        "discovery_providers": ["openalex"],
        "url": url,
        "title": title,
        "source_type": "paper",
        "trust_tier": "academic_primary",
        "relevance_score": clamp_unit(0.55 + min(0.3, overlap * 0.08) + min(0.1, cited_by / 1000)),
        "trust_score": 0.75,
        "official_source": None,
        "jurisdiction": None,
        "license": paper.get("license"),
        "terms_url": OPENALEX_TERMS_URL,
        "rationale": "OpenAlex-indexed paper candidate linked to the source request; index inclusion does not prove peer review.",
        "recommended_action": "review",
        "network_io_executed": True,
        "paper": paper,
        "provider_budget": {
            "provider": "openalex",
            "network_io_executed": True,
            "token_used": openalex_api_key() is not None,
            "max_results": max_results,
            "max_results_cap": OPENALEX_DISCOVERY_MAX_RESULTS_CAP,
        },
        "reasoning": _academic_reasoning(
            query,
            title,
            authority="OpenAlex supplies scholarly index metadata; publisher authority and peer review are not inferred.",
        ),
        "provider_records": {"openalex": work},
    }
    if paper.get("license"):
        candidate["reasoning"]["risk_flags"] = []
    return candidate


def _academic_identity_keys(candidate: dict[str, Any]) -> list[str]:
    paper = candidate.get("paper") if isinstance(candidate.get("paper"), dict) else {}
    provider_ids = paper.get("provider_ids") if isinstance(paper.get("provider_ids"), dict) else {}
    keys: list[str] = []
    doi = _clean_doi(provider_ids.get("doi") or paper.get("doi"))
    if doi:
        keys.append(f"doi:{doi}")
    arxiv_id = provider_ids.get("arxiv") or paper.get("arxiv_id")
    if isinstance(arxiv_id, str) and arxiv_id.strip():
        normalized_arxiv_id = re.sub(r"v\d+$", "", arxiv_id.strip(), flags=re.IGNORECASE).lower()
        keys.append(f"arxiv:{normalized_arxiv_id}")
    openalex_id = provider_ids.get("openalex")
    if isinstance(openalex_id, str) and openalex_id.strip():
        keys.append(f"openalex:{openalex_id.strip().upper()}")
    title = clean_author_text(paper.get("title") or candidate.get("title"))
    if title:
        keys.append(f"title:{title.casefold()}:{paper.get('publication_year') or ''}")
    return keys


def _merge_academic_candidate(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    # Prefer arXiv as the acquisition-facing record when available, while
    # retaining OpenAlex identity and open-access metadata in the paper object.
    if incoming.get("provider") == "arxiv" and existing.get("provider") != "arxiv":
        primary, secondary = incoming, existing
    else:
        primary, secondary = existing, incoming
    merged = dict(primary)
    primary_paper = dict(primary.get("paper") or {})
    secondary_paper = secondary.get("paper") if isinstance(secondary.get("paper"), dict) else {}
    primary_ids = dict(primary_paper.get("provider_ids") or {})
    secondary_ids = secondary_paper.get("provider_ids") if isinstance(secondary_paper.get("provider_ids"), dict) else {}
    for key in ("arxiv", "openalex", "doi"):
        if not primary_ids.get(key) and secondary_ids.get(key):
            primary_ids[key] = secondary_ids[key]
    primary_paper["provider_ids"] = primary_ids
    for key, value in secondary_paper.items():
        if key == "provider_ids":
            continue
        if primary_paper.get(key) is None or primary_paper.get(key) == []:
            primary_paper[key] = value
    merged["paper"] = primary_paper
    merged["candidate_id"] = _academic_candidate_id(primary_paper)
    merged["discovery_providers"] = list(
        dict.fromkeys([*(primary.get("discovery_providers") or []), *(secondary.get("discovery_providers") or [])])
    )
    merged["provider_records"] = {
        **(secondary.get("provider_records") or {}),
        **(primary.get("provider_records") or {}),
    }
    merged["relevance_score"] = max(float(primary.get("relevance_score", 0)), float(secondary.get("relevance_score", 0)))
    merged["trust_score"] = max(float(primary.get("trust_score", 0)), float(secondary.get("trust_score", 0)))
    return merged


def _dedupe_academic_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    key_to_index: dict[str, int] = {}
    for candidate in candidates:
        keys = _academic_identity_keys(candidate)
        matched = next((key_to_index[key] for key in keys if key in key_to_index), None)
        if matched is None:
            matched = len(deduped)
            deduped.append(candidate)
        else:
            deduped[matched] = _merge_academic_candidate(deduped[matched], candidate)
        for key in _academic_identity_keys(deduped[matched]):
            key_to_index[key] = matched
    deduped.sort(
        key=lambda item: (
            -float(item.get("relevance_score", 0)),
            -float(item.get("trust_score", 0)),
            str(item.get("title", "")).casefold(),
        )
    )
    return deduped


def run_academic_discovery(
    project_root: Path,
    config: dict[str, Any],
    discovery: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    del discovery  # authorization is enforced before this function is called
    request_id = require_non_empty(args.request_id, "--request-id")
    request_record = _academic_request(project_root, config, request_id)
    query_value = args.query if isinstance(args.query, str) and args.query.strip() else request_record.get("query_or_identifier")
    query = require_non_empty(query_value, "source request query_or_identifier")
    budget_context = academic_provider_budget_context(
        project_root,
        config,
        args.run_id,
        command="academic",
        scope_id=request_id,
    )
    cap = min(args.max_results, OPENALEX_DISCOVERY_MAX_RESULTS_CAP)
    discovered_at = timestamp_utc()
    candidates: list[dict[str, Any]] = []
    provider_counts: dict[str, int] = {}

    for provider in args.provider:
        if provider == "arxiv":
            url = f"{ARXIV_API_URL}?{urlencode({'search_query': f'all:{query}', 'start': 0, 'max_results': cap, 'sortBy': 'relevance', 'sortOrder': 'descending'})}"
            records = _parse_arxiv_results(
                _arxiv_fetch_url(url, budget_context=budget_context)
            )
            provider_candidates = [
                _arxiv_academic_candidate(
                    record,
                    request_id=request_id,
                    query=query,
                    discovered_at=discovered_at,
                    max_results=cap,
                )
                for record in records[:cap]
            ]
        else:
            url = openalex_build_url(OPENALEX_WORKS_PATH, {"search": query, "per_page": cap})
            document = openalex_json_response(
                openalex_fetch_url(
                    url,
                    command="academic",
                    budget_context=budget_context,
                ),
                command="academic",
            )
            works = openalex_results_list(document, endpoint="works", command="academic")
            provider_candidates = [
                _openalex_academic_candidate(
                    work,
                    request_id=request_id,
                    query=query,
                    discovered_at=discovered_at,
                    max_results=cap,
                )
                for work in works[:cap]
            ]
        provider_counts[provider] = len(provider_candidates)
        candidates.extend(provider_candidates)

    deduped = _dedupe_academic_candidates(candidates)[:cap]
    store_path = candidate_store_path(project_root, config)
    written = append_candidates(store_path, deduped)
    warnings = []
    for provider, count in provider_counts.items():
        if count == 0:
            warnings.append(
                {
                    "code": "no_provider_results",
                    "provider": provider,
                    "message": f"{provider} returned no candidates for this bounded query.",
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "academic",
        "command": "academic",
        "generated_at": discovered_at,
        "request_id": request_id,
        "request_kind": request_record.get("kind"),
        "query": query,
        "query_source": "argument" if args.query else "source_request",
        "providers": list(args.provider),
        "provider_counts": provider_counts,
        "max_results": cap,
        "count": len(deduped),
        "candidates": deduped,
        "candidates_path": relative_label(project_root, store_path),
        "written": len(written),
        "warnings": warnings,
        "token_used": openalex_api_key() is not None if "openalex" in args.provider else False,
        "network_io_executed": True,
    }


def openalex_provider_budget(max_results: int) -> dict[str, Any]:
    return {
        "provider": "openalex",
        "network_io_executed": True,
        "token_used": openalex_api_key() is not None,
        "max_results": max_results,
        "per_provider_limit": OPENALEX_DISCOVERY_PER_AUTHOR,
        "max_authors": OPENALEX_DISCOVERY_MAX_AUTHORS,
        "max_results_cap": OPENALEX_DISCOVERY_MAX_RESULTS_CAP,
    }


def build_seed_paper_context(record: dict[str, Any], frontmatter: dict[str, Any] | None) -> dict[str, Any]:
    """Topical context from the analyzed paper used to score publication relevance
    and to skip the seed paper itself. Only metadata already present is used."""
    title: str | None = None
    year: int | None = None
    if isinstance(frontmatter, dict):
        title = clean_author_text(frontmatter.get("title"))
        year = _frontmatter_year(frontmatter)
    doi: str | None = None
    openalex_id: str | None = None
    metadata = record.get("metadata") if isinstance(record, dict) else None
    if isinstance(metadata, dict):
        doi = _clean_doi(metadata.get("doi"))
        openalex_id = _openalex_work_id(metadata.get("id") or metadata.get("openalex_id"))
    if doi is None and isinstance(frontmatter, dict):
        doi = _clean_doi(frontmatter.get("doi"))
    return {
        "title": title,
        "title_tokens": query_tokens(title) if title else [],
        "year": year,
        "doi": doi,
        "openalex_id": openalex_id,
    }


def author_name_similarity(seed_name: str | None, candidate_name: str | None) -> float:
    """Token-overlap (Jaccard) similarity in [0,1] between two author names. Exact
    case-folded equality is 1.0; no overlap is 0.0."""
    if not seed_name or not candidate_name:
        return 0.0
    if seed_name.casefold() == candidate_name.casefold():
        return 1.0
    seed_tokens = set(query_tokens(seed_name))
    candidate_tokens = set(query_tokens(candidate_name))
    if not seed_tokens or not candidate_tokens:
        return 0.0
    return len(seed_tokens & candidate_tokens) / len(seed_tokens | candidate_tokens)


def pick_openalex_author(seed_name: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick a single best OpenAlex author match for a seed name, or None when the
    name is ambiguous. A result must clear the overlap threshold; if two different
    results both clear it the name is ambiguous unless the best is an exact match."""
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for entry in results:
        display = entry.get("display_name")
        if not isinstance(display, str) or not display.strip():
            continue
        similarity = author_name_similarity(seed_name, display)
        works = entry.get("works_count") if isinstance(entry.get("works_count"), int) else 0
        scored.append((similarity, works, entry))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_similarity, _, best = scored[0]
    if best_similarity < AUTHOR_NAME_RESOLVE_OVERLAP:
        return None
    if len(scored) >= 2:
        second_similarity = scored[1][0]
        # An exact match wins outright; otherwise two near-equal matches are ambiguous.
        if second_similarity >= AUTHOR_NAME_RESOLVE_OVERLAP and not (
            best_similarity == 1.0 and second_similarity < 1.0
        ):
            return None
    return best


def resolve_author_identity(
    seed: dict[str, Any],
    seed_context: dict[str, Any],
    *,
    budget_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve an author seed to an OpenAlex identity. ORCID seeds are exact and
    need no lookup (works are filtered by author.orcid). Name-only seeds search
    the authors endpoint and either resolve a single best match or flag ambiguity.

    Returns a record with ``identity`` (one of AUTHOR_IDENTITY_*), an optional
    resolved ``openalex_author_id``/``orcid``, a human ``rationale``, and a
    ``works_filter`` tuple ``(key, value)`` (or None when no works can be listed)."""
    name = seed["name"]
    orcid = seed.get("orcid")
    if orcid:
        return {
            "identity": AUTHOR_IDENTITY_ORCID_EXACT,
            "openalex_author_id": None,
            "orcid": orcid,
            "name": name,
            "rationale": f"ORCID {orcid} is an exact author identity; works filtered by author.orcid.",
            "works_filter": ("author.orcid", orcid_url(orcid)),
        }
    if not author_context_signals(seed, seed_context):
        return {
            "identity": AUTHOR_IDENTITY_CONTEXT_MISSING,
            "openalex_author_id": None,
            "orcid": None,
            "name": name,
            "rationale": (
                f"Author seed {name!r} has no ORCID and no seed-title or affiliation context; "
                "identity is not strong enough for publication expansion."
            ),
            "works_filter": None,
        }
    results = openalex_author_search(name, budget_context=budget_context)
    resolved = pick_openalex_author(name, results)
    if resolved is None:
        identity = AUTHOR_IDENTITY_AMBIGUOUS if results else AUTHOR_IDENTITY_NO_MATCH
        rationale = (
            f"OpenAlex author search for {name!r} returned multiple plausible matches; "
            "identity is ambiguous, so no publications were proposed for this author."
            if results
            else f"OpenAlex author search for {name!r} returned no match; no publications proposed."
        )
        return {
            "identity": identity,
            "openalex_author_id": None,
            "orcid": None,
            "name": name,
            "rationale": rationale,
            "works_filter": None,
        }
    author_id = _openalex_author_id(resolved.get("id"))
    display = resolved.get("display_name") if isinstance(resolved.get("display_name"), str) else name
    resolved_orcid = normalize_orcid(resolved.get("orcid"))
    return {
        "identity": AUTHOR_IDENTITY_NAME_RESOLVED,
        "openalex_author_id": author_id,
        "orcid": resolved_orcid,
        "name": name,
        "rationale": (
            f"OpenAlex author search resolved {name!r} to {display!r} "
            f"(OpenAlex author {author_id}); identity inferred by name, not ORCID."
        ),
        "works_filter": ("author.id", author_id) if author_id else None,
    }


def publication_candidate_id(work: dict[str, Any]) -> str:
    work_id = _openalex_work_id(work.get("id"))
    if work_id:
        key = work_id
    elif _clean_doi(work.get("doi")):
        key = _clean_doi(work.get("doi"))
    elif isinstance(work.get("display_name"), str):
        key = work["display_name"].lower()
    else:
        key = str(id(work))
    digest = hashlib.sha1(f"openalex:{key}".encode(), usedforsecurity=False).hexdigest()[:10]
    return f"cand-{digest}"


def _is_seed_paper(
    work_id: str | None, doi: str | None, title: str | None, seed_context: dict[str, Any]
) -> bool:
    if work_id and seed_context.get("openalex_id") and work_id == seed_context["openalex_id"]:
        return True
    if doi and seed_context.get("doi") and doi == seed_context["doi"]:
        return True
    if title and seed_context.get("title") and title.casefold() == seed_context["title"].casefold():
        return True
    return False


def build_publication_candidate(
    work: dict[str, Any],
    *,
    seed: dict[str, Any],
    identity: str,
    author_quality_gate: dict[str, Any],
    seed_context: dict[str, Any],
    source_id: str,
    discovered_at: str,
    discovery_id: str,
    max_results: int,
) -> dict[str, Any] | None:
    """Build one publication `source_candidate` from an OpenAlex work, or None when
    the work is the seed paper itself (never recommend the paper we expand from)."""
    title = clean_author_text(work.get("display_name")) or "(untitled work)"
    work_id = _openalex_work_id(work.get("id"))
    doi = _clean_doi(work.get("doi"))
    if _is_seed_paper(work_id, doi, title, seed_context):
        return None

    year = work.get("publication_year") if isinstance(work.get("publication_year"), int) else None
    work_type = work.get("type") if isinstance(work.get("type"), str) else None
    cited_by = work.get("cited_by_count") if isinstance(work.get("cited_by_count"), int) else 0
    open_access = work.get("open_access") if isinstance(work.get("open_access"), dict) else {}
    is_oa = bool(open_access.get("is_oa"))
    oa_status = open_access.get("oa_status") if isinstance(open_access.get("oa_status"), str) else None
    landing_url, pdf_url, license_key = _work_location_urls(work)

    seed_title_tokens = set(seed_context["title_tokens"])
    work_title_tokens = set(query_tokens(work.get("display_name") or ""))
    overlap_tokens = sorted(seed_title_tokens & work_title_tokens)
    overlap_ratio = (len(overlap_tokens) / len(seed_title_tokens)) if seed_title_tokens else 0.0
    related = bool(overlap_tokens)
    out_of_scope = not related

    # Relevance (0..1): topical overlap dominates; year freshness, citation count
    # (a weak authority signal), and open access add small bonuses.
    relevance = clamp_unit(0.35 + 0.45 * overlap_ratio)
    if year is not None:
        relevance = clamp_unit(relevance + 0.05 * _year_freshness(year, seed_context.get("year")))
    relevance = clamp_unit(relevance + min(0.1, cited_by / 1000.0))
    if is_oa:
        relevance = clamp_unit(relevance + 0.05)

    # Trust (0..1): a research paper is a primary, non-official source. Certain
    # identity (ORCID) and open access raise trust; identity is never enough alone
    # to assert official publisher authority (that stays unknown / review-required).
    trust = 0.7
    if identity == AUTHOR_IDENTITY_ORCID_EXACT:
        trust += 0.05
    if is_oa:
        trust += 0.05
    trust = clamp_unit(trust + 0.05 * overlap_ratio)

    risk_flags: list[str] = ["unknown_officialness"]
    if identity == AUTHOR_IDENTITY_NAME_RESOLVED:
        risk_flags.append(PUBLICATION_RISK_IDENTITY_INFERRED)
    if out_of_scope:
        risk_flags.append(PUBLICATION_RISK_OUT_OF_SCOPE)
    if not is_oa:
        risk_flags.append(PUBLICATION_RISK_NOT_OPEN_ACCESS)

    recommended_action = "reject" if out_of_scope else "review"

    # The contract requires non-empty matched_query_terms. A related work shares
    # title tokens with the analyzed paper; an out-of-scope work matched the author
    # identity, so the author's name tokens are the recorded match.
    if overlap_tokens:
        matched_terms = overlap_tokens
    else:
        matched_terms = query_tokens(seed["name"])[:1] or [seed["name"].lower()[:1]]

    identity_phrase = {
        AUTHOR_IDENTITY_ORCID_EXACT: "an exact ORCID identity match",
        AUTHOR_IDENTITY_NAME_RESOLVED: "a name-resolved identity match (inferred, not ORCID)",
    }.get(identity, "an unresolved identity match")

    authority_reason = (
        f"Publication '{title}' attributed to {seed['name']} via OpenAlex with {identity_phrase}. "
        "A research paper is a primary, non-official source; canonical publisher authority is not "
        "verified from the index, so official_source is unknown and the candidate requires review "
        "before fetch."
    )
    freshness_reason = (
        f"Published in {year or 'an unknown year'}; "
        + ("recent relative to the analyzed paper." if year and seed_context.get("year")
           and abs(year - seed_context["year"]) <= 3
           else "older or undated; treat as background context.")
    )
    scope_reason = (
        f"Title shares {len(overlap_tokens)} analyzed-paper term(s) with the seed paper; "
        "retained as a related publication for review."
        if related
        else "Title shares no analyzed-paper terms; recommended for rejection as out of scope "
             "(still by the author, but unrelated to this paper's topic)."
    )
    oa_phrase = "open access" if is_oa else "not open access"
    rationale = (
        f"OpenAlex work {work_id or 'unknown'} by {seed['name']} ({identity_phrase}) proposed as a "
        f"related publication; {oa_phrase}; {scope_reason.lower()} Recommended for "
        f"{'rejection as out of scope' if out_of_scope else 'review before fetch'}."
    )

    url = landing_url or (f"https://doi.org/{doi}" if doi else None)
    if url is None and isinstance(work.get("id"), str):
        url = work["id"]

    candidate: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": publication_candidate_id(work),
        "request_id": None,
        "seed_source_id": source_id,
        "discovery_run_id": discovery_id,
        "discovered_at": discovered_at,
        "discovered_by": OPENALEX_DISCOVERED_BY,
        "provider": "openalex",
        "url": url,
        "title": title,
        "source_type": "paper",
        "trust_tier": "primary_non_official",
        "relevance_score": relevance,
        "trust_score": trust,
        "official_source": None,
        "jurisdiction": None,
        "license": license_key,
        "terms_url": OPENALEX_TERMS_URL,
        "rationale": rationale,
        "recommended_action": recommended_action,
        "network_io_executed": True,
        "paper": openalex_paper_metadata(work, title),
        "provider_budget": openalex_provider_budget(max_results),
        "quality_gates": {
            "author_identity": author_quality_gate,
        },
        "reasoning": {
            "matched_query_terms": matched_terms,
            "authority_reason": authority_reason,
            "freshness_reason": freshness_reason,
            "scope_reason": scope_reason,
            "risk_flags": risk_flags,
        },
        "openalex": {
            "work_id": work_id,
            "doi": doi,
            "publication_year": year,
            "type": work_type,
            "cited_by_count": cited_by,
            "is_oa": is_oa,
            "oa_status": oa_status,
            "landing_page_url": landing_url,
            "pdf_url": pdf_url,
            "identity_match": identity,
            "author_name": seed["name"],
            "author_orcid": seed.get("orcid"),
            "topical_overlap": round(overlap_ratio, 2),
        },
    }
    return candidate


def publication_candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, float, float, int, str]:
    """Rank publication candidates: ORCID-exact before name-resolved; review before
    reject; then relevance, trust, recency, and finally a stable title tiebreak."""
    identity = candidate.get("openalex", {}).get("identity_match")
    identity_rank = 0 if identity == AUTHOR_IDENTITY_ORCID_EXACT else 1
    action_rank = 0 if candidate["recommended_action"] == "review" else 1
    year = candidate.get("openalex", {}).get("publication_year") or 0
    return (
        identity_rank,
        action_rank,
        -candidate["relevance_score"],
        -candidate["trust_score"],
        -year,
        candidate["title"].lower(),
    )


def discover_author_publications(
    *,
    project_root: Path,
    config: dict[str, Any],
    source_id: str,
    seeds: list[dict[str, Any]],
    seed_context: dict[str, Any],
    max_results: int,
    warnings: list[dict[str, str]],
    budget_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve each author seed to an OpenAlex identity and propose that author's
    works as related-publication candidates. Network I/O is performed against
    OpenAlex; nothing is downloaded. Ambiguous/missing identities emit warnings and
    propose no candidates for that author. Returns the publication-discovery report
    fields to merge into the `authors` command report."""
    cap = min(max_results, OPENALEX_DISCOVERY_MAX_RESULTS_CAP)
    discovered_at = timestamp_utc()
    discovery_id = discovery_run_id("authors/publications", [source_id])
    candidates: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []

    for seed in seeds[:OPENALEX_DISCOVERY_MAX_AUTHORS]:
        identity_record = resolve_author_identity(
            seed,
            seed_context,
            budget_context=budget_context,
        )
        works_filter = identity_record["works_filter"]
        author_quality_gate = author_identity_quality_gate(
            identity_record["identity"], seed=seed, seed_context=seed_context
        )
        identities.append(
            {
                "name": seed["name"],
                "seed_orcid": seed.get("orcid"),
                "identity": identity_record["identity"],
                "openalex_author_id": identity_record.get("openalex_author_id"),
                "resolved_orcid": identity_record.get("orcid"),
                "works_filter": (
                    f"{works_filter[0]}:{works_filter[1]}" if works_filter else None
                ),
                "rationale": identity_record["rationale"],
                "quality_gates": {
                    "author_identity": author_quality_gate,
                },
            }
        )
        if works_filter is None:
            code = {
                AUTHOR_IDENTITY_AMBIGUOUS: "author_identity_ambiguous",
                AUTHOR_IDENTITY_CONTEXT_MISSING: "author_identity_context_missing",
            }.get(identity_record["identity"], "author_identity_no_match")
            warnings.append(
                {
                    "code": code,
                    "message": identity_record["rationale"],
                }
            )
            continue

        works = openalex_works_by_filter(
            works_filter[0],
            works_filter[1],
            OPENALEX_DISCOVERY_PER_AUTHOR,
            budget_context=budget_context,
        )
        for work in works:
            candidate = build_publication_candidate(
                work,
                seed=seed,
                identity=identity_record["identity"],
                author_quality_gate=author_quality_gate,
                seed_context=seed_context,
                source_id=source_id,
                discovered_at=discovered_at,
                discovery_id=discovery_id,
                max_results=cap,
            )
            if candidate is not None:
                candidates.append(candidate)

    candidates.sort(key=publication_candidate_sort_key)
    candidates = candidates[:cap]
    store_path = candidate_store_path(project_root, config)
    written = append_candidates(store_path, candidates)

    return {
        "publications_provider": "openalex",
        "discovery_run_id": discovery_id,
        "max_results": cap,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "candidates_path": relative_label(project_root, store_path),
        "written": len(written),
        "author_identity": identities,
        "token_used": openalex_api_key() is not None,
        "network_io_executed": True,
    }


def run_authors_discovery(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Extract a bounded author seed list from a normalized paper source (E35-T01),
    and with ``--discover-publications`` propose that author's related works as
    publication candidates (E35-T02).

    The extraction path is read-only: it reads the manifest record and the
    normalized frontmatter for the source id, plus any provider author metadata
    already captured on the record, and emits authors with provenance (`source`)
    and `confidence`. It performs no network I/O and never infers personal data —
    only metadata already present in the source or provider response is surfaced.

    With ``--discover-publications``, each extracted seed is resolved to an
    OpenAlex identity (by ORCID when present, otherwise by name) and that author's
    works are proposed as `source_candidate` records of source_type `paper` for
    review. Candidates are appended to the durable store; nothing is downloaded.
    OpenAlex contact sets ``network_io_executed: true``; ``OPENALEX_API_KEY`` is
    read from the environment only and never emitted."""
    import normalize_sources

    source_id = require_non_empty(args.source_id, "--source-id")
    manifest_rel, normalized_rel = normalize_sources.source_paths(config)
    manifest_path = project_root / manifest_rel
    records = load_manifest_records(manifest_path)
    record = next((item for item in records if item.get("id") == source_id), None)
    if record is None:
        raise DiscoverSourcesError(
            "SOURCE_UNKNOWN",
            f"Unknown source id: {source_id} (no record in {relative_label(project_root, manifest_path)}).",
            remediation="Run source_inventory.py and list manifest ids, then pass an existing source id.",
            details={"source_id": source_id, "network_io_executed": False},
        )

    warnings: list[dict[str, str]] = []
    normalized_path = project_root / normalized_rel / f"{normalize_sources.safe_source_id(source_id)}.md"
    frontmatter: dict[str, Any] | None = None
    if normalized_path.is_file():
        frontmatter = normalize_sources.read_output_frontmatter(normalized_path)
    else:
        warnings.append(
            {
                "code": "no_normalized_record",
                "message": (
                    f"No normalized record for {source_id}; extracting from provider metadata only. "
                    "Run normalize_sources.py --all to extract author names from the source."
                ),
            }
        )

    discover_publications = bool(getattr(args, "discover_publications", False))
    # In extraction mode --max-results bounds the author list (E35-T01). In
    # discovery mode it bounds the final candidate list instead, so seeds are
    # extracted up to the author bound and publication candidates are capped later.
    seed_cap = OPENALEX_DISCOVERY_MAX_AUTHORS if discover_publications else args.max_results
    seeds = extract_author_seeds(
        frontmatter=frontmatter,
        provider_metadata=record.get("metadata"),
        max_results=seed_cap,
    )
    if not seeds:
        warnings.append(
            {
                "code": "no_author_metadata",
                "message": (
                    f"No author metadata found for {source_id}. The normalized record and provider "
                    "metadata carried no author names; nothing was inferred."
                ),
            }
        )

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provider": "authors",
        "command": "authors",
        "generated_at": timestamp_utc(),
        "source_id": source_id,
        "source_kind": record.get("kind"),
        "discovered_by": AUTHORS_DISCOVERED_BY,
        "normalized_record": relative_label(project_root, normalized_path) if normalized_path.is_file() else None,
        "count": len(seeds),
        "authors": seeds,
        "warnings": warnings,
        "network_io_executed": False,
    }
    if not discover_publications:
        return report

    # Publication discovery (E35-T02): resolve each author seed to an OpenAlex
    # identity and propose that author's works as related-publication candidates.
    # Discovery appends to the same `warnings` list surfaced in the report.
    budget_context = academic_provider_budget_context(
        project_root,
        config,
        args.run_id,
        command="authors",
        scope_id=source_id,
    )
    seed_context = build_seed_paper_context(record, frontmatter)
    report.update(
        discover_author_publications(
            project_root=project_root,
            config=config,
            source_id=source_id,
            seeds=seeds,
            seed_context=seed_context,
            max_results=args.max_results,
            warnings=warnings,
            budget_context=budget_context,
        )
    )
    return report


# --- Companion artifact discovery (E35-T03) ----------------------------------


def classify_companion_host(host: str) -> tuple[str, str, bool | None]:
    """Classify a companion link host into (source_type, base_trust_tier,
    official_source). Recognized repository/dataset/preprint hosts are
    primary_non_official; a DOI/publisher landing page is the canonical publisher
    (official_primary); a generic host defaults to project_page."""
    if domain_matches(host, list(COMPANION_REPOSITORY_HOSTS)):
        return "code_repository", "primary_non_official", None
    if domain_matches(host, list(COMPANION_DATASET_HOSTS)):
        return "dataset", "primary_non_official", None
    if domain_matches(host, list(COMPANION_PUBLISHER_HOSTS)):
        return "publisher_page", "official_primary", True
    if domain_matches(host, list(COMPANION_PREPRINT_HOSTS)):
        return "supplemental_material", "primary_non_official", None
    return "project_page", "primary_non_official", None


def _github_owner_repo_from_url(url: Any) -> str | None:
    """owner/repo from a github.com URL, or None. Used to dedupe inline github
    links against github-search results."""
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def companion_candidate_id(url: str) -> str:
    digest = hashlib.sha1(f"companion:{url.strip().lower()}".encode(), usedforsecurity=False).hexdigest()[:10]
    return f"cand-{digest}"


def companion_repository_quality_gate(evidence_origin: str) -> dict[str, Any]:
    origin_confidence = COMPANION_REPOSITORY_ORIGIN_CONFIDENCE.get(evidence_origin, "search_only")
    reason = {
        COMPANION_ORIGIN_PAPER_INLINE: (
            "Repository link was cited directly in the analyzed paper; ownership still requires review."
        ),
        COMPANION_ORIGIN_PROVIDER_METADATA: (
            "Repository link came from provider metadata for the analyzed paper; ownership still requires review."
        ),
        COMPANION_ORIGIN_GITHUB_SEARCH: (
            "Repository was found by GitHub search and is not verified as an author-owned companion."
        ),
        COMPANION_ORIGIN_SEARCH: (
            "Repository was found by general search and is not verified as an author-owned companion."
        ),
    }.get(evidence_origin, "Repository origin is not verified; review is required.")
    return {
        "status": "review_required",
        "repository_link_origin": evidence_origin,
        "origin_confidence": origin_confidence,
        "review_required": True,
        "reason": reason,
    }


def attach_companion_repository_gate(candidate: dict[str, Any], evidence_origin: str) -> dict[str, Any]:
    if candidate.get("source_type") != "code_repository":
        return candidate
    companions = candidate.setdefault("companions", {})
    if isinstance(companions, dict):
        companions["repository_link_origin"] = evidence_origin
    quality_gates = candidate.setdefault("quality_gates", {})
    if isinstance(quality_gates, dict):
        quality_gates["companion_repository"] = companion_repository_quality_gate(evidence_origin)
    return candidate


def build_companion_candidate(
    url: Any,
    *,
    title: str | None,
    evidence_origin: str,
    seed_source_id: str,
    request_id: str | None,
    discovered_at: str,
    discovery_id: str,
    seed_title_tokens: set[str],
) -> dict[str, Any] | None:
    """Build one companion `source_candidate` from an inline/provider/search URL,
    or None when the URL is unusable. Host classification picks the source_type;
    evidence_origin picks the trust tier (paper-cited links outrank search hits)."""
    canonical = http_url(url)
    if canonical is None:
        return None
    host = result_host(canonical)
    source_type, base_tier, official = classify_companion_host(host)
    trust_tier = base_tier
    # A generic project_page found only via search is unverified -> secondary_unknown.
    if (
        evidence_origin == COMPANION_ORIGIN_SEARCH
        and source_type == "project_page"
    ):
        trust_tier = "secondary_unknown"

    candidate_text = f"{host} {urlparse(canonical).path} {title or ''}"
    candidate_tokens = set(query_tokens(candidate_text))
    overlap_tokens = sorted(seed_title_tokens & candidate_tokens)
    overlap_ratio = (len(overlap_tokens) / len(seed_title_tokens)) if seed_title_tokens else 0.0

    risk_flags: list[str] = []
    if official is None:
        risk_flags.append(SEARCH_RISK_UNKNOWN_OFFICIALNESS)

    # Relevance: inline/provider links are paper-cited (high baseline); search
    # hits rely on topical overlap with the analyzed paper's title.
    if evidence_origin in (COMPANION_ORIGIN_PAPER_INLINE, COMPANION_ORIGIN_PROVIDER_METADATA):
        relevance = clamp_unit(0.75 + 0.2 * overlap_ratio)
    else:
        relevance = clamp_unit(0.4 + 0.5 * overlap_ratio)

    trust_score = clamp_unit(
        SEARCH_TIER_TRUST_BASE.get(trust_tier, 0.4)
        + (0.05 if evidence_origin == COMPANION_ORIGIN_PAPER_INLINE else 0.0)
        + 0.04 * overlap_ratio
        - 0.05 * len(risk_flags)
    )

    if not overlap_tokens:
        matched_terms = [host] if host else [(title or canonical).lower()[:1]]
    else:
        matched_terms = overlap_tokens

    origin_phrase = {
        COMPANION_ORIGIN_PAPER_INLINE: "a link cited directly in the analyzed paper",
        COMPANION_ORIGIN_PROVIDER_METADATA: "a link carried in the paper's provider metadata",
        COMPANION_ORIGIN_SEARCH: "a general-search result",
    }.get(evidence_origin, "a discovered link")
    authority_reason = (
        f"{host} classified as {source_type} ({trust_tier}) from its host; the link is {origin_phrase}. "
        "Canonical ownership is not verified from the link, so official_source is recorded as "
        "known only for a recognized publisher landing page and otherwise requires review."
    )
    freshness_reason = (
        "No separate freshness signal; the link is treated as evergreen evidence context."
    )
    scope_reason = (
        f"Title/URL shares {len(overlap_tokens)} analyzed-paper term(s); retained as a companion "
        f"{source_type} for review."
    )
    rationale = (
        f"Companion {source_type} at {canonical} ({origin_phrase}) classified {trust_tier}; "
        f"recommended for review before fetch. {scope_reason}"
    )

    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": companion_candidate_id(canonical),
        "request_id": request_id,
        "seed_source_id": seed_source_id,
        "discovery_run_id": discovery_id,
        "discovered_at": discovered_at,
        "discovered_by": COMPANIONS_DISCOVERED_BY,
        "provider": "companions",
        "url": canonical,
        "title": (title.strip() if isinstance(title, str) and title.strip() else canonical),
        "source_type": source_type,
        "trust_tier": trust_tier,
        "relevance_score": relevance,
        "trust_score": trust_score,
        "official_source": official,
        "jurisdiction": None,
        "license": None,
        "terms_url": None,
        "rationale": rationale,
        "recommended_action": "review",
        "network_io_executed": evidence_origin
        in (COMPANION_ORIGIN_GITHUB_SEARCH, COMPANION_ORIGIN_SEARCH),
        "evidence_origin": evidence_origin,
        "reasoning": {
            "matched_query_terms": matched_terms,
            "authority_reason": authority_reason,
            "freshness_reason": freshness_reason,
            "scope_reason": scope_reason,
            "risk_flags": risk_flags,
        },
        "companions": {
            "host": host,
            "evidence_origin": evidence_origin,
            "source_type": source_type,
        },
    }
    return attach_companion_repository_gate(candidate, evidence_origin)


def companion_dedup_key(candidate: dict[str, Any]) -> tuple[str, str]:
    """Dedup key: owner/repo for any GitHub link (so an inline github link and a
    github-search hit on the same repo collapse), else the normalized URL."""
    github_block = candidate.get("github")
    if isinstance(github_block, dict) and isinstance(github_block.get("full_name"), str):
        return ("gh", github_block["full_name"].lower())
    owner_repo = _github_owner_repo_from_url(candidate.get("url"))
    if owner_repo:
        return ("gh", owner_repo.lower())
    url = candidate.get("url") or ""
    return ("url", url.strip().lower().rstrip("/"))


def companion_candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, float, float, str]:
    """Rank: paper-cited links first, then github search, then general search;
    within an origin by trust tier, officialness, trust/relevance scores, title."""
    origin = candidate.get("evidence_origin", COMPANION_ORIGIN_SEARCH)
    return (
        COMPANION_ORIGIN_RANK.get(origin, 99),
        TIER_RANK[candidate["trust_tier"]],
        0 if candidate.get("official_source") is True else 1,
        -candidate["trust_score"],
        -candidate["relevance_score"],
        candidate["title"].lower(),
    )


def _read_normalized_body(path: Path) -> str:
    """Read the body text beneath a normalized record's frontmatter (or '' )."""
    if not path.is_file():
        return ""
    text = path.read_text(errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None or closing + 1 >= len(lines):
        return ""
    return "\n".join(lines[closing + 1:])


def extract_inline_companion_urls(frontmatter: dict[str, Any] | None, body: str) -> list[str]:
    """Inline companion URLs from the normalized record's `links` frontmatter
    field plus bare URLs in the body text. Order preserved; de-duplicated."""
    urls: list[str] = []
    seen: set[str] = set()
    if isinstance(frontmatter, dict):
        links = frontmatter.get("links")
        if isinstance(links, list):
            for value in links:
                candidate = http_url(value)
                if candidate and candidate not in seen:
                    urls.append(candidate)
                    seen.add(candidate)
    for match in COMPANION_URL_RE.findall(body or ""):
        # Strip trailing sentence punctuation the greedy class can swallow (e.g.
        # a URL ending a sentence: "...lives at https://host/x." -> drop the dot).
        candidate = http_url(match.rstrip(".,;:!?"))
        if candidate and candidate not in seen:
            urls.append(candidate)
            seen.add(candidate)
    return urls


def provider_metadata_companion_urls(metadata: Any) -> list[str]:
    """Landing-page URLs carried in OpenAlex-style provider metadata locations
    (primary/best/locations). The paper's own publisher landing page is a desired
    `publisher_page` companion; PDF URLs (the paper itself) are not collected."""
    if not isinstance(metadata, dict):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    sources = []
    for key in ("primary_location", "best_oa_location"):
        value = metadata.get(key)
        if isinstance(value, dict):
            sources.append(value)
    locations = metadata.get("locations")
    if isinstance(locations, list):
        sources.extend(loc for loc in locations if isinstance(loc, dict))
    for location in sources:
        landing = http_url(location.get("landing_page_url"))
        if landing and landing not in seen:
            urls.append(landing)
            seen.add(landing)
    return urls


def is_paper_self_link(url: str, arxiv_id: str | None) -> bool:
    """True for a link that points to the analyzed paper's own arXiv listing."""
    if not arxiv_id:
        return False
    if result_host(url) != "arxiv.org":
        return False
    path = urlparse(url).path.lower()
    token = arxiv_id.lower()
    return f"/abs/{token}" in path or f"/pdf/{token}" in path


def companion_author_surnames(seeds: list[dict[str, Any]], *, limit: int) -> list[str]:
    """Lead-author surnames from extracted author seeds (E35-T01), de-duplicated
    and capped. Single-initial tokens are skipped (too weak to scope a query)."""
    surnames: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        name = seed.get("name") if isinstance(seed, dict) else None
        if not isinstance(name, str):
            continue
        parts = name.split()
        token = parts[-1].strip(".,;") if parts else ""
        if len(token) < 2:
            continue  # skip initials like "A." — not a useful search term
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        surnames.append(token)
        if len(surnames) >= limit:
            break
    return surnames


def companion_project_names(title: str | None) -> list[str]:
    """Candidate project/system name(s) extracted from the paper title. Bounded and
    low-noise: only the segment before a colon when it is short (the common
    "SystemName: descriptive subtitle" paper-title convention), e.g. "BERT" from
    "BERT: Pre-training of Deep ...". Returns [] when no such name is evident."""
    if not title or ":" not in title:
        return []
    head = title.split(":", 1)[0].strip()
    if head and 1 <= len(head.split()) <= COMPANION_PROJECT_NAME_MAX_WORDS and len(head) <= COMPANION_PROJECT_NAME_MAX_LEN:
        return [head]
    return []


def companion_query_plan(
    *,
    title: str | None,
    author_surnames: list[str],
    arxiv_id: str | None,
    doi: str | None,
    project_names: list[str],
    max_queries: int,
) -> dict[str, list[dict[str, str]]]:
    """Bounded, explainable companion query plan. Returns `{github: [...], search:
    [...]}` where each entry is `{query, reason}`. Drives recall beyond the title
    alone (E35-T03) without crawling: queries are de-duplicated (case-insensitive)
    and each phase is capped to `max_queries`."""
    github: list[dict[str, str]] = []
    search: list[dict[str, str]] = []

    def add(target: list[dict[str, str]], query: str, reason: str) -> None:
        collapsed = " ".join(query.split())
        if not collapsed:
            return
        if any(entry["query"].lower() == collapsed.lower() for entry in target):
            return
        target.append({"query": collapsed, "reason": reason})

    surname = author_surnames[0] if author_surnames else None
    identifier = arxiv_id or doi
    if title:
        add(github, title, "paper title")
        add(search, f"{title} dataset", "dataset companion for the paper title")
    for name in project_names:
        add(github, name, "named system/project from the paper title")
    if title and surname:
        add(github, f"{title} {surname}", "paper title scoped to the lead author surname")
        add(search, f"{title} {surname} code", "author-scoped companion search")
    if identifier:
        add(search, f"{identifier} dataset", "identifier-scoped companion search (arXiv/DOI)")
    return {"github": github[:max_queries], "search": search[:max_queries]}


def companion_github_candidates(
    query: str,
    *,
    seed_source_id: str,
    request_id: str | None,
    discovery_id: str,
    discovered_at: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Run GitHub repository discovery (E32-T02 transport + build_github_candidate)
    for one query and return candidates *without* storing them. The companions
    orchestrator stores the merged, deduped list once."""
    per_page = min(max_results, GITHUB_MAX_RESULTS_CAP)
    document = github_json_object(github_fetch_url(github_search_url(query, per_page)))
    items = document.get("items")
    if not isinstance(items, list):
        raise DiscoverSourcesError(
            "DISCOVERY_RESPONSE_INVALID",
            "GitHub search response did not contain an items list.",
            remediation="Retry later or inspect the provider response outside the workspace.",
            details=github_error_details(),
        )
    terms = query_tokens(query)
    candidates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = build_github_candidate(
            item,
            query=query,
            terms=terms,
            request_id=request_id,
            discovery_id=discovery_id,
            discovered_at=discovered_at,
        )
        if candidate is None:
            continue
        # Re-badge the github candidate as a companion search hit while keeping its
        # rich github metadata block for the store.
        candidate["seed_source_id"] = seed_source_id
        candidate["evidence_origin"] = COMPANION_ORIGIN_GITHUB_SEARCH
        candidate["discovered_by"] = COMPANIONS_DISCOVERED_BY
        candidate["provider"] = "companions"
        candidate["companions"] = {
            "host": candidate["github"]["owner"] if isinstance(candidate.get("github"), dict) else None,
            "evidence_origin": COMPANION_ORIGIN_GITHUB_SEARCH,
            "source_type": "code_repository",
        }
        attach_companion_repository_gate(candidate, COMPANION_ORIGIN_GITHUB_SEARCH)
        candidates.append(candidate)
        if len(candidates) >= max_results:
            break
    return candidates


def companion_search_results_for_query(
    project_root: Path,
    provider: str,
    search_cfg: dict[str, Any],
    *,
    query: str,
    seed_source_id: str,
    request_id: str | None,
    discovery_id: str,
    discovered_at: str,
    seed_title_tokens: set[str],
    max_results: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Run one bounded general-search query (E33 provider) and return companion
    candidates *without* storing them. Returns (candidates, network_executed); the
    network flag comes from the adapter (fixture/command-local backends do no
    network), so the companion report's per-phase accounting stays accurate."""
    request = {
        "query": query,
        "domain_allowlist": [],
        "domain_blocklist": [],
        "max_results": min(max_results, COMPANION_SEARCH_MAX_RESULTS),
        "prefer_official": False,
        "expected_source_type": None,
        "official_domains": [],
        "jurisdiction": None,
    }
    results, network = gather_search_results(project_root, provider, search_cfg, request)
    candidates: list[dict[str, Any]] = []
    for result in results:
        url = http_url(result.get("url")) or http_url(result.get("link"))
        if url is None:
            continue
        candidate = build_companion_candidate(
            url,
            title=result.get("title") or result.get("name"),
            evidence_origin=COMPANION_ORIGIN_SEARCH,
            seed_source_id=seed_source_id,
            request_id=request_id,
            discovered_at=discovered_at,
            discovery_id=discovery_id,
            seed_title_tokens=seed_title_tokens,
        )
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= request["max_results"]:
            break
    return candidates, network


def dedupe_companion_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank then collapse companion candidates by dedup key (owner/repo for GitHub
    links, normalized URL otherwise), keeping the best-ranked per key. Used both
    per-phase (honest counts across a multi-query plan) and once globally."""
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in sorted(candidates, key=companion_candidate_sort_key):
        key = companion_dedup_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def run_companions_discovery(
    project_root: Path, config: dict[str, Any], discovery: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    """Discover a paper's companion repositories, datasets, project pages,
    supplemental material, and publisher pages (E35-T03).

    Three composed phases: (1) inline extraction from the normalized paper
    body/frontmatter `links` + provider-metadata locations (no network, highest
    trust); (2) GitHub repository discovery (network); (3) bounded general search
    for datasets/project pages (network, only when a search provider is
    configured). Phases 2 and 3 run a small, explainable query plan derived from
    the paper title, a pre-colon project/system name, the lead-author surname, and
    the arXiv/DOI identifier (not the title alone), capped per phase. Candidates
    are merged, de-duplicated (inline github link and github-search hit on the same
    repo collapse), ranked (paper-cited first), capped, and appended to the store.
    Nothing is fetched."""
    import normalize_sources

    source_id = require_non_empty(args.source_id, "--source-id")
    request_id_arg = getattr(args, "request_id", None)
    request_id = request_id_arg.strip() if isinstance(request_id_arg, str) and request_id_arg.strip() else None
    configured_providers = validate_provider_ids(
        discovery.get("providers", []),
        phase="discovery",
    )
    github_requested = not bool(getattr(args, "no_github", False))
    search_requested = not bool(getattr(args, "no_search", False))
    use_github = github_requested and provider_is_allowed(configured_providers, "github")
    use_search = search_requested and provider_is_allowed(configured_providers, "search")

    manifest_rel, normalized_rel = normalize_sources.source_paths(config)
    manifest_path = project_root / manifest_rel
    records = load_manifest_records(manifest_path)
    record = next((item for item in records if item.get("id") == source_id), None)
    if record is None:
        raise DiscoverSourcesError(
            "SOURCE_UNKNOWN",
            f"Unknown source id: {source_id} (no record in {relative_label(project_root, manifest_path)}).",
            remediation="Run source_inventory.py and list manifest ids, then pass an existing source id.",
            details={"source_id": source_id, "network_io_executed": False},
        )

    normalized_path = project_root / normalized_rel / f"{normalize_sources.safe_source_id(source_id)}.md"
    frontmatter: dict[str, Any] | None = None
    if normalized_path.is_file():
        frontmatter = normalize_sources.read_output_frontmatter(normalized_path)
    body = _read_normalized_body(normalized_path)

    title = clean_author_text((frontmatter or {}).get("title"))
    seed_title_tokens = set(query_tokens(title)) if title else set()
    arxiv_id = (frontmatter or {}).get("arxiv_id")
    # An unquoted id like `arxiv_id: 2005.11401` parses as a YAML float; coerce it
    # back to text so self-link exclusion still recognizes the paper's own listing.
    if isinstance(arxiv_id, (int, float)) and not isinstance(arxiv_id, bool):
        arxiv_id = str(arxiv_id)
    arxiv_id = arxiv_id.strip() if isinstance(arxiv_id, str) and arxiv_id.strip() else None
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else None
    doi_value = (frontmatter or {}).get("doi") or (metadata or {}).get("doi")
    doi = doi_value.strip() if isinstance(doi_value, str) and doi_value.strip() else None

    # Companion search is driven by a bounded query plan (E35-T03): the paper
    # title, a short pre-colon project/system name, the lead-author surname, and
    # the arXiv/DOI identifier — not the title alone — to widen recall for repos
    # and datasets whose names do not echo the title. Author seeds reuse E35-T01.
    author_seeds = extract_author_seeds(
        frontmatter=frontmatter, provider_metadata=metadata, max_results=COMPANION_MAX_AUTHOR_SEEDS
    )
    author_surnames = companion_author_surnames(author_seeds, limit=COMPANION_MAX_AUTHOR_SURNAMES)
    project_names = companion_project_names(title)
    plan = companion_query_plan(
        title=title,
        author_surnames=author_surnames,
        arxiv_id=arxiv_id,
        doi=doi,
        project_names=project_names,
        max_queries=COMPANION_MAX_QUERIES_PER_PHASE,
    )

    discovered_at = timestamp_utc()
    discovery_id = discovery_run_id("companions", [source_id])
    warnings: list[dict[str, str]] = []
    if github_requested and not use_github:
        warnings.append(
            {
                "code": "github_provider_disabled",
                "message": (
                    "Skipped the GitHub companion phase because github is not explicitly listed in "
                    "integrations.discovery.providers."
                ),
            }
        )
    if search_requested and not use_search:
        warnings.append(
            {
                "code": "search_provider_disabled",
                "message": (
                    "Skipped the search companion phase because search is not explicitly listed in "
                    "integrations.discovery.providers."
                ),
            }
        )
    candidates: list[dict[str, Any]] = []
    phases: list[dict[str, Any]] = []

    # --- Phase 1: inline extraction (no network) ---------------------------
    inline_urls = extract_inline_companion_urls(frontmatter, body)
    provider_urls = provider_metadata_companion_urls(metadata)
    inline_count = 0
    for url in inline_urls:
        if is_paper_self_link(url, arxiv_id):
            continue
        candidate = build_companion_candidate(
            url,
            title=None,
            evidence_origin=COMPANION_ORIGIN_PAPER_INLINE,
            seed_source_id=source_id,
            request_id=request_id,
            discovered_at=discovered_at,
            discovery_id=discovery_id,
            seed_title_tokens=seed_title_tokens,
        )
        if candidate is not None:
            candidates.append(candidate)
            inline_count += 1
    provider_count = 0
    for url in provider_urls:
        if is_paper_self_link(url, arxiv_id):
            continue
        candidate = build_companion_candidate(
            url,
            title=None,
            evidence_origin=COMPANION_ORIGIN_PROVIDER_METADATA,
            seed_source_id=source_id,
            request_id=request_id,
            discovered_at=discovered_at,
            discovery_id=discovery_id,
            seed_title_tokens=seed_title_tokens,
        )
        if candidate is not None:
            candidates.append(candidate)
            provider_count += 1
    phases.append(
        {
            "phase": "inline",
            "network_io_executed": False,
            "candidate_count": inline_count + provider_count,
        }
    )

    # --- Phase 2: GitHub repository discovery (network) ---------------------
    if use_github:
        github_queries = plan["github"]
        if github_queries:
            github_raw: list[dict[str, Any]] = []
            for entry in github_queries:
                github_raw.extend(
                    companion_github_candidates(
                        entry["query"],
                        seed_source_id=source_id,
                        request_id=request_id,
                        discovery_id=discovery_id,
                        discovered_at=discovered_at,
                        max_results=COMPANION_GITHUB_MAX_RESULTS,
                    )
                )
            github_phase = dedupe_companion_candidates(github_raw)[:COMPANION_GITHUB_MAX_RESULTS]
            candidates.extend(github_phase)
            phases.append(
                {
                    "phase": "github",
                    "network_io_executed": True,
                    "candidate_count": len(github_phase),
                    "queries": [entry["query"] for entry in github_queries],
                }
            )
        else:
            warnings.append(
                {
                    "code": "no_github_query",
                    "message": "Seed paper has no title; skipping GitHub repository discovery.",
                }
            )
            phases.append({"phase": "github", "network_io_executed": False, "candidate_count": 0})
    else:
        phases.append({"phase": "github", "network_io_executed": False, "candidate_count": 0, "skipped": True})

    # --- Phase 3: general search (network, optional) ------------------------
    if use_search:
        search_queries = plan["search"]
        if not search_queries:
            warnings.append(
                {
                    "code": "no_search_query",
                    "message": "Seed paper has no title; skipping the general-search companion phase.",
                }
            )
            phases.append({"phase": "search", "network_io_executed": False, "candidate_count": 0})
        else:
            search_cfg = search_provider_config(discovery)
            provider = selected_search_provider(search_cfg)
            query_texts = [entry["query"] for entry in search_queries]
            if provider is None:
                warnings.append(
                    {
                        "code": "no_search_provider",
                        "message": (
                            "No search provider configured (integrations.discovery.search.provider is "
                            "unset); skipped the general-search companion phase. Inline and GitHub "
                            "results are still proposed."
                        ),
                    }
                )
                phases.append(
                    {
                        "phase": "search",
                        "network_io_executed": False,
                        "candidate_count": 0,
                        "provider_configured": False,
                        "queries": query_texts,
                    }
                )
            else:
                search_raw: list[dict[str, Any]] = []
                search_network = False
                for entry in search_queries:
                    found, query_network = companion_search_results_for_query(
                        project_root,
                        provider,
                        search_cfg,
                        query=entry["query"],
                        seed_source_id=source_id,
                        request_id=request_id,
                        discovery_id=discovery_id,
                        discovered_at=discovered_at,
                        seed_title_tokens=seed_title_tokens,
                        max_results=COMPANION_SEARCH_MAX_RESULTS,
                    )
                    search_raw.extend(found)
                    search_network = search_network or query_network
                search_phase = dedupe_companion_candidates(search_raw)[:COMPANION_SEARCH_MAX_RESULTS]
                candidates.extend(search_phase)
                phases.append(
                    {
                        "phase": "search",
                        "network_io_executed": search_network,
                        "candidate_count": len(search_phase),
                        "provider_configured": True,
                        "queries": query_texts,
                    }
                )
    else:
        phases.append({"phase": "search", "network_io_executed": False, "candidate_count": 0, "skipped": True})

    # --- Merge, dedupe, rank, cap, store ------------------------------------
    # Global rank+dedupe across phases (collapses an inline GitHub link against its
    # github-search hit; the higher-ranked paper-cited origin wins).
    deduped = dedupe_companion_candidates(candidates)
    cap = min(args.max_results, COMPANION_DISCOVERY_MAX_RESULTS_CAP)
    deduped = deduped[:cap]
    store_path = candidate_store_path(project_root, config)
    written = append_candidates(store_path, deduped)
    network_io = any(phase.get("network_io_executed") for phase in phases)

    if not deduped:
        warnings.append(
            {
                "code": "no_companion_candidates",
                "message": (
                    f"No companion artifacts found for {source_id}. The paper body, frontmatter links, "
                    "provider metadata, GitHub, and search produced no reviewable candidates."
                ),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "companions",
        "command": "companions",
        "generated_at": discovered_at,
        "source_id": source_id,
        "source_kind": record.get("kind"),
        "discovered_by": COMPANIONS_DISCOVERED_BY,
        "discovery_run_id": discovery_id,
        "request_id": request_id,
        "seed_title": title,
        "query_plan": plan,
        "max_results": cap,
        "phases": phases,
        "network_io_executed": network_io,
        "token_used": github_token() is not None,
        "count": len(deduped),
        "candidates": deduped,
        "candidates_path": relative_label(project_root, store_path),
        "written": len(written),
        "warnings": warnings,
    }


def run_discovery_command(args: argparse.Namespace) -> dict[str, Any]:
    validate_command_arguments(args)
    project_root = Path(args.project_root).expanduser().resolve()
    config = load_config(project_root)
    # Candidate review and selection is an offline read/write step: it inspects
    # and updates the durable candidate store, never contacts a provider, and is
    # allowed even when discovery is disabled. So it runs before the network gate.
    if args.command == "candidates":
        return run_candidates_command(project_root, config, args)
    if args.command == "jurisdictions":
        # Jurisdiction profile validation/inspection is an offline step: it reads
        # a workspace-local yml file and never contacts a provider, so it runs
        # before the discovery gate (profiles can be curated with discovery off).
        return run_jurisdictions_command(project_root, config, args)
    discovery = require_discovery_enabled(config, args.command)
    if args.command == "academic":
        for provider in args.provider:
            require_discovery_provider_allowed("academic", discovery, (provider,))
        return run_academic_discovery(project_root, config, discovery, args)
    if args.command == "github":
        require_discovery_provider_allowed("github", discovery, ("github",))
        return run_github_discovery(project_root, config, args)
    if args.command == "search":
        if args.execute:
            require_discovery_provider_allowed("search", discovery, ("search",))
        return run_search_discovery(project_root, config, discovery, args)
    if args.command == "legal":
        # Legal planning is read-only by default; --execute runs the plan through
        # the configured search backend and ranks candidates by officialness
        # (E34-T03). Either way official-source reasoning is recorded per candidate.
        if args.execute:
            require_discovery_provider_allowed("legal", discovery, ("search",))
        return run_legal_discovery(project_root, config, discovery, args)
    if args.command == "standards":
        # Standards registry discovery is fixture-backed in this implementation
        # wave. It emits first-class candidates and never crawls or downloads
        # standards text.
        return run_standards_discovery(project_root, config, discovery, args)
    if args.command == "authors":
        # Author extraction (E35-T01) is the read-only preparation path: it reads a
        # normalized paper source and any provider author metadata and emits a
        # bounded author seed list. With --discover-publications (E35-T02) it also
        # resolves each author to an OpenAlex identity and proposes that author's
        # works as related-publication candidates.
        if args.discover_publications:
            require_discovery_provider_allowed("authors", discovery, ("openalex",))
        return run_authors_discovery(project_root, config, args)
    if args.command == "companions":
        # Companion artifact discovery (E35-T03): a paper-centered composite that
        # prefers links already in the paper/provider metadata, then GitHub and the
        # configured search provider, to propose repositories, datasets, project
        # pages, supplemental material, and publisher pages for review.
        return run_companions_discovery(project_root, config, discovery, args)
    raise DiscoverSourcesError(
        "NOT_IMPLEMENTED",
        f"discovery {args.command} provider transport is not implemented.",
        remediation=(
            "Use a discovery command whose provider transport is implemented, "
            "or add the missing adapter first (see docs/source-discovery.md)."
        ),
        details={"command": args.command, "network_io_executed": False},
    )


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=False)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.output_format == "json")
    try:
        report = run_discovery_command(args)
    except DiscoverSourcesError as exc:
        emit_error(
            exc.message,
            json_mode=json_mode,
            error_code=exc.error_code,
            recoverable=exc.recoverable,
            remediation=exc.remediation,
            details=exc.details,
        )
        return EXIT_INVALID
    except LockUnavailableError as exc:
        emit_error(
            str(exc),
            json_mode=json_mode,
            error_code=exc.error_code,
            details=exc.details,
        )
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    if args.output_format == "json":
        print(compact_json(report))
    else:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=False) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
