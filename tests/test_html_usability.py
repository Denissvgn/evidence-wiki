"""HTML usability counterexamples, extraction compatibility, and capture contracts."""

import contextlib
import hashlib
import io
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from evidence_wiki.cli import main
from evidence_wiki.pack_discovery import owner

CORPUS_ROOT = Path(__file__).parent / "fixtures" / "html-usability"
PAGES = json.loads((CORPUS_ROOT / "pages.json").read_text(encoding="utf-8"))
NORMALIZE = owner("normalize_sources")


def verdict_case(page):
    marks = ()
    if page["rule"] in {"gateway", "authentication"}:
        marks = pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=f"Native {page['rule']} shell detection is absent.",
        )
    return pytest.param(page, id=page["name"], marks=marks)


def normalize_page(root, page):
    relative = "raw/web/" + page["file_name"]
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page["html"], encoding="utf-8")
    record = {"id": "web:" + page["name"], "kind": "html", "raw_paths": [relative], "status": "discovered"}
    source = NORMALIZE.normalize_html_record(root, record)
    metadata = NORMALIZE.frontmatter_for(
        source, "sources/manifest.jsonl", root / "sources/normalized/capture.md", "2026-09-28"
    )
    return source, metadata


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page["name"])
def test_html_extraction_preserves_retained_evidence(tmp_path, page):
    source, _ = normalize_page(tmp_path, page)
    actual = {
        key: getattr(source, key)
        for key in ("title", "title_confidence", "abstract", "outline", "extracted_text", "links")
    }
    actual["outline"] = [list(entry) for entry in actual["outline"]]
    actual["content_hash"] = NORMALIZE.content_hash(source)
    assert actual == page["extraction"]


@pytest.mark.parametrize("page", [verdict_case(page) for page in PAGES])
def test_html_page_evidence_usability(tmp_path, page):
    source, metadata = normalize_page(tmp_path, page)
    assert (metadata["unusable_evidence_reasons"] or []) == page["expected_reasons"]
    assert metadata["evidence_usable"] is (not page["expected_reasons"])
    for reason in page["expected_reasons"]:
        assert any(f"unusable evidence: {reason}" in warning for warning in source.warnings)


MESSAGE_NAMES = {"gateway-body", "signin-body", "maintenance", "short-http-definition", "authentication-guide"}


@pytest.mark.parametrize("page", [verdict_case(page) for page in PAGES if page["name"] in MESSAGE_NAMES])
def test_html_message_helper_retains_positional_contract(page):
    expected = page["extraction"]
    reasons = NORMALIZE.html_unusable_evidence_reasons(expected["title"], expected["extracted_text"], page["html"])
    assert reasons == page["expected_reasons"]


CAPTURE_PAGES = [page for page in PAGES if page["name"] in {"gateway-body", "signin-body"}]


@pytest.fixture(params=CAPTURE_PAGES, ids=lambda page: page["name"])
def captured_html(request, tmp_path):
    page = request.param
    root = tmp_path / "workspace"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert main([
            "init", "--target", str(root), "--project-name", "html-capture",
            "--project-description", "Retain HTML capture observations.",
        ]) == 0
        relative = "raw/web/" + page["file_name"]
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = page["html"].encode("utf-8")
        path.write_bytes(data)
        provenance = {
            "origin_url": "https://example.org/capture", "retrieved_at": "2026-09-28T10:00:00Z",
            "retrieved_by": "html-capture", "source_type": "official_web", "license": "CC0-1.0",
            "checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
        }
        path.with_name(path.name + ".provenance.yml").write_text(yaml.safe_dump(provenance), encoding="utf-8")
        assert owner("source_inventory").main(["--project-root", str(root)]) == 0
        records = [json.loads(line) for line in (root / "sources/manifest.jsonl").read_text().splitlines()]
        record = next(record for record in records if relative in record["raw_paths"])
        assert NORMALIZE.main(["--project-root", str(root), "--source-id", record["id"], "--format", "json"]) == 0
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    verification = owner("normalize_verify").run_verify(root, source_ids=[record["id"]])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert main(["agent", "source-status", "--target", str(root), "--source-id", record["id"]]) == 0
    observation = json.loads(output.getvalue())["sources"][0]
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    return page, verification, observation, before, after


