"""HTML usability counterexamples, extraction compatibility, and capture contracts."""

import contextlib
import copy
import hashlib
import io
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import mock_open

import pytest
import yaml

from evidence_wiki.cli import main
from evidence_wiki.pack_discovery import owner

CORPUS_ROOT = Path(__file__).parent / "fixtures" / "html-usability"
PAGES = json.loads((CORPUS_ROOT / "pages.json").read_text(encoding="utf-8"))
NORMALIZE = owner("normalize_sources")


def normalize_page(root, page, *, record_changes=None, raw_bytes=None):
    relative = "raw/web/" + page["file_name"]
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw_bytes is None:
        path.write_text(page["html"], encoding="utf-8")
    else:
        path.write_bytes(raw_bytes)
    record = {"id": "web:" + page["name"], "kind": "html", "raw_paths": [relative], "status": "discovered"}
    record.update(copy.deepcopy(record_changes or {}))
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


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page["name"])
def test_html_page_evidence_usability(tmp_path, page):
    source, metadata = normalize_page(tmp_path, page)
    assert (metadata["unusable_evidence_reasons"] or []) == page["expected_reasons"]
    assert metadata["evidence_usable"] is (not page["expected_reasons"])
    for reason in page["expected_reasons"]:
        assert any(f"unusable evidence: {reason}" in warning for warning in source.warnings)


MESSAGE_NAMES = {"gateway-body", "signin-body", "maintenance", "short-http-definition", "authentication-guide"}


@pytest.mark.parametrize("page", [page for page in PAGES if page["name"] in MESSAGE_NAMES], ids=lambda page: page["name"])
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


@pytest.mark.parametrize("captured_html", CAPTURE_PAGES, ids=lambda page: page["name"], indirect=True)
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


@pytest.fixture(params=[False, True], ids=["immediate", "buffered"])
def parser_buffering(request, monkeypatch):
    """Exercise eager and deferred base parsing without depending on a runtime patch."""
    def feed(parser, data):
        if not request.param:
            parser.rawdata += data
            parser.goahead(False)
            return
        if not hasattr(parser, "_queued_input"):
            parser._queued_input, parser._queued_size, parser._queue_threshold = [], 0, 1
        parser._queued_input.append(data)
        parser._queued_size += len(data)
        if parser._queued_size < parser._queue_threshold:
            return
        parser.rawdata += "".join(parser._queued_input)
        parser._queued_input.clear()
        parser._queued_size = 0
        size = len(parser.rawdata)
        parser.goahead(False)
        parser._queue_threshold = 1 if len(parser.rawdata) < size else size

    def close(parser):
        parser.rawdata += "".join(getattr(parser, "_queued_input", []))
        if hasattr(parser, "_queued_input"):
            parser._queued_input.clear()
            parser._queued_size = 0
        parser.goahead(True)

    monkeypatch.setattr(NORMALIZE.HTMLParser, "feed", feed)
    monkeypatch.setattr(NORMALIZE.HTMLParser, "close", close)


@pytest.mark.parametrize("prefix,suffix", [
    ('<!-- completed comment', '-->'), ('<br title="', 'x">'), ('<script>hidden</scr', 'ipt>'),
], ids=["comment", "tag", "skipped-content"])
def test_deferred_completed_markup_does_not_degrade_classification(parser_buffering, prefix, suffix):
    parser = NORMALIZE.HTMLContentExtractor()
    parser.feed('<p>Measured value: 42.</p>')
    parser.feed(prefix)
    parser.feed(suffix)
    assert parser.classification_context().degraded
    parser.close()
    assert not parser.classification_context().degraded
    assert not parser.unbalanced_skip_tags
    assert NORMALIZE.normalize_html_body_text(parser.text_chunks) == 'Measured value: 42.'
    complete = parser.classification_context()
    parser.close()
    assert parser.classification_context() == complete


@pytest.mark.parametrize("tail", ['<!-- unfinished', '<!doctype', '<?unfinished', '<unfinished attr="', '</unfinished'])
@pytest.mark.parametrize("chunk_size", [1, 7, None])
def test_deferred_unfinished_markup_stays_degraded(parser_buffering, tail, chunk_size):
    html = '<p>Bad Gateway.</p>' + tail
    parser = NORMALIZE.HTMLContentExtractor()
    size = chunk_size or len(html)
    for offset in range(0, len(html), size):
        parser.feed(html[offset:offset + size])
    parser.close()
    assert parser.classification_context().degraded
    assert not NORMALIZE.html_gateway_shell(parser.classification_context())
    parser.close()
    assert parser.classification_context().degraded


