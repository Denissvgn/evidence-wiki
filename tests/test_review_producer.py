"""Owned producer observations and import-generation custody."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evidence_wiki import _review_producer as producer

ROOT = Path(__file__).resolve().parents[1]


def _tree(tmp_path):
    package = tmp_path / "evidence_wiki"
    package.mkdir()
    (package / "owner.py").write_bytes(b"value = 1\n")
    return (("package/evidence_wiki", package),)


def test_observation_reads_every_owned_extension_and_hidden_member(tmp_path):
    roots = _tree(tmp_path)
    (roots[0][1] / ".policy").write_bytes(b"retain\n")
    (roots[0][1] / "catalog.json").write_bytes(b"{}\n")
    observed = producer.observe_roots(roots, read=True)
    assert set(observed[3]) == {"package/evidence_wiki/owner.py", "package/evidence_wiki/.policy",
                                "package/evidence_wiki/catalog.json"}
    assert observed[3]["package/evidence_wiki/.policy"] == b"retain\n"


def test_cache_residue_changes_do_not_change_interval_identity(tmp_path):
    roots = _tree(tmp_path)
    before = producer.observe_roots(roots)
    cache = roots[0][1] / "__pycache__"
    cache.mkdir()
    (cache / "owner.pyc").write_bytes(b"cache")
    (roots[0][1] / "old.pyc").write_bytes(b"cache")
    assert producer._interval(before) == producer._interval(producer.observe_roots(roots))


@pytest.mark.parametrize("prefix", ["package/evidence_wiki", "assets/domain-packs"])
def test_package_backup_exclusion_preserves_complete_asset_observation(tmp_path, prefix):
    root = _tree(tmp_path)[0][1]
    nested = root / "nested"
    nested.mkdir()
    (nested / "catalog.json").write_bytes(b"{}\n")
    roots = ((prefix, root),)
    before = producer.observe_roots(roots, read=True)
    backup = nested / "catalog.json.orig"
    backup.write_bytes(b"old catalog\n")
    observed = producer.observe_roots(roots, read=True)
    if prefix == "package/evidence_wiki":
        assert producer._interval(before) == producer._interval(observed)
        assert observed[3] == before[3]
    else:
        assert observed[3][prefix + "/nested/catalog.json.orig"] == b"old catalog\n"
    assert observed[3][prefix + "/nested/catalog.json"] == b"{}\n"
    assert backup.read_bytes() == b"old catalog\n"


@pytest.mark.parametrize("name", ["alias.py", "__pycache__", "owner.pyc", "owner.py.orig"])
def test_links_are_refused_before_file_exclusions(tmp_path, name):
    roots = _tree(tmp_path)
    (roots[0][1] / name).symlink_to(roots[0][1] / "owner.py")
    with pytest.raises(producer.ProducerUnavailable):
        producer.observe_roots(roots)


def test_hardlinked_member_is_refused(tmp_path):
    roots = _tree(tmp_path)
    os.link(roots[0][1] / "owner.py", roots[0][1] / "duplicate.py")
    with pytest.raises(producer.ProducerUnavailable):
        producer.observe_roots(roots)


def test_member_size_bound_precedes_read(tmp_path):
    roots = _tree(tmp_path)
    with (roots[0][1] / "large").open("wb") as stream:
        stream.truncate(producer.MAX_MEMBER_BYTES + 1)
    with pytest.raises(producer.ProducerUnavailable, match="member_bound"):
        producer.observe_roots(roots, read=True)


@pytest.mark.parametrize("path", ["package/evidence_wiki/../owner.py", "package/evidence_wiki/a\\b",
                                  "package/evidence_wiki/a ", "package/evidence_wiki/assets/hidden",
                                  "assets/unknown/a", "package/evidence_wiki/\ud800"])
def test_nonportable_logical_paths_refuse(path):
    with pytest.raises(producer.ProducerUnavailable):
        producer.validate_path(path)


def test_inventory_is_closed_canonical_and_self_listing():
    document = {"schema_version": "evidence-review-producer-inventory/v1", "paths": [producer.INVENTORY_PATH]}
    raw = producer.canonical(document)
    assert producer.inventory_document(raw) == document
    for invalid in (raw.rstrip(), json.dumps(document, indent=2).encode(), raw.replace(b'"paths":', b'"paths":[],"paths":')):
        with pytest.raises((ValueError, producer.ProducerUnavailable)):
            producer.inventory_document(invalid)


def test_new_ids_include_native_lf():
    value = {"schema_version": "example/v1", "label": "caf\u00e9"}
    expected = hashlib.sha256(b"example/v1\0" + producer.canonical(value)).hexdigest()
    assert producer.content_id("example/v1", value) == "sha256:" + expected


def test_generation_cannot_be_replaced_by_serialized_identity():
    with pytest.raises(producer.ProducerUnavailable, match="unowned_generation"):
        producer.assert_current({"producer_id": "sha256:" + "0" * 64})


def test_native_registries_retain_actual_bridge_and_repeated_generations(tmp_path, monkeypatch):
    from evidence_wiki import host_context

    root = tmp_path / "workspace-template/scripts"
    monkeypatch.setattr(producer, "_PAIR", (tmp_path / "package", tmp_path))
    monkeypatch.setattr(producer, "_UNAVAILABLE", None)
    monkeypatch.setattr(producer, "_NATIVE_GENERATIONS", set())
    monkeypatch.setattr(producer, "_LOADER_GENERATIONS", set())
    for _ in range(2):
        producer.register_native(root, "a" * 64, host_context)
        producer.register_native(root, None, host_context, loader_hash="b" * 64)
    assert producer._NATIVE_GENERATIONS == {(root, "a" * 64, host_context)}
    assert producer._LOADER_GENERATIONS == {(root, "b" * 64, host_context)}
    assert next(iter(producer._NATIVE_GENERATIONS))[2] is host_context
    assert next(iter(producer._LOADER_GENERATIONS))[2] is host_context


@pytest.mark.parametrize("loader", [False, True])
def test_changed_registration_is_sticky_and_does_not_grow_registry(tmp_path, monkeypatch, loader):
    from evidence_wiki import host_context

    root = tmp_path / "workspace-template/scripts"
    monkeypatch.setattr(producer, "_PAIR", (tmp_path / "package", tmp_path))
    monkeypatch.setattr(producer, "_UNAVAILABLE", None)
    monkeypatch.setattr(producer, "_NATIVE_GENERATIONS", set())
    monkeypatch.setattr(producer, "_LOADER_GENERATIONS", set())
    if loader:
        producer.register_native(root, None, host_context, loader_hash="a" * 64)
        producer.register_native(root, None, host_context, loader_hash="b" * 64)
        assert producer._LOADER_GENERATIONS == {(root, "a" * 64, host_context)}
    else:
        producer.register_native(root, "a" * 64, host_context)
        producer.register_native(root, "b" * 64, host_context)
        assert producer._NATIVE_GENERATIONS == {(root, "a" * 64, host_context)}
    assert producer._UNAVAILABLE == "native_load_generation_changed"


def test_native_registration_refuses_a_lookalike_bridge(tmp_path, monkeypatch):
    from types import ModuleType

    monkeypatch.setattr(producer, "_UNAVAILABLE", None)
    with pytest.raises(producer.ProducerUnavailable, match="untrusted_native_bridge"):
        producer.register_native(tmp_path, "a" * 64, ModuleType("evidence_wiki.host_context"))


def _isolated(code):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", code], env=environment, cwd=ROOT, text=True,
                          capture_output=True, check=True).stdout


def test_import_baseline_preserves_lazy_public_exports():
    output = _isolated("import evidence_wiki, sys; print('evidence_wiki.workspace' in sys.modules); "
                       "print(evidence_wiki._review_producer_owner._PACKAGE_BASELINE is not None)")
    assert output.splitlines() == ["False", "True"]


def test_bound_producer_documents_are_detached_and_legacy_identity_agrees():
    output = _isolated('''from evidence_wiki import host_context, _script_host
with host_context.review_producer_binding() as generation:
    document = generation.document()
    document["manifest"]["members"].clear()
    assert generation.document()["manifest"]["members"]
    native = _script_host.load_packaged_script(_script_host.shared_assets_root(), "_selected_publication")
    assert native.producer_identity() == generation.legacy_producer_id
    assert host_context.current_review_producer() is generation
print("passed")
''')
    assert output.strip() == "passed"


def test_live_generation_refuses_replacement_of_the_actual_loader_module():
    output = _isolated('''from evidence_wiki import _review_producer as producer, _script_host
generation = producer.capture()
_script_host._LOADER_MODULE_CACHE.clear()
try:
    producer.assert_current(generation)
except producer.ProducerUnavailable:
    print("refused")
else:
    raise RuntimeError("replacement loader accepted")
''')
    assert output.strip() == "refused"


def test_live_generation_pins_native_module_lifetime_and_refuses_cache_eviction():
    output = _isolated('''import gc, weakref
from evidence_wiki import _review_producer as producer, _script_host
native = _script_host.load_packaged_script(_script_host.shared_assets_root(), "_strict_contract")
reference = weakref.ref(native)
generation = producer.capture()
key = next(key for key, value in _script_host._SCRIPT_MODULE_CACHE.items() if value is native)
del _script_host._SCRIPT_MODULE_CACHE[key]
del native
gc.collect()
assert reference() is not None
try:
    producer.assert_current(generation)
except producer.ProducerUnavailable:
    print("refused")
else:
    raise RuntimeError("evicted native module accepted")
''')
    assert output.strip() == "refused"


def test_actual_owned_membership_mutation_refuses_closing_capture(tmp_path):
    copied = tmp_path / "source"
    import shutil

    for name in ("src", "workspace-template", "domain-packs", "orchestrator"):
        shutil.copytree(ROOT / name, copied / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(ROOT / "pyproject.toml", copied / "pyproject.toml")
    environment = {**os.environ, "PYTHONPATH": str(copied / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", '''from evidence_wiki import _review_producer as producer
from pathlib import Path
generation = producer.capture()
Path("workspace-template/.new-policy").write_text("new")
try:
    producer.assert_current(generation)
except producer.ProducerUnavailable:
    print("refused")
else:
    raise RuntimeError("changed producer accepted")
'''], cwd=copied, env=environment, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "refused"


def test_complete_logical_identity_matches_source_and_installed_layouts(tmp_path):
    import shutil

    source_id = _isolated("from evidence_wiki import _review_producer as p; print(p.capture().producer_id)").strip()
    installation = tmp_path / "installation"
    package = installation / "evidence_wiki"
    shutil.copytree(ROOT / "src/evidence_wiki", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in producer.ASSET_NAMES:
        shutil.copytree(ROOT / name, package / "assets" / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    environment = {**os.environ, "PYTHONPATH": str(installation), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", "from evidence_wiki import _review_producer as p; print(p.capture().producer_id)"],
                            cwd=tmp_path, env=environment, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == source_id


def test_non_python_owned_changes_after_import_refuse_first_capture(tmp_path):
    import shutil

    installation = tmp_path / "installation"
    package = installation / "evidence_wiki"
    shutil.copytree(ROOT / "src/evidence_wiki", package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in producer.ASSET_NAMES:
        shutil.copytree(ROOT / name, package / "assets" / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    environment = {**os.environ, "PYTHONPATH": str(installation), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", '''import evidence_wiki
from evidence_wiki import _review_producer as producer, resources
with resources.assets_root() as root:
    policy = root / "workspace-template/research.yml"
    policy.write_bytes(policy.read_bytes() + b"\\n")
try:
    producer.capture()
except producer.ProducerUnavailable:
    print("refused")
else:
    raise RuntimeError("post-baseline asset bytes accepted")
'''], cwd=tmp_path, env=environment, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "refused"