def test_html_capture_format_and_readonly_observation(captured_html):
    _, verification, row, before, after = captured_html
    assert verification["overall_result"] == "verified"
    assert row["extraction"] == "content_extracted"
    assert row["retrieval"] == "lexically_indexable"
    assert row["complete"] is True and row["completeness_basis"] == "normalizer_status"
    assert row["semantic_adequacy"] == "not_evaluated" and row["evidence_accepted"] is False
    assert before == after


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Native shell captures are still classified as usable.")
def test_html_capture_shell_is_unusable(captured_html):
    page, _, row, _, _ = captured_html
    assert row["evidence_usable"] is False
    assert row["usability"] == "not_ready"
    assert row["reasons"] == page["expected_reasons"]


def parse_context(html):
    extractor = NORMALIZE.HTMLContentExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.classification_context()


def test_classification_context_retains_actual_titles_headings_and_forms():
    context = parse_context(
        '<html><head><title>SIGN IN</title></head><body><h1>Sign in</h1>'
        '<form><label>Password<input type="PASSWORD"></label><button>Sign in</button></form>'
        '<p>The observed reflectance is 0.74.</p></body></html>'
    )
    assert context.title == "sign in"
    assert context.password_form_ids == (1,)
    assert [(block.tag, block.text, block.form_id) for block in context.blocks] == [
        ("title", "sign in", None), ("h1", "sign in", None), ("label", "password", 1),
        ("button", "sign in", 1), ("p", "the observed reflectance is 0.74.", None),
    ]
    assert not context.degraded and not context.truncated
    with pytest.raises(FrozenInstanceError):
        context.degraded = True
    with pytest.raises(FrozenInstanceError):
        context.blocks[0].text = "changed"


@pytest.mark.parametrize("tag", ["script", "style", "nav", "noscript", "template", "svg", "iframe"])
def test_classification_context_excludes_skipped_content(tag):
    context = parse_context(f'<{tag}>Please sign in to continue.</{tag}><p>Measured value: 42.</p>')
    assert context.body_text == "measured value: 42."
    assert context.password_form_ids == ()


@pytest.mark.parametrize("attribute", ['hidden', 'hidden="false"', 'aria-hidden="TRUE"'])
def test_hidden_ancestry_cannot_supply_text_or_password_controls(attribute):
    context = parse_context(
        f'<div {attribute}><span><form><input type=password><p>Please sign in to continue.</p></form></span></div>'
        '<p>Measured value: 42.</p>'
    )
    assert context.body_text == "measured value: 42."
    assert context.password_form_ids == ()


@pytest.mark.parametrize("tag", ["code", "pre", "blockquote", "q", "kbd", "samp", "textarea"])
def test_examples_remain_independent_text_without_credential_signals(tag):
    context = parse_context(f'<{tag}><form>502 Bad Gateway<input type=password></form></{tag}>')
    # RCDATA parsing can retain the literal markup; either representation is protected text.
    assert context.body_text in {"502 bad gateway", "<form>502 bad gateway<input type=password></form>"}
    assert context.password_form_ids == ()
    assert all(block.is_example for block in context.blocks)


def test_attributes_comments_and_head_metadata_do_not_become_messages():
    context = parse_context(
        '<html><head>Unstructured head data<meta name="description" content="Please sign in to continue.">'
        '<title>Observations</title></head><body><!-- 502 Bad Gateway -->'
        '<p data-message="Password required">Measured value: 42.</p>'
        '<img alt="Please sign in to continue."></body></html>'
    )
    assert context.title == "observations"
    assert context.body_text == "measured value: 42."
    assert not context.degraded


def test_hidden_or_example_titles_are_not_primary_title_evidence():
    context = parse_context('<title hidden>Login</title><code><title>Sign in</title></code><h1>Observations</h1>')
    assert context.title == ""
    assert context.body_text == "sign in observations"
    assert context.blocks[0].is_example


