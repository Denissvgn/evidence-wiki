"""CR-19, U1c: the codebase-record blast radius, established by running it.

CR-19 records that a **directory-shaped** ``raw_paths`` entry cannot be delivered inside
an acquisition order: ``raw_tree_snapshot`` records one entry per regular *file*, while
each arm's ``allowed_new_raw_paths`` builder adds the literal ``raw_paths`` string with no
prefix expansion, so the guard admits zero of what the record declares and every member
file lands in ``unexpected_new_raw_paths``. That was reproduced for arXiv ``--format
source`` bundles. The backlog then listed ``build_local_codebase_record`` -- which emits a
directory ``raw_paths`` for a local repository under ``raw/code/<repo>/`` -- as the same
shape, explicitly marked **"read, not run"**.

This file runs it. What execution established, none of which was inferable from reading:

1. **Reachability is gated on more than one flag.** ``codebase_analysis_enabled(config)``
   must be true, but turning it on alone makes the workspace *unshippable*: smoke
   validation raises two HIGH issues -- an enabled integration with no ``provider``, and a
   missing ``sources/code_wikis`` -- and ``choose_route`` sends the session straight to
   ``no_ship`` before any acquisition order is ever issued. A codebase record cannot be
   delivered into an order in a workspace that merely flipped ``enabled``.
   (``test_enabling_codebase_analysis_alone_never_reaches_an_acquisition_order``)

2. **Inventory does produce the directory shape.** One record, kind
   ``codebase_architecture``, ``raw_paths == ["raw/code/<repo>"]``, no member file named
   anywhere in the record. (``test_inventory_records_a_local_repository_as_one_directory``)

3. **CR-19 is NOT the first refusal on the naive path.** A local repository with no
   external-worker artifact normalizes to ``status: stubbed``, and the delegated
   postcondition's usable-evidence guard refuses that several checks *before* the raw-scope
   guard is consulted. Fixing CR-19 alone therefore does not make the plain local-repo flow
   work. (``test_a_codebase_record_without_a_worker_artifact_is_refused_before_raw_scope``)

4. **Past that guard, CR-19 reproduces exactly.** With a validated external-worker artifact
   deposited under ``sources/code_wikis/<id>/`` the record normalizes to
   ``codebase_context`` / ``content_extracted``, and submit then refuses with
   ``delegated acquisition changed raw evidence outside newly fulfilled manifest source
   scope``, naming every file in the repository. So the backlog's read-only claim is
   confirmed -- but only for the configuration this file builds, which is a narrower
   reachability story than the backlog implied.
   (``test_a_local_codebase_repository_delivered_inside_an_order_can_fulfil_it``, RED)

5. **The record's own bound was measured over a different tree than the one that gets
   refused.** ``codebase_intake.bounded`` was decided by `local_repo_file_count`, which
   filtered members through `should_skip` and so counted no dot-prefixed path, while
   `raw_tree_snapshot` fingerprints every regular file and refuses past
   ``MAX_RAW_TREE_SNAPSHOT_ENTRIES``. Two 10,000 caps over two different sets: a checkout
   whose ``.git`` carried the difference was stamped ``bounded: true`` and then refused as
   unbounded. Fixed by counting the tree the snapshot walks; the ``.git`` pair in the
   fixture is what makes the two counts diverge at a scale of five files. The caps are still
   not aggregated the same way -- the snapshot totals across all raw roots, inventory counts
   one repository -- so ``bounded`` stays a per-record statement.
   (`CodebaseIntakeBoundTests`)

The RED test asserts the success CR-19 owes. It was verified to be genuinely red rather
than red-for-another-reason: against a scratch copy of ``orchestration_controller.py``
whose delegated builder prefix-expands a directory entry, the identical walk returns exit 0
and routes back to ``research``. Nothing downstream of the raw guard refuses this delivery.

Only the delegated arm is exercised. The provider arm has no route to acquisition in this
harness (see ``ClosedGateTests`` in ``test_delegated_acquisition_e2e``), and a local code
repository is not a shape any acquisition provider produces; the provider builder is
byte-identical to the delegated one, but that is a reading, not a run, and is deliberately
not asserted here.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    ACQUIRER,
    CONTROLLER,
    INVENTORY,
    NORMALIZE,
    DelegatedWorkspace,
    load_script_module,
)

SMOKE = load_script_module("e2e_codebase_smoke", "smoke_validate_workspace.py")

CODEBASE_PROVIDER = "agent-wiki-cli"
REPO_NAME = "solid-electrolyte-sim"
REPO_RELATIVE = f"raw/code/{REPO_NAME}"

# Five regular files under the repository directory, one of them nested, plus the
# ``pyproject.toml`` marker that makes `iter_local_code_repos` recognise the tree at all
# (`CODEBASE_LOCAL_REPO_MARKERS`). The nesting matters: the raw snapshot walks recursively,
# so a member two levels down is exactly the entry a naive one-level fix would still miss.
#
# The two ``.git`` members matter for a second reason. Dot-prefixed paths are the subset
# `should_skip` withholds from record *selection*, and a checkout delivered by any real
# acquirer carries them -- ``.git`` is itself one of the markers that makes the tree a
# repository at all. The raw snapshot fingerprints them like any other regular file, so
# they are the part of the tree the two counts used to disagree about (see
# `CodebaseIntakeBoundTests`). The object body is a placeholder: nothing reads these bytes,
# only their existence as regular files under the declared directory is load-bearing.
REPO_FILES = {
    "pyproject.toml": '[project]\nname = "solid-electrolyte-sim"\nversion = "0.1.0"\n',
    "README.md": "# solid-electrolyte-sim\n\nConductivity model for sulfide electrolytes.\n",
    "src/model.py": "CONDUCTIVITY_MS_CM = 1.4\n",
    ".git/HEAD": "ref: refs/heads/main\n",
    ".git/objects/9a/1f0c7d4b2e6f8a0c3d5e7f9b1d3f5a7c9e1b3d5f": "loose object placeholder\n",
}
REPO_MEMBER_PATHS = sorted(f"{REPO_RELATIVE}/{name}" for name in REPO_FILES)
# The members `should_skip` withholds from record selection: what the old bound left out.
REPO_DOT_MEMBER_PATHS = sorted(
    f"{REPO_RELATIVE}/{name}" for name in REPO_FILES if name.startswith(".")
)

# The one artifact file a separately authorized external worker is allowed to deposit.
# `normalize_codebase_record` reads it as data; nothing in the repository is executed.
ARTIFACT_CONTEXT = (
    json.dumps(
        {
            "summary": "Architecture context for the solid-electrolyte conductivity simulator.",
            "components": ["ingest boundary", "conductivity model", "report writer"],
            "observations": [
                "The product reads this file as data and never executes repository content.",
            ],
        },
        indent=2,
    )
    + "\n"
)


class CodebaseWorkspace(DelegatedWorkspace):
    """Delegated scaffolding plus the codebase-analysis integration turned on properly.

    Deliberately not a TestCase, for the reason `DelegatedWorkspace` gives: subclassing one
    that carries tests would re-run the whole parent suite under every child class.
    """

    def enable_codebase_analysis(
        self, workspace: Path, *, provider: str | None = CODEBASE_PROVIDER
    ) -> None:
        """What an operator writes by hand to make local repositories inventoriable.

        Three separate edits, and the reachability tests below show why all three are
        required: ``enabled`` is what `build_records` consults, ``raw/code`` must be a
        declared source root before `configured_codebase_source_roots` will look in it
        (the init profile this suite uses declares only ``raw/data``, ``raw/links`` and
        ``raw/papers``), and smoke validation refuses an enabled integration that names no
        provider or whose configured output directory does not exist.
        """
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/code"})
        codebase = config.setdefault("integrations", {}).setdefault("codebase_analysis", {})
        codebase["enabled"] = True
        if provider is not None:
            codebase["provider"] = provider
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        (workspace / "raw" / "code").mkdir(parents=True, exist_ok=True)
        if provider is not None:
            (workspace / codebase["output_dir"]).mkdir(parents=True, exist_ok=True)

    def deliver_local_repository(self, workspace: Path, request_id: str) -> None:
        """The acquirer's delivery: a repository tree and one sidecar naming the request.

        The sidecar sits *beside* the directory (``raw/code/<repo>.provenance.yml``), which
        is where `provenance_candidate_paths` looks for it, and carries no checksum:
        `source_inventory.py` cannot hash a tree.
        """
        repo = workspace / REPO_RELATIVE
        for name, body in REPO_FILES.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8", newline="\n")
        (workspace / f"{REPO_RELATIVE}.provenance.yml").write_text(
            yaml.safe_dump(
                {
                    "origin_url": f"https://example.test/{REPO_NAME}",
                    "license": "MIT",
                    "retrieved_at": "2026-08-17T12:00:00Z",
                    "retrieved_by": ACQUIRER,
                    "request_id": request_id,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def deposit_worker_artifact(self, workspace: Path, record: dict) -> None:
        """The inert artifact bundle a codebase record needs to be usable evidence.

        Without it the record normalizes to ``codebase_stub`` / ``status: stubbed``, which
        the delegated postcondition refuses long before the raw-scope guard runs -- see
        ``test_a_codebase_record_without_a_worker_artifact_is_refused_before_raw_scope``.
        The manifest shape is what `codebase_manifest_errors` demands: schema version, kind,
        the record's own source id, a named producer, an invocation that disclaims plugins,
        hooks and network, and a size/checksum for every deposited file.
        """
        source_id = str(record["id"])
        artifact_dir = workspace / record["metadata"]["codebase_output_dir"]
        artifact_dir.mkdir(parents=True, exist_ok=True)
        context = artifact_dir / "context.json"
        context.write_text(ARTIFACT_CONTEXT, encoding="utf-8", newline="\n")
        payload = context.read_bytes()
        (artifact_dir / "artifact-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "artifact_kind": "codebase_evidence",
                    "source_id": source_id,
                    "generated_at": "2026-08-17T13:00:00Z",
                    "producer": {"name": "synthetic-authorized-worker", "version": "1.0"},
                    "invocation": {
                        "argv": ["external-analyzer", "analyze", "--input", REPO_RELATIVE],
                        "executed_by": "external_worker",
                        "plugins_enabled": False,
                        "hooks_enabled": False,
                        "network_access": False,
                    },
                    "files": [
                        {
                            "path": "context.json",
                            "size_bytes": len(payload),
                            "sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def manifest_records(self, workspace: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in (workspace / "sources" / "manifest.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    def normalized_status(self, workspace: Path, source_id: str) -> dict[str, object]:
        """The frontmatter fields the usable-evidence guard reads back."""
        text = self.normalized_record_for(workspace, source_id).read_text(encoding="utf-8")
        lines = text.replace("\r\n", "\n").split("\n")
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        frontmatter = yaml.safe_load("\n".join(lines[1:closing])) or {}
        return {
            key: frontmatter.get(key)
            for key in ("status", "evidence_usable", "extraction_method")
        }

    def normalized_raw_paths(self, workspace: Path, source_id: str) -> list[str]:
        text = self.normalized_record_for(workspace, source_id).read_text(encoding="utf-8")
        lines = text.replace("\r\n", "\n").split("\n")
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        frontmatter = yaml.safe_load("\n".join(lines[1:closing])) or {}
        return list(frontmatter.get("raw_paths") or [])

    def arrive_at_a_codebase_delivery(
        self, root: Path, *, deposit_artifact: bool
    ) -> tuple[Path, str, dict, dict]:
        """Walk to a pending delegated order and deliver a local repository inside it.

        Returns the workspace, the fulfilled source id, the pending order, and the record
        inventory wrote, so each test can assert on whichever of those it is about.
        """
        workspace, request_id = self.make_workspace(root)
        self.enable_codebase_analysis(workspace)
        self.start(workspace)
        order = self.pending_order(workspace)
        self.assertEqual([request_id], order["scope"]["request_ids"], order)

        self.deliver_local_repository(workspace, request_id)
        self.run_script(INVENTORY, ["--report"], workspace)
        records = self.manifest_records(workspace)
        self.assertEqual(1, len(records), records)
        record = records[0]
        source_id = str(record["id"])
        if deposit_artifact:
            self.deposit_worker_artifact(workspace, record)
        self.run_script(NORMALIZE, ["--source-id", source_id], workspace)
        self.fulfil_and_reopen(workspace, request_id, source_id)
        return workspace, source_id, order, record


class CodebaseReachabilityTests(CodebaseWorkspace, unittest.TestCase):
    """Whether a codebase record can be delivered into an order at all.

    CR-19 scoped this kind from a reading of `build_local_codebase_record` alone. Reaching
    it takes three configuration edits and one deposited artifact, and getting any of them
    wrong refuses the flow somewhere other than CR-19 -- which is the difference between
    "the defect covers this kind" and "an operator following the docs hits the defect".
    """

    def test_enabling_codebase_analysis_alone_never_reaches_an_acquisition_order(self):
        """Flipping ``enabled`` makes the workspace unshippable, not codebase-capable.

        `smoke_validate_workspace` raises two HIGH issues for a half-configured
        integration, `readiness_section` turns that into ``attention_required``, and
        `choose_route` refuses to issue *any* order -- the session terminates ``no_ship``
        with no pending action at all. Pinned because it is the state a reader who tried
        this defect from the backlog description would land in, and its refusal names
        nothing about raw paths, directories or acquisition scope.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, _ = self.make_workspace(Path(tmpdir))
            self.enable_codebase_analysis(workspace, provider=None)

            smoke = SMOKE.run_checks(workspace)
            self.assertFalse(smoke["ok"], smoke)
            self.assertEqual(
                [
                    "enabled codebase analysis must name a provider",
                    "Missing directory: sources/code_wikis",
                ],
                [issue["message"] for issue in smoke["issues"]],
                smoke,
            )
            self.assertEqual(
                ["HIGH", "HIGH"], [issue["severity"] for issue in smoke["issues"]], smoke
            )

            self.start(workspace)
            code, session = self.next_action(workspace)

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, session)
            self.assertEqual("no_ship", session["phase"], session)
            self.assertEqual(
                "Workspace health or HIGH validation findings require operator attention.",
                session["pause_reason"],
                session,
            )
            self.assertIsNone(session["pending_action_id"], session)

    def test_inventory_records_a_local_repository_as_one_directory(self):
        """The attribution fact the whole CR-19 fix keys on, measured rather than read.

        One record for the tree, ``raw_paths`` holding the *directory*, and not one member
        file named anywhere in the record -- while all five are on disk. That gap is the
        defect in one assertion: the raw snapshot enumerates the members, the record
        declares only their parent, and the guard subtracts one set from the other.

        ``file_count`` is asserted against every regular file the fixture wrote, the
        ``.git`` pair included. That equality is what `CodebaseIntakeBoundTests` explains:
        the count is the measurement the record's ``bounded`` promise is made from, so it
        has to cover the same tree the snapshot will walk.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.enable_codebase_analysis(workspace)
            self.deliver_local_repository(workspace, request_id)

            report = self.run_script(INVENTORY, ["--report"], workspace)
            self.assertEqual([], report["warnings"], report)
            records = self.manifest_records(workspace)

            self.assertEqual(1, len(records), records)
            record = records[0]
            self.assertEqual("codebase_architecture", record["kind"], record)
            self.assertEqual([REPO_RELATIVE], record["raw_paths"], record)
            self.assertEqual("local_repo", record["metadata"]["codebase_source_type"], record)
            self.assertEqual(len(REPO_FILES), record["metadata"]["file_count"], record)
            self.assertEqual(
                request_id, record["provenance"]["request_id"], record
            )

            on_disk = sorted(
                path.relative_to(workspace).as_posix()
                for path in (workspace / REPO_RELATIVE).rglob("*")
                if path.is_file()
            )
            self.assertEqual(REPO_MEMBER_PATHS, on_disk)
            serialized = json.dumps(record, sort_keys=True)
            self.assertEqual(
                [],
                [member for member in REPO_MEMBER_PATHS if member in serialized],
                "the record must name no member file: the members the guard sees are "
                "reachable only by walking the directory it declares",
            )


class CodebaseIntakeBoundTests(CodebaseWorkspace, unittest.TestCase):
    """What ``codebase_intake.bounded`` promises, measured against whoever consumes it.

    A local repository record carries ``codebase_intake.bounded``, and the consumer of that
    promise is the controller's raw tree snapshot: it fingerprints one entry per regular
    file beneath every configured raw root and refuses the whole workspace with
    ``ORCHESTRATION_WORKSPACE_UNSAFE`` past ``MAX_RAW_TREE_SNAPSHOT_ENTRIES``. Inventory's
    own cap, ``CODEBASE_MAX_LOCAL_REPO_FILES``, is the same number -- 10,000 either side.

    Two identical caps over two different sets is not a bound. `local_repo_file_count` used
    to filter members through `should_skip`, which withholds every dot-prefixed path, so a
    checkout whose ``.git`` carried the difference was stamped ``bounded: true`` on a subset
    and then refused as unbounded on the whole, the refusal naming a tree the record had
    already declared admissible. ``.git`` is one of the markers that makes the tree a
    repository in the first place, so the excluded subset was not an exotic case: it was
    every checkout an acquirer clones.

    What is pinned here is that both sides count the same *set* for one repository, which is
    all the per-record flag can carry. The snapshot's cap totals across every configured raw
    root while inventory's is per repository, so two bounded checkouts can still add up past
    it -- that gap is a workspace-wide accounting question and is deliberately not asserted
    here. Both tests measure against the snapshot's own enumeration rather than against a
    restatement of its rule, because a restatement is exactly what drifted.
    """

    def test_a_symlinked_member_does_not_move_the_bound(self):
        """`is_file()` resolves symlinks; the snapshot refuses them. The bound follows the snapshot.

        Aligning the *subset* was half the job. `is_file()` also answers True for a symlink
        to a file, while the snapshot refuses one outright as "contains a symbolic link or
        junction" -- so counting it would restore the same mismatch this class exists to
        close, with the excluded set merely moved to the other side.

        Only the configured source root and the repository root are symlink-checked during
        discovery, so an entry *inside* a checkout reaches this count unfiltered.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.enable_codebase_analysis(workspace)
            self.deliver_local_repository(workspace, request_id)
            repo = workspace / REPO_RELATIVE

            baseline = INVENTORY.local_repo_file_count(repo)
            target = next(p for p in sorted(repo.rglob("*")) if p.is_file())
            (repo / "linked.py").symlink_to(target)

            self.assertEqual(
                baseline,
                INVENTORY.local_repo_file_count(repo),
                "a symlink is not a file the snapshot will enumerate, so it is not evidence "
                "this record admits and must not move its bound",
            )

    def test_a_multiply_linked_member_is_excluded_because_the_snapshot_refuses_it(self):
        """A hardlink drops *both* copies from the bound, and the snapshot refuses the tree.

        The snapshot admits only a "singly linked regular file", so a hardlink disqualifies
        the original as much as the new name -- neither is a file it will enumerate. The
        count therefore falls by one rather than rising by one, which looks wrong until you
        see the other half asserted here: for that same tree the snapshot raises rather than
        returning entries at all. Excluding them is what keeps the bound a statement about
        the set the snapshot walks.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.enable_codebase_analysis(workspace)
            self.deliver_local_repository(workspace, request_id)
            repo = workspace / REPO_RELATIVE

            baseline = INVENTORY.local_repo_file_count(repo)
            target = next(p for p in sorted(repo.rglob("*")) if p.is_file())
            try:
                os.link(target, repo / "hardlinked.py")
            except OSError as exc:  # pragma: no cover - platform without hardlink support
                self.skipTest(f"hardlinks are unavailable on this platform: {exc}")

            self.assertEqual(
                baseline - 1,
                INVENTORY.local_repo_file_count(repo),
                "both names of a hardlinked file must leave the bound: the snapshot admits "
                "only a singly linked regular file",
            )
            with self.assertRaises(Exception) as caught:
                CONTROLLER.raw_tree_snapshot(
                    workspace, CONTROLLER.load_config(workspace), include_entries=True
                )
            self.assertIn(
                "singly linked regular file",
                str(getattr(caught.exception, "message", caught.exception)),
                "the exclusion has to track a real refusal, or it is just a different subset",
            )

    def test_the_bound_is_measured_over_the_tree_the_raw_snapshot_walks(self):
        """The record's file count equals the snapshot's, over the directory it declares.

        Both sides are run rather than modelled: inventory builds the record, and the
        controller's own `raw_tree_snapshot` enumerates ``raw/`` with entries requested, out
        of which the members of the declared directory are selected. Before the fix the two
        differed by the two ``.git`` members -- the whole defect, at a scale that does not
        need ten thousand files to show.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.enable_codebase_analysis(workspace)
            self.deliver_local_repository(workspace, request_id)

            report = self.run_script(INVENTORY, ["--report"], workspace)
            self.assertEqual([], report["warnings"], report)
            records = self.manifest_records(workspace)
            self.assertEqual(1, len(records), records)
            record = records[0]
            snapshot = CONTROLLER.raw_tree_snapshot(
                workspace, CONTROLLER.load_config(workspace), include_entries=True
            )
            members = sorted(
                path for path in snapshot["entries"] if path.startswith(f"{REPO_RELATIVE}/")
            )

            self.assertEqual(REPO_MEMBER_PATHS, members, snapshot)
            self.assertEqual(
                REPO_DOT_MEMBER_PATHS,
                [path for path in members if path.startswith(f"{REPO_RELATIVE}/.")],
                "the snapshot has no skip predicate: dot-prefixed members are fingerprinted "
                "like any other regular file, which is why the bound has to count them",
            )
            self.assertEqual(
                len(members),
                record["metadata"]["file_count"],
                "the count the bounded promise is made from must cover the same tree the "
                "snapshot will walk, or bounded: true can be followed by an unbounded refusal",
            )
            self.assertEqual(
                len(members), record["metadata"]["codebase_intake"]["file_count"], record
            )
            self.assertTrue(record["metadata"]["codebase_intake"]["bounded"], record)
            self.assertEqual(
                INVENTORY.CODEBASE_MAX_LOCAL_REPO_FILES,
                record["metadata"]["codebase_intake"]["file_limit"],
                record,
            )
            self.assertIn(
                ".git",
                record["metadata"]["markers"],
                "the directory that qualifies the tree as a repository is the one the old "
                "count excluded from measuring it",
            )

    def test_a_dot_directory_alone_can_push_a_repository_over_the_bound(self):
        """Counting the dot members is only half of it: they have to decide the bound too.

        Counting them and then deciding on something else would be the same defect wearing a
        report field. With the limit lowered to the number of non-dot members, this
        repository must come back ``bounded: false`` and flagged for review, because the tree
        the snapshot will walk is over the limit even though the part inventory would record
        as sources is not. The limit is patched rather than the fixture grown to ten thousand
        files: the accounting rule is what is under test, not the constant.

        ``file_count`` is asserted as *over* the limit rather than equal to the member total
        because `local_repo_file_count` stops walking one past its limit -- an oversize tree
        is refused without enumerating all of it, and the exact stopping point is not a
        promise worth pinning.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.enable_codebase_analysis(workspace)
            self.deliver_local_repository(workspace, request_id)
            visible_members = len(REPO_MEMBER_PATHS) - len(REPO_DOT_MEMBER_PATHS)

            with mock.patch.object(
                INVENTORY, "CODEBASE_MAX_LOCAL_REPO_FILES", visible_members
            ):
                self.run_script(INVENTORY, ["--report"], workspace)

            records = self.manifest_records(workspace)
            self.assertEqual(1, len(records), records)
            record = records[0]
            self.assertGreater(record["metadata"]["file_count"], visible_members, record)
            self.assertFalse(record["metadata"]["codebase_intake"]["bounded"], record)
            self.assertTrue(record["metadata"]["review_required"], record)
            self.assertIn(
                "exceeding the bounded intake limit",
                record["metadata"]["warnings"][0],
                record,
            )


class CodebaseDirectoryAcquisitionTests(CodebaseWorkspace, unittest.TestCase):
    """A local code repository delivered inside a pending delegated acquisition order."""

    def test_a_codebase_record_without_a_worker_artifact_is_refused_before_raw_scope(self):
        """The naive local-repo delivery never reaches CR-19's guard, and this pins why.

        `normalize_codebase_record` has no artifact to read, so it writes a
        ``codebase_stub`` record, and `status_for` makes that ``stubbed`` -- which
        `normalized_source_quality_failure` refuses outright. The refusal fires several
        checks *ahead* of the raw-scope guard, so the CR-19 fix on its own would leave this
        exact walk still refusing, with a message that has nothing to do with directories.
        A backlog entry that says "codebase records hit CR-19" without this caveat sends
        the next reader to the wrong guard.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, source_id, order, _ = self.arrive_at_a_codebase_delivery(
                Path(tmpdir), deposit_artifact=False
            )
            self.assertEqual(
                {
                    "status": "stubbed",
                    "evidence_usable": True,
                    "extraction_method": "codebase_stub",
                },
                self.normalized_status(workspace, source_id),
            )
            self.assertEqual(
                [REPO_RELATIVE], self.normalized_raw_paths(workspace, source_id)
            )

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
            self.assertEqual(
                "fulfilled source requests do not have usable normalized evidence",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                [
                    {
                        "source_id": source_id,
                        "reason": "normalized evidence has unusable extraction status 'stubbed'",
                    }
                ],
                envelope["details"]["quality_failures"],
                envelope,
            )
            self.assertNotIn("unexpected_new_raw_paths", envelope["details"], envelope)

    def test_a_local_codebase_repository_delivered_inside_an_order_can_fulfil_it(self):
        """RED (CR-19): a directory-shaped ``raw_paths`` entry cannot be delivered in-order.

        Everything the order asked for is done and correct: one repository delivered under
        a declared raw source root, one manifest record inventory built for it, a validated
        external-worker artifact, a ``content_extracted`` normalized record, the scoped
        request fulfilled and the question reopened. Submit nevertheless refuses, naming
        every file in the repository as raw evidence outside scope -- because the record
        declares the directory and the raw snapshot enumerates the files, and the guard
        subtracts one from the other.

        The remediation is unactionable for the same reason it is on the arXiv arm: the
        files it says to remove *are* the fulfilled source. Verified to be red for CR-19's
        reason alone -- against a scratch build whose delegated builder prefix-expands a
        directory entry, this identical walk returns exit 0 and routes back to ``research``.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, source_id, order, _ = self.arrive_at_a_codebase_delivery(
                Path(tmpdir), deposit_artifact=True
            )
            self.assertEqual(
                {
                    "status": "content_extracted",
                    "evidence_usable": True,
                    "extraction_method": "codebase_context",
                },
                self.normalized_status(workspace, source_id),
                "the fixture must clear the usable-evidence guard, or this test would be "
                "measuring that guard instead of the raw-scope one",
            )
            self.assertIn(
                REPO_RELATIVE,
                self.normalized_raw_paths(workspace, source_id),
                "the normalized record must still name the directory as its raw input",
            )

            code, envelope = self.submit(workspace, order["action_id"])

            details = envelope.get("details", {}) if isinstance(envelope, dict) else {}
            self.assertEqual(
                0,
                code,
                "a local code repository delivered inside its own acquisition order must "
                "be admitted: every file named here is a member of the directory the "
                "fulfilled record declares.\n"
                f"  message: {envelope.get('message')!r}\n"
                f"  unexpected_new_raw_paths: {details.get('unexpected_new_raw_paths')}\n"
                f"  allowed_new_raw_paths: {details.get('allowed_new_raw_paths')}\n"
                f"  raw_scope_violations: {details.get('raw_scope_violations')}",
            )
            self.assertEqual("research", envelope["phase"], envelope)
            self.assertEqual(order["action_id"], envelope["last_completed_action_id"], envelope)
            self.assertEqual("open", self.question_status(workspace))


if __name__ == "__main__":
    unittest.main()
