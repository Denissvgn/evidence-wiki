"""Read complete native review material against the current global owner basis."""

from evidence_wiki._facades._base import _translated
from evidence_wiki._review_dependency_json import digest
from evidence_wiki.pack_discovery import owner


def current_review_material(root, prepared):
    """Keep the strict review snapshot intact while reading its full dependency closure."""
    try:
        if prepared.get("review_schema") == "evidence-strict-review/v3":
            return owner("strict_evidence").prepare_dependency_review_material(root, prepared["snapshot"]["claim"]["id"],
                expected_workspace_basis_id=prepared["observed_workspace_basis_id"],
                expected_claim_basis_id=prepared["claim_basis_id"], expected_producer_id=prepared["review_producer_id"],
                expected_native_snapshot_sha256=digest(prepared["snapshot"]))
        return owner("strict_evidence").prepare_review_material(root, prepared["snapshot"]["claim"]["id"],
            expected_basis_id=prepared["result"]["basis_id"],
            expected_native_snapshot_sha256=digest(prepared["snapshot"]))
    except Exception as error:
        translated = _translated(error)
        if translated is None:
            raise
        raise translated from None