def test_heading_flush_follows_deferred_eof_data(parser_buffering):
    parser = NORMALIZE.HTMLContentExtractor()
    parser.feed('<h1>')
    parser.feed('Measured &am')
    parser.feed('p;')
    parser.close()
    assert parser.outline == [(2, 'Measured &')]
    assert NORMALIZE.normalize_html_body_text(parser.text_chunks) == 'Measured &'


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


@pytest.mark.parametrize("message", [
    "Bad Gateway", "502 Bad Gateway", "HTTP 502 Bad Gateway", "HTTP Error 502 Bad Gateway",
    "HTTP Error 502: Bad Gateway", "Gateway Timeout", "Gateway Time-out", "504 Gateway Timeout",
    "504 Gateway Time-out", "HTTP 504 Gateway Timeout", "HTTP 504 Gateway Time-out",
    "HTTP Error 504 Gateway Timeout", "HTTP Error 504: Gateway Timeout",
    "HTTP Error 504 Gateway Time-out", "HTTP Error 504: Gateway Time-out",
])
def test_canonical_gateway_messages_are_shells(message):
    assert NORMALIZE.html_gateway_shell(parse_context(f'<p>{message}. Please try again later.</p>'))


@pytest.mark.parametrize("html", [
    '<p>502</p>', '<p>504</p>', '<p>HTTP 502: Bad Gateway</p>',
    '<p>HTTP Error 502: Gateway Timeout</p>', '<p>HTTP Error 504: Bad Gateway</p>',
    '<p>502 Bad Gateway means an invalid upstream response.</p>',
    '<title>Bad Gateway</title><p>A proxy received an invalid response.</p>',
    '<p>A prior outage returned 504 Gateway Timeout; the service recovered.</p>',
    '<h1>502 Bad Gateway</h1><pre>Measured value: 42.</pre>',
    '<blockquote>502 Bad Gateway.</blockquote>', '<p>"502 Bad Gateway."</p>',
    '<a href="/guide"><h1>502 Bad Gateway</h1></a>',
    '<button><div>504 Gateway Timeout</div></button>',
    '<label><div>Bad Gateway</div></label>',
    '<meta name="description" content="502 Bad Gateway">',
    '<p data-error="502 Bad Gateway">Measured value: 42.</p>',
    '<!-- 502 Bad Gateway --><p>Measured value: 42.</p>',
    '<script>const message = "502 Bad Gateway";</script><p>Measured value: 42.</p>',
])
def test_gateway_mentions_and_control_ancestry_are_not_primary_error_pages(html):
    assert not NORMALIZE.html_gateway_shell(parse_context(html))


@pytest.mark.parametrize("flag", ["degraded", "truncated"])
def test_incomplete_gateway_context_cannot_establish_a_shell(flag):
    context = replace(parse_context('<p>502 Bad Gateway.</p>'), **{flag: True})
    assert not NORMALIZE.html_gateway_shell(context)


def test_gateway_detection_never_promotes_a_caller_supplied_title():
    assert NORMALIZE.html_unusable_evidence_reasons('502 Bad Gateway', 'Measured value: 42.', '<p>Measured value: 42.</p>') == []


def test_native_html_normalization_reuses_its_single_parse(tmp_path, monkeypatch):
    calls = []
    original = NORMALIZE.HTMLContentExtractor.feed

    def observe_feed(self, data):
        calls.append(data)
        return original(self, data)

    monkeypatch.setattr(NORMALIZE.HTMLContentExtractor, "feed", observe_feed)
    page = next(page for page in PAGES if page["name"] == "gateway-body")
    _, metadata = normalize_page(tmp_path, page)
    assert not metadata["evidence_usable"]
    assert calls == [page["html"]]


def test_direct_parser_fallback_enforces_byte_limit_before_parsing(monkeypatch):
    monkeypatch.setattr(NORMALIZE, "HTML_MAX_BYTES", 32)

    def forbidden_feed(self, data):
        pytest.fail("Oversized direct context input reached the parser")

    monkeypatch.setattr(NORMALIZE.HTMLContentExtractor, "feed", forbidden_feed)
    context = NORMALIZE.html_context_from_text('é' * 20)
    assert context.degraded and context.truncated


