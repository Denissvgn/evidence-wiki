"""Scoped worker surfaces retain untouched inventories only in parent custody."""

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.mutation_contracts import canonical, detached, digest
from evidence_wiki.pack_discovery import owner


def refused(reason):
    raise ExecutionError("repair_surface_" + reason)


def checked_inventory(value):
    if len(canonical(value)) > 1048576:
        refused("inventory_bound")
    return owner("_strict_contract").claims_document(detached(value))


def require_current_dependencies(inventory, packets, computation=None):
    """The installed admission owner checks all retained claims, including peers.

    These are owner-observed packets, not caller assertions of permission. This
    shape/hash comparison creates no grant. Missing peer custody must refuse
    before model dispatch, since full native admission cannot validate that peer.
    """
    inventory = checked_inventory(inventory)
    current = {packet["source_id"]: packet["record_sha256"] for packet in packets}
    dependencies = [(evidence["source_id"], evidence["record_sha256"])
        for claim in inventory["claims"] for evidence in claim["evidence"]]
    if computation is not None:
        dependencies.extend((source["source_id"], source["record_sha256"]) for source in computation["sources"])
    if len(set(identifier for identifier, _hash in dependencies)) > 32:
        refused("source_bound")
    if any(current.get(identifier) != expected for identifier, expected in dependencies):
        refused("dependency_not_current_permitted")


def worker_inventory(inventory, *, target_claim_id, premise_ids):
    """Return only the permitted decisive target/premises, never unrelated peers."""
    before = checked_inventory(inventory)
    selected = {target_claim_id, *premise_ids}
    by_id = {claim["id"]: claim for claim in before["claims"]}
    if target_claim_id not in by_id or not selected <= set(by_id):
        refused("target_or_premise_missing")
    claims = [claim for claim in before["claims"] if claim["id"] in selected]
    slugs = {claim["question_slug"] for claim in claims}
    return {"schema_version": before["schema_version"],
        "questions": [question for question in before["questions"] if question["slug"] in slugs], "claims": claims}


def merge_inventory(before, current, supplied, *, target_claim_id, worker_before):
    """Merge only untouched current peers after a closed scoped worker proposal."""
    before, current, supplied = map(checked_inventory, (before, current, supplied))
    worker_before = checked_inventory(worker_before)
    if current != before:
        refused("parent_inventory_changed")
    if (supplied["schema_version"] != before["schema_version"]
            or supplied["questions"] != worker_before["questions"]):
        refused("question_or_format_changed")
    peers = {claim["id"]: claim for claim in before["claims"]}
    proposed = {claim["id"]: claim for claim in supplied["claims"]}
    allowed = {claim["id"] for claim in worker_before["claims"]}
    if target_claim_id not in proposed or not set(proposed) <= allowed:
        refused("claim_surface_changed")
    if proposed[target_claim_id]["question_slug"] != peers[target_claim_id]["question_slug"]:
        refused("target_association_changed")
    if any(claim != peers[identifier] for identifier, claim in proposed.items() if identifier != target_claim_id):
        refused("peer_changed")
    result = detached(before)
    result["claims"] = [proposed[target_claim_id] if claim["id"] == target_claim_id else claim for claim in before["claims"]]
    return result


def project_proposal(before, current, raw_proposal, *, target_claim_id, worker_before):
    """Bind a deterministic parent merge to the exact actual worker proposal."""
    from .draft_contracts import PROPOSAL, validate_document
    raw = validate_document(PROPOSAL, raw_proposal)
    projected = detached(raw)
    projected["inventory"] = merge_inventory(before, current, raw["inventory"],
        target_claim_id=target_claim_id, worker_before=worker_before)
    proof = {"schema_version": "research-scoped-repair-projection/v1", "target_claim_id": target_claim_id,
        "parent_before_sha256": digest(before), "worker_before_sha256": digest(worker_before),
        "raw_proposal_sha256": digest(raw), "merged_proposal_sha256": digest(projected)}
    return projected, proof
