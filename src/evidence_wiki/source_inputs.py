"""Scoped local observations for source inspection, without inventory or execution."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ._pack_io import canonical, identity, json_document, read_file, relative_path, yaml_document
from .errors import EvidenceWikiError
from .source_contracts import refuse


class SourceView:
    """Retain and recheck only the caller's selected configuration/source bytes."""

    def __init__(self, target):
        self.root = Path(target).expanduser().absolute() if target is not None else None
        self.files = {}
        self.root_identity = identity(self.root) if self.root is not None and self.root.is_dir() else None
        self.config = {}
        self.workspace = "absent"
        if self.root is not None and (self.root.exists() or self.root.is_symlink()):
            self.workspace = "invalid"
            if not self.root.is_dir() or self.root.is_symlink():
                return
            raw = self.read("research.yml")
            system = self.read("workspace-system.yml")
            if raw is not None and system is not None:
                try:
                    config, marker = yaml_document(raw), yaml_document(system)
                    if isinstance(config, dict) and isinstance(marker, dict) and isinstance(marker.get("workspace_system"), dict):
                        self.config = config
                        self.workspace = "present"
                        self.marker = marker["workspace_system"]
                except (EvidenceWikiError, ValueError, TypeError):
                    pass

    def read(self, relative, maximum=1_048_576):
        relative_path(relative)
        if relative in self.files:
            return self.files[relative]
        if self.root is None or not self.root.is_dir():
            return None
        path = self.root / relative
        if not path.exists() and not path.is_symlink():
            self.files[relative] = None
            return None
        value = read_file(self.root, relative, maximum)
        if sum(len(item) for item in self.files.values() if item is not None) + len(value) > 33_554_432:
            refuse("source_observation_bytes_bound", "ONBOARDING_LIMIT")
        self.files[relative] = value
        return value

    def generated_path(self, name, default):
        sources = self.config.get("sources")
        value = sources.get(name, default) if isinstance(sources, dict) else default
        relative_path(value)
        if not value.startswith("sources/"):
            refuse("source_generated_path_invalid")
        return value

    def manifest(self):
        path = self.generated_path("manifest_path", "sources/manifest.jsonl")
        raw = self.read(path, 8_388_608)
        if raw is None:
            return {}
        result = {}
        lines = raw.splitlines()
        if len(lines) > 4096:
            refuse("source_manifest_bound", "ONBOARDING_LIMIT")
        for line in lines:
            if not line.strip():
                continue
            row = json_document(line)
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not 1 <= len(row["id"]) <= 256 or row["id"] in result):
                refuse("source_manifest_invalid")
            result[row["id"]] = row
        return result

    def finish(self):
        if self.root_identity is not None and (self.root is None or not self.root.is_dir() or identity(self.root) != self.root_identity):
            refuse("source_target_changed", "ONBOARDING_PLAN_STALE")
        for relative, raw in self.files.items():
            path = self.root / relative
            if raw is None:
                if path.exists() or path.is_symlink():
                    refuse("source_inputs_changed", "ONBOARDING_PLAN_STALE")
            elif read_file(self.root, relative, max(1, len(raw))) != raw:
                refuse("source_inputs_changed", "ONBOARDING_PLAN_STALE")
        values = {name: hashlib.sha256(raw).hexdigest() if raw is not None else None for name, raw in self.files.items()}
        return {"sha256": hashlib.sha256(canonical(values)).hexdigest(), "files_observed": len(values),
                "scope": "selected_local_inputs", "atomic_host_snapshot": False}
