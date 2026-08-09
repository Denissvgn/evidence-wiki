#!/usr/bin/env python3
"""Entry-point registry for third-party providers, validated structurally.

A workspace's provider allow-lists are authorization boundaries (see
``_provider_registry``), and until now the universe of ids they could name was
closed: a source family this package never heard of could not exist in a
workspace without forking it. Registration opens that universe *behind a
declaration*. A pip-installed distribution advertises a provider through one of
the ``evidence_wiki.*_providers`` entry-point groups, and the object it names
must state, machine-checkably, which domains it may reach, which credentials it
needs by name, and what the package does with what it fetches.

Registration makes a provider **available**, never **enabled**: ``research.yml``
still authorizes by explicit id, exactly as it does for the built-ins.

Three rules shape everything below.

**Validation is structural, never nominal.** A provider is judged by the
attributes it carries and the values in them, never by ``isinstance`` against
the package's own classes, and this module never imports ``evidence_wiki``.
Workspace scripts must keep running where only the scripts exist, and a plugin
authored against a different release must not become invalid because a base
class moved. So a provider written without the package installed at all is valid
when it matches the shape, and a provider that subclasses the published base
classes is invalid when it breaks a value rule.

**Enumeration never raises.** A distribution that fails to import, declares a
reserved id, or violates a capability rule becomes an :class:`InvalidRegistration`
carrying its reason. Dropping it silently would make a broken plugin
indistinguishable from an uninstalled one at the exact moment an operator needs
to tell them apart; raising would let one bad plugin take down every unrelated
command. Both failures stay visible: ``doctor`` lists them through
:func:`registration_report`, and :func:`require_registration` refuses with a
message that says which of the two happened.

**Duplicate ids fail closed on both sides.** When two distributions claim one
id, neither is used. First-wins would make a workspace's behaviour depend on
installation order, which is not a thing an audit trail can record.

An entry point may name either a class or a ready-made object. A class is
instantiated once, with no arguments, and the instance is what the rest of the
package calls; a provider that cannot be constructed that way is invalid rather
than half-loaded. Reading capabilities from the instance is what lets a provider
compute its declaration in ``__init__`` instead of hard-coding it as a class
attribute.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
# The reserved-id set is the built-in allow-list itself, read from the module that owns
# it, so a registration can never shadow a provider this package already ships. The
# dependency runs one way only: the low-level validator never imports this module.
from _provider_registry import (  # noqa: E402
    ACQUISITION_PROVIDER_IDS,
    DISCOVERY_PROVIDER_IDS,
    LEGACY_DISCOVERY_STRATEGY_IDS,
)

ACQUISITION_PHASE = "acquisition"
DISCOVERY_PHASE = "discovery"
PROVIDER_PHASES = (ACQUISITION_PHASE, DISCOVERY_PHASE)

ENTRY_POINT_GROUPS = {
    ACQUISITION_PHASE: "evidence_wiki.acquisition_providers",
    DISCOVERY_PHASE: "evidence_wiki.discovery_providers",
}

# v1 of the declared contract. A provider that predates or postdates it is refused with
# its version named rather than validated against rules it never agreed to.
SUPPORTED_PROVIDER_API_VERSIONS = (1,)

PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Bare lowercase hostnames, matching the shape rules the web provider's configured
# allow-list already enforces: no scheme, no path, no port, no trailing dot.
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
CREDENTIAL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

LICENSE_INFERENCE_VALUES = ("yes", "partial", "none")
RATE_LIMIT_WINDOWS = ("minute", "hour")
TERMS_URL_PREFIX = "https://"
TERMS_URL_PER_ORIGIN = "per-origin"

CAPABILITY_FIELDS = (
    "allowed_domains",
    "terms_urls",
    "license_inference",
    "captures_raw",
    "quarantine_on_incomplete",
    "rate_limit",
    "credentials",
    "request_kinds",
)
REGISTRATION_BLOCK_FIELDS = (
    "id",
    "phase",
    "distribution",
    "version",
    "entry_point",
    "provider_api_version",
)
REQUIRED_PROVIDER_METHODS = {
    ACQUISITION_PHASE: ("validate_request", "plan_fetch", "interpret"),
    DISCOVERY_PHASE: ("validate_request", "plan_search", "interpret_candidates"),
}

# Built-in ids, the standards:* family, and the legacy strategy aliases can never be
# shadowed in either direction; third-party ids need no namespace of their own.
RESERVED_PROVIDER_IDS = frozenset(
    (
        *DISCOVERY_PROVIDER_IDS,
        *ACQUISITION_PROVIDER_IDS,
        *LEGACY_DISCOVERY_STRATEGY_IDS,
    )
)

UNKNOWN_DISTRIBUTION = "<unknown distribution>"
UNKNOWN_VERSION = "<unknown version>"

PROVIDER_PLUGIN_REMEDIATION = (
    "Install a distribution that registers the provider, or remove the provider from the "
    "research.yml allow-list until one is installed."
)


class ProviderPluginError(Exception):
    """Structured registration failure carrying a stable machine-readable code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.remediation = remediation or PROVIDER_PLUGIN_REMEDIATION
        self.details = details or {}