def test_form_prose_and_control_labels_are_preserved_separately():
    context = parse_context(
        '<form><p>This document explains authentication failures.</p>'
        '<label>Password<input type=password></label><button>Sign in</button></form>'
    )
    assert [(block.tag, block.text, block.form_id) for block in context.blocks] == [
        ("p", "this document explains authentication failures.", 1),
        ("label", "password", 1), ("button", "sign in", 1),
    ]


@pytest.mark.parametrize("html", [
    '<form><input type=password disabled></form>',
    '<form><fieldset disabled><legend><input type=password></legend></fieldset></form>',
    '<form><input type=password form="other"></form>',
    '<input type=password>',
    '<template><form><input type=password></form></template>',
])
def test_ineligible_password_controls_never_supply_a_form_signal(html):
    assert parse_context(html).password_form_ids == ()


def test_distinct_forms_have_distinct_local_identifiers():
    context = parse_context('<form><input type=password /></form><form><input TYPE="password"></form>')
    assert context.password_form_ids == (1, 2)
    assert not context.degraded


@pytest.mark.parametrize("html", [
    '<form><form><input type=password></form></form>',
    '<form><input type=text type=password></form>',
    '<div><span>Message</div>',
    '<title>One</title><title>Two</title>',
    '<p>Message',
    '</unknown><p>Message</p>',
])
def test_ambiguous_or_unbalanced_context_is_qualified_as_degraded(html):
    assert parse_context(html).degraded


def test_nested_forms_and_duplicate_control_types_cannot_supply_password_signals():
    for html in (
        '<form><form><input type=password></form></form>',
        '<form><input type=text type=password></form>',
    ):
        assert parse_context(html).password_form_ids == ()


def test_inline_markup_keeps_original_word_boundaries_and_normalizes_supported_text():
    context = parse_context('<p>Please <b>SIGN&#x2011;IN</b>&nbsp;to\ncontinue. Straße</p>')
    assert context.body_text == "please sign-in to continue. strasse"
    assert parse_context('<p>Bad<span>Gateway</span></p>').body_text == "badgateway"
    assert parse_context('<p>Bad <span>Gateway</span></p>').body_text == "bad gateway"


def test_protected_inline_content_is_not_rejoined_into_an_unquoted_message():
    context = parse_context('<p>Please <q>sign in</q> to continue.</p>')
    assert [(block.text, block.is_example) for block in context.blocks] == [
        ("please", False), ("sign in", True), ("to continue.", False),
    ]


def test_classification_is_independent_of_feed_chunking():
    html = '<title>Login</title><form><label>Password<input TYPE=password></label></form><p>Bad&nbsp;Gateway</p>'
    extractor = NORMALIZE.HTMLContentExtractor()
    for character in html:
        extractor.feed(character)
    extractor.close()
    assert extractor.classification_context() == parse_context(html)


def test_snapshot_before_parser_close_is_not_a_complete_observation():
    extractor = NORMALIZE.HTMLContentExtractor()
    assert extractor.classification_context().degraded
    extractor.feed('<p>Measured value: 42.</p>')
    assert extractor.classification_context().degraded
    extractor.close()
    complete = extractor.classification_context()
    assert not complete.degraded
    extractor.feed('<p>Another observation.</p>')
    assert extractor.classification_context().degraded
    extractor.close()
    assert not extractor.classification_context().degraded
    assert complete.body_text == "measured value: 42."
    extractor.feed('<!-- incomplete comment')
    assert extractor.classification_context().degraded
    extractor.feed('-->')
    assert extractor.classification_context().degraded
    extractor.close()
    assert not extractor.classification_context().degraded


def test_long_retained_evidence_continues_after_context_limit():
    text = 'a' * (NORMALIZE.HTML_CONTEXT_MAX_CHARS + 500)
    extractor = NORMALIZE.HTMLContentExtractor()
    extractor.feed('<p>' + text + '</p><p>Final observation.</p>')
    extractor.close()
    assert extractor.classification_context().truncated
    body = NORMALIZE.normalize_html_body_text(extractor.text_chunks)
    assert text in body and body.endswith('Final observation.')


