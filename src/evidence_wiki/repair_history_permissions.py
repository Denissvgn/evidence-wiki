"""Current native rights and exact source closure guard historical commitments."""

import hashlib

from evidence_wiki.pack_discovery import owner


def permitted(session, row, application, current_packets, *, usage_view=None):
    """Return no historical material facts unless every exact dependency is usable.

    Missing retained closure, ambiguous history and changed material are finite
    unavailable outcomes. This reads existing owners and creates no grant/store.
    """
    current = {packet["source_id"]: packet for packet in current_packets}
    retained = {packet["source_id"]: packet for packet in row.get("repair_parent_sources", row["attempt"]["input"].get("sources", []))}
    scope = row["attempt"]["input"].get("repair_scope", {})
    inventories = [row.get("repair_parent_inventory", scope.get("before_inventory", {"claims": scope.get("claims", [])}))]
    if application is not None:
        inventories.append(application["proposal"]["inventory"])
    dependencies = {}
    for inventory in inventories:
        for claim in inventory["claims"]:
            for evidence in claim["evidence"]:
                dependencies.setdefault(evidence["source_id"], set()).add(evidence["record_sha256"])
    computation = row["attempt"]["input"].get("computation")
    if computation is not None:
        for source in computation["sources"]:
            dependencies.setdefault(source["source_id"], set()).add(source["record_sha256"])
    if not dependencies or len(dependencies) > 32:
        return False
    for identifier, hashes in dependencies.items():
        before, now = retained.get(identifier), current.get(identifier)
        if (before is None or now is None or len(hashes) != 1
                or before["record_sha256"] != next(iter(hashes))
                or now["record_sha256"] != before["record_sha256"]
                or now.get("source_binding") != before.get("source_binding")):
            return False
    config = owner("_strict_evidence").configuration(session.root)
    usage = owner("_evidence_usage")
    if not usage.configured(config):
        return False
    from contextlib import nullcontext
    with nullcontext(usage_view) if usage_view is not None else usage.current_view(session.root, config) as live:
        for identifier, hashes in dependencies.items():
            matches = []
            for revision, record in live.state.revisions.items():
                path = record["descriptor"]["normalized_path"]
                if record["source_id"] == identifier and path is not None and (
                        "sha256:" + hashlib.sha256(record["files"][path]).hexdigest()) in hashes:
                    matches.append(revision)
            if len(matches) != 1:
                return False
            decision = live.check(matches[0], uses=["retrieval"], purpose="research", consumer="evidence-wiki")
            if decision["eligible"] is not True or decision["complete"] is not True:
                return False
        live.revalidate(session.root, config)
    return True
