"""Regenerate the pre-4.0.0 packet-validator snapshot with the scripts that exported it.

This is not run by the suite. Check out the last commit before the agent-wiki-cli 4.0.0
validator upgrade (``15f8d48^``), install it into its own Python 3.14 virtual environment,
and run from that checkout:

    PYTHONPATH=. .venv/bin/python <this file> <output directory>

The snapshot carries both packet qualification carriers: an execution record's
``context-packet`` artifact and an ancestor source's qualified packet delivery. Only the
authority expiry is fixed far ahead, so the check path does not expire with the real clock.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

import tests._execution_fixture as execution_fixture
import tests._usage_fixture as usage_fixture

EXPIRES_AT = "2099-01-01T00:00:00Z"
VALIDATOR = "agent-wiki-cli/1.8.0:offline-validation-closure/v1"


def main(output: Path) -> None:
    script = Path("workspace-template/scripts/_qualified_packet.py").read_text(encoding="utf-8")
    if f'VALIDATOR = "{VALIDATOR}"' not in script:
        raise SystemExit("run from a checkout whose packet validator is " + VALIDATOR)
    execution_fixture.AUTHORITY_EXPIRES_AT = usage_fixture.AUTHORITY_EXPIRES_AT = EXPIRES_AT
    from tests._execution_fixture import binding
    from tests._snapshot_fixture import SnapshotFixture

    output.mkdir(parents=True, exist_ok=True)
    work = output / "work"
    work.mkdir()
    host = SnapshotFixture(work, pytest.MonkeyPatch())
    packet = Path("tests/fixtures/codebase-intake/native-packets/native-v2-current.json").read_bytes()
    source_id = "codebase:pre-upgrade-delivery"
    manifest = {
        "schema_version": "1", "artifact_kind": "codebase_evidence", "source_id": source_id,
        "intake_profile": "qualified_context_packet/v1", "packet_path": "packet.json",
        "generated_at": "2026-09-10T00:00:00Z", "producer": {"name": "agent-wiki-cli", "version": "1.8.0"},
        "invocation": {"executed_by": "external_worker", "argv": ["llm-wiki", "context"],
                       "plugins_enabled": False, "hooks_enabled": False, "network_access": False},
        "files": [{"path": "packet.json", "size_bytes": len(packet), "sha256": hashlib.sha256(packet).hexdigest()}],
    }
    delivery, files = host.source(source_id, evidence={"artifact-manifest.json": json.dumps(manifest).encode(),
                                                       "packet.json": packet})
    host.transact(host.module, "deposit", delivery, files)

    def add_packet(evidence, record):
        evidence["context.json"] = packet
        record["artifacts"].append({"path": "context.json", **binding(packet), "role": "context-packet"})
        record["records"][-1]["payload"]["model"]["context"] = {"path": "context.json",
                                                                "content_hash": binding(packet)["content_hash"]}

    body, _ = host.add_execution(source_id="execution:pre-upgrade-packet", edit=add_packet,
                                 extra_parents=[delivery["source_revision"]])
    data, preparation, _, _ = host.export(host.selection(body))
    validators = [item["validator"] for item in preparation["manifest"]["qualifications"]]
    if validators != [VALIDATOR, VALIDATOR]:
        raise SystemExit(f"expected two {VALIDATOR} qualifications, got {validators}")
    (output / "snapshot.json").write_bytes(data)
    (output / "authority.json").write_bytes(host.policy_path.read_bytes())


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