@pytest.mark.parametrize("length", [999, 1000, 1001])
def test_gateway_body_character_boundary_is_strict(length):
    repeat, padding = divmod(length - len("bad gateway"), len(". try again later"))
    body = "bad gateway" + ". try again later" * repeat + "." * padding
    context = parse_context('<p>' + body + '</p>')
    assert len(context.body_text) == length
    assert NORMALIZE.html_gateway_shell(context) is (length < 1000)


@pytest.mark.parametrize("prefix", ["", "Please "])
@pytest.mark.parametrize("action", ["sign in", "sign-in", "log in", "log-in", "login"])
@pytest.mark.parametrize("destination", [
    "continue", "continue reading", "view this page", "view this content", "view the content",
    "read this page", "read this content", "read the content", "access this page", "access this content",
    "access the content",
])
def test_supported_access_directives_require_no_form(prefix, action, destination):
    context = parse_context(f'<p>{prefix}{action} to {destination}.</p>')
    assert NORMALIZE.html_authentication_shell(context)


@pytest.mark.parametrize("message", [
    "Authentication required", "Login required", "Sign-in required", "Please enter your password to continue",
])
def test_explicit_authentication_requirements_are_shells(message):
    assert NORMALIZE.html_authentication_shell(parse_context(f'<h1>{message}!</h1>'))


@pytest.mark.parametrize("html", [
    '<p>Password required.</p>', '<p>Password</p>', '<p>Login</p>', '<h1>Sign in</h1>',
    '<p>Password required length is twelve characters.</p>',
    '<p>Authentication required by the protocol is explained here.</p>',
    '<p>Please sign in to continue. This guide explains the old login prompt.</p>',
    '<p>"Please sign in to continue."</p>', '<blockquote>Please sign in to continue.</blockquote>',
    '<code>Please sign in to continue.</code>', '<a href="/login">Please sign in to continue.</a>',
    '<a href="/login"><h1>Please sign in to continue.</h1></a>',
    '<button><div>Please sign in to continue.</div></button>',
    '<h1>Authentication required</h1><pre>Measured result: 42.</pre>',
    '<form><p>Please sign in to continue.</p><p>This guide explains authentication failures.</p></form>',
    '<meta name="description" content="Please sign in to continue.">',
    '<div hidden>Please sign in to continue.</div><p>Measured result: 42.</p>',
])
def test_authentication_subjects_examples_and_controls_do_not_supply_a_gate(html):
    assert not NORMALIZE.html_authentication_shell(parse_context(html))


def test_authentication_title_and_password_corroboration_preserve_textual_gate():
    context = parse_context('<title>Sign in</title><p>Please sign in to continue. Password required.</p>')
    assert NORMALIZE.html_authentication_shell(context)


@pytest.mark.parametrize("length", [199, 200, 201])
def test_authentication_body_character_boundary_is_strict(length):
    base = "authentication required"
    repeat, padding = divmod(length - len(base), len(". try again later"))
    body = base + ". try again later" * repeat + "." * padding
    context = parse_context('<p>' + body + '</p>')
    assert len(context.body_text) == length
    assert NORMALIZE.html_authentication_shell(context) is (length < 200)


@pytest.mark.parametrize("flag", ["degraded", "truncated"])
def test_incomplete_authentication_context_cannot_establish_a_gate(flag):
    context = replace(parse_context('<p>Please sign in to continue.</p>'), **{flag: True})
    assert not NORMALIZE.html_authentication_shell(context)


@pytest.mark.parametrize("html", [
    '<title>Sign in</title><form><label>Password<input type=password></label></form>',
    '<h1>Login</h1><form><input name=p type="PASSWORD" /></form>',
    '<form><label>Password<input type=password></label><button>Log in</button></form>',
    '<form><h2>Password required</h2><input type=password></form>',
    '<h1>Password required</h1><form><input type=password></form>',
    '<form><input type=password><button>Please sign in to continue.</button></form>',
    '<title>Sign-in</title><form><label>Username<input name=user></label>'
    '<label>Password<input type=password></label><a href="/reset">Forgot password</a></form>',
])
def test_credential_form_requires_a_corresponding_authentication_cue(html):
    context = parse_context(html)
    assert context.password_form_ids
    assert NORMALIZE.html_authentication_shell(context)


