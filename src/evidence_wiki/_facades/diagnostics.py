"""``ws.diagnostics`` -- workspace health, lint, and smoke checks.

Also the home of :func:`fleet_status`, which is a *module-level* function rather
than a method. Fleet status aggregates across many workspaces at once, so there
is no single handle it could hang off; it takes target paths directly, the way
``evidence-wiki fleet-status --target PATH --target PATH`` does. It lives beside
the per-workspace diagnostics because it is the same concern at fleet scope, and
``evidence_wiki.__init__`` re-exports it lazily under its declared contract name.

Add methods here rather than in a shared module: parallel work units fill
sibling namespaces, and one shared module would put them all in each other's
diff.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .._script_host import load_packaged_script, shared_assets_root
from ._base import Namespace, call_seam


class DiagnosticsNamespace(Namespace):
    """Diagnostic operations for the owning workspace."""

    def doctor(self) -> dict[str, Any]:
        """Diagnose this workspace's runtime, tooling, and configuration.

        Returns exactly the report ``evidence-wiki doctor --format json`` prints.
        Reached as ``ws.doctor()`` too, which is the name the published
        capability contract declares.

        **Almost nothing refuses here, by design.** A contract breach is report
        content, not an exception: every domain error is folded into the
        ``check_item`` that provoked it, and a workspace the doctor cannot read
        at all comes back as ``verdict: "missing"`` with a full report. Diagnosing
        a broken workspace is the reason to call this, so it is not a reason to
        withhold the diagnosis. Callers branch on ``report["verdict"]``.

        The script's ``env`` injection point is deliberately **not** exposed.
        ``DoctorEnvironment`` is defined inside a packaged script asset rather
        than in this package, so a host has no supported way to name the type it
        would have to construct; it exists for the doctor's own tests, which
        reach the seam directly. Exposing it would publish a parameter whose only
        legal value is one the public API cannot hand out.

        Raises:
            ConfigError: if this handle is closed, or the packaged doctor script
                cannot be loaded.
        """
        return self._call("doctor", "run_doctor", self._root)


def fleet_status(targets: Sequence[str | Path], *, no_cache: bool = False) -> dict[str, Any]:
    """Summarize many workspaces in one report.

    Returns exactly the document ``evidence-wiki fleet-status --format json``
    prints. ``targets`` mirrors repeated ``--target`` and takes workspace paths
    directly rather than open handles, because this is a fleet-wide read that no
    single handle owns. ``no_cache`` mirrors ``--no-cache``.

    **A target this cannot read is reported, never raised.** It comes back as an
    entry with ``ok: False`` carrying that target's own ``error_code`` and
    message, so ten good workspaces and one bad path yield eleven answers rather
    than one exception -- that degradation is the contract of this call, and the
    reason an operator can point it at a whole fleet without pre-validating it.
    Read ``report["counts"]["errors"]`` for the tally.

    Raises:
        EvidenceWikiError: only if the packaged scripts themselves cannot be
            loaded, which is an installation problem rather than a fleet finding.
    """
    # No handle owns a fleet read, so the loader is bound here rather than
    # borrowed from a Namespace; ``call_seam`` still guards the load itself.
    def load(stem: str):
        return load_packaged_script(shared_assets_root(), stem)

    return call_seam(load, "fleet_status", "run_fleet_status", targets, no_cache=no_cache)