@dataclass(frozen=True)
class CapabilitySummary:
    """JSON-safe mirror of a validated declaration, recorded in provenance.

    Credentials are the declared environment-variable *names*. A value never enters
    this summary, so nothing that reads it — provenance sidecars, doctor output, an
    error detail — can leak one.
    """

    allowed_domains: tuple[str, ...]
    terms_urls: tuple[str, ...]
    license_inference: str
    captures_raw: bool
    quarantine_on_incomplete: bool
    rate_limit: dict[str, Any] | None
    credentials: tuple[str, ...]
    request_kinds: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return the declaration as plain JSON-safe types, keys in declaration order."""
        return {
            "allowed_domains": list(self.allowed_domains),
            "terms_urls": list(self.terms_urls),
            "license_inference": self.license_inference,
            "captures_raw": self.captures_raw,
            "quarantine_on_incomplete": self.quarantine_on_incomplete,
            "rate_limit": dict(self.rate_limit) if self.rate_limit is not None else None,
            "credentials": list(self.credentials),
            "request_kinds": list(self.request_kinds),
        }


@dataclass(frozen=True)
class Registration:
    """One valid provider registration and the packaging identity that supplied it."""

    provider_id: str
    phase: str
    provider: Any
    distribution: str
    version: str
    entry_point: str
    provider_api_version: int
    capabilities: CapabilitySummary

    def registration_block(self) -> dict[str, Any]:
        """Return the identity block stamped into provenance for acquired evidence."""
        return {
            "id": self.provider_id,
            "phase": self.phase,
            "distribution": self.distribution,
            "version": self.version,
            "entry_point": self.entry_point,
            "provider_api_version": self.provider_api_version,
        }


@dataclass(frozen=True)
class InvalidRegistration:
    """A registration that exists but cannot be used, and why.

    ``provider_id`` is ``None`` when the failure happened before an id could be read —
    an entry point that does not import has no declaration to name.
    """

    provider_id: str | None
    phase: str
    distribution: str
    entry_point: str
    reason: str


def _entry_point_group(phase: str) -> str:
    group = ENTRY_POINT_GROUPS.get(phase)
    if group is None:  # pragma: no cover - internal programming guard
        raise ValueError(f"unknown provider phase: {phase}")
    return group


def _entry_points(group: str) -> tuple[Any, ...]:
    """Return the installed entry points in one group.

    The single seam every other function consumes, and the one place tests patch.
    ``importlib.invalidate_caches()`` runs first because a distribution installed (or
    put on ``sys.path``) after this interpreter started is invisible to the metadata
    finders until their path caches are dropped.
    """
    importlib.invalidate_caches()
    return tuple(importlib.metadata.entry_points(group=group))


def _one_line(value: object) -> str:
    """Collapse an arbitrary message into a single line for a reason string."""
    return " ".join(str(value).split())


def _exception_reason(exc: BaseException) -> str:
    return _one_line(f"{type(exc).__name__}: {exc}")


def _distribution_identity(entry_point: Any) -> tuple[str, str]:
    """Return the (name, version) of the distribution advertising an entry point."""
    distribution = getattr(entry_point, "dist", None)
    name = _distribution_attribute(distribution, "name")
    version = _distribution_attribute(distribution, "version")
    return name or UNKNOWN_DISTRIBUTION, version or UNKNOWN_VERSION


def _distribution_attribute(distribution: Any, attribute: str) -> str | None:
    if distribution is None:
        return None
    try:
        value = getattr(distribution, attribute, None)
    except Exception:  # pragma: no cover - unreadable metadata is still enumerable
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_sequence(value: Any) -> tuple[str, ...] | None:
    """Return ``value`` as a tuple of strings, or ``None`` when it is not one.

    A bare string is refused deliberately: ``allowed_domains="api.keepa.com"`` would
    otherwise iterate into single characters and declare an allow-list of letters.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _duplicates(values: tuple[str, ...]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def _capability_summary(capabilities: Any) -> tuple[CapabilitySummary | None, list[str]]:
    """Validate a declaration field by field; return its summary or every violation.

    Fields the contract gives a default keep that default when absent; fields it
    requires are violations when absent. Reading through :func:`getattr` rather than
    the dataclass is what admits a provider authored without this package installed.
    """
    if isinstance(capabilities, dict):
        # The likeliest authoring slip, and unhelpful to report as eight missing fields.
        return None, ["capabilities must be a declaration object with attributes, not a mapping"]

    errors: list[str] = []

    domains = _string_sequence(getattr(capabilities, "allowed_domains", None))
    if domains is None:
        errors.append("capabilities.allowed_domains must be a list or tuple of domain names")
    elif not domains:
        errors.append("capabilities.allowed_domains must declare at least one domain")
    else:
        invalid_domains = [domain for domain in domains if DOMAIN_RE.fullmatch(domain) is None]
        if invalid_domains:
            errors.append(
                "capabilities.allowed_domains must be bare lowercase hostnames; "
                f"invalid: {', '.join(invalid_domains)}"
            )
        duplicate_domains = _duplicates(domains)
        if duplicate_domains:
            errors.append(f"capabilities.allowed_domains has duplicate domain(s): {', '.join(duplicate_domains)}")

    terms_urls = _string_sequence(getattr(capabilities, "terms_urls", None))
    if terms_urls is None:
        errors.append("capabilities.terms_urls must be a list or tuple of terms URLs")
    elif not terms_urls:
        errors.append("capabilities.terms_urls must declare at least one terms URL")
    else:
        invalid_terms = [
            value
            for value in terms_urls
            if value != TERMS_URL_PER_ORIGIN
            and not (value.startswith(TERMS_URL_PREFIX) and len(value) > len(TERMS_URL_PREFIX))
        ]
        if invalid_terms:
            errors.append(
                f"capabilities.terms_urls entries must be https:// URLs or {TERMS_URL_PER_ORIGIN!r}; "
                f"invalid: {', '.join(invalid_terms)}"
            )

    license_inference = getattr(capabilities, "license_inference", None)
    if license_inference not in LICENSE_INFERENCE_VALUES:
        errors.append(
            f"capabilities.license_inference must be one of: {', '.join(LICENSE_INFERENCE_VALUES)}"
        )

    # The package's own writer provides both properties unconditionally, so a declared
    # False is a contract this package will not honour rather than a supported mode.
    for field in ("captures_raw", "quarantine_on_incomplete"):
        if getattr(capabilities, field, True) is not True:
            errors.append(f"capabilities.{field} must be True in provider API version 1")

    rate_limit_summary: dict[str, Any] | None = None
    rate_limit = getattr(capabilities, "rate_limit", None)
    if rate_limit is not None:
        requests = getattr(rate_limit, "requests", None)
        per = getattr(rate_limit, "per", None)
        rate_limit_errors: list[str] = []
        if not isinstance(requests, int) or isinstance(requests, bool) or requests < 1:
            rate_limit_errors.append("capabilities.rate_limit.requests must be an integer of at least 1")
        if per not in RATE_LIMIT_WINDOWS:
            rate_limit_errors.append(
                f"capabilities.rate_limit.per must be one of: {', '.join(RATE_LIMIT_WINDOWS)}"
            )
        errors.extend(rate_limit_errors)
        if not rate_limit_errors:
            rate_limit_summary = {"requests": int(requests), "per": per}

    credentials = _string_sequence(getattr(capabilities, "credentials", ()))
    if credentials is None:
        errors.append("capabilities.credentials must be a list or tuple of environment variable names")
        credentials = ()
    else:
        invalid_credentials = [name for name in credentials if CREDENTIAL_NAME_RE.fullmatch(name) is None]
        if invalid_credentials:
            errors.append(
                "capabilities.credentials must be environment variable names matching "
                f"[A-Z][A-Z0-9_]*; invalid: {', '.join(invalid_credentials)}"
            )

    request_kinds = _string_sequence(getattr(capabilities, "request_kinds", ()))
    if request_kinds is None:
        errors.append("capabilities.request_kinds must be a list or tuple of request kind ids")
        request_kinds = ()
    elif any(not kind.strip() or kind.strip() != kind for kind in request_kinds):
        errors.append("capabilities.request_kinds entries must be non-empty ids without surrounding whitespace")
    else:
        # Checked like allowed_domains and credentials above. The field is recorded rather
        # than routed in v1, but it is the declared seam for CR-4 kind routing, where a
        # repeated id stops being cosmetic and becomes an ambiguity to resolve.
        duplicate_kinds = _duplicates(request_kinds)
        if duplicate_kinds:
            errors.append(f"capabilities.request_kinds has duplicate id(s): {', '.join(duplicate_kinds)}")

    if errors:
        return None, errors

    return (
        CapabilitySummary(
            allowed_domains=tuple(domains or ()),
            terms_urls=tuple(terms_urls or ()),
            license_inference=str(license_inference),
            captures_raw=True,
            quarantine_on_incomplete=True,
            rate_limit=rate_limit_summary,
            credentials=tuple(credentials),
            request_kinds=tuple(request_kinds),
        ),
        [],
    )


def _provider_id_errors(provider_id: Any) -> list[str]:
    if not isinstance(provider_id, str) or not provider_id:
        return ["provider must declare a non-empty string id"]
    # Reserved first: standards:nist fails the id pattern too, and "you shadowed a
    # built-in" is the reason that tells the author what to actually change.
    if provider_id in RESERVED_PROVIDER_IDS:
        return [f"provider id {provider_id!r} is reserved for a built-in provider or strategy alias"]
    if PROVIDER_ID_RE.fullmatch(provider_id) is None:
        return [f"provider id {provider_id!r} must match {PROVIDER_ID_RE.pattern}"]
    return []


def _method_errors(provider: Any, phase: str) -> list[str]:
    errors: list[str] = []
    for name in REQUIRED_PROVIDER_METHODS[phase]:
        attribute = getattr(provider, name, None)
        if attribute is None:
            errors.append(f"provider must define {name}() for the {phase} phase")
        elif not callable(attribute):
            errors.append(f"provider attribute {name} must be callable")
    return errors


def _validate_loaded(
    loaded: Any, phase: str
) -> tuple[str | None, Any, int | None, CapabilitySummary | None, list[str]]:
    """Validate one loaded entry-point object; return what could be read plus violations."""
    raw_id = getattr(loaded, "id", None)
    errors = _provider_id_errors(raw_id)
    provider_id = raw_id if isinstance(raw_id, str) and raw_id else None

    provider = loaded
    if inspect.isclass(loaded):
        try:
            provider = loaded()
        except Exception as exc:
            errors.append(f"provider class could not be instantiated with no arguments: {_exception_reason(exc)}")
            return provider_id, None, None, None, errors

    api_version = getattr(provider, "provider_api_version", None)
    if not isinstance(api_version, int) or isinstance(api_version, bool):
        errors.append("provider must declare an integer provider_api_version")
        api_version = None
    elif api_version not in SUPPORTED_PROVIDER_API_VERSIONS:
        supported = ", ".join(str(version) for version in SUPPORTED_PROVIDER_API_VERSIONS)
        errors.append(f"provider_api_version {api_version} is not supported; this workspace supports: {supported}")
        api_version = None

    capabilities = getattr(provider, "capabilities", None)
    if capabilities is None:
        errors.append("provider must declare capabilities")
        summary = None
    else:
        summary, capability_errors = _capability_summary(capabilities)
        errors.extend(capability_errors)

    errors.extend(_method_errors(provider, phase))
    return provider_id, provider, api_version, summary, errors


def _entry_point_name(entry_point: Any) -> str:
    try:
        name = getattr(entry_point, "name", "")
    except Exception:  # pragma: no cover - an entry point too broken to name itself
        return ""
    return name if isinstance(name, str) else ""


def _registration_or_reason(entry_point: Any, phase: str) -> Registration | InvalidRegistration:
    """Turn one entry point into a registration, or into the reason it is unusable."""
    name = _entry_point_name(entry_point)
    distribution, version = _distribution_identity(entry_point)

    try:
        loaded = entry_point.load()
    except Exception as exc:
        return InvalidRegistration(
            provider_id=None,
            phase=phase,
            distribution=distribution,
            entry_point=name,
            reason=f"entry point failed to load: {_exception_reason(exc)}",
        )

    try:
        provider_id, provider, api_version, summary, errors = _validate_loaded(loaded, phase)
    except Exception as exc:
        # Reading a declaration runs plugin code — a property that raises, a descriptor
        # with side effects. Refuse this one registration, with its identity intact.
        return InvalidRegistration(
            provider_id=None,
            phase=phase,
            distribution=distribution,
            entry_point=name,
            reason=f"provider declaration could not be read: {_exception_reason(exc)}",
        )

    if errors or provider_id is None or api_version is None or summary is None:
        return InvalidRegistration(
            provider_id=provider_id,
            phase=phase,
            distribution=distribution,
            entry_point=name,
            reason="; ".join(errors) or "provider declaration is incomplete",
        )

    return Registration(
        provider_id=provider_id,
        phase=phase,
        provider=provider,
        distribution=distribution,
        version=version,
        entry_point=name,
        provider_api_version=api_version,
        capabilities=summary,
    )


def _load_phase(phase: str) -> tuple[dict[str, Registration], tuple[InvalidRegistration, ...]]:
    group = _entry_point_group(phase)
    candidates: list[Registration] = []
    invalid: list[InvalidRegistration] = []

    try:
        entry_points = _entry_points(group)
    except Exception as exc:
        # Unreadable installed metadata must not take down commands that never asked
        # for a registered provider; report it as one unusable registration instead.
        return {}, (
            InvalidRegistration(
                provider_id=None,
                phase=phase,
                distribution=UNKNOWN_DISTRIBUTION,
                entry_point="",
                reason=f"entry-point enumeration failed for {group}: {_exception_reason(exc)}",
            ),
        )

    for entry_point in entry_points:
        try:
            outcome = _registration_or_reason(entry_point, phase)
        except Exception as exc:  # pragma: no cover - last-resort never-raise backstop
            # One plugin's failure is one unusable registration, never a crashed command
            # for every other provider in the workspace.
            outcome = InvalidRegistration(
                provider_id=None,
                phase=phase,
                distribution=UNKNOWN_DISTRIBUTION,
                entry_point=_entry_point_name(entry_point),
                reason=f"provider declaration could not be read: {_exception_reason(exc)}",
            )
        if isinstance(outcome, Registration):
            candidates.append(outcome)
        else:
            invalid.append(outcome)

    claimed: dict[str, list[Registration]] = {}
    for registration in candidates:
        claimed.setdefault(registration.provider_id, []).append(registration)

    valid: dict[str, Registration] = {}
    for provider_id, claims in claimed.items():
        if len(claims) == 1:
            valid[provider_id] = claims[0]
            continue
        for claim in claims:
            others = ", ".join(
                f"{other.distribution} (entry point {other.entry_point})" for other in claims if other is not claim
            )
            invalid.append(
                InvalidRegistration(
                    provider_id=provider_id,
                    phase=phase,
                    distribution=claim.distribution,
                    entry_point=claim.entry_point,
                    reason=(
                        f"provider id {provider_id!r} is also registered by {others}; "
                        "every claim on a duplicated id is refused"
                    ),
                )
            )

    ordered_invalid = tuple(
        sorted(invalid, key=lambda item: (item.distribution, item.entry_point, item.provider_id or ""))
    )
    return dict(sorted(valid.items())), ordered_invalid


_REGISTRATION_CACHE: dict[str, tuple[dict[str, Registration], tuple[InvalidRegistration, ...]]] = {}


def load_registrations(phase: str) -> tuple[dict[str, Registration], tuple[InvalidRegistration, ...]]:
    """Return ``(valid registrations by id, invalid registrations)`` for one phase.

    Never raises for a plugin's sake: every failure is an :class:`InvalidRegistration`.
    Results are cached for the life of the process because entry points cannot change
    under a running command; :func:`clear_cache` exists for tests.
    """
    _entry_point_group(phase)
    cached = _REGISTRATION_CACHE.get(phase)
    if cached is None:
        cached = _load_phase(phase)
        _REGISTRATION_CACHE[phase] = cached
    valid, invalid = cached
    # A copy, so a caller mutating its result cannot poison the process-wide cache.
    return dict(valid), invalid


def registered_ids(phase: str) -> tuple[str, ...]:
    """Return the sorted ids validly registered for one phase; ``()`` when none are."""
    valid, _invalid = load_registrations(phase)
    return tuple(valid)


def require_registration(phase: str, provider_id: str) -> Registration:
    """Return the registration for ``provider_id``, or refuse with a stable code.

    The two refusals are deliberately distinct: nothing supplies the id
    (``PROVIDER_NOT_REGISTERED``, fix by installing), or something does and its
    declaration is broken (``PROVIDER_REGISTRATION_INVALID``, fix by upgrading the
    named distribution). Telling an operator to install what is already installed is
    the failure mode this split exists to prevent.
    """
    group = _entry_point_group(phase)
    valid, invalid = load_registrations(phase)
    registration = valid.get(provider_id)
    if registration is not None:
        return registration

    # An entry point conventionally carries the provider id as its name, so a broken
    # entry point named for the requested id is the same provider, not a coincidence.
    matches = [
        item
        for item in invalid
        if item.provider_id == provider_id or (item.provider_id is None and item.entry_point == provider_id)
    ]
    if matches:
        reasons = "; ".join(f"{item.distribution}: {item.reason}" for item in matches)
        distributions = ", ".join(sorted({item.distribution for item in matches}))
        raise ProviderPluginError(
            "PROVIDER_REGISTRATION_INVALID",
            f"Provider {provider_id!r} is registered for {phase} but its declaration is invalid: {reasons}",
            remediation=(
                f"Upgrade or fix {distributions} so its {group} entry point satisfies the provider "
                f"contract, or remove {provider_id!r} from the {phase} provider list in research.yml."
            ),
            details={
                "provider_id": provider_id,
                "phase": phase,
                "entry_point_group": group,
                "distributions": sorted({item.distribution for item in matches}),
                "reasons": [item.reason for item in matches],
            },
        )

    available = ", ".join(valid) or "none"
    raise ProviderPluginError(
        "PROVIDER_NOT_REGISTERED",
        f"Provider {provider_id!r} is not registered for {phase}. Registered providers: {available}.",
        remediation=(
            f"Install a distribution that registers {provider_id!r} in the {group} entry-point group, "
            f"or remove it from the {phase} provider list in research.yml."
        ),
        details={
            "provider_id": provider_id,
            "phase": phase,
            "entry_point_group": group,
            "registered": list(valid),
        },
    )


def registration_report() -> dict[str, Any]:
    """Return a JSON-safe view of both phases for doctor: what is available, and what broke."""
    report: dict[str, Any] = {}
    for phase in PROVIDER_PHASES:
        valid, invalid = load_registrations(phase)
        report[phase] = {
            "entry_point_group": ENTRY_POINT_GROUPS[phase],
            "registered": [
                {
                    **registration.registration_block(),
                    "capabilities": registration.capabilities.as_dict(),
                }
                for registration in valid.values()
            ],
            "invalid": [
                {
                    "id": item.provider_id,
                    "phase": item.phase,
                    "distribution": item.distribution,
                    "entry_point": item.entry_point,
                    "reason": item.reason,
                }
                for item in invalid
            ],
        }
    return report


def clear_cache() -> None:
    """Drop the per-process registration cache. Tests only; entry points are static."""
    _REGISTRATION_CACHE.clear()
