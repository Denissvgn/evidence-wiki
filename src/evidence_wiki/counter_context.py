"""Counterevidence examination uses existing blinded reviewer context custody."""

import hashlib

from evidence_wiki.mutation_contracts import detached
from evidence_wiki.review_context import ReviewContext

from .coverage_examination import COUNTER, check_instruction, codec_boundary, exact, fail, reduce_counterevidence, rows


@codec_boundary
def packet(instruction, context):
    """Return evaluator-visible data only; aliases and parent identity stay private."""
    instruction = check_instruction(instruction)
    if type(context) is not ReviewContext or context.document()["task_role"] != "critic":
        fail("critic_context_required")
    document = context.document()
    if not document["manifest"]["context_complete"]:
        fail("critic_context_incomplete")
    aliases = context.binding()["aliases"]
    claims = {row["id"] for row in instruction["claims"]}
    source_ids = {row["source_id"] for row in instruction["source_contexts"]}
    if not set(aliases["claims"]) <= claims or not set(aliases["sources"]) <= source_ids:
        fail("critic_owner_scope_changed")
    retained = {row["source_id"]: row for row in instruction["source_contexts"]}
    reverse = {alias: identifier for identifier, alias in aliases["sources"].items()}
    for row in document["sources"]:
        source = retained[reverse[row["source_id"]]]
        if row["record_sha256"] != source["record_sha256"] or row["content"] != source["text"]:
            fail("critic_source_context_changed")
    facet_aliases = {row["facet_id"]: "facet-" + str(index + 1) for index, row in enumerate(instruction["facets"])}
    criteria = [{**detached(row), "facet_id": facet_aliases[row["facet_id"]]} for row in instruction["criteria"]]
    alternatives = [{"source_id": alias, "record_sha256": retained[identifier]["record_sha256"],
        "content": retained[identifier]["text"]} for identifier, alias in alternative_aliases(instruction, context).items()]
    return {"packet": document, "examination": {"basis_id": context.sha256, "alternative_sources": alternatives,
        "criteria": criteria, "scope": instruction["scope"], "semantic_approval": "not_evaluated"}}


@codec_boundary
def translate(instruction, context, proposal):
    """Check exact blinded packet again before resolving parent references."""
    projected = packet(instruction, context)
    exact(proposal, {"schema_version", "basis_id", "findings"})
    if proposal["schema_version"] != COUNTER or proposal["basis_id"] != projected["examination"]["basis_id"]:
        fail("critic_packet_changed")
    aliases = context.binding()["aliases"]
    claim_ids = {alias: identifier for identifier, alias in aliases["claims"].items()}
    sources = {alias: identifier for identifier, alias in {
        **aliases["sources"], **alternative_aliases(instruction, context)}.items()}
    facets = {"facet-" + str(index + 1): row["facet_id"] for index, row in enumerate(instruction["facets"])}
    findings = []
    for row in rows(proposal["findings"], 32):
        exact(row, {"claim_id", "facet_id", "kind", "query", "exclusions", "source_ids", "evidence", "rationale"})
        if row["claim_id"] not in claim_ids or row["facet_id"] not in facets or any(sid not in sources for sid in row["source_ids"]):
            fail("critic_alias_unknown")
        evidence = []
        for item in rows(row["evidence"], 16):
            if type(item) is not dict or item.get("source_id") not in sources:
                fail("critic_alias_unknown")
            evidence.append({**detached(item), "source_id": sources[item["source_id"]]})
        findings.append({**detached(row), "claim_id": claim_ids[row["claim_id"]], "facet_id": facets[row["facet_id"]],
            "source_ids": [sources[sid] for sid in row["source_ids"]], "evidence": evidence})
    return reduce_counterevidence(instruction, {"schema_version": COUNTER, "basis_id": instruction["basis_id"], "findings": findings})


def alternative_aliases(instruction, context):
    """Bound permitted alternative context without adding fabricated premises."""
    primary = context.binding()["aliases"]["sources"]
    return {row["source_id"]: "alternative-" + str(index + 1) for index, row in enumerate(
        sorted((source for source in instruction["source_contexts"] if source["source_id"] not in primary),
            key=lambda row: hashlib.sha256(bytes.fromhex(context.binding()["order_seed"]) + row["source_id"].encode()).digest()))}
