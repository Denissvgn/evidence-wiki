"""Delegated slices retain precision and gaps without granting rights or running tools."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from evidence_wiki import Workspace, contract, verify_snapshot
from evidence_wiki.errors import SourceError
from tests._execution_fixture import authenticate, binding, canonical, closure
from tests._market_fixture import SOURCE_ID, example, listing, revise, workspace
from tests._script_loader import load_isolated_module
from tests._temporal_fixture import TemporalFixture, clock_claim
from tests._usage_fixture import UsageFixture

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
MARKET = load_isolated_module("market_intake", SCRIPTS / "_market_evidence.py")
NORMALIZE = load_isolated_module("market_normalize", SCRIPTS / "normalize_sources.py")
VERIFY = load_isolated_module("market_verify", SCRIPTS / "normalize_verify.py")
LINT = load_isolated_module("market_lint", SCRIPTS / "lint.py")
POLICY = load_isolated_module("market_policy", SCRIPTS / "_evidence_policies.py")
STRUCTURED = load_isolated_module("market_structured", SCRIPTS / "_structured_view.py")
USAGE = load_isolated_module("market_usage", SCRIPTS / "_evidence_usage.py")
QUALIFICATIONS = load_isolated_module("market_qualifications", SCRIPTS / "_snapshot_qualifications.py")
SNAPSHOT = load_isolated_module("market_snapshot", SCRIPTS / "_snapshot_verifier.py")


def normalize(root, config, record):
    source = NORMALIZE.normalize_selected_record(root, config, NORMALIZE.EligibleRecord(
        record=copy.deepcopy(record), method=NORMALIZE.normalization_method(root, record)))
    path, _ = NORMALIZE.write_normalized_source(source, root / "sources/normalized", "sources/manifest.jsonl",
                                              "2026-09-10", project_root=root, force=True,
                                              normalized_at="2026-09-10T00:00:00Z")
    return source, path


def reasons(root, config, record, path):
    frontmatter, _ = LINT.load_frontmatter(path)
    inputs = POLICY.PolicyInputs(root, config, {SOURCE_ID: record}, {SOURCE_ID: frontmatter}, {}, [], {}, {}, {})
    return POLICY.source_unusable_evidence_reasons(inputs, SOURCE_ID)


@pytest.mark.parametrize("route", MARKET.ROUTES)
def test_inert_exact_decimal_observations_and_grounding(tmp_path, route):
    files, _ = example(route)
    config, record, _ = workspace(tmp_path, files)
    before = {path: data for path, data in files.items()}
    with patch("subprocess.run", side_effect=AssertionError("inert delivery")), patch("socket.create_connection", side_effect=AssertionError("offline")):
        report = MARKET.inspect_market(tmp_path, config, record)
        source, path = normalize(tmp_path, config, record)
    assert report["valid"] and report["completeness"]["complete"], report
    assert report["authority"] == "not_evaluated"
    assert files == before
    values = report["data"]["observations"]
    if route == "sec-company-concept":
        assert len(values) == 4
        assert {item["unit"] for item in values} == {"USD", "EUR"}
        assert {item["value"] for item in values} == {"1234567890123456789", "1234567890123456790"}
        assert {item["form"] for item in values} == {"10-Q", "10-Q/A"}
        assert all(item["available_at"] is None and item["scale"] == 0 for item in values)
        pointer, exact, rounded = "/observations/0/value", "1234567890123456789", "1234567890123456800"
    else:
        assert values[0]["close"] == "10.1234567890123456789"
        assert values[1]["close"] == "21"
        assert report["data"]["listings"][1]["status"] == "delisted"
        assert [item["type"] for item in report["data"]["provenance"]["corporate_actions"]] == ["split", "dividend"]
        pointer, exact, rounded = "/observations/0/close", "10.1234567890123456789", "10.123456789012346"
    frontmatter, _ = LINT.load_frontmatter(path)
    sidecar = STRUCTURED.sidecar_path(path.parent, SOURCE_ID)
    assert STRUCTURED.resolve_anchor(frontmatter, sidecar, pointer, exact).ok
    assert not STRUCTURED.resolve_anchor(frontmatter, sidecar, pointer, rounded).ok
    assert source.structured == report["data"]
    assert VERIFY.build_report(tmp_path)["overall_result"] == "not_verified"
    assert "usage_authority_required" in reasons(tmp_path, config, record, path)


@pytest.mark.parametrize("change,gap", [
    ("missing-member", "universe_member_observation_missing"),
    ("pagination", "provider_pagination_incomplete"),
    ("pagination-unknown", "provider_pagination_completion_unknown"),
    ("unfinished", "price_bar_incomplete"),
    ("universe", "historical_universe_incomplete"),
    ("unknown-delay", "feed_delay_unknown"),
    ("currency", "listing_price_currency_mismatch"),
    ("corporate-currency", "corporate_action_currency_mismatch"),
    ("corporate-identity", "corporate_action_identity_unknown"),
    ("corporate-unknown", "corporate_action_lineage_incomplete"),
    ("ticker-reuse", "ticker_identity_ambiguous"),
    ("mapping-date", "symbol_mapping_date_ambiguous"),
    ("excluded", "observations_excluded"),
    ("conflict", "provider_value_conflict"),
    ("duplicate", "duplicate_or_conflicting_price_bar"),
    ("terms", "usage_policy_evidence_missing"),
])
def test_missing_or_ambiguous_market_scope_is_a_gap(tmp_path, change, gap):
    files, document = example()

    def mutate(doc, artifacts):
        page = json.loads(artifacts["page.json"])
        if change == "missing-member":
            del page["bars"]["BBB"]
        elif change == "pagination":
            page["next_page_token"] = "continue-at-host"
        elif change == "pagination-unknown":
            del page["next_page_token"]
        elif change == "unfinished":
            doc["provenance"]["retrieved_at"] = "2026-09-09T13:45:00Z"
        elif change == "universe":
            doc["provenance"]["universe"]["coverage"] = "survivors-only"
        elif change == "unknown-delay":
            doc["request"]["delay"] = "unknown"
        elif change == "currency":
            doc["listings"][1]["currency"] = "EUR"
        elif change == "corporate-currency":
            doc["provenance"]["corporate_actions"][1]["currency"] = "EUR"
        elif change == "corporate-identity":
            doc["provenance"]["corporate_actions"][0]["listing_id"] = "unrelated"
        elif change == "corporate-unknown":
            doc["provenance"]["corporate_action_coverage"] = "unknown"
        elif change in {"ticker-reuse", "mapping-date"}:
            doc["listings"].append(listing(listing_id="other-listing", issuer_id="other-issuer"))
            if change == "mapping-date":
                doc["request"]["asof"] = "2026-09-09"
                doc["listings"][-1]["valid_from"] = "2026-09-09T12:00:00Z"
        elif change in {"excluded", "conflict"}:
            doc["provenance"]["excluded" if change == "excluded" else "conflicts"] = ["Host qualification"]
        elif change == "duplicate":
            page["bars"]["AAA"].append(copy.deepcopy(page["bars"]["AAA"][0]))
        elif change == "terms":
            doc["provenance"]["policy_reference"] = None
            del artifacts["terms.txt"]
        artifacts["page.json"] = canonical(page)

    files, _ = revise(files, document, mutate)
    config, record, _ = workspace(tmp_path, files)
    report = MARKET.inspect_market(tmp_path, config, record)
    assert report["valid"], report
    assert not report["completeness"]["complete"]
    assert gap in report["completeness"]["gaps"]
    assert report["originals"]["page.json"] == binding(files["page.json"])


def test_ticker_rename_and_reuse_keep_distinct_issuer_identity():
    files, document = example()

    def mutate(doc, _artifacts):
        old = listing(listing_id="old-listing", issuer_id="old-issuer")
        old["valid_to"] = "2021-01-01T00:00:00Z"
        doc["listings"][0]["valid_from"] = "2021-01-01T00:00:00Z"
        renamed = copy.deepcopy(doc["listings"][0])
        renamed.update(symbol="OLD", issuer_name="Previous name", valid_from="2020-01-01T00:00:00Z", valid_to="2021-01-01T00:00:00Z")
        doc["listings"].extend([old, renamed])

    files, _ = revise(files, document, mutate)
    report = MARKET.validate_closure(SOURCE_ID, files)
    assert report["completeness"]["complete"], report
    assert report["data"]["observations"][0]["listing_id"] == "listing-a"


def test_pagination_requires_a_complete_ordered_chain():
    files, document = example()

    def split(doc, artifacts):
        first = json.loads(artifacts["page.json"])
        second = {"bars": {"BBB": first["bars"].pop("BBB")}, "next_page_token": None}
        first["next_page_token"] = "second"
        artifacts["page.json"] = canonical(first)
        artifacts["page-2.json"] = canonical(second)
        doc["pages"].append({"artifact": {"path": "page-2.json", "content_hash": binding(artifacts["page-2.json"])["content_hash"]}, "request_token": "second"})

    files, document = revise(files, document, split)
    assert MARKET.validate_closure(SOURCE_ID, files)["completeness"]["complete"]
    files, _ = revise(files, document, lambda doc, _: doc["pages"][1].update(request_token="wrong"))  # noqa: S106 -- synthetic pagination cursor
    with pytest.raises(ValueError, match="market_pagination_chain_invalid"):
        MARKET.validate_closure(SOURCE_ID, files)


@pytest.mark.parametrize("change", ["bytes", "duplicate-json", "overflow", "nan", "unrequested", "page-bound", "bad-date", "bad-timezone", "bad-schema", "missing-file", "extra-file"])
def test_malformed_or_rebound_slices_refuse(tmp_path, change):
    files, document = example()
    if change == "bytes":
        files["page.json"] += b" "
    elif change == "missing-file":
        del files["page.json"]
    elif change == "extra-file":
        files["undisclosed.txt"] = b"extra"
    else:
        def mutate(doc, artifacts):
            if change == "duplicate-json":
                artifacts["page.json"] = b'{"bars":{},"bars":{}}'
            elif change in {"overflow", "nan"}:
                artifacts["page.json"] = artifacts["page.json"].replace(b"10.1234567890123456789", b"1e999" if change == "overflow" else b"NaN")
            elif change == "unrequested":
                artifacts["page.json"] = artifacts["page.json"].replace(b'"BBB"', b'"CCC"')
            elif change == "page-bound":
                doc["pages"] *= 17
            elif change == "bad-date":
                doc["request"]["asof"] = "2026-09-09T12:00:00Z"
            elif change == "bad-timezone":
                doc["request"]["timezone"] = "unknown/timezone"
            elif change == "bad-schema":
                doc["schema_version"] = "market-evidence/v999"
        files, _ = revise(files, document, mutate)
    config, record, _ = workspace(tmp_path, files)
    report = MARKET.inspect_market(tmp_path, config, record)
    assert not report["valid"], report
    source, _ = normalize(tmp_path, config, record)
    assert source.extraction_method == "market_stub"


@pytest.mark.parametrize("change", ["original", "report", "sidecar"])
def test_normalized_market_bytes_cannot_detach_from_originals(tmp_path, change):
    config, record, folder = workspace(tmp_path)
    _, path = normalize(tmp_path, config, record)
    frontmatter, body, _ = LINT._normalized_contract.split_record(path.read_text())
    if change == "original":
        (folder / "page.json").write_bytes(b"{}")
    elif change == "report":
        frontmatter["market_evidence"]["data"]["observations"][0]["close"] = "999"
        path.write_text("---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n" + body)
    else:
        sidecar = STRUCTURED.sidecar_path(path.parent, SOURCE_ID)
        data = json.loads(sidecar.read_bytes())
        data["observations"][0]["close"] = "999"
        sidecar.write_bytes(canonical(data))
        frontmatter["structured_view"]["content_hash"] = binding(sidecar.read_bytes())["content_hash"]
        path.write_text("---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n" + body)
    report = VERIFY.build_report(tmp_path)
    assert report["overall_result"] == "not_verified", report
    assert reasons(tmp_path, config, record, path)


def test_delayed_research_can_be_authorized_while_training_and_export_are_denied(tmp_path, monkeypatch):
    originals, document = example()
    originals, _ = revise(originals, document, lambda doc, _: doc["request"].update(delay="delayed"))
    sanitized = tmp_path / "sanitizer"
    config, record, _ = workspace(sanitized, originals)
    _, normalized_path = normalize(sanitized, config, record)
    host = UsageFixture(tmp_path, monkeypatch)
    config, record, _ = workspace(host.root, originals)
    path = host.root / normalized_path.relative_to(sanitized)
    path.write_bytes(normalized_path.read_bytes())
    sidecar = STRUCTURED.sidecar_path(path.parent, SOURCE_ID)
    sidecar.write_bytes(STRUCTURED.sidecar_path(normalized_path.parent, SOURCE_ID).read_bytes())
    body, files = host.source(SOURCE_ID, normalized=path.read_bytes(), evidence=originals, training=False, export=False)
    files[sidecar.name] = sidecar.read_bytes()
    revision = closure(files)
    body["source_revision"] = revision
    grant, scrub = body["grant"]["payload"], body["scrub"]["payload"]
    grant["source_revision"], scrub["sanitized_revision"] = revision, revision
    body["grant"], body["scrub"] = authenticate(grant, "owner", "usage"), authenticate(scrub, "runner", "scrubber")
    config.update(host.config)
    (host.root / "research.yml").write_text(yaml.safe_dump(config))
    host.transact(USAGE, "initialize")
    host.transact(USAGE, "deposit", body, files)
    assert not reasons(host.root, config, record, path)
    ws = Workspace.open(host.root)
    assert ws.normalize.validate_market(SOURCE_ID)["valid"]
    assert ws.usage.check(revision, uses=["retrieval"], purpose="research", consumer="evidence-wiki")["eligible"]
    for use, purpose in (("training", "training-snapshot"), ("export", "qa-export")):
        assert not ws.usage.check(revision, uses=[use], purpose=purpose, consumer="evidence-wiki")["eligible"]
    host.revoke(USAGE, body)
    assert reasons(host.root, config, record, path)
    assert not ws.normalize.validate_market(SOURCE_ID)["valid"]


def test_real_cli_and_public_api_report_the_same_bounded_slice(tmp_path):
    workspace(tmp_path)
    ws = Workspace.open(tmp_path)
    report = ws.normalize.validate_market(SOURCE_ID)
    result = subprocess.run([sys.executable, "-m", "evidence_wiki.cli", "normalize", "market", "--target", str(tmp_path),
                             "--source-id", SOURCE_ID, "--format", "json"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == report
    assert ws.normalize.profiles() == contract()["intake_profiles"]
    assert "normalize.validate_market" in contract()["library_api"]["surface"]
    with pytest.raises(SourceError):
        ws.normalize.validate_market("missing")


def test_required_unknown_profile_never_falls_back(tmp_path):
    config, record, _ = workspace(tmp_path)
    record["metadata"]["market_profile"] = "market_evidence/v999"
    assert NORMALIZE.normalization_method(tmp_path, record) == "market"
    source, _ = normalize(tmp_path, config, record)
    assert source.extraction_method == "market_stub"
    assert source.record["metadata"]["market_evidence"]["reason"] == "unsupported_market_profile"


def test_unknown_rights_block_legacy_search_and_export(tmp_path):
    config, record, _ = workspace(tmp_path)
    normalize(tmp_path, config, record)
    ws = Workspace.open(tmp_path)
    with pytest.raises(SourceError, match="not authorized"):
        ws.export_answers()
    usage = load_isolated_module("market_usage_guard", SCRIPTS / "_usage_gate.py")
    with pytest.raises(SystemExit):
        usage.require_unrestricted_legacy(tmp_path, config)
    # A manifest alone still opts into required rights, before any normalized record exists.
    for path in (tmp_path / "sources/normalized").iterdir():
        path.unlink()
    assert usage.workspace_has_claims(tmp_path, config)


def captured_market(host, originals, *, mutate=None):
    report = MARKET.validate_closure(SOURCE_ID, originals)
    scalar = MARKET.scalar_bytes(report)
    metadata = {"source_id": SOURCE_ID, "market_evidence": report,
                "structured_view": {"schema_version": "structured-view/v1", "path": "structured.json", **binding(scalar)}}
    normalized = ("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\nMarket observations\n").encode()
    body, files = host.source(SOURCE_ID, normalized=normalized, evidence=originals)
    descriptor = json.loads(files["source-record.json"])
    descriptor["temporal"] = {"schema_version": "evidence-temporal-source/v1", "asserting_principal": "runner",
        "measurement": {"start": clock_claim("2026-01-01T00:00:00Z"), "end": clock_claim("2026-09-09T14:30:00Z")},
        "published_at": clock_claim("2026-09-09T15:00:00Z"), "available_at": clock_claim("2026-09-09T21:00:00Z"),
        "claimed_retrieved_at": clock_claim("2026-09-09T21:00:00Z"), "effective": None, "expires_at": None,
        "supersedes": None, "record_path": "structured.json", "provenance_path": None}
    files["structured.json"] = scalar
    if mutate:
        mutate(descriptor, files, metadata)
    files["source-record.json"] = canonical(descriptor)
    return host.deposit_files(body, files), files


@pytest.mark.parametrize("route", MARKET.ROUTES)
def test_captured_market_replays_exact_grounding_and_offline_qualification(tmp_path, monkeypatch, route):
    host = TemporalFixture(tmp_path, monkeypatch)
    originals, _ = example(route)
    body, files = captured_market(host, originals)
    request = host.request(SOURCE_ID)
    pointer, value = ("/observations/0/value", "1234567890123456789") if route == "sec-company-concept" else (
                     "/observations/0/close", "10.1234567890123456789")
    request["analysis"]["grounding"] = [{"id": "observed", "source_id": SOURCE_ID, "pointer": pointer, "expected": value}]
    report = host.evaluate(request)
    assert report["result"]["complete"], report
    assert report["result"]["grounding"][0]["outcome"] == "pass"
    source = {"source_id": SOURCE_ID, "descriptor": json.loads(files["source-record.json"])}
    offline = QUALIFICATIONS.qualify_source(source, files)
    assert offline[0]["qualifications"] == MARKET.validate_closure(SOURCE_ID, originals)
    host.revoke(host.module, body)
    assert not host.evaluate(host.request(SOURCE_ID))["result"]["complete"]


@pytest.mark.parametrize("corruption,reason", [
    ("sidecar", "snapshot_market_delivery_invalid"), ("report", "snapshot_market_delivery_invalid"),
    ("original", "snapshot_market_delivery_invalid"), ("missing", "snapshot_market_delivery_invalid"),
    ("past-measurement", "market_bar_outside_qualified_interval"), ("retrieval", "market_retrieval_time_mismatch"),
    ("future-filing", "market_filing_outside_qualified_interval"), ("incomplete", "market_scope_incomplete"),
])
def test_re_signed_market_revision_cannot_bypass_qualification(tmp_path, monkeypatch, corruption, reason):
    host = TemporalFixture(tmp_path, monkeypatch)
    originals, document = example("sec-company-concept" if corruption == "future-filing" else "alpaca-stock-bars")
    if corruption == "incomplete":
        originals, _ = revise(originals, document, lambda doc, _: doc["provenance"]["universe"].update(coverage="survivors-only"))

    def mutate(descriptor, files, metadata):
        if corruption == "sidecar":
            files["structured.json"] = canonical({"observations": [{"close": "999"}]})
            metadata["structured_view"].update(binding(files["structured.json"]))
        elif corruption == "report":
            metadata["market_evidence"]["data"]["observations"][0]["close"] = "999"
        elif corruption == "original":
            files["evidence/page.json"] = b"{}"
        elif corruption == "missing":
            files.pop("evidence/market-record.json")
        elif corruption == "past-measurement":
            descriptor["temporal"]["measurement"]["end"] = clock_claim("2026-09-09T13:30:00Z")
        elif corruption == "retrieval":
            descriptor["temporal"]["claimed_retrieved_at"] = clock_claim("2026-09-09T22:00:00Z")
        elif corruption == "future-filing":
            descriptor["temporal"]["published_at"] = clock_claim("2026-07-01T00:00:00Z")
        files["normalized.md"] = ("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\nMarket observations\n").encode()

    captured_market(host, originals, mutate=mutate)
    report = host.evaluate(host.request(SOURCE_ID))
    assert not report["result"]["complete"], report
    assert report["result"]["retrieval"]["indexed_documents"] == 0
    assert report["result"]["exclusions"][0]["reason"] == reason, report


def test_nonfinancial_prose_does_not_select_market_rules():
    normalized = b"---\nsource_id: science:review\nkind: technical_report\n---\nReview of market_evidence and market_profile schemas.\n"
    source = {"source_id": "science:review", "descriptor": {"evidence_root": None, "normalized_path": "normalized.md", "temporal": {}}}
    assert QUALIFICATIONS.qualify_source(source, {"normalized.md": normalized}) == []
    usage = load_isolated_module("market_generic_usage", SCRIPTS / "_usage_gate.py")
    assert not usage.bytes_have_claims("sources/normalized/review.md", normalized, {})
    assert usage.source_decision(Path("."), {}, "science:review", [{"kind": "technical_report"}])["eligible"]


@pytest.mark.parametrize("corrupt", [False, True])
def test_current_query_rechecks_captured_market_qualifications(tmp_path, monkeypatch, corrupt):
    host = TemporalFixture(tmp_path, monkeypatch)
    originals, _ = example()

    def mutate(_descriptor, files, _metadata):
        if corrupt:
            files["evidence/page.json"] = b"{}"

    body, _files = captured_market(host, originals, mutate=mutate)
    host.workspace.usage.materialize(body["source_revision"])
    query = load_isolated_module("market_live_query", SCRIPTS / "query_index.py")
    payload = query.query_authorized(host.root, host.config, "normalized", "observations", 10, vars(query))
    assert payload["indexed_documents"] == (0 if corrupt else 1)


@pytest.mark.parametrize("invalid", [False, True])
def test_snapshot_checks_market_time_for_every_ancestor(tmp_path, monkeypatch, invalid):
    host = TemporalFixture(tmp_path, monkeypatch)

    def mutate(descriptor, _files, _metadata):
        if invalid:
            descriptor["temporal"]["measurement"]["end"] = clock_claim("2026-09-09T13:30:00Z")

    market, market_files = captured_market(host, example()[0], mutate=mutate)
    body, files, selection = host.execution_source()
    descriptor = json.loads(files["source-record.json"])
    descriptor["parents"].append(market["source_revision"])
    files["source-record.json"] = canonical(descriptor)
    body = host.deposit_files(body, files)
    selection["source_revisions"] = [body["source_revision"]]
    selection["temporal"]["checkpoint"] = host.checkpoint
    source = {"source_id": SOURCE_ID, "source_revision": market["source_revision"],
              "descriptor": json.loads(market_files["source-record.json"]), "observed_at": host.clock.value.isoformat()}
    if invalid:
        # This same source check runs offline, independent of accepted host state.
        with pytest.raises(SNAPSHOT.SnapshotInvalid, match="market_bar_outside_qualified_interval"):
            SNAPSHOT.temporal_source(source, market_files, None, host.policy, selection["temporal"], host.clock.value)
        before = (host.host / "evidence-state.json").read_bytes()
        with pytest.raises(SourceError) as refusal:
            host.workspace.snapshots.prepare(selection)
        assert refusal.value.details["reason"] == "snapshot_no_eligible_examples"
        assert (host.host / "evidence-state.json").read_bytes() == before
        assert not (host.root / "exports").exists()
    else:
        data, _, _, _ = host.export(selection)
        report = verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())
        assert report["valid"], report


@pytest.mark.parametrize("route,policies", [
    ("sec-company-concept", ["complete-filings", "filing-48h", "issuer-match"]),
    ("alpaca-stock-bars", ["complete-prices", "price-48h", "listing-review"]),
])
def test_pack_rules_resolve_real_normalized_values(route, policies):
    rules = load_isolated_module("market_pack_primitives", SCRIPTS / "_policy_primitives.py")
    config = yaml.safe_load((SCRIPTS.parents[1] / "domain-packs/capital-markets/research.overlay.yml").read_text())
    data = MARKET.validate_closure(SOURCE_ID, example(route)[0])["data"]
    parsed = rules.pack_policy_rules(config)
    context = rules.RuleContext(SOURCE_ID, data, None, {}, {"metadata": {"issuer_id": "issuer-a", "currency": "USD"}},
                                None, (), datetime(2026, 9, 10, tzinfo=timezone.utc), lambda a, b: a == b)
    for policy in policies:
        rule = parsed["pack:capital-markets/" + policy]
        assert rules.evaluate_rule(rule, context).outcome == "pass"
    if route == "alpaca-stock-bars":
        assert parsed["pack:capital-markets/listing-review"].manual_review_required
    data["complete"] = False
    assert rules.evaluate_rule(parsed["pack:capital-markets/" + policies[0]], context).outcome == "fail"
    context.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    assert rules.evaluate_rule(parsed["pack:capital-markets/" + policies[1]], context).outcome == "fail"
    context.question_frontmatter["metadata"] = {"issuer_id": "unrelated", "currency": "EUR"}
    assert rules.evaluate_rule(parsed["pack:capital-markets/" + policies[2]], context).outcome == "fail"


def test_optional_pack_adoption_and_refresh_preserve_provider_choices(tmp_path):
    def command(*args):
        result = subprocess.run([sys.executable, "-m", "evidence_wiki.cli", *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr or result.stdout
        return result.stdout

    root = tmp_path / "workspace"
    command("init", "--target", str(root), "--project-name", "Evidence review", "--project-description", "Bounded evidence",
            "--owner-goal", "Compare observations", "--domain-pack", "capital-markets")
    config_path = root / "research.yml"
    config = yaml.safe_load(config_path.read_text())
    starter = yaml.safe_load((SCRIPTS.parent / "research.yml").read_text())
    for key in ("acquisition", "discovery"):
        assert config["integrations"][key] == starter["integrations"][key]
    config["integrations"]["acquisition"]["max_downloads_per_run"] = 3
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    (root / "domain-packs/.evidence-wiki-state.yml").unlink()
    command("pack", "adopt", "--target", str(root))
    candidate = tmp_path / "capital-markets"
    shutil.copytree(SCRIPTS.parents[1] / "domain-packs/capital-markets", candidate)
    overlay_path = candidate / "research.overlay.yml"
    overlay = yaml.safe_load(overlay_path.read_text())
    overlay["domain_pack"].update(version="0.2.0", description="Updated review guidance")
    overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))
    command("pack", "refresh", "--target", str(root), "--path", str(candidate))
    revised = yaml.safe_load(config_path.read_text())
    assert revised["domain_pack"]["version"] == "0.2.0"
    assert revised["integrations"] == config["integrations"]
    assert not revised["integrations"]["acquisition"]["enabled"]
    assert not revised["integrations"]["discovery"]["enabled"]
