"""Reconciliation validators consume captured membership, never live aliases."""

import json

import pytest
import yaml

from evidence_wiki import Workspace, _script_host, mutation_operations
from evidence_wiki.host_context import HostContext
from tests.test_mutation_operations import GRANTS


def test_absolute_wiki_path_cannot_reopen_live_question_for_reconciliation(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    control = tmp_path / "control"
    questions = root / "wiki" / "questions"
    questions.mkdir(parents=True)
    (root / "sources").mkdir()
    control.mkdir(mode=0o700)
    config = {
        "wiki": {"root": str(root / "wiki")},
        "sources": {
            "manifest_path": "sources/manifest.jsonl",
            "coverage_dir": "sources/coverage",
            "normalized_dir": "sources/normalized",
        },
    }
    (root / "research.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "sources" / "manifest.jsonl").write_text("", encoding="utf-8")
    question = questions / "sample.md"
    original = b"---\ntype: question\nstatus: open\nquestion: Existing observation?\n---\n"
    question.write_bytes(original)
    selected = HostContext(
        [root], GRANTS, "caller", [root], mutation_control_directory=control,
    )
    arguments = {
        "kind": "other", "query_or_identifier": "Existing observations",
        "rationale": "Retain the evidence gap", "question_slugs": ["sample"],
    }
    module = _script_host.load_packaged_script(_script_host.shared_assets_root(), "source_requests")

    with selected.activate(), Workspace.open(root) as workspace:
        workspace.source_requests.add(**arguments)
        request = workspace.mutations.prepare(
            request_id="source-gap", operation="source_requests.add", arguments=arguments,
        )

        def stop_before_owner(*args):
            raise KeyboardInterrupt

        with monkeypatch.context() as interrupted:
            interrupted.setattr(mutation_operations, "_invoke", stop_before_owner)
            with pytest.raises(KeyboardInterrupt):
                workspace.mutations.submit(request)

        assert workspace.mutations.inspect("source-gap")["record"]["state"]["status"] == "executing"
        question.unlink()
        validation_directories = []
        validate = module.validate_question_slugs

        def transient_live_question(project, current_config, slugs):
            validation_directories.append(module.questions_directory(project, current_config))
            # A briefly restored live page must not fill a hole in the capture.
            question.write_bytes(original)
            try:
                return validate(project, current_config, slugs)
            finally:
                question.unlink()

        monkeypatch.setattr(module, "validate_question_slugs", transient_live_question)
        result = workspace.mutations.reconcile("source-gap")

    assert not question.exists()
    assert not result["desired_state_proved"]
    assert not result["completion_proved"]
    assert result["status"] in {"partial", "commit_unknown"}
    assert result["authorized"] is False and result["evidence_accepted"] is False
    assert all(path.resolve() != questions.resolve() for path in validation_directories)
    assert json.loads((root / "sources" / "source-requests.jsonl").read_text())["question_slugs"] == ["sample"]
