"""Read versioned requirements through their protected existing mutation owner."""

import hashlib

from _record_artifacts import json_document

PATH = "docs/research-requirements.json"
BASE = "evidence-research-requirements/v1"
REVISED = "evidence-research-requirements/v2"


def effective(root, raw):
    document = json_document(raw)
    if document.get("schema_version") == BASE:
        return document
    if document.get("schema_version") != REVISED:
        raise ValueError("caller_requirements_version_unsupported")
    try:
        from evidence_wiki.requirements_operations import read_effective
    except ImportError:
        raise ValueError("caller_requirements_owner_unavailable") from None
    return read_effective(root, raw=raw)


def instruction_matches(root, path, raw, expected):
    if path != PATH or json_document(raw).get("schema_version") != REVISED:
        return "sha256:" + hashlib.sha256(raw).hexdigest() == expected
    try:
        from evidence_wiki.requirements_operations import instruction_matches as owned_match
    except ImportError:
        raise ValueError("caller_requirements_owner_unavailable") from None
    return owned_match(root, raw, expected)