@pytest.mark.parametrize("html", [
    '<form><label>Password<input type=password></label></form>',
    '<h1>Sign in</h1><input type=password>',
    '<h1>Sign in</h1><form><input type=text></form>',
    '<h1>Sign in</h1><form><input type=password disabled></form>',
    '<h1>Sign in</h1><form><fieldset disabled><input type=password></fieldset></form>',
    '<h1>Sign in</h1><form hidden><input type=password></form>',
    '<h1>Sign in</h1><form aria-hidden="true"><input type=password></form>',
    '<h1>Sign in</h1><form><input type=password form="other"></form>',
    '<form><input type=password></form><form><button>Sign in</button></form>',
    '<form><input type=password></form><form><h1>Sign in</h1></form>',
    '<a href="/login"><h1>Sign in</h1></a><form><input type=password></form>',
    '<h1>Sign in</h1><form><p>This document explains credential controls.</p><input type=password></form>',
    '<p>Measured value: 42.</p><form><input type=password><button>Sign in</button></form>',
    '<h1>Sign in</h1><form><form><input type=password></form></form>',
    '<h1>Sign in</h1><form><input type=text type=password></form>',
    '<h1>Sign in</h1><pre><form><input type=password></form></pre>',
    '<h1>Sign in</h1><textarea><form><input type=password></form></textarea>',
])
def test_unrelated_inactive_ambiguous_and_example_forms_do_not_establish_a_gate(html):
    assert not NORMALIZE.html_authentication_shell(parse_context(html))


@pytest.mark.parametrize("control", ["a", "button", "select", "option"])
def test_password_inputs_inside_other_interactive_controls_are_not_eligible(control):
    html = f'<h1>Sign in</h1><form><{control}><input type=password></{control}></form>'
    context = parse_context(html)
    assert context.password_form_ids == ()
    assert not NORMALIZE.html_authentication_shell(context)


def html_page(html):
    return {"name": "captured-content", "file_name": "capture.html", "html": html}


def audited_override():
    return {
        "usable": True, "reviewed_by": "fixture-reviewer", "reviewed_at": "2026-09-28T10:00:00Z",
        "reason": "Reviewer verified the retained content beyond the generic JavaScript heuristic.",
    }


@pytest.mark.parametrize("html,reasons", [
    ('<p>502 Bad Gateway. Please sign in to continue. Password required.</p>',
     ["html_error_page:official_error_page", "html_authentication_shell"]),
    ('<p>502 Bad Gateway. Please sign in to continue.</p><script src="a.js"></script><script src="b.js"></script>',
     ["html_error_page:official_error_page", "html_javascript_shell", "html_authentication_shell"]),
    ('<p>Please sign in to continue. Please enable JavaScript.</p>',
     ["html_javascript_shell", "html_authentication_shell"]),
    ('<p>Service temporarily unavailable. Please sign in to continue.</p>',
     ["html_error_page:official_error_page", "html_authentication_shell"]),
    ('<p>404 Not Found. Please sign in to continue.</p>',
     ["html_error_page:not_found", "html_authentication_shell"]),
    ('<p>502 Bad Gateway. 502 Bad Gateway. Please sign in to continue. Please sign in to continue.</p>',
     ["html_error_page:official_error_page", "html_authentication_shell"]),
])
def test_independent_shell_reasons_compose_and_remain_unique(tmp_path, html, reasons):
    source, metadata = normalize_page(tmp_path, html_page(html))
    assert metadata["unusable_evidence_reasons"] == reasons
    assert not metadata["evidence_usable"]
    for reason in reasons:
        assert source.warnings.count(f"web:captured-content: unusable evidence: {reason}") == 1


@pytest.mark.parametrize("gate,remaining", [
    ('502 Bad Gateway.', ["html_error_page:official_error_page"]),
    ('Please sign in to continue.', ["html_authentication_shell"]),
    ('502 Bad Gateway. Please sign in to continue.', ["html_error_page:official_error_page", "html_authentication_shell"]),
])
def test_javascript_override_cannot_clear_other_shell_reasons(tmp_path, gate, remaining):
    html = f'<p>{gate}</p><script src="a.js"></script><script src="b.js"></script>'
    source, metadata = normalize_page(tmp_path, html_page(html), record_changes={
        "provenance": {"evidence_usability_override": audited_override(), "evidence_usability_override_applied": True},
    })
    assert not metadata["evidence_usable"]
    assert metadata["unusable_evidence_reasons"] == remaining
    assert "evidence_usability_override_applied" not in metadata["provenance"]
    assert not any("unusable evidence: html_javascript_shell" in warning for warning in source.warnings)
    assert any("override cleared: html_javascript_shell" in warning for warning in source.warnings)


