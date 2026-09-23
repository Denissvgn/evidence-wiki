#!/usr/bin/env python3
"""Freeze minimal fresh-agent prompts separately from their private reference rubric."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import uuid
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._journey_cases import (  # noqa: E402
    canonical,
    computation,
    digest,
    fixture_html,
    frozen_suite,
    load_cases,
    minimal_prompt,
)


def freeze(output, *, package, python, capabilities, work_root=None):
    from evidence_wiki import __version__
    from evidence_wiki.frameworks import compatibility

    package, python, output = Path(package).resolve(), Path(python).absolute(), Path(output).resolve()
    work_root = (Path(work_root) if work_root is not None else
                 Path(tempfile.gettempdir()) / ("evidence-wiki-trials-" + uuid.uuid4().hex)).resolve()
    if (work_root.is_relative_to(ROOT) or ROOT.is_relative_to(work_root)
            or work_root.is_relative_to(output) or output.is_relative_to(work_root)):
        raise ValueError("trial_workspaces_must_be_outside_checkout_and_private_reports")
    if not package.is_file() or package.suffix != ".whl" or not python.is_file():
        raise ValueError("installed_interpreter_and_wheel_required")
    suite = load_cases(ROOT / "tests/fixtures/onboarding-journeys/cases.json")
    with zipfile.ZipFile(package) as archive:
        metadata = [row for row in archive.infolist() if row.filename.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or metadata[0].file_size > 1_048_576:
            raise ValueError("candidate_metadata_invalid")
        fields = BytesParser().parsebytes(archive.read(metadata[0]))
    if fields["Name"] != "evidence-wiki" or fields["Version"] != __version__:
        raise ValueError("candidate_version_mismatch")
    identity = {"version": fields["Version"], "wheel": package.name, "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                "interpreter": str(python), "interpreter_qualification": "requires_selected_installation_observation"}
    report = frozen_suite(suite, identity)
    report.update(status="awaiting_live_execution", trials=[], model_results=[], work_root=str(work_root),
                  semantic_metrics=None, operator_interventions=None, publication_ready=False)
    output.mkdir(parents=True, exist_ok=False)
    work_root.mkdir(parents=True, exist_ok=False)
    for harness in compatibility()["frameworks"]:
        for case in suite["cases"]:
            for trial in range(1, suite["trials"] + 1):
                work = work_root / harness["id"] / case["id"] / str(trial)
                work.mkdir(parents=True)
                sources = []
                if case["source_mode"] == "local":
                    source = work / "observation.html"
                    source.write_bytes(fixture_html(case))
                    sources.append({"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                    "rights": "MIT; authored synthetic observations"})
                if case["quantitative"]:
                    rules = work / "declared-computation.json"
                    rules.write_bytes(canonical(computation(case)))
                    sources.append({"path": str(rules), "role": "Caller-declared computation requirements",
                                    "sha256": hashlib.sha256(rules.read_bytes()).hexdigest()})
                prompt = minimal_prompt(case, package_reference=str(package), python=python, root=work,
                                        sources=sources, capabilities=capabilities)
                if case["id"] == "authored-revision":
                    prompt += "Author reusable local guidance for this scope. After the first answer, revise it to require retaining the northern-area limitation and reevaluate affected research.\n"
                prompt_path = output / harness["id"] / case["id"] / str(trial) / "prompt.txt"
                prompt_path.parent.mkdir(parents=True)
                prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
                report["trials"].append({"framework": harness["id"], "version": harness["version"], "case_id": case["id"],
                    "trial": trial, "prompt": str(prompt_path.relative_to(output)), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "source_inputs": sources, "work": str(work), "status": "not_run", "provider": None, "model": None,
                    "interventions": None, "schema_repairs": None, "time_to_first_usable_evidence": None,
                    "completeness": None, "unsupported_source_handling": None, "pack_decisions": None,
                    "unsupported_claim_escapes": None, "appropriate_abstentions": None, "unnecessary_refusals": None,
                    "policy_violations": None, "operator_setting_explanations": None})
    report["limits"].append("Prompt preparation is not execution. Freeze model/provider, host-review and cost conditions before running each fresh harness; keep private rubrics and reference credentials outside its workspace.")
    report["limits"].append("An unrelated workspace prevents inherited checkout instructions; it does not create an OS sandbox. Qualify isolated harness configuration and resource discovery separately.")
    path = output / "frozen-suite.json"
    path.write_bytes(canonical(report))
    return {"status": report["status"], "trials": len(report["trials"]), "sha256": digest(report), "path": str(path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, help="New workspace root outside the checkout and private report tree; defaults to an external temporary location.")
    parser.add_argument("--capability", action="append", default=[])
    args = parser.parse_args(argv)
    print(json.dumps(freeze(args.output, package=args.wheel, python=args.python, capabilities=args.capability, work_root=args.work_root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
