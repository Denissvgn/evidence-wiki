"""Read-only revision boundaries shared by pack, run and computation owners."""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

import yaml
from _evidence_revision import capture_workspace, content_id, observation, read_observed_file
from _record_artifacts import json_document
from _script_errors import ScriptRefusal
from _workspace_module_loader import load_workspace_module

SCRIPT_DIR = Path(__file__).resolve().parent
_MODULES = {}


def sibling(name):
    return load_workspace_module(SCRIPT_DIR, name, cache=_MODULES)


def require(condition, reason):
    if not condition:
        raise ScriptRefusal("DOMAIN_PACK_REVISION_CONFLICT", "Pack revision requirements were not satisfied.",
            exit_code=3, recoverable=False, details={"reason": reason},
            remediation="Preserve pending work and its original inputs. Finish or explicitly abandon it before revising requirements.")


def inputs(capture):
    """Locks have no evidence content; all other retained files are preconditions."""
    return content_id("evidence-pack-revision-inputs/v1", {
        name: hashlib.sha256(raw).hexdigest() for name, raw in capture.files.items()
        if research_input(name)})


def research_input(name):
    return ".locks" not in Path(name).parts and name != "runs/computation/operation.lock" and not name.startswith(".replaced/domain-packs/")


def controls(root):
    capture = capture_workspace(root)
    names = {"AGENTS.md", "workspace-system.yml", ".evidence-wiki.yml"}
    selected = {name: hashlib.sha256(raw).hexdigest() for name, raw in capture.files.items()
                if name in names or name.startswith(("domain-packs/", "scripts/", "skills/", "docs/"))}
    try:
        config = yaml.safe_load(capture.files["research.yml"])
    except yaml.YAMLError:
        require(False, "revision_configuration_invalid")
    require(isinstance(config, dict), "revision_configuration_invalid")
    selected["requirements"] = {key: config.get(key) for key in ("domain_pack", "computation", "strict_evidence", "evidence_trust")}
    return content_id("evidence-pack-requirements/v1", selected)


def idle(root, *, capture=None):
    """Missing historical bindings never make a live action safe to rebase."""
    root = Path(root)
    capture = capture or capture_workspace(root)
    setup_idle(root, capture)
    for name, raw in capture.files.items():
        parts = Path(name).parts
        if len(parts) == 4 and parts[:2] == ("runs", "orchestrations") and parts[-1] == "session.json":
            session = json_document(raw)
            require(session.get("status") in {"complete", "failed", "blocked_on_sources", "no_ship", "active", "paused"},
                    "managed_session_state_unknown")
            require(session["status"] not in {"active", "paused"} and not session.get("pending_action_id"), "managed_session_active")
        if len(parts) == 3 and parts[0] == "runs" and parts[-1] == "run-state.json":
            run = json_document(raw)
            require(run.get("state", {}).get("current") in sibling("run_controller").TERMINAL_STATES
                    and not run.get("_pending_event"), "child_run_active_or_unbound")
    computation, _ = sibling("_computation_effects").state_from(capture)
    require(not any(row["status"] == "pending" for row in computation["requests"].values()), "computation_recovery_pending")
    # Claims outside a run are still active research.
    config = sibling("_domain_pack_lifecycle").load_mapping(root / "research.yml", "configuration")
    q = sibling("question_status")
    prefix = q.questions_directory(root, config).relative_to(root).as_posix() + "/"
    for name, raw in capture.files.items():
        if Path(name).parent.as_posix() + "/" == prefix and name.endswith(".md"):
            fm = q.frontmatter_from_text(raw.decode("utf-8")) or {}
            require(fm.get("type") != "question" or fm.get("status") != "in_progress", "question_claim_active")
    return inputs(capture)


def setup_idle(root, capture):
    raw = capture.files.get("docs/research-requirements.json")
    if raw is None:
        return
    frozen = json_document(raw)
    require(frozen.get("schema_version") == "evidence-research-requirements/v1", "revision_setup_requirements_unknown")
    target = frozen["request"]["payload"]["target"]
    parent = Path(target["writable_root"]).resolve()
    require((parent / target["relative_path"]).resolve() == root and root.is_relative_to(parent), "revision_setup_target_changed")
    key = hashlib.sha256("\0".join(unicodedata.normalize("NFC", target[k]).casefold()
                                 for k in ("writable_root", "relative_path")).encode()).hexdigest()
    folder = parent / ".evidence-wiki/setup/targets" / key / "transactions"
    # The setup owner is the only writer; this is a bounded passive boundary check.
    require(folder.is_dir() and not folder.is_symlink(), "revision_setup_state_missing")
    transactions = list(folder.iterdir())
    require(len(transactions) == 1 and transactions[0].is_dir() and not transactions[0].is_symlink(), "revision_setup_state_ambiguous")
    path = transactions[0] / "checkpoint.json"
    checkpoint = json_document(read_observed_file(parent, path.relative_to(parent).as_posix(), observation(path.lstat())))
    require(checkpoint.get("schema_version") == "evidence-setup-checkpoint/v1" and checkpoint.get("state") == "complete"
            and checkpoint.get("pending") is None, "revision_setup_incomplete")


def latest(state, slug):
    for record in reversed(state.get("research_revisions", [])):
        if any(row["slug"] == slug for row in record["impact"]["questions"]):
            return record
    return None


def pending_question(root, slug, manifest=None):
    path = Path(root) / "domain-packs/.evidence-wiki-state.yml"
    if not path.exists():
        return None
    state = sibling("_domain_pack_lifecycle").load_state(Path(root))
    record = latest(state, slug)
    if record is None:
        return None
    basis = (manifest or {}).get("revision_basis", {})
    if basis.get("revision_id") == record["revision_id"]:
        verify_migration(root, slug, record, basis)
        return None
    return record["revision_id"]


def verify_migration(root, slug, record, basis):
    """One archive proof for both current eligibility and carried obligations."""
    sibling("_coverage_revision").validate_basis(basis)
    expected = "runs/pack-revisions/" + record["revision_id"][7:] + "/" + slug + "-" + basis["migration_id"][7:] + ".json"
    require(basis["archive"] == expected, "revision_archive_path_invalid")
    path = Path(root) / expected
    raw = read_observed_file(Path(root), expected, observation(path.lstat()))
    require("sha256:" + hashlib.sha256(raw).hexdigest() == basis["archive_sha256"], "revision_archive_changed")
    history = json_document(raw)
    require(history.get("schema_version") == "evidence-coverage-history/v1"
            and sibling("_pack_revision_impact").digest(history["request"]) == basis["migration_id"]
            and history["request"]["revision_id"] == record["revision_id"] and history["request"]["slug"] == slug, "revision_archive_mismatch")
