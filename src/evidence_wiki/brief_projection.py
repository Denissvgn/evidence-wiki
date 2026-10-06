"""Exact permitted resolution content with literal deterministic Markdown."""

import hashlib
import string
import unicodedata
from copy import deepcopy

from .brief_contracts import BRIEF, MARKDOWN, MAX_BYTES, checked, refuse
from .mutation_contracts import canonical, digest
from .resolution_contracts import checked as resolution_document


def project(resolution, *, readability=None):
    """Pure projection; supplied dictionaries do not authenticate a current read."""
    value = resolution_document(resolution)
    result = {"schema_version": BRIEF, "resolution_basis_id": value["current_basis_id"],
        "verdict": value["verdict"], **{key: deepcopy(value[key]) for key in
        ("original_outcomes", "global_blockers", "accounting_complete", "limitations", "lifecycle")},
        "readability": readability or {"state": "not_requested", "qualification": "not_observed",
            "scope": "not_qualified", "semantic_approval": False},
        "authorized": False, "external_action_authorized": False}
    return checked(BRIEF, result)


def literal(value):
    """Content is literal text, including controls, bidirectional marks and URLs."""
    if type(value) is not str:
        refuse("text_invalid")
    return "".join("⟦U+" + format(ord(c), "04X") + "⟧" if unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"}
        else "&#" + str(ord(c)) + ";" if c in string.punctuation else c for c in value)


def render_content(content):
    """One fixed content hierarchy for public and blinded permitted fields."""
    lines = ["# Evidence brief", ""]
    size = sum(len(line.encode()) + 1 for line in lines)

    def append(line):
        nonlocal size
        size += len(line.encode()) + 1
        if size > MAX_BYTES:
            refuse("markdown_bound")
        lines.append(line)

    def emit(label, value):
        text = "null" if value is None else str(value).lower() if type(value) is bool else str(value)
        append(literal(label) + ": " + literal(text))

    def sequence(label, values):
        if not values:
            emit(label, "[]")
        for value in values:
            emit(label, value)

    emit("Resolution", content["verdict"])
    emit("Original accounting complete", content["accounting_complete"])
    for original in content["original_outcomes"]:
        append("")
        append("## Original question")
        emit("Question", original["original_text"])
        emit("Original reference", original["original_id"])
        emit("Outcome", original["outcome"])
        for question in original["eligible_questions"]:
            append("")
            append("### Reviewed question")
            emit("Question", question["question"])
            emit("Question reference", question["slug"])
            emit("Original reference", question["original_id"])
            if "status" in question:
                emit("Native status", question["status"])
            if "accepted" in question:
                emit("Native eligible", question["accepted"])
            for section, selected in (("Findings", False), ("Inferences", True)):
                append("")
                append("#### " + section)
                claims = [claim for claim in question["claims"] if (claim["qualification"] == "inference") == selected]
                if not claims:
                    emit("Entries", "[]")
                for claim in claims:
                    append("")
                    emit("Claim reference", claim["id"])
                    emit("Question reference", claim["question_slug"])
                    emit("Statement", claim["text"])
                    for key in ("qualification", "scope", "time", "units", "derivation"):
                        emit(key.capitalize(), claim[key])
                    sequence("Premise reference", claim["premises"])
                    append("")
                    append("##### Evidence")
                    if not claim["evidence"]:
                        emit("Evidence entries", "[]")
                    for evidence in claim["evidence"]:
                        for key in ("source_id", "record_sha256", "observed_at", "capture", "quote", "location_hint"):
                            emit(key.replace("_", " ").capitalize(), evidence[key])
                        if evidence["anchor"] is None:
                            emit("Anchor", None)
                        else:
                            for key, value in evidence["anchor"].items():
                                emit("Anchor " + key, value)
                    append("")
                    append("##### Limitations")
                    sequence("Limitation", claim["limitations"])
                    if "calculations" in claim:
                        append("")
                        append("##### Native calculation references")
                        if not claim["calculations"]:
                            emit("Calculation references", "[]")
                        for reference in claim["calculations"]:
                            for key, value in reference.items():
                                emit("Calculation " + key.replace("_", " "), value)
            sequence("Question gaps", question["gaps"])
            if not question["unresolved_claims"]:
                emit("Unresolved claims", "[]")
            for unresolved in question["unresolved_claims"]:
                for key, value in unresolved.items():
                    sequence("Unresolved claim " + key, value) if type(value) is list else emit("Unresolved claim " + key, value)
        append("")
        append("### Derived and unresolved outcomes")
        for question in original["questions"]:
            emit("Question identifier", question["question_id"])
            emit("Question reference", question["slug"])
            emit("Outcome", question["outcome"])
            sequence("Eligible claim reference", question["eligible_claim_refs"])
            sequence("Unresolved reason", question["reasons"])
            if "current_checks" in question:
                checks = question["current_checks"]
                if checks is None:
                    emit("Current checks", None)
                else:
                    for key, value in checks.items():
                        emit("Current check " + key.replace("_", " "), value)
        sequence("Missing question identifier", original["missing_question_ids"])
        sequence("Original unresolved reason", original["reasons"])
    append("")
    append("## Global limits and unresolved work")
    sequence("Global blocker", content["global_blockers"])
    sequence("Limitation", content["limitations"])
    return "\n".join(lines) + "\n"


def render(brief):
    """Deterministic literal presentation without recommendations or truncation."""
    value = checked(BRIEF, brief)
    markdown = render_content(substance(value))
    # Historical lifecycle and current critic state remain distinct from the
    # scientific content. They are withheld from the blind critic projection.
    extra = ["", "## Current observations"]
    for name in ("lifecycle", "readability"):
        for key, part in value[name].items():
            text = "null" if part is None else str(part).lower() if type(part) is bool else str(part)
            extra.append(literal(name + " " + key.replace("_", " ")) + ": " + literal(text))
    markdown += "\n".join(extra) + "\n"
    if len(markdown.encode()) > MAX_BYTES:
        refuse("markdown_bound")
    return checked(MARKDOWN, {"schema_version": MARKDOWN, "markdown": markdown,
        "markdown_sha256": "sha256:" + hashlib.sha256(markdown.encode()).hexdigest(),
        "brief_sha256": digest(value), "rendering": "fixed_literal_projection",
        "authorized": False, "external_action_authorized": False})


def substance(brief):
    """The critic assesses permitted content, never lifecycle/control identities."""
    value = checked(BRIEF, brief)
    return {key: deepcopy(value[key]) for key in
        ("verdict", "original_outcomes", "global_blockers", "accounting_complete", "limitations")}


def canonical_bytes(brief):
    return canonical(checked(BRIEF, brief))