def test_javascript_only_override_records_clearance_without_an_active_refusal_warning(tmp_path):
    html = '<p>Measured reflectance: 0.74.</p><script src="a.js"></script><script src="b.js"></script>'
    source, metadata = normalize_page(tmp_path, html_page(html), record_changes={
        "provenance": {"evidence_usability_override": audited_override()},
    })
    assert metadata["evidence_usable"] and metadata["unusable_evidence_reasons"] is None
    assert metadata["provenance"]["evidence_usability_override_applied"] is True
    assert not any("unusable evidence:" in warning for warning in source.warnings)
    assert any("override cleared: html_javascript_shell" in warning for warning in source.warnings)


@pytest.mark.parametrize("field,value", [
    ("usable", False), ("usable", 1), ("usable", "true"), ("reviewed_by", None),
    ("reviewed_by", ""), ("reviewed_at", 42), ("reviewed_at", "  "), ("reason", ""), ("reason", []),
])
def test_invalid_overrides_cannot_clear_javascript_refusal(tmp_path, field, value):
    review = audited_override()
    review[field] = value
    page = next(page for page in PAGES if page["name"] == "javascript-only")
    _, metadata = normalize_page(tmp_path, page, record_changes={"provenance": {
        "evidence_usability_override": review, "evidence_usability_override_applied": True,
    }})
    assert not metadata["evidence_usable"]
    assert metadata["unusable_evidence_reasons"] == ["html_javascript_shell"]
    assert "evidence_usability_override_applied" not in metadata["provenance"]


@pytest.mark.parametrize("field", ["usable", "reviewed_by", "reviewed_at", "reason"])
def test_incomplete_overrides_cannot_clear_javascript_refusal(tmp_path, field):
    review = audited_override()
    del review[field]
    page = next(page for page in PAGES if page["name"] == "javascript-only")
    _, metadata = normalize_page(tmp_path, page, record_changes={"provenance": {"evidence_usability_override": review}})
    assert metadata["unusable_evidence_reasons"] == ["html_javascript_shell"]


@pytest.mark.parametrize("review", [None, {}, "reviewed", {"usable": True}, audited_override()])
def test_an_applied_marker_is_not_authority_to_erase_explicit_unusability(review):
    record = {"evidence_usable": False, "provenance": {
        "evidence_usability_override": review, "evidence_usability_override_applied": True,
    }}
    NORMALIZE.apply_record_usability_override(record)
    assert record["evidence_usable"] is False
    assert record["unusable_evidence_reasons"] == ["evidence_usable:false"]
    assert "evidence_usability_override_applied" not in record["provenance"]


def test_remaining_explicit_reasons_remove_stale_full_clearance_marker():
    record = {"evidence_usable": False, "unusable_evidence_reasons": ["html_authentication_shell"], "provenance": {
        "evidence_usability_override": audited_override(), "evidence_usability_override_applied": True,
    }}
    assert NORMALIZE.record_unusable_evidence_reasons(record) == ["html_authentication_shell"]
    assert "evidence_usability_override_applied" not in record["provenance"]


@pytest.mark.parametrize("failure", ["source_status", "delivery_failure_code", "explicit_flag", "explicit_reason"])
def test_existing_refusals_survive_clean_content_and_a_review(tmp_path, failure):
    changes = {"provenance": {"evidence_usability_override": audited_override()}}
    if failure == "source_status":
        changes["provenance"]["source_status"] = "unavailable"
        expected = "source_status:unavailable"
    elif failure == "delivery_failure_code":
        changes["provenance"]["delivery_failure_code"] = "http_error"
        expected = "delivery_failure_code:http_error"
    elif failure == "explicit_flag":
        changes["evidence_usable"] = False
        expected = "evidence_usable:false"
    else:
        changes["unusable_evidence_reasons"] = ["manual_review_required", "manual_review_required"]
        expected = "manual_review_required"
    _, metadata = normalize_page(tmp_path, html_page('<p>Measured reflectance: 0.74.</p>'), record_changes=changes)
    assert not metadata["evidence_usable"] and metadata["unusable_evidence_reasons"] == [expected]


