from __future__ import annotations

import contextlib
import io
import json
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import cli  # noqa: E402


class DomainPackLifecycleTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(args))
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def init_workspace(self, root: Path, *, pack: str = "general-science") -> Path:
        target = root / "workspace"
        code, _stdout, stderr = self.run_cli(
            "init",
            "--target",
            str(target),
            "--project-name",
            "lifecycle-project",
            "--project-description",
            "Workspace for domain-pack lifecycle regression tests.",
            "--owner-goal",
            "Prove safe pack reconciliation.",
            "--domain-pack",
            pack,
        )
        self.assertEqual(0, code, stderr)
        return target

    def revision(
        self,
        root: Path,
        *,
        version: str = "0.2.0",
        description: str = "Revised scientific domain guidance.",
    ) -> Path:
        destination = root / "candidate" / "general-science"
        shutil.copytree(REPO_ROOT / "domain-packs" / "general-science", destination)
        overlay_path = destination / "research.overlay.yml"
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        overlay["domain_pack"]["version"] = version
        overlay["domain_pack"]["description"] = description
        overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
        (destination / "README.md").write_text(
            (destination / "README.md").read_text(encoding="utf-8") + "\nCandidate revision marker.\n",
            encoding="utf-8",
        )
        return destination

    def config(self, workspace: Path) -> dict:
        return yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))

    def set_config_value(self, workspace: Path, *keys: str, value: object) -> None:
        document = self.config(workspace)
        current = document
        for key in keys[:-1]:
            current = current[key]
        current[keys[-1]] = value
        (workspace / "research.yml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def lifecycle_state(self, workspace: Path) -> dict:
        return yaml.safe_load(
            (workspace / "domain-packs" / ".evidence-wiki-state.yml").read_text(encoding="utf-8")
        )

    def test_config_reconciliation_units_cover_local_only_convergence_lists_shapes_and_new_collisions(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")

        local_document = {"root": {"leaf": "local"}}
        local = lifecycle._ConfigPlanner(
            local_document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        local.owned(
            ("root", "leaf"),
            base="v1",
            incoming_present=True,
            incoming="v1",
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual("local", local_document["root"]["leaf"])
        self.assertEqual([], local.conflicts)
        self.assertIn("config:/root/leaf", local.local_overrides)
        self.assertEqual([], local.ownership)

        converged_document = {"root": {"leaf": "v2"}}
        converged = lifecycle._ConfigPlanner(
            converged_document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        converged.owned(
            ("root", "leaf"),
            base="v1",
            incoming_present=True,
            incoming="v2",
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual([], converged.conflicts)
        self.assertEqual("v2", converged.ownership[0]["last_applied"])

        nested_document = {"root": {"scalar": 1, "items": ["local"]}}
        nested = lifecycle._ConfigPlanner(
            nested_document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        nested.owned(
            ("root",),
            base={"scalar": 1, "items": ["v1"]},
            incoming_present=True,
            incoming={"scalar": 2, "items": ["v2"]},
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual(2, nested_document["root"]["scalar"])
        self.assertEqual(["local"], nested_document["root"]["items"])
        self.assertEqual(["config:/root/items"], [item["target"] for item in nested.conflicts])

        shape_document = {"root": {"value": {"nested": True}}}
        shape = lifecycle._ConfigPlanner(
            shape_document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        shape.owned(
            ("root", "value"),
            base={"nested": True},
            incoming_present=True,
            incoming=["whole", "replacement"],
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual(["whole", "replacement"], shape_document["root"]["value"])

        collision_document = {"root": {"new": "same"}}
        collision = lifecycle._ConfigPlanner(
            collision_document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        collision.new(("root", "new"), incoming="same")
        self.assertEqual(["config:/root/new"], [item["target"] for item in collision.conflicts])

    def test_config_value_equality_is_canonical_and_scalar_type_aware(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")

        self.assertTrue(lifecycle._same(True, {"b": 2, "a": ["x"]}, True, {"a": ["x"], "b": 2}))
        self.assertFalse(lifecycle._same(True, True, True, 1))
        self.assertFalse(lifecycle._same(True, 1, True, 1.0))
        self.assertFalse(lifecycle._same(True, {"nested": [False]}, True, {"nested": [0]}))
        self.assertFalse(lifecycle._same(False, None, True, None))

    def test_retired_config_uses_effective_fallback_and_requires_unknown_review(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        target = "config:/section/value"

        converged_document = {"section": {"value": "starter"}}
        converged = lifecycle._ConfigPlanner(
            converged_document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        converged.owned(
            ("section", "value"),
            base="pack-v1",
            incoming_present=False,
            incoming=None,
            fallback=lifecycle.Fallback(True, True, "starter"),
        )
        self.assertEqual([], converged.conflicts)
        self.assertEqual("starter", converged_document["section"]["value"])
        self.assertEqual([], converged.ownership)

        for resolution, expected_present in (("keep", False), ("accept", True)):
            with self.subTest(known_fallback_resolution=resolution):
                document = {"section": {}}
                planner = lifecycle._ConfigPlanner(
                    document,
                    keep_local={target} if resolution == "keep" else set(),
                    accept_pack={target} if resolution == "accept" else set(),
                    local_overrides=set(),
                )
                planner.owned(
                    ("section", "value"),
                    base="pack-v1",
                    incoming_present=False,
                    incoming=None,
                    fallback=lifecycle.Fallback(True, True, "starter"),
                )
                self.assertEqual([], planner.conflicts)
                self.assertEqual(expected_present, "value" in document["section"])
                if expected_present:
                    self.assertEqual("starter", document["section"]["value"])

        unresolved = lifecycle._ConfigPlanner(
            {"section": {}},
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        unresolved.owned(
            ("section", "value"),
            base="pack-v1",
            incoming_present=False,
            incoming=None,
            fallback=lifecycle.Fallback(False),
        )
        self.assertEqual([target], [item["target"] for item in unresolved.conflicts])

        for resolution in ("keep", "accept"):
            with self.subTest(unknown_fallback_resolution=resolution):
                document = {"section": {}}
                planner = lifecycle._ConfigPlanner(
                    document,
                    keep_local={target} if resolution == "keep" else set(),
                    accept_pack={target} if resolution == "accept" else set(),
                    local_overrides=set(),
                )
                planner.owned(
                    ("section", "value"),
                    base="pack-v1",
                    incoming_present=False,
                    incoming=None,
                    fallback=lifecycle.Fallback(False),
                )
                self.assertEqual([], planner.conflicts)
                self.assertNotIn("value", document["section"])

    def test_non_json_live_yaml_scalar_is_a_local_change_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            self.set_config_value(
                workspace,
                "domain_pack",
                "description",
                value=date(2026, 8, 13),
            )

            code, stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate),
                "--dry-run", "--format", "json",
            )

            self.assertEqual(3, code, stderr)
            self.assertIn(
                "config:/domain_pack/description",
                [item["target"] for item in json.loads(stdout)["conflicts"]],
            )

    def test_released_config_ancestor_covers_children_added_by_later_revisions(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        document = {"branch": {"local": "operator"}}
        planner = lifecycle._ConfigPlanner(
            document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides={"config:/branch"},
        )

        for parts, incoming in lifecycle._new_overlay_units(
            {"branch": {"pack": "v1"}},
            {"branch": {"pack": "v1", "new": {"child": "v2"}}},
        ):
            if planner.overlapping_override_targets(parts):
                continue
            planner.new(parts, incoming=incoming)

        self.assertEqual({"branch": {"local": "operator"}}, document)
        self.assertEqual({"config:/branch"}, planner.local_overrides)
        self.assertEqual([], planner.ownership)
        self.assertEqual([], planner.conflicts)

    def test_released_config_ancestor_stays_unowned_across_refresh_revisions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            document = self.config(workspace)
            document["released_branch"] = {"operator": "keep"}
            (workspace / "research.yml").write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )

            first = self.revision(root, version="0.2.0")
            first_overlay_path = first / "research.overlay.yml"
            first_overlay = yaml.safe_load(first_overlay_path.read_text(encoding="utf-8"))
            first_overlay["released_branch"] = {"pack": "v1"}
            first_overlay_path.write_text(
                yaml.safe_dump(first_overlay, sort_keys=False), encoding="utf-8"
            )
            code, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(first),
                "--keep-local",
                "config:/released_branch",
                "--format",
                "json",
            )
            self.assertEqual(0, code, stderr)

            second = root / "candidate-v2" / "general-science"
            shutil.copytree(first, second)
            second_overlay_path = second / "research.overlay.yml"
            second_overlay = yaml.safe_load(second_overlay_path.read_text(encoding="utf-8"))
            second_overlay["domain_pack"]["version"] = "0.3.0"
            second_overlay["released_branch"]["new_child"] = "pack-v2"
            second_overlay_path.write_text(
                yaml.safe_dump(second_overlay, sort_keys=False), encoding="utf-8"
            )
            code, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(second),
                "--format",
                "json",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual({"operator": "keep"}, self.config(workspace)["released_branch"])
            state = self.lifecycle_state(workspace)
            self.assertIn("config:/released_branch", state["local_overrides"])
            self.assertFalse(
                any(entry["path"].startswith("/released_branch") for entry in state["config_ownership"])
            )

    def test_released_descendant_requires_whole_shape_transition_resolution(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        local = {"branch": {"leaf": "operator", "pack_sibling": "v1"}}
        planner = lifecycle._ConfigPlanner(
            local,
            keep_local=set(),
            accept_pack=set(),
            local_overrides={"config:/branch/leaf"},
        )
        planner.released_shape_transition(
            ("branch",),
            incoming=["pack-v2"],
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual(["config:/branch"], [item["target"] for item in planner.conflicts])
        self.assertEqual({"leaf": "operator", "pack_sibling": "v1"}, local["branch"])

        accepted_document = {"branch": {"leaf": "operator", "pack_sibling": "v1"}}
        accepted = lifecycle._ConfigPlanner(
            accepted_document,
            keep_local=set(),
            accept_pack={"config:/branch"},
            local_overrides={"config:/branch/leaf"},
        )
        accepted.released_shape_transition(
            ("branch",),
            incoming=["pack-v2"],
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual(["pack-v2"], accepted_document["branch"])
        self.assertEqual(set(), accepted.local_overrides)
        self.assertEqual(["/branch"], [entry["path"] for entry in accepted.ownership])

    def test_file_reconciliation_treats_new_collisions_and_locally_edited_retirements_as_conflicts(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            installed = root / "installed"
            candidate = root / "candidate"
            installed.mkdir()
            candidate.mkdir()
            (installed / "retired.md").write_text("locally edited\n", encoding="utf-8")
            (installed / "new.md").write_text("same bytes\n", encoding="utf-8")
            (candidate / "new.md").write_text("same bytes\n", encoding="utf-8")
            state = {
                "managed_files": [
                    {"path": "retired.md", "sha256": lifecycle.sha256_bytes(b"pack v1\n")}
                ],
                "revision_files": [
                    {"path": "retired.md", "sha256": lifecycle.sha256_bytes(b"pack v1\n")}
                ],
            }
            _changes, conflicts, targets, desired, _managed = lifecycle._refresh_file_plan(
                installed_root=installed,
                candidate_root=candidate,
                state=state,
                keep=set(),
                accept=set(),
                local_overrides=set(),
            )
            self.assertEqual(
                ["file:new.md", "file:retired.md"],
                sorted(item["target"] for item in conflicts),
            )
            self.assertEqual({"file:new.md", "file:retired.md"}, targets)
            self.assertEqual({}, desired)

    def test_released_file_stays_unowned_until_the_declaration_is_retired(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            installed = root / "installed"
            candidate = root / "candidate"
            installed.mkdir()
            candidate.mkdir()
            prior_hash = lifecycle.sha256_bytes(b"pack v1\n")
            (installed / "guide.md").write_text("local edit\n", encoding="utf-8")
            (candidate / "guide.md").write_text("pack v2\n", encoding="utf-8")
            state = {
                "managed_files": [],
                "revision_files": [{"path": "guide.md", "sha256": prior_hash}],
            }
            overrides = {"file:guide.md"}

            changes, conflicts, targets, desired, managed = lifecycle._refresh_file_plan(
                installed_root=installed,
                candidate_root=candidate,
                state=state,
                keep=set(),
                accept=set(),
                local_overrides=overrides,
            )
            self.assertEqual([], changes)
            self.assertEqual([], conflicts)
            self.assertEqual(set(), targets)
            self.assertEqual({}, desired)
            self.assertEqual([], managed)
            self.assertEqual({"file:guide.md"}, overrides)

            (candidate / "guide.md").unlink()
            lifecycle._refresh_file_plan(
                installed_root=installed,
                candidate_root=candidate,
                state=state,
                keep=set(),
                accept=set(),
                local_overrides=overrides,
            )
            self.assertEqual(set(), overrides)

    def test_mapping_shape_conflicts_are_reported_and_accept_pack_can_resolve(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        document = {"root": "local scalar"}
        planner = lifecycle._ConfigPlanner(
            document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        planner.mapping_shape_conflict(
            ("root",),
            incoming={"leaf": "pack v2"},
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual(["config:/root"], [item["target"] for item in planner.conflicts])

        accepted_document = {"root": "local scalar"}
        accepted = lifecycle._ConfigPlanner(
            accepted_document,
            keep_local=set(),
            accept_pack={"config:/root"},
            local_overrides=set(),
        )
        accepted.mapping_shape_conflict(
            ("root",),
            incoming={"leaf": "pack v2"},
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual({"leaf": "pack v2"}, accepted_document["root"])
        self.assertEqual([], accepted.conflicts)
        self.assertEqual("/root", accepted.ownership[0]["path"])

    def test_explicit_empty_mapping_remains_owned_until_later_retirement(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        document = {"branch": {"leaf": "pack-v1"}}
        emptied = lifecycle._ConfigPlanner(
            document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        emptied.owned(
            ("branch",),
            base={"leaf": "pack-v1"},
            incoming_present=True,
            incoming={},
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual({}, document["branch"])
        self.assertEqual(["/branch"], [entry["path"] for entry in emptied.ownership])

        retired = lifecycle._ConfigPlanner(
            document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        retired.owned(
            ("branch",),
            base={},
            incoming_present=False,
            incoming=None,
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertNotIn("branch", document)
        self.assertEqual([], retired.conflicts)

    def test_locally_changed_identity_supports_both_reported_resolutions(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        parts = ("domain_pack", "version")
        document = {"domain_pack": {"version": "local"}}
        planner = lifecycle._ConfigPlanner(
            document,
            keep_local=set(),
            accept_pack=set(),
            local_overrides=set(),
        )
        planner.owned(
            parts,
            base="1",
            incoming_present=True,
            incoming="1",
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual(["config:/domain_pack/version"], [item["target"] for item in planner.conflicts])
        self.assertNotIn("config:/domain_pack/version", planner.local_overrides)

        kept_document = {"domain_pack": {"version": "local"}}
        kept = lifecycle._ConfigPlanner(
            kept_document,
            keep_local={"config:/domain_pack/version"},
            accept_pack=set(),
            local_overrides=set(),
        )
        kept.owned(
            parts,
            base="1",
            incoming_present=True,
            incoming="1",
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual("local", kept_document["domain_pack"]["version"])
        self.assertEqual([], kept.conflicts)
        self.assertIn("config:/domain_pack/version", kept.local_overrides)

        accepted_document = {"domain_pack": {"version": "local"}}
        accepted = lifecycle._ConfigPlanner(
            accepted_document,
            keep_local=set(),
            accept_pack={"config:/domain_pack/version"},
            local_overrides=set(),
        )
        accepted.owned(
            parts,
            base="1",
            incoming_present=True,
            incoming="1",
            fallback=lifecycle.Fallback(True, False),
        )
        self.assertEqual("1", accepted_document["domain_pack"]["version"])
        self.assertEqual([], accepted.conflicts)

        for resolution in ("--keep-local", "--accept-pack"):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                workspace = self.init_workspace(root)
                candidate = self.revision(root)
                self.set_config_value(workspace, "domain_pack", "version", value="local")
                code, _stdout, stderr = self.run_cli(
                    "pack", "refresh", "--target", str(workspace), "--path", str(candidate),
                    resolution, "config:/domain_pack/version", "--format", "json",
                )
                self.assertEqual(0, code, stderr)
                configured_version = self.config(workspace)["domain_pack"]["version"]
                self.assertEqual(
                    "local" if resolution == "--keep-local" else "0.2.0",
                    configured_version,
                )
                expected_state = (
                    "local_modifications" if resolution == "--keep-local" else "current"
                )
                self.assertEqual(
                    expected_state,
                    lifecycle.inspect_workspace(workspace)["state"],
                )

    def test_init_tracks_pack_and_identical_refresh_is_a_zero_write_dry_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            state_path = workspace / "domain-packs" / ".evidence-wiki-state.yml"
            self.assertTrue(state_path.is_file())
            state = self.lifecycle_state(workspace)
            self.assertEqual("1.0", state["schema_version"])
            self.assertEqual("general-science", state["pack"]["name"])
            self.assertEqual("bundled", state["pack"]["source_kind"])
            self.assertTrue(state["config_ownership"])
            self.assertTrue(state["managed_files"])

            before = {
                path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }
            code, stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                "general-science",
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(0, code, stderr)
            report = json.loads(stdout)
            self.assertEqual("no_changes", report["status"])
            self.assertEqual([], report["changes"])
            self.assertFalse(report["log_appended"])
            self.assertFalse((workspace / ".locks").exists())
            after = {
                path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_inspector_distinguishes_missing_overlay_malformed_declaration_and_invalid_state(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            state_path = workspace / "domain-packs/.evidence-wiki-state.yml"
            overlay_path = workspace / "domain-packs/general-science/research.overlay.yml"
            overlay_bytes = overlay_path.read_bytes()

            overlay_path.unlink()
            self.assertEqual("pack_missing", lifecycle.inspect_workspace(workspace)["state"])
            overlay_path.write_bytes(b"not: [valid\n")
            self.assertEqual("config_tree_skew", lifecycle.inspect_workspace(workspace)["state"])
            overlay_path.write_bytes(overlay_bytes)

            state = self.lifecycle_state(workspace)
            state["transaction_id"] = "stale-transaction"
            state_path.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")
            self.assertEqual("state_invalid", lifecycle.inspect_workspace(workspace)["state"])

            state_path.unlink()
            research = self.config(workspace)
            research["domain_pack"] = "malformed"
            (workspace / "research.yml").write_text(
                yaml.safe_dump(research, sort_keys=False), encoding="utf-8"
            )
            self.assertEqual("state_invalid", lifecycle.inspect_workspace(workspace)["state"])

    def test_inspector_reconciles_workspace_and_pack_contract_metadata(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            metadata_path = workspace / "workspace-system.yml"
            original_metadata = metadata_path.read_text(encoding="utf-8")

            metadata = yaml.safe_load(original_metadata)
            metadata["workspace_system"]["compatible_research_yml_contract"] = "99.0"
            metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")
            inspection = lifecycle.inspect_workspace(workspace)
            self.assertEqual("config_tree_skew", inspection["state"])
            self.assertEqual(1, inspection["conflict_count"])

            metadata["workspace_system"]["compatible_research_yml_contract"] = []
            metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")
            self.assertEqual("state_invalid", lifecycle.inspect_workspace(workspace)["state"])

            metadata_path.write_text("workspace_system: [not, a, mapping]\n", encoding="utf-8")
            self.assertEqual("state_invalid", lifecycle.inspect_workspace(workspace)["state"])

            metadata_path.write_text(original_metadata, encoding="utf-8")
            config = self.config(workspace)
            config["domain_pack"]["compatible_research_yml_contract"] = []
            (workspace / "research.yml").write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
            self.assertEqual("state_invalid", lifecycle.inspect_workspace(workspace)["state"])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_domain_packs_parent_symlink_is_invalid_for_status_refresh_and_adoption(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            external = root / "external-domain-packs"
            (workspace / "domain-packs").rename(external)
            (workspace / "domain-packs").symlink_to(external, target_is_directory=True)

            self.assertEqual("state_invalid", lifecycle.inspect_workspace(workspace)["state"])
            code, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                "general-science",
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(2, code)
            self.assertEqual("DOMAIN_PACK_STATE_INVALID", json.loads(stderr)["error_code"])

            (external / ".evidence-wiki-state.yml").unlink()
            code, _stdout, stderr = self.run_cli(
                "pack",
                "adopt",
                "--target",
                str(workspace),
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(2, code)
            self.assertIn(
                json.loads(stderr)["error_code"],
                {"DOMAIN_PACK_STATE_INVALID", "DOMAIN_PACK_UNTRACKED"},
            )
            self.assertFalse((external / ".evidence-wiki-state.yml").exists())

    def test_adoption_rechecks_the_installed_pack_tree_before_writing_state(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            (workspace / "domain-packs/.evidence-wiki-state.yml").unlink()
            plan = lifecycle.plan_adopt(workspace, dry_run=False)
            managed = workspace / "domain-packs/general-science/README.md"
            managed.write_text(managed.read_text(encoding="utf-8") + "\nchanged later\n", encoding="utf-8")

            with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                lifecycle.apply_plan(workspace, plan, installed_target_relative=None)
            self.assertEqual("DOMAIN_PACK_REFRESH_CONFLICT", caught.exception.error_code)
            self.assertFalse((workspace / "domain-packs/.evidence-wiki-state.yml").exists())
            self.assertFalse((workspace / "domain-packs/.evidence-wiki-transaction.yml").exists())

    def test_refresh_updates_config_and_files_while_preserving_comments_and_user_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            research = workspace / "research.yml"
            research.write_text(
                research.read_text(encoding="utf-8").replace(
                    "project:\n", "# operator comment survives refresh\nproject:\n", 1
                ),
                encoding="utf-8",
            )
            raw_note = workspace / "raw" / "other" / "operator.txt"
            raw_note.write_text("preserve me\n", encoding="utf-8")
            wiki_note = workspace / "wiki" / "concepts" / "operator.md"
            wiki_note.write_text("# Operator note\n", encoding="utf-8")
            index_before = (workspace / "index.md").read_bytes()
            log_before = (workspace / "log.md").read_text(encoding="utf-8")

            dry_code, dry_stdout, dry_stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(0, dry_code, dry_stderr)
            dry_report = json.loads(dry_stdout)
            self.assertEqual("planned", dry_report["status"])
            self.assertEqual(
                sorted(dry_report["changes"], key=lambda item: (item["path"], item["action"])),
                dry_report["changes"],
            )
            self.assertNotIn("value", json.dumps(dry_report["changes"]))
            self.assertEqual(log_before, (workspace / "log.md").read_text(encoding="utf-8"))
            self.assertFalse((workspace / ".locks").exists())

            code, stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--format",
                "json",
            )
            self.assertEqual(0, code, stderr)
            report = json.loads(stdout)
            self.assertEqual("applied", report["status"])
            self.assertTrue(report["log_appended"])
            self.assertEqual("0.2.0", self.config(workspace)["domain_pack"]["version"])
            self.assertEqual(
                "Revised scientific domain guidance.",
                self.config(workspace)["domain_pack"]["description"],
            )
            self.assertIn("operator comment survives refresh", research.read_text(encoding="utf-8"))
            self.assertEqual("preserve me\n", raw_note.read_text(encoding="utf-8"))
            self.assertEqual("# Operator note\n", wiki_note.read_text(encoding="utf-8"))
            self.assertEqual(index_before, (workspace / "index.md").read_bytes())
            self.assertIn("Candidate revision marker.", (workspace / "domain-packs/general-science/README.md").read_text())
            self.assertEqual(1, (workspace / "log.md").read_text().count("domain-pack-refresh |"))
            self.assertEqual("current", json.loads(self.run_cli("status", "--target", str(workspace), "--format", "json")[1])["domain_pack"]["state"])

            second_code, second_stdout, second_stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate), "--format", "json"
            )
            self.assertEqual(0, second_code, second_stderr)
            self.assertEqual("no_changes", json.loads(second_stdout)["status"])
            self.assertEqual(1, (workspace / "log.md").read_text().count("domain-pack-refresh |"))

    def test_config_conflict_is_zero_write_and_both_resolutions_are_path_specific(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            self.set_config_value(
                workspace,
                "domain_pack",
                "description",
                value="Local operator description.",
            )
            protected = {
                "research": (workspace / "research.yml").read_bytes(),
                "state": (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes(),
                "readme": (workspace / "domain-packs/general-science/README.md").read_bytes(),
                "log": (workspace / "log.md").read_bytes(),
            }

            code, stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate), "--format", "json"
            )
            self.assertEqual(3, code)
            report = json.loads(stdout)
            error = json.loads(stderr)
            self.assertEqual("conflict", report["status"])
            self.assertEqual("DOMAIN_PACK_REFRESH_CONFLICT", error["error_code"])
            self.assertIn("config:/domain_pack/description", [item["target"] for item in report["conflicts"]])
            self.assertEqual(protected["research"], (workspace / "research.yml").read_bytes())
            self.assertEqual(protected["state"], (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes())
            self.assertEqual(protected["readme"], (workspace / "domain-packs/general-science/README.md").read_bytes())
            self.assertEqual(protected["log"], (workspace / "log.md").read_bytes())

            keep_code, keep_stdout, keep_stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--keep-local",
                "config:/domain_pack/description",
                "--format",
                "json",
            )
            self.assertEqual(0, keep_code, keep_stderr)
            self.assertEqual("applied", json.loads(keep_stdout)["status"])
            self.assertEqual("Local operator description.", self.config(workspace)["domain_pack"]["description"])
            state = self.lifecycle_state(workspace)
            self.assertIn("config:/domain_pack/description", state["local_overrides"])
            self.assertEqual(
                "local_modifications",
                cli._packaged_script("_domain_pack_lifecycle").inspect_workspace(workspace)["state"],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            self.set_config_value(workspace, "domain_pack", "description", value="Displaced local value.")
            accept_code, accept_stdout, accept_stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--accept-pack",
                "config:/domain_pack/description",
                "--format",
                "json",
            )
            self.assertEqual(0, accept_code, accept_stderr)
            self.assertEqual("applied", json.loads(accept_stdout)["status"])
            self.assertEqual(
                "Revised scientific domain guidance.", self.config(workspace)["domain_pack"]["description"]
            )
            backups = list((workspace / ".replaced/domain-packs").glob("*/backup/research.yml"))
            self.assertEqual(1, len(backups))
            self.assertIn("Displaced local value.", backups[0].read_text(encoding="utf-8"))

    def test_refresh_collapses_mapping_shape_transition_to_one_resolvable_target(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            overlay_path = candidate / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            overlay["taxonomy"] = ["pack", "replacement"]
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")

            plan = lifecycle.plan_refresh(
                workspace,
                candidate,
                dry_run=True,
            )
            self.assertEqual([], plan.report["conflicts"])
            self.assertEqual(
                ["pack", "replacement"], yaml.safe_load(plan.config_text)["taxonomy"]
            )

            # A descendant released by an earlier revision keeps the whole
            # mapping-to-list transition ambiguous, even though sibling values
            # remain pack-owned. The resolution target is the transition root.
            document = self.config(workspace)
            document["taxonomy"]["entity_types"] = ["operator-type"]
            (workspace / "research.yml").write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            state = lifecycle.load_state(workspace)
            state["config_ownership"] = [
                entry
                for entry in state["config_ownership"]
                if entry["path"] != "/taxonomy/entity_types"
            ]
            state["local_overrides"].append("config:/taxonomy/entity_types")
            lifecycle.write_initial_state(workspace, state)
            with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                lifecycle.plan_refresh(workspace, candidate, dry_run=True)
            self.assertEqual("DOMAIN_PACK_REFRESH_CONFLICT", caught.exception.error_code)
            self.assertEqual(
                ["config:/taxonomy"],
                [item["target"] for item in caught.exception.details["report"]["conflicts"]],
            )
            kept = lifecycle.plan_refresh(
                workspace,
                candidate,
                keep_local=["config:/taxonomy"],
                dry_run=True,
            )
            self.assertIsInstance(yaml.safe_load(kept.config_text)["taxonomy"], dict)
            self.assertIn("config:/taxonomy", kept.state["local_overrides"])
            self.assertNotIn("config:/taxonomy/entity_types", kept.state["local_overrides"])
            accepted = lifecycle.plan_refresh(
                workspace,
                candidate,
                accept_pack=["config:/taxonomy"],
                dry_run=True,
            )
            self.assertEqual(
                ["pack", "replacement"], yaml.safe_load(accepted.config_text)["taxonomy"]
            )

    def test_file_conflict_and_unknown_or_contradictory_resolutions_refuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            installed_readme = workspace / "domain-packs/general-science/README.md"
            installed_readme.write_text("local pack file\n", encoding="utf-8")
            before = installed_readme.read_bytes()

            code, stdout, _stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate), "--format", "json"
            )
            self.assertEqual(3, code)
            self.assertIn("file:README.md", [item["target"] for item in json.loads(stdout)["conflicts"]])
            self.assertEqual(before, installed_readme.read_bytes())

            for flags in (
                ("--accept-pack", "file:not-a-conflict.md"),
                (
                    "--keep-local",
                    "file:README.md",
                    "--accept-pack",
                    "file:README.md",
                ),
            ):
                refused, _out, err = self.run_cli(
                    "pack",
                    "refresh",
                    "--target",
                    str(workspace),
                    "--path",
                    str(candidate),
                    *flags,
                    "--format",
                    "json",
                )
                self.assertEqual(3, refused)
                self.assertEqual("DOMAIN_PACK_REFRESH_CONFLICT", json.loads(err)["error_code"])
                self.assertEqual(before, installed_readme.read_bytes())

            accepted, accepted_stdout, accepted_stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--accept-pack",
                "file:README.md",
                "--format",
                "json",
            )
            self.assertEqual(0, accepted, accepted_stderr)
            self.assertEqual("applied", json.loads(accepted_stdout)["status"])
            self.assertIn("Candidate revision marker.", installed_readme.read_text(encoding="utf-8"))

    def test_refresh_retires_unchanged_file_and_restores_starter_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            overlay_path = candidate / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            overlay.pop("wiki")
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
            (candidate / "README.md").unlink()
            starter = yaml.safe_load((REPO_ROOT / "workspace-template/research.yml").read_text())

            code, stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate), "--format", "json"
            )
            self.assertEqual(0, code, stderr)
            report = json.loads(stdout)
            self.assertIn(
                {"path": "file:README.md", "action": "delete"},
                report["changes"],
            )
            self.assertFalse((workspace / "domain-packs/general-science/README.md").exists())
            self.assertEqual(starter["wiki"]["required_dirs"], self.config(workspace)["wiki"]["required_dirs"])

    def test_refresh_delivers_a_policy_rule_that_changes_runtime_evaluation(self):
        policy_id = "pack:general-science/openalex-delivery"

        def write_overlay(pack: Path, *, version: str, with_rule: bool) -> None:
            overlay_path = pack / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            domain_pack = overlay["domain_pack"]
            domain_pack["version"] = version
            domain_pack.setdefault("policy_vocabularies", {}).setdefault("source_policy", {})[
                policy_id
            ] = "Evidence must be delivered by OpenAlex."
            if with_rule:
                domain_pack["policy_rules"] = {
                    policy_id: {
                        "all_of": [
                            {"one_of_provenance": {"providers": ["openalex"]}}
                        ]
                    }
                }
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")

        def verdict(workspace: Path) -> str:
            evidence = cli._packaged_script("_evidence_policies")
            inputs = evidence.PolicyInputs(
                project_root=workspace,
                config=self.config(workspace),
                manifest_records={"source-1": {"id": "source-1"}},
                normalized_records={},
                provenance_by_source_id={
                    "source-1": {"provider_registration": {"id": "openalex"}}
                },
                candidates=[],
                candidates_by_request_id={},
                jurisdiction_profiles={},
                coverage_manifests={},
            )
            return evidence.evaluate_source_policy(policy_id, ["source-1"], inputs).verdict

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            v1 = root / "v1/general-science"
            shutil.copytree(REPO_ROOT / "domain-packs/general-science", v1)
            write_overlay(v1, version="1.0.0", with_rule=False)
            workspace = self.init_workspace(root, pack=str(v1))
            self.assertEqual("manual_review", verdict(workspace))

            v2 = root / "v2/general-science"
            shutil.copytree(v1, v2)
            write_overlay(v2, version="2.0.0", with_rule=True)
            code, stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(v2), "--format", "json"
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("applied", json.loads(stdout)["status"])
            self.assertEqual("pass", verdict(workspace))

    def test_legacy_adoption_requires_acknowledgment_and_unknown_fallback_deletion_conflicts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            state_path = workspace / "domain-packs/.evidence-wiki-state.yml"
            state_path.unlink()
            status = json.loads(self.run_cli("status", "--target", str(workspace), "--format", "json")[1])
            self.assertEqual("legacy_untracked", status["domain_pack"]["state"])

            dry_code, dry_stdout, dry_stderr = self.run_cli(
                "pack", "adopt", "--target", str(workspace), "--dry-run", "--format", "json"
            )
            self.assertEqual(0, dry_code, dry_stderr)
            dry_report = json.loads(dry_stdout)
            self.assertEqual("planned", dry_report["status"])
            self.assertIn(
                "file:research.overlay.yml",
                [item["path"] for item in dry_report["changes"]],
            )
            self.assertFalse(state_path.exists())
            self.assertFalse((workspace / ".locks").exists())

            adopt_code, adopt_stdout, adopt_stderr = self.run_cli(
                "pack", "adopt", "--target", str(workspace), "--format", "json"
            )
            self.assertEqual(0, adopt_code, adopt_stderr)
            self.assertEqual("applied", json.loads(adopt_stdout)["status"])
            adopted = self.lifecycle_state(workspace)
            self.assertTrue(adopted["config_ownership"])
            self.assertTrue(all(not entry["fallback_known"] for entry in adopted["config_ownership"]))

            candidate = self.revision(root)
            overlay_path = candidate / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            overlay["domain_pack"].pop("description")
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
            conflict, conflict_stdout, _conflict_stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate), "--format", "json"
            )
            self.assertEqual(3, conflict)
            self.assertIn(
                "config:/domain_pack/description",
                [item["target"] for item in json.loads(conflict_stdout)["conflicts"]],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            (workspace / "domain-packs/.evidence-wiki-state.yml").unlink()
            self.set_config_value(workspace, "domain_pack", "description", value="legacy local override")
            refused, _stdout, stderr = self.run_cli(
                "pack", "adopt", "--target", str(workspace), "--format", "json"
            )
            self.assertEqual(3, refused)
            self.assertEqual("DOMAIN_PACK_REFRESH_CONFLICT", json.loads(stderr)["error_code"])
            accepted, accepted_stdout, accepted_stderr = self.run_cli(
                "pack",
                "adopt",
                "--target",
                str(workspace),
                "--accept-local-overrides",
                "--format",
                "json",
            )
            self.assertEqual(0, accepted, accepted_stderr)
            self.assertEqual("applied", json.loads(accepted_stdout)["status"])
            self.assertIn(
                "config:/domain_pack/description",
                self.lifecycle_state(workspace)["local_overrides"],
            )
            self.assertEqual(
                "local_modifications",
                cli._packaged_script("_domain_pack_lifecycle").inspect_workspace(workspace)["state"],
            )

    def test_refresh_reports_untracked_invalid_state_incomplete_transaction_and_invalid_candidate_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            state_path = workspace / "domain-packs/.evidence-wiki-state.yml"
            state_bytes = state_path.read_bytes()
            state_path.unlink()
            untracked, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                "general-science",
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(2, untracked)
            self.assertEqual("DOMAIN_PACK_UNTRACKED", json.loads(stderr)["error_code"])

            state_path.write_bytes(b"not: [valid\n")
            invalid, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                "general-science",
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(2, invalid)
            self.assertEqual("DOMAIN_PACK_STATE_INVALID", json.loads(stderr)["error_code"])

            state_path.write_bytes(state_bytes)
            journal = workspace / "domain-packs/.evidence-wiki-transaction.yml"
            journal.write_text("schema_version: '1.0'\ntransaction_id: interrupted\n", encoding="utf-8")
            incomplete, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                "general-science",
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(2, incomplete)
            self.assertEqual("DOMAIN_PACK_TRANSACTION_INCOMPLETE", json.loads(stderr)["error_code"])
            self.assertTrue(journal.exists())

            malformed = yaml.safe_load(journal.read_text(encoding="utf-8"))
            malformed["transaction_id"] = "../../control\nvalue"
            journal.write_text(yaml.safe_dump(malformed, sort_keys=False), encoding="utf-8")
            inspected = cli._packaged_script("_domain_pack_lifecycle").inspect_workspace(workspace)
            self.assertEqual("transaction_incomplete", inspected["state"])
            self.assertIsNone(inspected["transaction_id"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            overlay_path = candidate / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            overlay["domain_pack"]["name"] = "wrong-name"
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
            before = (workspace / "research.yml").read_bytes()
            invalid, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--format",
                "json",
            )
            self.assertEqual(2, invalid)
            self.assertEqual("DOMAIN_PACK_INVALID", json.loads(stderr)["error_code"])
            self.assertEqual(before, (workspace / "research.yml").read_bytes())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_symlinked_candidate_installed_file_and_lock_directory_refuse_before_pack_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            linked_candidate = root / "linked-candidate"
            linked_candidate.symlink_to(candidate, target_is_directory=True)
            invalid, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(linked_candidate),
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(2, invalid)
            self.assertEqual("DOMAIN_PACK_INVALID", json.loads(stderr)["error_code"])

            outside = root / "outside.md"
            outside.write_text("outside sentinel\n", encoding="utf-8")
            installed = workspace / "domain-packs/general-science/README.md"
            installed.unlink()
            installed.symlink_to(outside)
            invalid_state, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--dry-run",
                "--format",
                "json",
            )
            self.assertEqual(2, invalid_state)
            self.assertEqual("DOMAIN_PACK_STATE_INVALID", json.loads(stderr)["error_code"])
            self.assertEqual("outside sentinel\n", outside.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            external_locks = root / "external-locks"
            external_locks.mkdir()
            (workspace / ".locks").symlink_to(external_locks, target_is_directory=True)
            failed, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--format",
                "json",
            )
            self.assertEqual(2, failed)
            self.assertEqual("DOMAIN_PACK_WRITE_FAILED", json.loads(stderr)["error_code"])
            self.assertEqual([], list(external_locks.iterdir()))

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_symlink_loop_target_uses_stable_json_refusal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidate = self.revision(root)
            loop_a = root / "loop-a"
            loop_b = root / "loop-b"
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)

            for operation, expected in (
                ("refresh", "DOMAIN_PACK_STATE_INVALID"),
                ("adopt", "DOMAIN_PACK_UNTRACKED"),
            ):
                args = ["pack", operation, "--target", str(loop_a)]
                if operation == "refresh":
                    args.extend(("--path", str(candidate)))
                args.extend(("--format", "json"))
                code, _stdout, stderr = self.run_cli(*args)
                self.assertEqual(2, code)
                self.assertEqual(expected, json.loads(stderr)["error_code"])

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_symlinked_workspace_target_refuses_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            alias = root / "workspace-alias"
            alias.symlink_to(workspace, target_is_directory=True)
            before = {
                path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }

            code, _stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(alias), "--path", str(candidate),
                "--format", "json",
            )

            self.assertEqual(2, code)
            self.assertEqual("DOMAIN_PACK_STATE_INVALID", json.loads(stderr)["error_code"])
            after = {
                path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_replaced_symlink_refuses_before_staging_any_workspace_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            external = root / "external-replacements"
            external.mkdir()
            (workspace / ".replaced").symlink_to(external, target_is_directory=True)
            before_research = (workspace / "research.yml").read_bytes()
            before_state = (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes()

            failed, _stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--format",
                "json",
            )

            self.assertEqual(2, failed)
            self.assertEqual("DOMAIN_PACK_WRITE_FAILED", json.loads(stderr)["error_code"])
            self.assertEqual([], list(external.iterdir()))
            self.assertEqual(before_research, (workspace / "research.yml").read_bytes())
            self.assertEqual(before_state, (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes())

    def test_recovery_rejects_forged_journal_destination_outside_lifecycle_outputs(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            (workspace / "domain-packs").mkdir(parents=True)
            raw_file = workspace / "raw/evidence.txt"
            raw_file.parent.mkdir()
            raw_file.write_text("immutable evidence\n", encoding="utf-8")
            transaction_id = "forged"
            backup = (
                workspace
                / ".replaced/domain-packs"
                / transaction_id
                / "backup/raw/evidence.txt"
            )
            backup.parent.mkdir(parents=True)
            backup.write_text("forged replacement\n", encoding="utf-8")
            journal = workspace / "domain-packs/.evidence-wiki-transaction.yml"
            journal.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0",
                        "transaction_id": transaction_id,
                        "operation": "adopt",
                        "phase": "prepared",
                        "pack_target_relative": None,
                        "entries": [
                            {
                                "path": "raw/evidence.txt",
                                "existed": True,
                                "mode": 0o600,
                                "backup": (
                                    f".replaced/domain-packs/{transaction_id}/"
                                    "backup/raw/evidence.txt"
                                ),
                                "staged": None,
                            }
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                lifecycle.recover_transaction(workspace)

            self.assertEqual("DOMAIN_PACK_TRANSACTION_INCOMPLETE", caught.exception.error_code)
            self.assertEqual("immutable evidence\n", raw_file.read_text(encoding="utf-8"))
            self.assertTrue(journal.exists())

    def test_log_change_invalidates_plan_and_successful_state_stays_restrictive_and_complete(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            plan = lifecycle.plan_refresh(workspace, candidate, dry_run=False, source_kind="path")
            with (workspace / "log.md").open("a", encoding="utf-8") as handle:
                handle.write("\nconcurrent append\n")

            with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                lifecycle.apply_plan(
                    workspace,
                    plan,
                    installed_target_relative="domain-packs/general-science",
                    candidate_root=candidate,
                )
            self.assertEqual("DOMAIN_PACK_REFRESH_CONFLICT", caught.exception.error_code)
            self.assertFalse((workspace / "domain-packs/.evidence-wiki-transaction.yml").exists())

            report = lifecycle.run_refresh(workspace, candidate, source_kind="path")
            self.assertEqual("applied", report["status"])
            state_path = workspace / "domain-packs/.evidence-wiki-state.yml"
            state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
            self.assertIsNone(state["transaction_id"])
            self.assertEqual(0o600, stat.S_IMODE(state_path.stat().st_mode))

    def test_new_pack_path_with_local_file_ancestor_is_a_zero_write_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            (candidate / "nested").mkdir()
            (candidate / "nested/new.md").write_text("new pack file\n", encoding="utf-8")
            local_ancestor = workspace / "domain-packs/general-science/nested"
            local_ancestor.write_text("local file blocks directory\n", encoding="utf-8")
            state_before = (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes()

            code, stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--dry-run",
                "--format",
                "json",
            )

            self.assertEqual(3, code, stderr)
            self.assertIn(
                "file:nested/new.md",
                [item["target"] for item in json.loads(stdout)["conflicts"]],
            )
            self.assertEqual("local file blocks directory\n", local_ancestor.read_text(encoding="utf-8"))
            self.assertEqual(state_before, (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes())

    def test_pack_file_directory_shape_transitions_are_atomic_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            first = self.revision(root / "first", version="0.2.0")
            (first / "shape.md").mkdir()
            (first / "shape.md/a.md").write_text("managed child\n", encoding="utf-8")
            applied = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(first),
            )
            self.assertEqual(0, applied[0], applied[2])

            second = self.revision(root / "second", version="0.3.0")
            (second / "shape.md").write_text("replacement file\n", encoding="utf-8")
            applied = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(second),
            )
            self.assertEqual(0, applied[0], applied[2])
            self.assertEqual(
                "replacement file\n",
                (workspace / "domain-packs/general-science/shape.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "current",
                cli._packaged_script("_domain_pack_lifecycle").inspect_workspace(workspace)["state"],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            first = self.revision(root / "first", version="0.2.0")
            (first / "shape.md").mkdir()
            (first / "shape.md/a.md").write_text("managed child\n", encoding="utf-8")
            self.assertEqual(
                0,
                self.run_cli("pack", "refresh", "--target", str(workspace), "--path", str(first))[0],
            )
            installed_shape = workspace / "domain-packs/general-science/shape.md"
            (installed_shape / "local.md").write_text("untracked local file\n", encoding="utf-8")
            second = self.revision(root / "second", version="0.3.0")
            (second / "shape.md").write_text("replacement file\n", encoding="utf-8")
            code, stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(second),
                "--dry-run", "--format", "json",
            )
            self.assertEqual(3, code, stderr)
            self.assertEqual(["file:shape.md"], [item["target"] for item in json.loads(stdout)["conflicts"]])
            self.assertEqual("untracked local file\n", (installed_shape / "local.md").read_text(encoding="utf-8"))

            kept = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(second),
                "--keep-local", "file:shape.md", "--format", "json",
            )
            self.assertEqual(0, kept[0], kept[2])
            self.assertEqual("applied", json.loads(kept[1])["status"])
            self.assertEqual(
                "untracked local file\n",
                (installed_shape / "local.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "local_modifications",
                cli._packaged_script("_domain_pack_lifecycle").inspect_workspace(workspace)["state"],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            first = self.revision(root / "first", version="0.2.0")
            (first / "shape.md").mkdir()
            (first / "shape.md/a.md").write_text("managed child\n", encoding="utf-8")
            self.assertEqual(
                0,
                self.run_cli("pack", "refresh", "--target", str(workspace), "--path", str(first))[0],
            )
            installed_shape = workspace / "domain-packs/general-science/shape.md"
            (installed_shape / "local.md").write_text("untracked local file\n", encoding="utf-8")
            second = self.revision(root / "second", version="0.3.0")
            (second / "shape.md").write_text("replacement file\n", encoding="utf-8")

            accepted = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(second),
                "--accept-pack", "file:shape.md", "--format", "json",
            )
            self.assertEqual(0, accepted[0], accepted[2])
            self.assertEqual("applied", json.loads(accepted[1])["status"])
            self.assertEqual("replacement file\n", installed_shape.read_text(encoding="utf-8"))
            backups = list(
                (workspace / ".replaced/domain-packs").glob(
                    "*/backup/domain-packs/general-science/shape.md/local.md"
                )
            )
            self.assertEqual(1, len(backups))
            self.assertEqual("untracked local file\n", backups[0].read_text(encoding="utf-8"))
            self.assertEqual(
                "current",
                cli._packaged_script("_domain_pack_lifecycle").inspect_workspace(workspace)["state"],
            )

    def test_one_ancestor_resolution_covers_multiple_new_pack_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            (candidate / "nested").mkdir()
            (candidate / "nested/a.md").write_text("a\n", encoding="utf-8")
            (candidate / "nested/b.md").write_text("b\n", encoding="utf-8")
            (workspace / "domain-packs/general-science/nested").write_text(
                "blocking local file\n", encoding="utf-8"
            )

            code, stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate),
                "--dry-run", "--format", "json",
            )
            self.assertEqual(3, code, stderr)
            targets = [item["target"] for item in json.loads(stdout)["conflicts"]]
            self.assertEqual(["file:nested/a.md"], targets)

            code, _stdout, stderr = self.run_cli(
                "pack", "refresh", "--target", str(workspace), "--path", str(candidate),
                "--accept-pack", targets[0],
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("a\n", (workspace / "domain-packs/general-science/nested/a.md").read_text())
            self.assertEqual("b\n", (workspace / "domain-packs/general-science/nested/b.md").read_text())

    def test_write_refresh_recovers_journal_before_candidate_validation(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            original = (workspace / "research.yml").read_bytes()
            original_mode = stat.S_IMODE((workspace / "research.yml").stat().st_mode)
            transaction_id = "interrupted"
            backup_relative = (
                f".replaced/domain-packs/{transaction_id}/backup/research.yml"
            )
            backup = workspace / backup_relative
            backup.parent.mkdir(parents=True)
            backup.write_bytes(original)
            (workspace / "research.yml").write_text("interrupted replacement\n", encoding="utf-8")
            journal = workspace / "domain-packs/.evidence-wiki-transaction.yml"
            journal.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0",
                        "transaction_id": transaction_id,
                        "operation": "refresh",
                        "phase": "prepared",
                        "pack_target_relative": "domain-packs/general-science",
                        "entries": [
                            {
                                "path": "research.yml",
                                "existed": True,
                                "mode": original_mode,
                                "backup": backup_relative,
                                "backup_sha256": lifecycle.sha256_bytes(original),
                                "staged": None,
                                "staged_sha256": None,
                            }
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            validator_called = False

            def invalid_after_recovery(_candidate: Path) -> str:
                nonlocal validator_called
                validator_called = True
                self.assertEqual(original, (workspace / "research.yml").read_bytes())
                self.assertFalse(journal.exists())
                raise lifecycle.LifecycleFailure("DOMAIN_PACK_INVALID", "injected invalid pack")

            with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                lifecycle.run_refresh(
                    workspace,
                    candidate,
                    source_kind="path",
                    candidate_validator=invalid_after_recovery,
                )

            self.assertTrue(validator_called)
            self.assertEqual("DOMAIN_PACK_INVALID", caught.exception.error_code)
            self.assertEqual(original, (workspace / "research.yml").read_bytes())
            self.assertFalse(journal.exists())

    def test_recovery_authenticates_every_backup_before_restoring_any_destination(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            transaction_id = "tampered-backup"
            transaction_root = workspace / ".replaced/domain-packs" / transaction_id
            original_log = (workspace / "log.md").read_bytes()
            original_state = (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes()
            backup_log_relative = (
                f".replaced/domain-packs/{transaction_id}/backup/log.md"
            )
            backup_state_relative = (
                f".replaced/domain-packs/{transaction_id}/backup/"
                "domain-packs/.evidence-wiki-state.yml"
            )
            backup_log = workspace / backup_log_relative
            backup_state = workspace / backup_state_relative
            backup_log.parent.mkdir(parents=True)
            backup_state.parent.mkdir(parents=True)
            backup_log.write_bytes(b"tampered backup\n")
            backup_state.write_bytes(original_state)
            interrupted_log = b"interrupted log replacement\n"
            interrupted_state = b"interrupted state replacement\n"
            (workspace / "log.md").write_bytes(interrupted_log)
            (workspace / "domain-packs/.evidence-wiki-state.yml").write_bytes(interrupted_state)
            journal = workspace / "domain-packs/.evidence-wiki-transaction.yml"
            journal.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0",
                        "transaction_id": transaction_id,
                        "operation": "adopt",
                        "phase": "prepared",
                        "pack_target_relative": None,
                        # Reverse-order recovery would restore state before it
                        # encountered the deliberately corrupt log backup.
                        "entries": [
                            {
                                "path": "log.md",
                                "existed": True,
                                "mode": 0o600,
                                "backup": backup_log_relative,
                                "backup_sha256": lifecycle.sha256_bytes(original_log),
                                "staged": None,
                                "staged_sha256": None,
                            },
                            {
                                "path": "domain-packs/.evidence-wiki-state.yml",
                                "existed": True,
                                "mode": 0o600,
                                "backup": backup_state_relative,
                                "backup_sha256": lifecycle.sha256_bytes(original_state),
                                "staged": None,
                                "staged_sha256": None,
                            },
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                lifecycle.recover_transaction(workspace)

            self.assertEqual("DOMAIN_PACK_TRANSACTION_INCOMPLETE", caught.exception.error_code)
            self.assertEqual(interrupted_log, (workspace / "log.md").read_bytes())
            self.assertEqual(
                interrupted_state,
                (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes(),
            )
            self.assertTrue(journal.exists())
            self.assertTrue(transaction_root.exists())

    def test_apply_rejects_tampered_staged_content_and_rolls_back_cleanly(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            research_before = (workspace / "research.yml").read_bytes()
            state_before = (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes()
            original_read_artifact = lifecycle._read_transaction_artifact
            tampered = False

            def tamper_staged(path_root, relative, expected_sha256, *, label):
                nonlocal tampered
                if not tampered and label == "staged transaction content":
                    tampered = True
                    (path_root / relative.as_posix()).write_bytes(b"tampered staged content\n")
                return original_read_artifact(
                    path_root,
                    relative,
                    expected_sha256,
                    label=label,
                )

            with mock.patch.object(
                lifecycle,
                "_read_transaction_artifact",
                side_effect=tamper_staged,
            ):
                with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                    lifecycle.run_refresh(workspace, candidate, source_kind="path")

            self.assertTrue(tampered)
            self.assertEqual("DOMAIN_PACK_TRANSACTION_INCOMPLETE", caught.exception.error_code)
            self.assertEqual(research_before, (workspace / "research.yml").read_bytes())
            self.assertEqual(
                state_before,
                (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes(),
            )
            self.assertFalse(lifecycle._transaction_path(workspace).exists())

    def test_write_commands_preflight_missing_or_invalid_targets_without_lock_artifacts(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            candidate = self.revision(root)
            missing_refresh = root / "missing-refresh"
            with self.assertRaises(lifecycle.LifecycleFailure):
                lifecycle.run_refresh(missing_refresh, candidate, source_kind="path")
            self.assertFalse(missing_refresh.exists())

            invalid_refresh = root / "invalid-refresh"
            invalid_refresh.mkdir()
            (invalid_refresh / "marker.txt").write_text("preserve\n", encoding="utf-8")
            with self.assertRaises(lifecycle.LifecycleFailure):
                lifecycle.run_refresh(invalid_refresh, candidate, source_kind="path")
            self.assertFalse((invalid_refresh / ".locks").exists())

            missing_adopt = root / "missing-adopt"
            with self.assertRaises(lifecycle.LifecycleFailure):
                lifecycle.run_adopt(missing_adopt)
            self.assertFalse(missing_adopt.exists())

            invalid_adopt = root / "invalid-adopt"
            invalid_adopt.mkdir()
            with self.assertRaises(lifecycle.LifecycleFailure):
                lifecycle.run_adopt(invalid_adopt)
            self.assertFalse((invalid_adopt / ".locks").exists())

    def test_ordinary_write_failure_rolls_back_and_interruption_is_recovered_on_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            lifecycle = cli._packaged_script("_domain_pack_lifecycle")
            original_atomic = lifecycle._atomic_write
            research_before = (workspace / "research.yml").read_bytes()
            state_before = (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes()

            failed_once = False

            def ordinary_failure(path, content, *, mode=lifecycle.RESTRICTIVE_FILE_MODE):
                nonlocal failed_once
                if (
                    not failed_once
                    and path.resolve() == (workspace / "research.yml").resolve()
                    and lifecycle._transaction_path(workspace.resolve()).exists()
                ):
                    failed_once = True
                    raise OSError("injected config replacement failure")
                return original_atomic(path, content, mode=mode)

            with mock.patch.object(lifecycle, "_atomic_write", side_effect=ordinary_failure):
                with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                    lifecycle.run_refresh(workspace, candidate, source_kind="path")
            self.assertEqual("DOMAIN_PACK_WRITE_FAILED", caught.exception.error_code)
            self.assertEqual(research_before, (workspace / "research.yml").read_bytes())
            self.assertEqual(state_before, (workspace / "domain-packs/.evidence-wiki-state.yml").read_bytes())
            self.assertFalse(lifecycle._transaction_path(workspace).exists())

            def interrupted(path, content, *, mode=lifecycle.RESTRICTIVE_FILE_MODE):
                if (
                    path.resolve() == (workspace / "research.yml").resolve()
                    and lifecycle._transaction_path(workspace.resolve()).exists()
                ):
                    raise KeyboardInterrupt()
                return original_atomic(path, content, mode=mode)

            with mock.patch.object(lifecycle, "_atomic_write", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    lifecycle.run_refresh(workspace, candidate, source_kind="path")
            self.assertTrue(lifecycle._transaction_path(workspace).exists())
            interrupted_journal = yaml.safe_load(
                lifecycle._transaction_path(workspace).read_text(encoding="utf-8")
            )
            for entry in interrupted_journal["entries"]:
                if entry["backup"] is None:
                    self.assertIsNone(entry["backup_sha256"])
                else:
                    self.assertEqual(
                        entry["backup_sha256"],
                        lifecycle.sha256_bytes((workspace / entry["backup"]).read_bytes()),
                    )
                if entry["staged"] is None:
                    self.assertIsNone(entry["staged_sha256"])
                else:
                    self.assertEqual(
                        entry["staged_sha256"],
                        lifecycle.sha256_bytes((workspace / entry["staged"]).read_bytes()),
                    )
            self.assertEqual("transaction_incomplete", lifecycle.inspect_workspace(workspace)["state"])

            report = lifecycle.run_refresh(workspace, candidate, source_kind="path")
            self.assertEqual("applied", report["status"])
            self.assertTrue(any("Recovered interrupted transaction" in warning for warning in report["warnings"]))
            self.assertFalse(lifecycle._transaction_path(workspace).exists())
            self.assertEqual("0.2.0", self.config(workspace)["domain_pack"]["version"])

    def test_each_transaction_write_phase_rolls_back_cleanly_and_retry_converges(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")
        phases = ("staging", "pack replacement", "state replacement", "log update")

        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                workspace = self.init_workspace(root)
                candidate = self.revision(root)
                journal = lifecycle._transaction_path(workspace)
                installed_readme = workspace / "domain-packs/general-science/README.md"
                state_path = workspace / "domain-packs/.evidence-wiki-state.yml"
                log_path = workspace / "log.md"
                watched = (
                    workspace / "research.yml",
                    state_path,
                    installed_readme,
                    workspace / "domain-packs/general-science/research.overlay.yml",
                    log_path,
                )
                before = {path: path.read_bytes() for path in watched}
                original_atomic = lifecycle._atomic_write
                failed_once = False

                def is_failure_point(
                    path: Path,
                    *,
                    selected_phase: str = phase,
                    readme_target: Path = installed_readme,
                    state_target: Path = state_path,
                    log_target: Path = log_path,
                    journal_target: Path = journal,
                ) -> bool:
                    if selected_phase == "staging":
                        return (
                            "/staged/domain-packs/general-science/README.md"
                            in path.as_posix()
                        )
                    if selected_phase == "pack replacement":
                        return path.resolve() == readme_target.resolve() and journal_target.exists()
                    if selected_phase == "state replacement":
                        return path.resolve() == state_target.resolve() and journal_target.exists()
                    return (
                        path.resolve() == log_target.resolve()
                        and journal_target.exists()
                    )

                def injected_failure(
                    path: Path,
                    content: bytes,
                    *,
                    mode: int = lifecycle.RESTRICTIVE_FILE_MODE,
                    selected_phase: str = phase,
                    failure_point=is_failure_point,
                    atomic_write=original_atomic,
                ) -> None:
                    nonlocal failed_once
                    if not failed_once and failure_point(Path(path)):
                        failed_once = True
                        raise OSError(f"injected {selected_phase} failure")
                    atomic_write(path, content, mode=mode)

                with mock.patch.object(
                    lifecycle,
                    "_atomic_write",
                    side_effect=injected_failure,
                ):
                    with self.assertRaises(lifecycle.LifecycleFailure) as caught:
                        lifecycle.run_refresh(workspace, candidate, source_kind="path")

                self.assertTrue(failed_once, f"{phase} injection point was not reached")
                self.assertEqual("DOMAIN_PACK_WRITE_FAILED", caught.exception.error_code)
                self.assertFalse(journal.exists())
                self.assertEqual(before, {path: path.read_bytes() for path in watched})

                report = lifecycle.run_refresh(workspace, candidate, source_kind="path")
                self.assertEqual("applied", report["status"])
                self.assertEqual("0.2.0", self.config(workspace)["domain_pack"]["version"])
                self.assertIn("Candidate revision marker.", installed_readme.read_text(encoding="utf-8"))
                self.assertFalse(journal.exists())

    def test_refresh_preserves_yaml_quotes_order_comments_and_optional_footer_exactly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.init_workspace(root)
            candidate = self.revision(root)
            research = workspace / "research.yml"
            original = research.read_text(encoding="utf-8")
            original_project = (
                "project:\n"
                "  name: lifecycle-project\n"
                "  description: Workspace for domain-pack lifecycle regression tests.\n"
                "  owner_goal: Prove safe pack reconciliation.\n"
                "  language: en\n"
            )
            personalized_project = (
                "project:\n"
                '  language: "en" # operator-chosen quoting\n'
                "  # arbitrary project annotation\n"
                "  owner_goal: Prove safe pack reconciliation.\n"
                "  name: 'lifecycle-project'\n"
                "  description: Workspace for domain-pack lifecycle regression tests.\n"
            )
            self.assertIn(original_project, original)
            configured = original.replace(original_project, personalized_project, 1)
            configured = configured.replace(
                "  version: 0.1.0\n",
                '  version: "0.1.0" # retain this pack-field note\n',
                1,
            )
            self.assertIn(
                '  version: "0.1.0" # retain this pack-field note\n',
                configured,
            )
            configured = configured.replace(
                "sources:\n",
                "# arbitrary operator boundary comment\nsources:\n",
                1,
            )
            research.write_text(configured, encoding="utf-8")

            footer_marker = "# Optional sections, shown commented out. None of them are active in this\n"
            self.assertIn(footer_marker, configured)
            footer_before = configured[configured.index(footer_marker) :]

            code, stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(candidate),
                "--format",
                "json",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("applied", json.loads(stdout)["status"])
            refreshed = research.read_text(encoding="utf-8")
            self.assertIn(personalized_project, refreshed)
            self.assertIn("# arbitrary operator boundary comment\nsources:\n", refreshed)
            self.assertIn(
                '  version: "0.2.0" # retain this pack-field note\n',
                refreshed,
            )
            self.assertEqual(footer_before, refreshed[refreshed.index(footer_marker) :])
            project_section = refreshed.split("project:\n", 1)[1].split("\ndomain_pack:\n", 1)[0]
            self.assertLess(project_section.index("  language:"), project_section.index("  owner_goal:"))
            self.assertLess(project_section.index("  owner_goal:"), project_section.index("  name:"))
            self.assertLess(project_section.index("  name:"), project_section.index("  description:"))

    def test_refresh_accepts_an_intentional_pack_version_downgrade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            high = self.revision(root / "high", version="9.0.0", description="High revision.")
            workspace = self.init_workspace(root, pack=str(high))
            low = self.revision(root / "low", version="0.0.1", description="Intentional downgrade.")

            code, stdout, stderr = self.run_cli(
                "pack",
                "refresh",
                "--target",
                str(workspace),
                "--path",
                str(low),
                "--format",
                "json",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("applied", json.loads(stdout)["status"])
            self.assertEqual("0.0.1", self.config(workspace)["domain_pack"]["version"])
            self.assertEqual(
                "0.0.1",
                self.lifecycle_state(workspace)["pack"]["installed_version"],
            )
            self.assertEqual("current", cli._packaged_script("_domain_pack_lifecycle").inspect_workspace(workspace)["state"])

    def test_locally_edited_retired_file_supports_both_resolutions_and_valid_state_reload(self):
        lifecycle = cli._packaged_script("_domain_pack_lifecycle")

        for resolution, keep_file in (("--keep-local", True), ("--accept-pack", False)):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                workspace = self.init_workspace(root)
                candidate = self.revision(root)
                (candidate / "README.md").unlink()
                installed_readme = workspace / "domain-packs/general-science/README.md"
                installed_readme.write_text("operator-owned retired file\n", encoding="utf-8")

                conflict, conflict_stdout, conflict_stderr = self.run_cli(
                    "pack",
                    "refresh",
                    "--target",
                    str(workspace),
                    "--path",
                    str(candidate),
                    "--dry-run",
                    "--format",
                    "json",
                )
                self.assertEqual(3, conflict, conflict_stderr)
                self.assertIn(
                    "file:README.md",
                    [item["target"] for item in json.loads(conflict_stdout)["conflicts"]],
                )

                code, stdout, stderr = self.run_cli(
                    "pack",
                    "refresh",
                    "--target",
                    str(workspace),
                    "--path",
                    str(candidate),
                    resolution,
                    "file:README.md",
                    "--format",
                    "json",
                )
                self.assertEqual(0, code, stderr)
                self.assertEqual("applied", json.loads(stdout)["status"])

                reloaded = lifecycle.load_state(workspace)
                self.assertIsNotNone(reloaded)
                self.assertNotIn(
                    "README.md",
                    {entry["path"] for entry in reloaded["managed_files"]},
                )
                self.assertNotIn(
                    "README.md",
                    {entry["path"] for entry in reloaded["revision_files"]},
                )
                self.assertNotIn("file:README.md", reloaded["local_overrides"])
                self.assertEqual("current", lifecycle.inspect_workspace(workspace)["state"])

                if keep_file:
                    self.assertEqual(
                        "operator-owned retired file\n",
                        installed_readme.read_text(encoding="utf-8"),
                    )
                else:
                    self.assertFalse(installed_readme.exists())
                    backups = list(
                        (workspace / ".replaced/domain-packs").glob(
                            "*/backup/domain-packs/general-science/README.md"
                        )
                    )
                    self.assertEqual(1, len(backups))
                    self.assertEqual(
                        "operator-owned retired file\n",
                        backups[0].read_text(encoding="utf-8"),
                    )

                retry, retry_stdout, retry_stderr = self.run_cli(
                    "pack",
                    "refresh",
                    "--target",
                    str(workspace),
                    "--path",
                    str(candidate),
                    "--format",
                    "json",
                )
                self.assertEqual(0, retry, retry_stderr)
                self.assertEqual("no_changes", json.loads(retry_stdout)["status"])


if __name__ == "__main__":
    unittest.main()
