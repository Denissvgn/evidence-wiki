#!/usr/bin/env python3
"""Require a trusted verification run and matching native Linux peer evidence."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools._local_ci_contract import checked_receipt, linux_native_pair  # noqa: E402
from tools.verify_local_ci import executable  # noqa: E402


def gh(arguments):
    result = subprocess.check_output([executable("gh"), *arguments], text=True, encoding="utf-8")  # noqa: S603 -- fixed authenticated API argv, no shell.
    return json.loads(result)


def verify_run(repo, commit, base, output):
    statuses = gh(["api", f"repos/{repo}/commits/{commit}/statuses", "--paginate", "--slurp"])
    rows = [row for page in statuses for row in page if row["context"] == "local-pre-pr"]
    if not rows or rows[0]["state"] != "success":
        raise ValueError("authorized local-pre-pr success is missing")
    match = re.fullmatch(r"https://github.com/" + re.escape(repo) + r"/actions/runs/(\d+)", rows[0]["target_url"] or "")
    if match is None:
        raise ValueError("local result does not name this repository's verification run")
    run_id = match[1]
    run = gh(["api", f"repos/{repo}/actions/runs/{run_id}"])
    default = gh(["api", f"repos/{repo}"])["default_branch"]
    if (run["event"] != "workflow_dispatch" or run["head_branch"] != default or run["conclusion"] != "success"
            or run["path"] != ".github/workflows/local-pre-pr.yml"):
        raise ValueError("local receipt was not verified by the trusted default-branch workflow")
    output.mkdir(parents=True, exist_ok=False)
    subprocess.run([executable("gh"), "run", "download", run_id, "--repo", repo, "--name", "verified-local-ci-" + run_id,  # noqa: S603 -- validated repository and numeric trusted run.
                    "--dir", str(output)], check=True)
    result = json.loads((output / "verified-local-ci.json").read_text(encoding="utf-8"))
    value = result["receipt"]
    tree = gh(["api", f"repos/{repo}/git/commits/{commit}"])["tree"]["sha"]
    checked_receipt(value, commit=commit, tree=tree, base=base, repository=repo, now=datetime.now(timezone.utc))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--native-peer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
            raise ValueError("repository name is invalid")
        if any(not re.fullmatch(r"[a-f0-9]{40}", value) for value in (args.commit, args.base)):
            raise ValueError("exact candidate and base revisions are required")
        receipt = verify_run(args.repo, args.commit, args.base, args.output)
        peer = json.loads(args.native_peer.read_text(encoding="utf-8"))
        local = receipt["profiles"]["linux-native"]
        pair = linux_native_pair(local, peer)
        print(json.dumps({"status": "passed", "commit": args.commit, "linux_architectures": pair,
                          "local_profiles": sorted(receipt["profiles"])}))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("Local CI status refused: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