def test_available_provenance_does_not_overrule_detected_authentication(tmp_path):
    _, metadata = normalize_page(tmp_path, html_page('<p>Please sign in to continue.</p>'), record_changes={
        "provenance": {"source_status": "available", "checksum_verified": True},
    })
    assert metadata["unusable_evidence_reasons"] == ["html_authentication_shell"]


@pytest.mark.parametrize("retain_failure", [False, True])
def test_fresh_inventory_replacement_recomputes_native_reasons_without_changing_raw_evidence(tmp_path, retain_failure):
    root = tmp_path / "workspace"
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        assert main(["init", "--target", str(root), "--project-name", "capture-replacement",
                     "--project-description", "Recompute capture observations."]) == 0
    raw = root / "raw/web/capture.html"
    sidecar = raw.with_name(raw.name + ".provenance.yml")
    results = []
    for html in ('<p>Please sign in to continue.</p>', '<p>Measured reflectance: 0.74.</p>'):
        raw.write_text(html, encoding="utf-8")
        provenance = {"origin_url": "https://example.org/capture", "retrieved_at": "2026-09-28T10:00:00Z",
                      "retrieved_by": "fixture", "source_type": "official_web", "license": "CC0-1.0",
                      "checksum": "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest()}
        if retain_failure:
            provenance["delivery_failure_code"] = "http_error"
        sidecar.write_text(yaml.safe_dump(provenance), encoding="utf-8")
        originals = raw.read_bytes(), sidecar.read_bytes()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            assert owner("source_inventory").main(["--project-root", str(root)]) == 0
            manifest = (root / "sources/manifest.jsonl").read_bytes()
            record = json.loads(manifest.decode().strip())
            assert NORMALIZE.main(["--project-root", str(root), "--source-id", record["id"], "--format", "json"]) == 0
        path = root / "sources/normalized" / (NORMALIZE.safe_source_id(record["id"]) + ".md")
        results.append(NORMALIZE.read_output_frontmatter(path))
        assert originals == (raw.read_bytes(), sidecar.read_bytes())
        assert (root / "sources/manifest.jsonl").read_bytes() == manifest
    assert "html_authentication_shell" in results[0]["unusable_evidence_reasons"]
    assert "html_authentication_shell" not in (results[1]["unusable_evidence_reasons"] or [])
    assert results[1]["evidence_usable"] is (not retain_failure)
    if retain_failure:
        assert results[1]["unusable_evidence_reasons"] == ["delivery_failure_code:http_error"]


@pytest.mark.parametrize("body,reason,retained_hash", [
    ("502 Bad Gateway.", "html_error_page:official_error_page",
     "sha256:6e288c37f1804cb5a539f2d7d47e1804c664d3f12ce9583ea97e7392476076aa"),
    ("Please sign in to continue.", "html_authentication_shell",
     "sha256:9d71c9c5a1bb46d2d49465eb5059c94bf751702bad8aa236df97531ea20c710d"),
])
def test_initial_bom_does_not_change_shell_verdict_or_retained_evidence(tmp_path, body, reason, retained_hash):
    source, metadata = normalize_page(tmp_path, html_page('\ufeff<p>' + body + '</p>'))
    assert metadata["unusable_evidence_reasons"] == [reason]
    assert source.extracted_text == '\ufeff\n' + body
    assert NORMALIZE.content_hash(source) == retained_hash


@pytest.mark.parametrize("html", [
    '<p>\ufeff502 Bad Gateway.</p>', '&#xfeff;<p>502 Bad Gateway.</p>',
    '\ufeff\ufeff<p>502 Bad Gateway.</p>', '<!-- before -->\ufeff<p>502 Bad Gateway.</p>',
])
def test_only_a_single_stream_initial_bom_is_ignored_for_matching(html):
    assert not NORMALIZE.html_gateway_shell(parse_context(html))


def test_initial_bom_matching_is_invariant_under_empty_and_chunked_feeds():
    html = '\ufeff<p>502 Bad Gateway.</p>'
    extractor = NORMALIZE.HTMLContentExtractor()
    extractor.feed('')
    for character in html:
        extractor.feed(character)
    extractor.close()
    assert extractor.classification_context() == parse_context(html)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_html_input_byte_cap_qualifies_new_shell_inference(tmp_path, offset):
    prefix, suffix = b'<p>502 Bad Gateway.</p><!--', b'-->'
    size = NORMALIZE.HTML_MAX_BYTES + offset
    data = prefix + b'x' * (size - len(prefix) - len(suffix)) + suffix
    source, metadata = normalize_page(tmp_path, html_page(''), raw_bytes=data)
    assert len(data) == size
    assert any('extraction truncated' in warning for warning in source.warnings) is (offset > 0)
    assert ('html_error_page:official_error_page' in (metadata['unusable_evidence_reasons'] or [])) is (offset <= 0)
    assert (tmp_path / 'raw/web/capture.html').read_bytes() == data


def test_html_reader_requests_one_bounded_read_with_a_truncation_sentinel(monkeypatch):
    opened = mock_open(read_data=b'<p>Measured value: 42.</p>')
    monkeypatch.setattr(Path, 'open', opened)
    text, warnings = NORMALIZE.read_html_text(Path('capture.html'), 'raw/web/capture.html')
    opened.assert_called_once_with('rb')
    opened().read.assert_called_once_with(NORMALIZE.HTML_MAX_BYTES + 1)
    assert text == '<p>Measured value: 42.</p>' and warnings == []


def test_message_cut_at_byte_limit_cannot_become_a_complete_gate(tmp_path, monkeypatch):
    data = b'<p>Please sign in to continue.</p>'
    monkeypatch.setattr(NORMALIZE, 'HTML_MAX_BYTES', len(data) - 5)
    source, metadata = normalize_page(tmp_path, html_page(''), raw_bytes=data)
    assert any('extraction truncated' in warning for warning in source.warnings)
    assert 'html_authentication_shell' not in (metadata['unusable_evidence_reasons'] or [])


@pytest.mark.parametrize("file_name", ['capture.html', 'capture.htm', 'capture.xhtml'])
@pytest.mark.parametrize("html,reason", [
    ('<p>502 Bad Gateway.</p>', 'html_error_page:official_error_page'),
    ('<p>Please sign in to continue.</p>', 'html_authentication_shell'),
])
def test_supported_html_extensions_share_classification(tmp_path, file_name, html, reason):
    page = {**html_page(html), 'file_name': file_name}
    _, metadata = normalize_page(tmp_path, page)
    assert metadata['unusable_evidence_reasons'] == [reason]


@pytest.mark.parametrize("html,reason", [
    ('<P>HTTP Error 502: BAD GATEWAY.</P>', 'html_error_page:official_error_page'),
    ('<!doctype html><html><body><main><p>502 Bad Gateway.</p></main></body></html>', 'html_error_page:official_error_page'),
    ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>502 Bad Gateway.</p></body></html>',
     'html_error_page:official_error_page'),
    ('<p>502\tBad\nGateway.</p>', 'html_error_page:official_error_page'),
    ('<p><span>502</span> <strong>Bad</strong>&nbsp;Gateway.</p>', 'html_error_page:official_error_page'),
    ('<p>502 Bad<!-- inert --> Gateway.</p>', 'html_error_page:official_error_page'),
    ('<p>Please <strong>SIGN&#x2010;IN</strong>&nbsp;to continue.</p>', 'html_authentication_shell'),
    ('<main><section><p>Please sign in to continue.</p></section></main>', 'html_authentication_shell'),
])
def test_supported_markup_and_text_transformations_preserve_the_verdict(tmp_path, html, reason):
    _, metadata = normalize_page(tmp_path, html_page(html))
    assert metadata['unusable_evidence_reasons'] == [reason]


