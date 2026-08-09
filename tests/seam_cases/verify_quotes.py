"""Seam-conformance cases for ``verify_quotes.py``.

The grounding verifier is the one enrolled command whose CLI exits non-zero on a
perfectly good document. When verification runs but a claim does not check out,
``main`` prints the report and exits ``EXIT_NOT_VERIFIED``; the report is the whole
answer, naming which claim failed against which record. So
``not_verified_is_a_document_not_a_refusal`` is declared ``SUCCESS``: the seam must
return that document rather than raise, and a rewrite that confused "non-zero exit"
with "refusal" fails here.

Refusals are declared once per verification form, because the two forms carry
different fatal codes on the ``--write`` path -- ``GROUNDING_QUOTE_INVALID`` for a
containment failure, ``GROUNDING_ANCHOR_INVALID`` as soon as one anchor entry failed
-- and a host switches on them. The unreadable-workspace case covers the third
route into a refusal: a ``SystemExit`` whose message the shared helper classifies.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import yaml

from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

SCRIPT = "verify_quotes.py"

SLUG = "supplier-price"
#: Manifest ids and the record filenames they map to under the contract's naming rule
#: (``:`` becomes ``--``), written out so the fixture states where the verifier looks.
QUOTE_SOURCE = "web:vendor-official-product-spec"
QUOTE_RECORD = "web--vendor-official-product-spec"
ANCHOR_SOURCE = "data:keepa-b0abc123"
ANCHOR_RECORD = "data--keepa-b0abc123"

RECORD_BODY = "Vendor-controlled product specification."
SIDECAR = {"supplier_quote": {"price": "23.99 EUR", "currency": "EUR"}}
VERIFIER = "seam-verifier-agent"


def quote_entry(quote: str = RECORD_BODY) -> dict:
    return {
        "claim": "The product spec is vendor-controlled.",
        "source_id": QUOTE_SOURCE,
        "quote": quote,
        "location_hint": "Official product spec",
    }


def anchor_entry(expected: str = "23.99 EUR") -> dict:
    return {
        "claim": "The current supplier price is 23.99 EUR.",
        "source_id": ANCHOR_SOURCE,
        "anchor": {"pointer": "supplier_quote/price", "expected": expected},
    }


def write_question(workspace: Path, grounding: list[dict]) -> None:
    page = {
        "type": "question",
        "status": "answered",
        "question": "What is the current supplier price?",
        "source_ids": [QUOTE_SOURCE, ANCHOR_SOURCE],
        "answer_page": "../synthesis/supplier-price.md",
        "answered_by": "answer-agent",
        "grounding": grounding,
    }
    questions = workspace / "wiki" / "questions"
    questions.mkdir(parents=True, exist_ok=True)
    (questions / f"{SLUG}.md").write_text(
        "---\n" + yaml.safe_dump(page, sort_keys=False) + "---\n\n# Supplier price\n",
        encoding="utf-8",
    )


def write_evidence(workspace: Path) -> None:
    """One normalized record per form: a body to quote, and a hash-bound structured view."""
    normalized = workspace / "sources" / "normalized"
    normalized.mkdir(parents=True, exist_ok=True)

    (normalized / f"{QUOTE_RECORD}.md").write_text(
        "---\n"
        + yaml.safe_dump(
            {"type": "normalized_source", "source_id": QUOTE_SOURCE, "title": "Official product spec"},
            sort_keys=False,
        )
        + f"---\n\n# Official product spec\n\n{RECORD_BODY}\n",
        encoding="utf-8",
    )

    payload = json.dumps(SIDECAR, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    (normalized / f"{ANCHOR_RECORD}.structured.json").write_bytes(payload)
    (normalized / f"{ANCHOR_RECORD}.md").write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "type": "normalized_source",
                "source_id": ANCHOR_SOURCE,
                "title": "Keepa supplier quote",
                "structured_view": {
                    "path": f"sources/normalized/{ANCHOR_RECORD}.structured.json",
                    "content_hash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                },
            },
            sort_keys=False,
        )
        + "---\n\n# Keepa supplier quote\n\nSupplier quote retained for the current period.\n",
        encoding="utf-8",
    )


def variant(workspace: Path, name: str, grounding: list[dict]) -> str:
    """A copy of the pristine workspace whose question grounds its claims differently."""
    target = workspace.parent / name
    shutil.copytree(workspace, target)
    write_question(target, grounding)
    return str(target)


def cases(workspace: Path) -> tuple[SeamCase, ...]:
    write_evidence(workspace)
    write_question(workspace, [quote_entry(), anchor_entry()])
    root = str(workspace)

    # Copied before any case runs, so each is a copy of the pristine workspace.
    unmatched_quote = variant(workspace, "unmatched-quote", [quote_entry("A sentence no retained record carries.")])
    unmatched_anchor = variant(workspace, "unmatched-anchor", [anchor_entry(expected="24.99 EUR")])

    uninitialized = workspace.parent / "not-a-workspace"
    uninitialized.mkdir()

    return (
        SeamCase(
            name="verified_document",
            argv=("--project-root", root, "--format", "json", "--slug", SLUG),
            call=lambda module: module.run_verify(root, [SLUG]),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="both forms verify; the whole per-entry report must match",
        ),
        SeamCase(
            name="not_verified_is_a_document_not_a_refusal",
            argv=("--project-root", unmatched_quote, "--format", "json", "--slug", SLUG),
            call=lambda module: module.run_verify(unmatched_quote, [SLUG]),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="the CLI exits EXIT_NOT_VERIFIED and still prints the report; the seam returns it",
        ),
        SeamCase(
            name="write_stamps_the_verified_questions",
            argv=(
                "--project-root", root, "--format", "json", "--slug", SLUG,
                "--write", "--verified-by", VERIFIER,
            ),
            call=lambda module: module.run_verify(root, [SLUG], write=True, verified_by=VERIFIER),
            expect=SUCCESS,
            volatile=("generated_at",),
            note="--write is the seam's own side effect, so both paths stamp and both report the same document",
        ),
        SeamCase(
            name="quote_failure_refuses_the_write",
            argv=(
                "--project-root", unmatched_quote, "--format", "json", "--slug", SLUG,
                "--write", "--verified-by", VERIFIER,
            ),
            call=lambda module: module.run_verify(unmatched_quote, [SLUG], write=True, verified_by=VERIFIER),
            expect=REFUSAL,
            note="containment failure; GROUNDING_QUOTE_INVALID",
        ),
        SeamCase(
            name="anchor_failure_refuses_the_write",
            argv=(
                "--project-root", unmatched_anchor, "--format", "json", "--slug", SLUG,
                "--write", "--verified-by", VERIFIER,
            ),
            call=lambda module: module.run_verify(unmatched_anchor, [SLUG], write=True, verified_by=VERIFIER),
            expect=REFUSAL,
            note="anchor resolution failure; GROUNDING_ANCHOR_INVALID",
        ),
        SeamCase(
            name="unreadable_workspace",
            argv=("--project-root", str(uninitialized), "--format", "json", "--slug", SLUG),
            call=lambda module: module.run_verify(str(uninitialized), [SLUG]),
            expect=REFUSAL,
            note="SystemExit funnel, whose error code the shared helper classifies from the message",
        ),
    )
