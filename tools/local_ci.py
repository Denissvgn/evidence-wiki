#!/usr/bin/env python3
"""Run clean local CI profiles, then seal and publish their exact results."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools._local_ci_contract import (  # noqa: E402
    GROUP_SIZE,
    GROUP_TIMEOUT,
    LINUX_NATIVE_TARGETS,
    PYTHON_PATCHES,
    REQUIRED_LOCAL_PROFILES,
    SCHEMA,
    SIGNING_NAMESPACE,
    architecture,
    canonical,
    checked_receipt,
    checked_runtime,
    digest,
    policy,
    runtime_identity,
    suite_result,
)
from tools.run_test_groups import source_identity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def executable(name):
    selected = shutil.which(name)
    if selected is None:
        raise ValueError("required executable is unavailable: " + name)
    return str(Path(selected).resolve())


def command(argv, *, cwd, log=None, env=None, timeout=None):
    """Host-owned return codes, not candidate stdout, determine stage completion."""
    selected = dict(os.environ if env is None else env)
    first = Path(argv[0])
    program = str(first) if first.is_absolute() else str((Path(cwd) / first).absolute()) if len(first.parts) > 1 else executable(argv[0])
    argv = [program, *argv[1:]]
    if log is None:
        return subprocess.run(argv, cwd=cwd, env=selected, check=True, timeout=timeout)  # noqa: S603 -- explicit local owner argv, no shell.
    with Path(log).open("wb") as output:
        return subprocess.run(argv, cwd=cwd, env=selected, stdout=output, stderr=subprocess.STDOUT,  # noqa: S603 -- explicit local owner argv, no shell.
                              check=True, timeout=timeout)


def git(root, *args):
    return subprocess.check_output([executable("git"), "-C", str(root), *args], text=True, encoding="utf-8").strip()  # noqa: S603 -- fixed Git operation argv.


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tracked_source_identity(root):
    """Read committed blobs only; editor backups and local secrets are never inputs."""
    names = subprocess.check_output([executable("git"), "-C", str(root), "ls-tree", "-rz", "--name-only", "HEAD"])  # noqa: S603 -- committed names only.
    prefixes = ("src/evidence_wiki/", "workspace-template/", "domain-packs/", "tests/", "tools/", ".github/")
    result = {}
    for raw in names.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8")
        if name.startswith(prefixes) or name in {"pyproject.toml", "README.md", "CHANGELOG.md"}:
            data = subprocess.check_output([executable("git"), "-C", str(root), "show", "HEAD:" + name])  # noqa: S603 -- exact committed blob argument.
            result[name] = hashlib.sha256(data).hexdigest()
    return result


def audit_profile(directory, *, commit, tree, source):
    """Reconcile original records before a host key is allowed to sign a summary."""
    row = read_json(directory / "profile.json")
    if row.get("commit") != commit or row.get("tree") != tree or row.get("status") != "passed":
        raise ValueError("profile failed or belongs to another candidate")
    runtime = checked_runtime(row["runtime"], system=profile_runtime(row["profile"])[0], python=profile_runtime(row["profile"])[1])
    compatibility = read_json(directory / "compatibility/summary.json")
    from tools.check_ci_compatibility import TARGETS, require_complete

    if (compatibility["status"] != "passed" or compatibility["sources_unchanged"] is not True
            or compatibility["source_sha256"] != source or compatibility["python_version"] != runtime["python"]
            or compatibility["expected_python"] != runtime["python"] or len(compatibility["runs"]) != 2):
        raise ValueError("compatibility original differs")
    collection, selected_run = compatibility["runs"]
    nodes = collection["result"]["collected"]
    expected = [node for node in nodes if any(node == target or node.startswith(target + "::")
                                             or node.startswith(target + "[") for target in TARGETS)]
    if collection["label"] != "collection" or selected_run["label"] != "compatibility" or not expected:
        raise ValueError("compatibility stage selection differs")
    require_complete(collection, nodes, collect=True)
    require_complete(selected_run, expected)
    if compatibility["collected"] != len(nodes) or compatibility["selected"] != len(expected):
        raise ValueError("compatibility denominators differ")
    name = row["profile"]
    if name in {"linux-3.10", "linux-3.14", "macos-3.12", "linux-native"}:
        targets = list(LINUX_NATIVE_TARGETS) if name == "linux-native" else ["tests"]
        actual = suite_result(read_json(directory / "suite/manifest.json"), commit=commit, source=source, targets=targets, runtime=runtime)
        if any(row.get(key) != value for key, value in actual.items()):
            raise ValueError("profile summary differs from complete original suite")
    elif name == "packages":
        from tools._installed_receipts import evidence_inventory

        for kind in ("wheel", "sdist"):
            report_path = directory / "installations" / kind / "summary.json"
            report = read_json(report_path)
            if (report["selection_status"] != "passed" or report["status"] != "partial"
                    or report["candidate_unchanged"] is not True or report["artifact"] != kind
                    or report.get("retained_files") != evidence_inventory(report_path.parent)
                    or (directory / (kind + "-producer.txt")).read_text(encoding="utf-8").strip()
                    != "report_sha256=" + file_hash(report_path)):
                raise ValueError("installed original, retained bytes or producer custody differs")
            for artifact in ("wheel", "sdist"):
                if report[artifact]["sha256"] != row[artifact + "_sha256"]:
                    raise ValueError("installation archives differ between independent routes")
        verified = read_json(directory / "installed-verification.json")
        if (verified["status"] != "passed" or verified["artifact"] != "both"
                or verified["provenance"] != "local" or verified["workflow"] is not None
                or verified["candidate_commit"] != commit):
            raise ValueError("complete local installation replay verification is missing")
        for kind in ("wheel", "sdist"):
            artifact = directory / "dist" / verified[kind]["name"]
            if file_hash(artifact) != row[kind + "_sha256"]:
                raise ValueError("retained local archive differs")
    for name in ("inventory", "resources", "compatibility", "ruff", "vendored", "source-unchanged"):
        log = directory / (name + ".log")
        if (not log.is_file() or row.get("stages", {}).get(name, {}).get("exit_code") != 0
                or row["stages"][name]["log_sha256"] != file_hash(log)):
            raise ValueError("required original stage log is missing")
    return row


def profile_runtime(name):
    if name in {"linux-3.10", "linux-3.14"}:
        return "Linux", PYTHON_PATCHES[name.removeprefix("linux-")]
    if name == "macos-3.12":
        return "Darwin", PYTHON_PATCHES["3.12"]
    if name == "macos-compatibility":
        return "Darwin", PYTHON_PATCHES["3.14"]
    return "Linux", PYTHON_PATCHES["3.12"]


def execute_profile(name, output, commit, tree):
    """Runs inside the clean candidate; no signing material is available here."""
    output.mkdir(parents=True, exist_ok=False)
    runtime = checked_runtime(runtime_identity(), system=profile_runtime(name)[0], python=profile_runtime(name)[1])
    if git(ROOT, "rev-parse", "HEAD") != commit or git(ROOT, "rev-parse", "HEAD^{tree}") != tree:
        raise ValueError("candidate checkout differs")
    import evidence_wiki

    if Path(evidence_wiki.__file__).resolve() != ROOT / "src/evidence_wiki/__init__.py":
        raise ValueError("installed candidate code origin differs")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_", "RUNNER_"))}
    env.update(EVIDENCE_WIKI_EXPECTED_PYTHON=runtime["python"], PYTHONUTF8="1")
    source = source_identity(ROOT)
    row = {"profile": name, "commit": commit, "tree": tree, "runtime": runtime, "status": "running", "stages": {}}

    def stage(label, args, timeout=None):
        print("START " + label, flush=True)
        command([str(ROOT / ".venv/bin/python"), *args], cwd=ROOT, log=output / (label + ".log"), env=env, timeout=timeout)
        row["stages"][label] = {"exit_code": 0, "log_sha256": file_hash(output / (label + ".log")), "argv": args}
        print("PASS " + label, flush=True)

    try:
        stage("inventory", ["tools/sync_review_producer_inventory.py", "--check"])
        stage("resources", ["tools/sync_agent_resources.py", "--check"])
        stage("compatibility", ["tools/check_ci_compatibility.py", "--output", str(output / "compatibility")])
        compatibility = read_json(output / "compatibility/summary.json")
        if compatibility["status"] != "passed" or compatibility["sources_unchanged"] is not True:
            raise ValueError("compatibility did not pass")
        row.update(compatibility="passed", collection_sha256=digest(compatibility["runs"][0]["result"]["collected"]))
        if name in {"linux-3.10", "linux-3.14", "macos-3.12", "linux-native"}:
            targets = list(LINUX_NATIVE_TARGETS) if name == "linux-native" else ["tests"]
            stage("suite", ["tools/run_test_groups.py", "--group-size", str(GROUP_SIZE), "--timeout", str(GROUP_TIMEOUT),
                            "--output", str(output / "suite"), *targets])
            report = read_json(output / "suite/manifest.json")
            row.update(suite_result(report, commit=commit, source=source, targets=targets, runtime=runtime), targets=targets)
        elif name == "packages":
            stage("build", ["-m", "build", "--wheel", "--sdist", "--no-isolation"])
            artifacts = sorted((ROOT / "dist").glob("*"))
            stage("twine", ["-m", "twine", "check", "--strict", *map(str, artifacts)])
            row.update(installations={}, limits=policy()["installation_limits"])
            report_hashes = []
            installations = output / "installations"
            installations.mkdir()
            for kind in ("wheel", "sdist"):
                stage("installed-" + kind, ["tools/validate_installed_artifacts.py", "--dist-dir", "dist", "--artifact", kind,
                      "--expected-version", "", "--command-timeout", "900", "--case-timeout", "1200",
                      "--journey-timeout", "5400", "--journey-workers", "3", "--heartbeat-seconds", "30",
                      "--evidence-dir", str(installations / kind),
                      "--custody-output", str(output / (kind + "-producer.txt"))], timeout=6000)
                report = read_json(installations / kind / "summary.json")
                if (report["status"] != "partial" or report["selection_status"] != "passed"
                        or report["candidate_unchanged"] is not True or report["artifact"] != kind
                        or report["workflow"] != dict.fromkeys(("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))):
                    raise ValueError("complete local installation or truthful provenance is missing")
                retained = (output / (kind + "-producer.txt")).read_text(encoding="utf-8").strip()
                checksum = file_hash(installations / kind / "summary.json")
                if retained != "report_sha256=" + checksum:
                    raise ValueError("installation producer custody differs")
                report_hashes.append(checksum)
                row["installations"][kind] = "passed"
            from tools.verify_installed_artifacts import verify

            verified = verify(ROOT / "dist", installations, commit=commit, run_id=None,
                              report_sha256=report_hashes, provenance="local")
            (output / "installed-verification.json").write_text(json.dumps(verified, indent=2) + "\n", encoding="utf-8", newline="\n")
            shutil.copytree(ROOT / "dist", output / "dist")
            wheel = list((ROOT / "dist").glob("*.whl"))
            sdist = list((ROOT / "dist").glob("*.tar.gz"))
            if len(wheel) != 1 or len(sdist) != 1:
                raise ValueError("exactly one wheel and source archive are required")
            row.update(wheel_sha256=file_hash(wheel[0]), sdist_sha256=file_hash(sdist[0]))
        stage("ruff", ["-m", "ruff", "check", "."])
        stage("vendored", ["tools/sync_vendored_scripts.py", "--check"])
        command(["git", "diff", "--exit-code"], cwd=ROOT, log=output / "source-unchanged.log")
        row["stages"]["source-unchanged"] = {"exit_code": 0, "log_sha256": file_hash(output / "source-unchanged.log"),
                                             "argv": ["git", "diff", "--exit-code"]}
        if source_identity(ROOT) != source:
            raise ValueError("candidate source changed")
        row["status"] = "passed"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        row.update(status="failed", error=type(error).__name__)
        raise
    finally:
        (output / "profile.json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8", newline="\n")
    return row


def run_profile(args):
    if git(ROOT, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("commit tracked changes before creating a clean candidate")
    commit, tree = git(ROOT, "rev-parse", "HEAD"), git(ROOT, "rev-parse", "HEAD^{tree}")
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("local evidence must stay outside the checkout")
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="local-ci-candidate-") as directory:
        scratch = Path(directory)
        bundle = scratch / "candidate.bundle"
        command(["git", "bundle", "create", str(bundle), "HEAD"], cwd=ROOT)
        system, version = profile_runtime(args.profile)
        if system == "Linux":
            native = architecture(__import__("platform").machine())
            docker_platform = "linux/arm64" if native == "ARM64" else "linux/amd64"
            base = "python:" + version + "-slim-bookworm"
            command(["docker", "pull", "--platform", docker_platform, base], cwd=ROOT, log=output / "pull.log")
            observed = subprocess.check_output([executable("docker"), "image", "inspect", base, "--format", "{{.Os}}/{{.Architecture}}"],  # noqa: S603 -- fixed native image selection.
                                               text=True, encoding="utf-8").strip()
            if observed != docker_platform:
                raise ValueError("Docker image is not native to the producer architecture")
            context = scratch / "context"
            context.mkdir()
            shutil.copyfile(bundle, context / "candidate.bundle")
            poppler = " poppler-utils" if args.profile == "linux-3.10" else ""
            upgrade = " && .venv/bin/python -m pip install --upgrade pip" if args.profile != "packages" else ""
            (context / "Dockerfile").write_text(
                "FROM " + base + "\nRUN apt-get update && apt-get install -y --no-install-recommends git openssh-client" + poppler
                + f" && rm -rf /var/lib/apt/lists/*\nRUN useradd -m -u {os.getuid()} runner && mkdir /candidate && chown runner /candidate\n"
                + "COPY candidate.bundle /opt/candidate.bundle\nUSER runner\nWORKDIR /candidate\n"
                + "ENV HOME=/home/runner PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1\n"
                + "RUN git init && git fetch /opt/candidate.bundle HEAD && git checkout --detach FETCH_HEAD\n"
                + "RUN mkdir -p .venv/bin && ln -s /usr/local/bin/python3 .venv/bin/python && .venv/bin/python -c "
                + "'import os,sys,venv; sys._base_executable=os.path.realpath(sys.executable); venv.main([\"--clear\",\".venv\"])'"
                + upgrade + " && .venv/bin/python -m pip install --editable '.[dev]'\n",
                encoding="utf-8", newline="\n")
            (context / ".dockerignore").write_text("**\n!Dockerfile\n!candidate.bundle\n", encoding="utf-8", newline="\n")
            tag = "evidence-wiki-local-ci:" + commit[:12] + "-" + args.profile.replace(".", "-")
            command(["docker", "build", "--platform", docker_platform, "-t", tag, str(context)], cwd=ROOT, log=output / "image.log")
            if subprocess.check_output([executable("docker"), "image", "inspect", tag, "--format", "{{.Os}}/{{.Architecture}}"],  # noqa: S603 -- locally constructed candidate tag.
                                       text=True, encoding="utf-8").strip() != docker_platform:
                raise ValueError("built candidate architecture differs")
            # Only the evidence directory crosses the boundary; no host home, key or Docker socket is mounted.
            command(["docker", "run", "--rm", "--platform", docker_platform,
                     "--mount", f"type=bind,source={output},target=/evidence", tag,
                     ".venv/bin/python", "tools/local_ci.py", "_execute", "--profile", args.profile,
                     "--output", "/evidence/result", "--commit", commit, "--tree", tree], cwd=ROOT, log=output / "run.log")
        else:
            if sys.platform != "darwin" or args.python is None:
                raise ValueError("native macOS and an explicit pinned interpreter are required")
            candidate = scratch / "candidate"
            candidate.mkdir()
            command(["git", "init", str(candidate)], cwd=ROOT, log=output / "checkout.log")
            command(["git", "fetch", str(bundle), "HEAD"], cwd=candidate, log=output / "fetch.log")
            command(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=candidate, log=output / "detach.log")
            command([str(args.python), "-m", "venv", ".venv"], cwd=candidate, log=output / "venv.log")
            command([".venv/bin/python", "-m", "pip", "install", "--upgrade", "pip"], cwd=candidate, log=output / "pip.log")
            command([".venv/bin/python", "-m", "pip", "install", "--editable", ".[dev]"], cwd=candidate, log=output / "install.log")
            command([".venv/bin/python", "tools/local_ci.py", "_execute", "--profile", args.profile,
                     "--output", str(output / "result"), "--commit", commit, "--tree", tree], cwd=candidate, log=output / "run.log")


def seal(args):
    commit, tree = git(ROOT, "rev-parse", "HEAD"), git(ROOT, "rev-parse", "HEAD^{tree}")
    if args.evidence.resolve().is_relative_to(ROOT) or args.output.resolve().is_relative_to(args.evidence.resolve()):
        raise ValueError("evidence must be external and the receipt outside its inventory")
    if args.output.exists() or Path(str(args.output) + ".sig").exists():
        raise ValueError("receipt outputs must be new")
    if git(ROOT, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("candidate tracked files changed")
    rows, source = {}, tracked_source_identity(ROOT)
    for path in args.evidence.rglob("profile.json"):
        row = audit_profile(path.parent, commit=commit, tree=tree, source=source)
        name = row["profile"]
        if name in rows:
            raise ValueError("duplicate local profile")
        fields = {"commit", "tree", "runtime", "status", "compatibility", "collection_sha256"}
        fields |= {"installations", "limits", "wheel_sha256", "sdist_sha256"} if name == "packages" else (
            {"targets", "collected", "executed", "groups", "combined_orders"} if name != "macos-compatibility" else set())
        if name == "linux-native":
            fields.add("native_checks")
        rows[name] = {key: row[key] for key in fields}
    inventory = {}
    for path in sorted(args.evidence.rglob("*")):
        if path.is_symlink():
            raise ValueError("linked local evidence is refused")
        if path.is_file():
            inventory[path.relative_to(args.evidence).as_posix()] = file_hash(path)
    instant = datetime.now(timezone.utc)
    value = {"schema_version": SCHEMA, "repository": args.repo, "commit": commit, "tree": tree, "base": args.base,
             "policy_sha256": digest(policy()), "created_utc": instant.isoformat(),
             "producer": args.producer,
             "profiles": rows, "evidence_sha256": digest(inventory), "status": "passed"}
    checked_receipt(value, commit=commit, tree=tree, base=args.base, repository=args.repo)
    if args.key.resolve().is_relative_to(ROOT) or args.key.resolve().is_relative_to(args.evidence.resolve()):
        raise ValueError("signing key must remain outside the repository and evidence")
    args.output.write_bytes(canonical(value))
    command(["ssh-keygen", "-Y", "sign", "-f", str(args.key), "-n", SIGNING_NAMESPACE, str(args.output)], cwd=ROOT)
    print("Sealed exact local results for " + commit)


def publish(args):
    value = read_json(args.receipt)
    if value["commit"] != git(ROOT, "rev-parse", "HEAD"):
        raise ValueError("receipt is for another candidate")
    # The required verifier runs default-branch code; publishing never pushes source or changes branch protection.
    payload = {"ref": args.ref, "inputs": {"receipt": base64.b64encode(args.receipt.read_bytes()).decode("ascii"),
                                          "signature": base64.b64encode(args.signature.read_bytes()).decode("ascii")}}
    if len(canonical(payload)) > 60000:
        raise ValueError("dispatch payload exceeds its bound")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as stream:
        json.dump(payload, stream)
        stream.flush()
        command(["gh", "api", "--method", "POST", f"repos/{args.repo}/actions/workflows/local-pre-pr.yml/dispatches",
                 "--input", stream.name], cwd=ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    run = commands.add_parser("run")
    run.add_argument("--profile", choices=REQUIRED_LOCAL_PROFILES, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--python", type=Path)
    execute = commands.add_parser("_execute")
    execute.add_argument("--profile", choices=REQUIRED_LOCAL_PROFILES, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--commit", required=True)
    execute.add_argument("--tree", required=True)
    signing = commands.add_parser("seal")
    signing.add_argument("--evidence", type=Path, required=True)
    signing.add_argument("--key", type=Path, required=True)
    signing.add_argument("--base", required=True)
    signing.add_argument("--repo", required=True)
    signing.add_argument("--producer", required=True)
    signing.add_argument("--output", type=Path, required=True)
    upload = commands.add_parser("publish")
    upload.add_argument("--receipt", type=Path, required=True)
    upload.add_argument("--signature", type=Path, required=True)
    upload.add_argument("--repo", required=True)
    upload.add_argument("--ref", default="main")
    args = parser.parse_args(argv)
    try:
        if args.operation == "run":
            run_profile(args)
        elif args.operation == "_execute":
            execute_profile(args.profile, args.output, args.commit, args.tree)
        elif args.operation == "seal":
            seal(args)
        else:
            publish(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Local CI refused: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