def test_whitespace_overflow_cannot_hide_later_independent_content():
    context = parse_context('<p>502 Bad Gateway' + ' ' * NORMALIZE.HTML_CONTEXT_MAX_CHARS + 'Useful explanation.</p>')
    assert context.truncated


def test_unsupported_unicode_spelling_is_not_silently_repaired():
    assert parse_context('<p>sign\u200bin</p>').body_text == 'sign\u200bin'


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_classification_character_storage_has_an_explicit_boundary(offset):
    limit = NORMALIZE.HTML_CONTEXT_MAX_CHARS
    context = parse_context('<p>' + 'a' * (limit + offset) + '</p>')
    assert context.truncated is (offset > 0)
    assert len(context.body_text) <= limit


def test_casefold_expansion_cannot_exceed_classification_storage():
    limit = NORMALIZE.HTML_CONTEXT_MAX_CHARS
    context = parse_context('<p>' + 'ß' * limit + '</p>')
    assert context.truncated and len(context.body_text) <= limit


@pytest.mark.parametrize("kind", ["blocks", "depth", "forms"])
@pytest.mark.parametrize("offset", [0, 1])
def test_classification_structure_limits_are_explicit(kind, offset):
    if kind == "blocks":
        html = '<p>a</p>' * (NORMALIZE.HTML_CONTEXT_MAX_BLOCKS + offset)
    elif kind == "depth":
        count = NORMALIZE.HTML_CONTEXT_MAX_DEPTH + offset
        html = '<div>' * count + 'a' + '</div>' * count
    else:
        html = '<form><input type=password></form>' * (NORMALIZE.HTML_CONTEXT_MAX_FORMS + offset)
    context = parse_context(html)
    assert context.truncated is (offset > 0)
    assert len(context.blocks) <= NORMALIZE.HTML_CONTEXT_MAX_BLOCKS
    assert len(context.password_form_ids) <= NORMALIZE.HTML_CONTEXT_MAX_FORMS


def test_empty_void_and_self_closing_tags_do_not_leave_context_frames():
    context = parse_context('<p>One<br>two<br/>three<hr>four<img src=x><input type=text><span/>five</p>')
    assert context.body_text == "one two three fourfive"
    assert not context.degraded


def test_normalizer_supplies_context_without_promoting_filename_to_title(tmp_path, monkeypatch):
    observed = []
    original = NORMALIZE.html_unusable_evidence_reasons

    def inspect_context(title, body_text, raw_html, *, context=None):
        observed.append(context)
        return original(title, body_text, raw_html, context=context)

    monkeypatch.setattr(NORMALIZE, "html_unusable_evidence_reasons", inspect_context)
    page = next(page for page in PAGES if page["name"] == "filename-sign-in")
    source, _ = normalize_page(tmp_path, page)
    assert source.title == "sign-in"
    assert observed[0].title == "" and observed[0].body_text == "measured value: 42."


@pytest.mark.parametrize("failure", ["parser", "reader"])
def test_degraded_extraction_cannot_supply_unqualified_classification_context(tmp_path, monkeypatch, failure):
    observed = []
    original = NORMALIZE.html_unusable_evidence_reasons

    def inspect_context(title, body_text, raw_html, *, context=None):
        observed.append(context)
        return original(title, body_text, raw_html, context=context)

    monkeypatch.setattr(NORMALIZE, "html_unusable_evidence_reasons", inspect_context)
    if failure == "parser":
        def broken_feed(self, text):
            raise ValueError("Invalid markup")
        monkeypatch.setattr(NORMALIZE.HTMLContentExtractor, "feed", broken_feed)
    else:
        monkeypatch.setattr(NORMALIZE, "HTML_MAX_BYTES", 12)
    page = next(page for page in PAGES if page["name"] == "gateway-body")
    source, _ = normalize_page(tmp_path, page)
    assert observed[0].degraded
    assert any("malformed HTML" in warning or "truncated" in warning for warning in source.warnings)