@pytest.mark.parametrize("gate", ['502 Bad Gateway.', 'Please sign in to continue.'])
@pytest.mark.parametrize("container", ['p', 'article', 'pre', 'blockquote', 'form'])
def test_adding_independent_content_prevents_new_whole_page_refusal(tmp_path, gate, container):
    html = f'<h1>{gate}</h1><{container}>The measured reflectance is 0.74.</{container}>'
    _, metadata = normalize_page(tmp_path, html_page(html))
    assert metadata['unusable_evidence_reasons'] is None
    assert metadata['evidence_usable'] is True


@pytest.mark.parametrize("html", [
    '<p>502 Bad Gateway.', '<form><p>Please sign in to continue.</p>',
    '<div><p>502 Bad Gateway.</div>', '<p>Please sign in to continue.</unknown>',
    '<script>unfinished', '<title>502 Bad Gateway</title><nav>unfinished',
])
def test_unbalanced_markup_does_not_create_structural_certainty(html):
    context = parse_context(html)
    assert context.degraded
    assert not NORMALIZE.html_gateway_shell(context)
    assert not NORMALIZE.html_authentication_shell(context)


@pytest.mark.parametrize("html,reasons", [
    ('', []), ('<meta name="description" content="Please sign in to continue.">', []),
    ('<title>502 Bad Gateway</title>', ['html_error_page:official_error_page']),
    ('<title>Authentication required</title>', ['html_authentication_shell']),
])
def test_empty_or_title_only_captures_do_not_manufacture_body_content(tmp_path, html, reasons):
    source, metadata = normalize_page(tmp_path, html_page(html))
    assert source.extracted_text == 'None extracted.'
    assert metadata['status'] == 'failed'
    assert (metadata['unusable_evidence_reasons'] or []) == reasons


