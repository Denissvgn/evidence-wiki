"""Resolve packaged starter and domain-pack assets."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

try:
    from importlib.resources.abc import Traversable
except ImportError:  # Python 3.10
    from importlib.abc import Traversable

ASSETS_DIR = "assets"
STARTER_DIR = "workspace-template"
DOMAIN_PACKS_DIR = "domain-packs"
ORCHESTRATOR_DIR = "orchestrator"
ORCHESTRATOR_SKILL = "research-orchestrate"
REQUIRED_STARTER_ASSETS = (
    "AGENTS.md",
    "index.md",
    "log.md",
    "research.yml",
    "workspace-system.yml",
    "scripts/doctor.py",
    "scripts/init_research_workspace.py",
    "scripts/lint.py",
    "scripts/smoke_validate_workspace.py",
    "scripts/workspace_status.py",
)
REQUIRED_DOMAIN_PACKS = (
    "general-science",
    "legal-regulatory",
    "llm-research",
    "standards-compliance",
)
REQUIRED_DOMAIN_PACK_ASSETS = (
    "README.md",
    "claims.md",
    "research.overlay.yml",
    "taxonomy.md",
)
REQUIRED_ORCHESTRATOR_ASSETS = (
    "README.md",
    f"skills/{ORCHESTRATOR_SKILL}.md",
)


def required_asset_manifest() -> dict[str, list[str]]:
    """Return stable source-relative anchors every distribution must contain."""
    return {
        "starter": [f"{STARTER_DIR}/{relative}" for relative in REQUIRED_STARTER_ASSETS],
        "domain_packs": [
            f"{DOMAIN_PACKS_DIR}/{pack}/{relative}"
            for pack in REQUIRED_DOMAIN_PACKS
            for relative in REQUIRED_DOMAIN_PACK_ASSETS
        ],
        "orchestrator": [f"{ORCHESTRATOR_DIR}/{relative}" for relative in REQUIRED_ORCHESTRATOR_ASSETS],
    }


def missing_required_assets(path: Path) -> list[str]:
    """Return required distribution anchors missing below an assets root."""
    manifest = required_asset_manifest()
    return [relative for group in manifest.values() for relative in group if not (path / relative).is_file()]


def _is_assets_root(path: Path) -> bool:
    return not missing_required_assets(path)


def orchestrator_skill_path(root: Path) -> Path:
    """Return the orchestrator playbook skill path within an assets root.

    The orchestrator skills ship alongside the starter and domain packs but are
    never copied into a created workspace; they target the external PM/parent
    agent that manages workspaces through the package CLI.
    """
    return root / ORCHESTRATOR_DIR / "skills" / f"{ORCHESTRATOR_SKILL}.md"


def _source_checkout_assets_root() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    if _is_assets_root(root):
        return root
    return None


def _copy_traversable_tree(source: Traversable, target: Path) -> None:
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_traversable_tree(child, target / child.name)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_file, target.open("wb") as target_file:
        shutil.copyfileobj(source_file, target_file)


@contextmanager
def assets_root() -> Iterator[Path]:
    """Yield a filesystem path containing the starter and domain packs."""
    packaged_assets = resources.files(__package__).joinpath(ASSETS_DIR)
    packaged_path = Path(str(packaged_assets))
    if _is_assets_root(packaged_path):
        yield packaged_path
        return

    if packaged_assets.is_dir():
        with tempfile.TemporaryDirectory(prefix="evidence-wiki-assets-") as tmpdir:
            extracted_root = Path(tmpdir)
            _copy_traversable_tree(packaged_assets, extracted_root)
            if not _is_assets_root(extracted_root):
                raise RuntimeError("Packaged EvidenceWiki assets are incomplete")
            yield extracted_root
            return

    source_root = _source_checkout_assets_root()
    if source_root is not None:
        yield source_root
        return

    raise RuntimeError("Cannot locate EvidenceWiki workspace template assets")
