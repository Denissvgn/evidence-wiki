"""Assessment capture qualifies complete approved source artifacts before materialization."""

from pathlib import Path

import pytest

from tests._execution_fixture import canonical
from tests._script_loader import load_isolated_module
from tests._usage_fixture import UsageFixture

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"


@pytest.fixture
def capture(tmp_path, monkeypatch):
    host = UsageFixture(tmp_path, monkeypatch)
    usage = load_isolated_module("publication_usage_store", SCRIPTS / "_evidence_usage.py")
    publication = load_isolated_module("publication_usage_capture", SCRIPTS / "_publication_usage.py")
    host.transact(usage, "initialize")

    def qualify(raw, *, paths, provenance=None, additional=None, changed=None, normalized=None):
        body, originals = host.source(evidence=raw, normalized=normalized)
        host.transact(usage, "deposit", body, originals)
        record = {"id": body["source_id"], "raw_paths": paths, "usage_revision_id": body["source_revision"]}
        if provenance is not None:
            record["provenance"] = provenance
        if additional is not None:
            record["additional_provenance"] = additional
        files = {**raw, "sources/manifest.jsonl": canonical(record),
                 "sources/normalized/lab--sample.md": originals["normalized.md"]}
        if changed is not None:
            changed(files)
        with usage.current_view(host.root, host.config) as view:
            return publication.qualify_capture(files, host.config, view, purpose="research", consumer="evidence-wiki")

    return qualify


@pytest.mark.parametrize("layout", ["file", "directory", "legacy-sidecar", "multiple-captures"])
def test_approved_source_layouts_include_sidecars_and_complete_subtrees(capture, layout):
    path = "raw/papers/paper.txt"
    paths = [path]
    raw = {path: b"Sanitized observations.\n", path + ".provenance.yml": b"license: MIT\n"}
    provenance = additional = None
    if layout == "directory":
        paths = ["raw/code/project"]
        raw = {"raw/code/project/main.py": b"value = 1\n", "raw/code/project/sub/data.json": b"{}\n",
               "raw/code/project.provenance.yml": b"license: MIT\n"}
    elif layout == "legacy-sidecar":
        sidecar = "raw/papers/paper.provenance.yml"
        raw[sidecar] = raw.pop(path + ".provenance.yml")
        provenance = {"sidecar_path": sidecar}
    elif layout == "multiple-captures":
        extra = "raw/papers/other.txt"
        paths.append(extra)
        raw.update({extra: b"Second capture.\n", "raw/papers/other.provenance.yml": b"license: MIT\n"})
        additional = [{"path": extra, "sidecar_path": "raw/papers/other.provenance.yml"}]
    assert capture(raw, paths=paths, provenance=provenance, additional=additional)["complete"]


@pytest.mark.parametrize("defect", ["changed", "missing", "extra", "sibling-prefix", "unrelated"])
def test_directory_approval_does_not_admit_missing_changed_or_extra_bytes(capture, defect):
    path = "raw/code/project/main.py"
    raw = {path: b"value = 1\n", "raw/code/project/sub/data.json": b"{}\n"}

    def change(files):
        if defect == "changed":
            files[path] = b"value = 2\n"
        elif defect == "missing":
            del files[path]
        else:
            extra = {"extra": "raw/code/project/extra.py", "sibling-prefix": "raw/code/project-other/main.py",
                     "unrelated": "raw/papers/unapproved.txt"}[defect]
            files[extra] = b"Not in the approved closure.\n"

    with pytest.raises(ValueError, match="assessment_(raw_revision_mismatch|unapproved_raw_input)"):
        capture(raw, paths=["raw/code/project"], changed=change)


@pytest.mark.parametrize("defect", ["changed", "missing", "unapproved"])
def test_sidecars_require_exact_host_approval(capture, defect):
    path = "raw/papers/paper.txt"
    sidecar = path + ".provenance.yml"
    raw = {path: b"Sanitized observations.\n"}
    if defect != "unapproved":
        raw[sidecar] = b"license: MIT\n"

    def change(files):
        if defect == "missing":
            del files[sidecar]
        else:
            files[sidecar] = b"license: altered\n"

    with pytest.raises(ValueError, match="assessment_raw_revision_mismatch"):
        capture(raw, paths=[path], changed=change)


def test_assessment_capture_reads_denials_after_inline_delimiters(capture):
    normalized = b"---\nsource_id: lab:sample\ntitle: Ordinary --- title\nexport_eligible: false\n---\nObservations.\n"
    with pytest.raises(ValueError, match="usage_permission_denied"):
        capture({"raw/papers/paper.txt": b"Observations.\n"}, paths=["raw/papers/paper.txt"], normalized=normalized)


def test_declared_provenance_cannot_escape_its_approved_revision(capture):
    with pytest.raises(ValueError, match="unsafe_artifact_path"):
        capture({"raw/papers/paper.txt": b"Observations.\n"}, paths=["raw/papers/paper.txt"],
                provenance={"sidecar_path": "../outside.provenance.yml"})
