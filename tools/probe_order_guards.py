#!/usr/bin/env python3
"""Probe twelve named controller guards in an isolated copy, preserving behavioral evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

CONTROLLER = 'workspace-template/scripts/orchestration_controller.py'
BOOK = 'tests/test_contingent_bookkeeping_baseline.py::'
CUMULATIVE = 'tests/test_cumulative_acquisition.py::CumulativeAcquisitionTests::'

# Exact source anchors make a stale probe refuse before it can claim a result.
MUTATIONS = [
    ('unreadable-claims',
     '''        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_UNREADABLE",
            f"{arm} claims could not be read: {exc.message}",
            recoverable=True,
            remediation="Restore the orchestration control tree; unreadable claims cannot authorize bookkeeping.",
        ) from exc''',
     '        claims = {"fulfilments": {}, "reopens": {}}',
     [BOOK + 'LedgerIsNotTrustedTests::test_an_unreadable_ledger_refuses_both_arms_rather_than_reading_as_no_claims']),
    ('claim-entry-bound', 'if entries > MAX_SCOPE_GUARD_ENTRIES:', 'if False:',
     [BOOK + 'LedgerIsNotTrustedTests::test_a_ledger_carrying_more_claims_than_the_bound_is_refused_rather_than_projected']),
    ('normalized-reopen-input', 'if not normalized_path.is_file():\n                unvalidated.append(',
     'if False:\n                unvalidated.append(',
     [BOOK + 'LedgerIsNotTrustedTests::test_a_reopen_claim_naming_an_unnormalized_source_is_refused']),
    ('request-replay-fingerprint',
     'if reverted_fingerprint == source_requests_before.get(claimed_id):', 'if True:',
     [BOOK + 'CommitReplayTests::test_a_replay_does_not_license_editing_the_rest_of_the_record']),
    ('whole-page-replay',
     'if actual_digest == f"sha256:{hashlib.sha256(expected).hexdigest()}":', 'if True:',
     [CUMULATIVE + 'test_replayed_commit_rejects_unrelated_page_edits']),
    ('sibling-request-fingerprint',
     'if fingerprint is None or fingerprint != source_requests_before.get(request_id):', 'if False:',
     [CUMULATIVE + 'test_changed_or_missing_fulfilled_sibling_is_refused_by_command_and_controller']),
    ('commit-late-claims',
     'if current.get("fulfilments") != fulfilment_claims or current.get("reopens") != reopen_claims:',
     'if False:',
     [BOOK + 'LedgerIsNotTrustedTests::test_a_claim_filed_after_verification_began_refuses_rather_than_being_dropped']),
    ('delegated-blocked-late-claims',
     'not late_claims.get("fulfilments") and not late_claims.get("reopens"),', 'True,',
     [BOOK + 'LedgerIsNotTrustedTests::test_a_blocked_delegated_action_that_claims_during_verification_is_refused']),
    ('provider-blocked-late-claims',
     'not late.get("fulfilments") and not late.get("reopens"),', 'True,',
     [BOOK + 'ProviderAcquisitionBookkeepingTests::test_a_blocked_action_that_claims_during_verification_is_refused',
      BOOK + 'ProviderAcquisitionBookkeepingTests::test_a_failed_route_that_claims_during_verification_is_refused']),
    ('provider-question-scope',
     '''            not unauthorized_reopen_claims,
            "acquisition claimed a question that is unscoped or not fully unblocked",''',
     '''            True,
            "acquisition claimed a question that is unscoped or not fully unblocked",''',
     [BOOK + 'ProviderAcquisitionBookkeepingTests::test_a_reopen_claim_for_a_question_this_order_never_scoped_is_refused']),
]

MUTATIONS += [('delegated-required-reopen', '        not question_transition_failures,\n        "delegated acquisition did not reopen every fully unblocked question with its fulfilled source",', '        True,\n        "delegated acquisition did not reopen every fully unblocked question with its fulfilled source",', ['tests/test_acquisition_transitions.py::test_fully_delivered_history_requires_explicit_question_reopen[delegated]']), ('provider-required-reopen', '            not question_transition_failures,\n            "acquisition did not reopen every scoped blocked question with fulfilled request/source linkage",', '            True,\n            "acquisition did not reopen every scoped blocked question with fulfilled request/source linkage",', ['tests/test_acquisition_transitions.py::test_fully_delivered_history_requires_explicit_question_reopen[provider]'])]

EXPECTED = {
    "unreadable-claims": ("refusal-path-change", "'ORCHESTRATION_STATE_UNREADABLE' != 'ORCHESTRATION_POSTCONDITION_FAILED'"),
    "claim-entry-bound": ("refusal-path-change", "'ORCHESTRATION_SCOPE_EXCEEDED' != 'ORCHESTRATION_POSTCONDITION_FAILED'"),
    "normalized-reopen-input": ("refusal-path-change", "'the reopen command would refuse' not found"),
    "delegated-blocked-late-claims": ("refusal-path-change", "'while it was being verified' not found"),
    "provider-blocked-late-claims": ("refusal-path-change", "'while it was being verified' not found"),
    "request-replay-fingerprint": ("unsafe-success", "0 == 0"),
    "whole-page-replay": ("unsafe-success", "0 == 0"),
    "sibling-request-fingerprint": ("unsafe-success", "2 != 0"),
    "commit-late-claims": ("unsafe-success", "0 == 0"),
    "provider-question-scope": ("unsafe-success", "0 == 0"),
    "delegated-required-reopen": ("unsafe-success", "assert 0 == 2"),
    "provider-required-reopen": ("unsafe-success", "assert 0 == 2"),
}


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def behavioral_result(xml: Path, nodes: list[str], assertion: str) -> dict:
    """Require named assertion failures; collection/import errors earn no result."""
    tree = ET.parse(xml)  # noqa: S314 - locally generated pytest output from this harness.
    failures = [{"class": case.get("classname"), "name": case.get("name"),
                 "message": failure.get("message"), "text": failure.text or ""}
                for case in tree.iter("testcase") for failure in case.findall("failure")]
    matched = [node for node in nodes if any(
        row["name"].startswith(node.rsplit("::", 1)[-1])
        and "AssertionError" in row["text"] and assertion in row["text"]
        for row in failures)]
    return {"failures": failures, "matched_nodes": matched,
            "confirmed": matched == nodes and not list(tree.iter("error"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("guard-evidence"))
    args = parser.parse_args(argv)
    root, output = args.root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    original = (root / CONTROLLER).read_bytes()
    source = original.decode("utf-8")
    report = {"source": CONTROLLER, "source_sha256": digest(original),
              "method": "One exact guard removal per fresh process in an isolated source copy.",
              "limits": ["Twelve named guards only; no repository-wide mutation score.",
                         "Vendored comparisons cannot establish behavioral detection.",
                         "Refusal-path changes do not establish unsafe acceptance."],
              "python": sys.version, "mutations": [], "status": "running"}
    manifest = output / "manifest.json"

    def save():
        manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")

    save()
    try:
        for name, anchor, _, _ in MUTATIONS:
            if source.count(anchor) != 1:
                raise ValueError(f"{name}: expected exactly one source anchor")
        git = shutil.which("git")
        if git is None:
            raise ValueError("git is required to bind the isolated inventory")
        report["commit"] = subprocess.check_output([git, "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8").strip()  # noqa: S603
        inventory = subprocess.check_output(  # noqa: S603 - resolved git and fixed read-only arguments.
            [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
        ).decode().split("\0")
        with tempfile.TemporaryDirectory(prefix="evidence-wiki-guards-") as directory:
            clone = Path(directory)
            identities = {}
            for relative in sorted(set(inventory) - {""}):
                path = root / relative
                if path.is_file():
                    content = path.read_bytes()
                    identities[relative] = digest(content)
                    target = clone / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    shutil.copymode(path, target)
            report["copied_source_sha256"] = identities
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                               PYTHONPATH=os.pathsep.join((str(clone), str(clone / "src"))))

            def run(label, nodes):
                log, xml, record = (output / f"{label}.{suffix}" for suffix in ("log", "xml", "json"))
                environment["EVIDENCE_WIKI_SUITE_RECORD"] = str(record)
                command = [sys.executable, "-B", "-m", "pytest", "-q", "--tb=short",
                           "-p", "tools._suite_plugin", f"--junitxml={xml}", *nodes]
                start = time.perf_counter()
                with log.open("w", encoding="utf-8", newline="\n") as stream:
                    completed = subprocess.run(command, cwd=clone, env=environment, stdout=stream,  # noqa: S603
                                               stderr=subprocess.STDOUT, timeout=600, check=False)
                recorded = json.loads(record.read_text(encoding="utf-8"))
                return {"exit_code": completed.returncode, "seconds": time.perf_counter() - start,
                        "log": str(log), "xml": str(xml), "command": command, "nodes": nodes,
                        "selection_matches": recorded["collected"] == nodes and recorded["executed"] == nodes}

            all_nodes = sorted({node for *_, nodes in MUTATIONS for node in nodes})
            report["baseline"] = run("baseline", all_nodes)
            save()
            if report["baseline"]["exit_code"] or not report["baseline"]["selection_matches"]:
                raise ValueError("unmodified behavioral baseline did not pass")
            for name, anchor, replacement, nodes in MUTATIONS:
                mutant = source.replace(anchor, replacement, 1).encode("utf-8")
                (clone / CONTROLLER).write_bytes(mutant)
                result = run(name, nodes)
                classification, assertion = EXPECTED[name]
                evidence = behavioral_result(Path(result["xml"]), nodes, assertion)
                result.update(name=name, mutant_sha256=digest(mutant), removed=anchor,
                              replacement=replacement, expected_assertion=assertion, evidence=evidence)
                confirmed = result["exit_code"] == 1 and result["selection_matches"] and evidence["confirmed"]
                result["classification"] = classification if confirmed else "inconclusive"
                report["mutations"].append(result)
                save()
                print(f"{name}: {result['classification']}", flush=True)
                (clone / CONTROLLER).write_bytes(original)
            report["sources_unchanged"] = all(
                (root / relative).is_file() and digest((root / relative).read_bytes()) == identity
                for relative, identity in identities.items())
        report["status"] = "passed" if report["sources_unchanged"] and all(
            row["classification"] != "inconclusive" for row in report["mutations"]) else "failed"
        return int(report["status"] != "passed")
    except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError) as error:
        report.update(status="failed", error=str(error))
        return 1
    finally:
        save()


if __name__ == "__main__":
    raise SystemExit(main())