def test_missing_html_original_preserves_extraction_failure(tmp_path):
    source = NORMALIZE.normalize_html_record(tmp_path, {'id': 'web:missing', 'kind': 'html', 'raw_paths': ['raw/web/missing.html']})
    assert NORMALIZE.status_for(source) == 'failed'
    assert source.extracted_text == 'None extracted.'
    assert any('raw HTML file not found' in warning for warning in source.warnings)


def test_unreadable_html_original_preserves_diagnostics(tmp_path, monkeypatch):
    original_open = Path.open

    def refuse_raw_read(path, mode='r', *args, **kwargs):
        if path == tmp_path / 'raw/web/capture.html' and mode == 'rb':
            raise PermissionError('Unreadable fixture')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', refuse_raw_read)
    source, metadata = normalize_page(tmp_path, html_page('<p>502 Bad Gateway.</p>'))
    assert metadata['status'] == 'failed'
    assert source.extracted_text == 'None extracted.'
    assert any('cannot read HTML file' in warning for warning in source.warnings)
    assert metadata['unusable_evidence_reasons'] is None


def test_invalid_utf8_is_retained_as_replacement_text_and_not_invented_as_a_message(tmp_path):
    source, metadata = normalize_page(tmp_path, html_page(''), raw_bytes=b'<p>502 Bad Gateway.\xff</p>')
    assert '\ufffd' in source.extracted_text
    assert metadata['unusable_evidence_reasons'] is None


@pytest.mark.parametrize("html", [
    '<p>502 Bad Gateway.</p>' * 5000,
    '<p>Please sign in to continue.</p>' + '<p>Useful explanation.</p>' * 1000,
    '<p>Useful observation.</p>' * 1000 + '<script></script>' * 100,
], ids=["repeated-gateway", "signin-with-content", "content-with-scripts"])
def test_large_inputs_keep_new_matching_bounded_and_qualified(html):
    context = parse_context(html)
    assert context.truncated
    assert len(context.blocks) <= NORMALIZE.HTML_CONTEXT_MAX_BLOCKS
    assert len(context.body_text) <= NORMALIZE.HTML_CONTEXT_MAX_CHARS
    assert not NORMALIZE.html_gateway_shell(context)
    assert not NORMALIZE.html_authentication_shell(context)


@pytest.mark.parametrize("message", ["502 Bad Gateway.", "Please sign in to continue."])
@pytest.mark.parametrize("tail", ['<!-- unfinished', '<!doctype', '<?unfinished', '<unfinished attr="', '</unfinished', '<![CDATA['])
@pytest.mark.parametrize("chunk_size", [1, 7, None])
def test_unfinished_markup_at_eof_keeps_classification_degraded(message, tail, chunk_size):
    html = '<p>' + message + '</p>' + tail
    parser = NORMALIZE.HTMLContentExtractor()
    size = chunk_size or len(html)
    for offset in range(0, len(html), size):
        parser.feed(html[offset:offset + size])
    before = parser.classification_context()
    parser.close()
    context = parser.classification_context()
    assert before.degraded and context.degraded
    assert not NORMALIZE.html_gateway_shell(context)
    assert not NORMALIZE.html_authentication_shell(context)
    parser.close()
    assert parser.classification_context().degraded


@pytest.mark.parametrize("tail,expected", [('&#46', True), ('&#x2e', True), ('&amp', False), ('<', False), ('</', False), ('<3', False)])
def test_valid_eof_text_and_entities_do_not_degrade_classification(tail, expected):
    parser = NORMALIZE.HTMLContentExtractor()
    parser.feed('<p>Bad Gateway.</p>' + tail)
    parser.close()
    context = parser.classification_context()
    assert not context.degraded
    assert NORMALIZE.html_gateway_shell(context) is expected
