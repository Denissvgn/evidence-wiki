#!/usr/bin/env python3
"""Verify an authorized local producer receipt without executing candidate code."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools._local_ci_contract import SIGNING_NAMESPACE, canonical, checked_receipt  # noqa: E402

MAX_RECEIPT_BYTES = 32768
MAX_SIGNATURE_BYTES = 4096


def git(root, *args):
    return subprocess.check_output([executable("git"), "-C", str(root), *args], text=True, encoding="utf-8").strip()  # noqa: S603 -- exact Git objects, no shell.


def executable(name):
    selected = shutil.which(name)
    if selected is None:
        raise ValueError("required executable is unavailable: " + name)
    return str(Path(selected).resolve())


def verify_signature(receipt, signature, public_key):
    """The public key is enrolled outside candidate-controlled source."""
    if (not isinstance(public_key, str) or "\n" in public_key.strip()
            or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\r\n]+)?", public_key.strip())):
        raise ValueError("LOCAL_CI_PUBLIC_KEY must contain one enrolled Ed25519 public key")
    if not 0 < len(receipt) <= MAX_RECEIPT_BYTES or not 0 < len(signature) <= MAX_SIGNATURE_BYTES:
        raise ValueError("receipt or signature exceeds its bound")
    with tempfile.TemporaryDirectory(prefix="local-ci-verify-") as directory:
        root = Path(directory)
        allowed, signed = root / "allowed-signers", root / "receipt.sig"
        allowed.write_text("local-ci " + public_key.strip() + "\n", encoding="utf-8", newline="\n")
        signed.write_bytes(signature)
        result = subprocess.run([executable("ssh-keygen"), "-Y", "verify", "-f", str(allowed), "-I", "local-ci",  # noqa: S603 -- bounded signature files and fixed namespace.
                                 "-n", SIGNING_NAMESPACE, "-s", str(signed)],
                                input=receipt, capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            raise ValueError("local producer signature does not verify")


def verify(root, receipt, signature, public_key, *, repository, expected_commit=None, expected_base=None, now=None):
    verify_signature(receipt, signature, public_key)
    value = json.loads(receipt)
    if canonical(value) != receipt:
        raise ValueError("receipt is not canonical JSON")
    commit = value["commit"] if expected_commit is None else expected_commit
    base = value["base"] if expected_base is None else expected_base
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"[a-f0-9]{40}", base):
        raise ValueError("candidate and base must be exact Git revisions")
    tree = git(root, "rev-parse", commit + "^{tree}")
    if git(root, "rev-parse", base + "^{commit}") != base:
        raise ValueError("base revision is unavailable")
    # A pre-PR head based on this exact base has the same tree as its PR merge.
    subprocess.run([executable("git"), "-C", str(root), "merge-base", "--is-ancestor", base, commit], check=True,  # noqa: S603 -- bounded exact revisions.
                   capture_output=True, timeout=30)
    return checked_receipt(value, commit=commit, tree=tree, base=base, repository=repository, now=now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipt", type=Path)
    source.add_argument("--event", type=Path)
    parser.add_argument("--signature", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--base")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.event:
            event = json.loads(args.event.read_text(encoding="utf-8"))
            if len(event["inputs"]["receipt"]) > 45000 or len(event["inputs"]["signature"]) > 6000:
                raise ValueError("encoded receipt exceeds its bound")
            raw = base64.b64decode(event["inputs"]["receipt"], validate=True)
            signed = base64.b64decode(event["inputs"]["signature"], validate=True)
        else:
            if args.signature is None:
                raise ValueError("detached signature is required")
            if args.receipt.stat().st_size > MAX_RECEIPT_BYTES or args.signature.stat().st_size > MAX_SIGNATURE_BYTES:
                raise ValueError("receipt file exceeds its bound")
            raw, signed = args.receipt.read_bytes(), args.signature.read_bytes()
        value = verify(args.root, raw, signed, os.environ.get("LOCAL_CI_PUBLIC_KEY", ""),
                       repository=args.repo, expected_commit=args.commit, expected_base=args.base)
        result = {"status": "passed", "commit": value["commit"], "tree": value["tree"],
                  "base": value["base"], "producer": value["producer"], "profiles": sorted(value["profiles"]),
                  "evidence_sha256": value["evidence_sha256"], "receipt": value}
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("Local CI verification refused: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
