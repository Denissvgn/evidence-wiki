"""Generation-keyed helper namespaces never replace actual current data reads."""

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evidence_wiki import Workspace, _script_host, host_context
from evidence_wiki.errors import ConfigError
from evidence_wiki.pack_discovery import owner
from tests import test_publication_readiness as _publication_fixtures

CONFIG = ("project: {}\nraw: {}\nsources: {}\nwiki: {}\ntaxonomy: {}\ningest: {}\n"
          "lint: {}\noutputs: {}\nintegrations: {}\nvalue: [original]\n")


@pytest.fixture
def health_assets(tmp_path):
    root = tmp_path / "assets"
    scripts = root / "workspace-template/scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
    for name in ("_workspace_health.py", "_workspace_module_loader.py", "_yaml_safe.py"):
        shutil.copyfile(source / name, scripts / name)
    return root, scripts, _script_host.load_packaged_script(root, "_workspace_health")


@pytest.fixture(scope="module")
def publication_base(tmp_path_factory):
    root = tmp_path_factory.mktemp("publication-reader")
    return _publication_fixtures.PublicationReadinessTests().init_workspace(root)


@pytest.fixture
def publication_root(tmp_path, publication_base):
    root = tmp_path / "workspace"
    shutil.copytree(publication_base, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_warm_yaml_namespace_reads_new_bytes_and_does_not_accumulate_findings(tmp_path, health_assets):
    _, _, health = health_assets
    config = tmp_path / "research.yml"
    config.write_text(CONFIG, encoding="utf-8")
    first = health._load_config(tmp_path, [])
    first["value"].append("caller edit")
    assert len(health._YAML_HELPER_CACHE) == 1
    assert health._load_config(tmp_path, [])["value"] == ["original"]
    config.write_text("value: [unfinished\n", encoding="utf-8")
    broken = []
    assert health._load_config(tmp_path, broken) == {}
    assert broken[0]["code"] == "RESEARCH_CONFIG_INVALID"
    config.write_text(CONFIG.replace("[original]", "[repaired]"), encoding="utf-8")
    repaired = []
    assert health._load_config(tmp_path, repaired)["value"] == ["repaired"]
    assert repaired == []


@pytest.mark.parametrize("change", ["helper", "unrelated", "missing", "outside_link"])
def test_warm_helper_lookup_still_observes_every_generation_change(tmp_path, health_assets, change):
    _, scripts, health = health_assets
    (tmp_path / "research.yml").write_text(CONFIG, encoding="utf-8")
    health._load_config(tmp_path, [])
    original = next(iter(health._YAML_HELPER_CACHE.values()))
    helper = scripts / "_yaml_safe.py"
    if change == "missing":
        helper.unlink()
        with pytest.raises(SystemExit):
            health._load_config(tmp_path, [])
        return
    if change == "outside_link":
        outside = tmp_path / "outside.py"
        outside.write_bytes(helper.read_bytes())
        helper.unlink()
        helper.symlink_to(outside)
        with pytest.raises(SystemExit):
            health._load_config(tmp_path, [])
        return
    if change == "helper":
        helper.write_bytes(helper.read_bytes() + b"\n# Current helper source.\n")
    else:
        (scripts / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert health._load_config(tmp_path, [])["value"] == ["original"]
    assert next(iter(health._YAML_HELPER_CACHE.values())) is not original


def test_warm_generation_refusal_is_not_bypassed_by_private_helper_cache(tmp_path, health_assets, monkeypatch):
    assets, scripts, health = health_assets
    monkeypatch.setattr(_script_host, "shared_assets_root", lambda: assets)
    (tmp_path / "research.yml").write_text(CONFIG, encoding="utf-8")
    context = host_context.HostContext(allowed_roots=[tmp_path], grants=[], actor_ref="owner")
    with context.activate(), pytest.raises(ConfigError):
        with host_context.call_binding():
            health._load_config(tmp_path, [])
            (scripts / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
            with pytest.raises(ConfigError):
                health._load_config(tmp_path, [])


def test_health_namespace_preserves_standalone_and_bridge_isolation(tmp_path, health_assets):
    _, scripts, health = health_assets
    cache = health._YAML_HELPER_CACHE
    loader = _script_host._load_workspace_loader(scripts)
    first = loader.load_workspace_module(scripts, "_yaml_safe", cache=cache)
    second = loader.load_workspace_module(scripts, "_yaml_safe", cache=cache, host_context_bridge=host_context)
    assert first is not second
    assert loader.load_workspace_module(scripts, "_yaml_safe", cache=cache) is not second


def test_cached_health_reader_keeps_workspace_and_concurrent_context_data_separate(tmp_path, health_assets):
    _, _, health = health_assets
    roots = []
    for value in ("first", "second"):
        root = tmp_path / value
        root.mkdir()
        (root / "research.yml").write_text(CONFIG.replace("[original]", f"[{value}]"), encoding="utf-8")
        roots.append(root)

    def read(root):
        context = host_context.HostContext(allowed_roots=[root], grants=[], actor_ref=root.name)
        with context.activate():
            result = health._load_config(root, [])
            assert host_context.current_context() is context
        assert host_context.current_context() is None
        return result["value"]

    assert [read(root) for root in (roots[0], roots[1], roots[0])] == [["first"], ["second"], ["first"]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, roots)) == [["first"], ["second"]]
    assert health._load_config(roots[0], [])["value"] == ["first"]


def test_publication_warm_helpers_observe_question_config_and_manifest_changes(publication_root):
    publication = owner("_selected_publication")
    root = publication_root
    slug = "vendor-product-spec"
    first = publication.run_selected_publication(root, [slug])
    before_modules = {key: value for key, value in publication._READ_HELPER_CACHE.items()}
    assert len(before_modules) == 5
    first["export"]["questions"][0]["status"] = "caller edit"
    clean = publication.run_selected_publication(root, [slug])
    assert clean["export"]["questions"][0]["status"] != "caller edit"
    question = root / "wiki/questions" / (slug + ".md")
    original_question = question.read_text()
    question.write_text(original_question.replace("status: open", "status: deferred"), encoding="utf-8")
    changed = publication.run_selected_publication(root, [slug])
    assert changed["export"]["questions"][0]["status"] == "deferred"
    assert changed["revision"]["revision_id"] != clean["revision"]["revision_id"]
    question.write_text(original_question, encoding="utf-8")
    manifest = root / "sources/manifest.jsonl"
    original_manifest = manifest.read_bytes()
    manifest.write_bytes(b"{malformed\n")
    invalid = publication.run_selected_publication(root, [slug])
    assert invalid["readiness"]["reasons"] != clean["readiness"]["reasons"]
    manifest.write_bytes(original_manifest)
    repaired = publication.run_selected_publication(root, [slug])
    assert repaired["readiness"]["reasons"] == clean["readiness"]["reasons"]
    assert publication._READ_HELPER_CACHE == before_modules
    path = root / "research.yml"
    original_config = path.read_bytes()
    path.write_bytes(b"project: [invalid\n")
    with pytest.raises(publication.refuse("PUBLICATION_CONFIG_INVALID", "Invalid current data").__class__):
        publication.run_selected_publication(root, [slug])
    path.write_bytes(original_config)
    assert publication.run_selected_publication(root, [slug])["readiness"]["reasons"] == clean["readiness"]["reasons"]


def test_publication_private_namespaces_do_not_carry_selection_between_workspaces(tmp_path, publication_base):
    publication = owner("_selected_publication")
    roots = []
    for name in ("first", "second"):
        root = tmp_path / name
        shutil.copytree(publication_base, root, ignore=shutil.ignore_patterns("__pycache__"))
        roots.append(root)
    slug = "vendor-product-spec"
    second_question = roots[1] / "wiki/questions" / (slug + ".md")
    second_question.write_text(second_question.read_text().replace("status: open", "status: deferred"), encoding="utf-8")
    documents = [publication.run_selected_publication(root, [slug]) for root in (roots[0], roots[1], roots[0])]
    assert [doc["export"]["questions"][0]["status"] for doc in documents] == ["open", "deferred", "open"]
    assert documents[0]["revision"]["revision_id"] == documents[2]["revision"]["revision_id"]
    assert host_context.current_context() is None


def test_actual_sdk_cached_publication_refuses_unscoped_capture_and_restores_legacy_reads(tmp_path, publication_base):
    roots = []
    for name in ("first", "second"):
        root = tmp_path / name
        shutil.copytree(publication_base, root, ignore=shutil.ignore_patterns("__pycache__"))
        roots.append(root)
    slug = "vendor-product-spec"
    second_question = roots[1] / "wiki/questions" / (slug + ".md")
    second_question.write_text(second_question.read_text().replace("status: open", "status: deferred"), encoding="utf-8")

    def read(root):
        with Workspace.open(root) as workspace:
            return workspace.publish_selected([slug])

    def scoped_refusal(root):
        context = host_context.HostContext(allowed_roots=[root],
            grants=["workspace.open", "publication_readiness.run_selected_publication"], actor_ref=root.name)
        with context.activate(), Workspace.open(root) as workspace:
            with pytest.raises(ConfigError) as refused:
                workspace.publish_selected([slug])
            assert refused.value.error_code == "HOST_CONTEXT_REFUSED"
            assert refused.value.details["reason"] == "host_path_outside_scope"
            assert host_context.current_context() is context
        assert host_context.current_context() is None

    documents = [read(root) for root in (roots[0], roots[1], roots[0])]
    assert [document["export"]["questions"][0]["status"] for document in documents] == ["open", "deferred", "open"]
    for root in (roots[0], roots[1], roots[0]):
        scoped_refusal(root)
    after_refusal = [read(root) for root in (roots[0], roots[1], roots[0])]
    assert [document["export"]["questions"][0]["status"] for document in after_refusal] == ["open", "deferred", "open"]
    assert [document["readiness"]["reasons"] for document in after_refusal] == [document["readiness"]["reasons"] for document in documents]
    context = host_context.HostContext(allowed_roots=[roots[0]],
        grants=["workspace.open", "publication_readiness.run_selected_publication"], actor_ref="host")
    with pytest.raises(RuntimeError, match="Caller interruption"):
        with context.activate(), Workspace.open(roots[0]) as workspace:
            with pytest.raises(ConfigError) as refused:
                workspace.publish_selected([slug])
            assert refused.value.details["reason"] == "host_path_outside_scope"
            raise RuntimeError("Caller interruption")
    assert host_context.current_context() is None
    assert read(roots[0])["readiness"]["reasons"] == documents[0]["readiness"]["reasons"]
    with Workspace.open(roots[0]) as workspace:
        assert workspace.publish_selected([slug])["export"]["questions"][0]["status"] == "open"
