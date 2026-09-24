"""Installed generation and trusted deployed-controller checks for explicit hosts."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from . import __version__
from ._pack_io import read_file
from ._script_host import shared_assets_root
from .planning_contracts import refuse


def generation():
    from ._contract import LIBRARY_API_VERSION
    from .strict_host import implementation_identity

    assets = shared_assets_root()
    runtime = {name: hashlib.sha256(read_file(assets, "workspace-template/scripts/" + name, maximum=2_097_152)).hexdigest()
               for name in _scripts(assets / "workspace-template/scripts")}
    return {"package_version": __version__, "library_api_version": LIBRARY_API_VERSION,
            "interpreter": str(Path(sys.executable).absolute()), "implementation": implementation_identity(),
            "runtime_sha256": hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest(),
            "catalog_sha256": hashlib.sha256(read_file(assets, "workspace-template/docs/agent-resources/catalog.json")).hexdigest()}


def _scripts(directory):
    if directory.is_symlink():
        refuse("host_runtime_directory_link", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    names = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= 512:
                refuse("host_runtime_directory_bound", "ONBOARDING_LIMIT")
            if entry.name.endswith(".py"):
                names.append(entry.name)
    return sorted(names)


def require_runtime(target):
    root = Path(target)
    starter = shared_assets_root() / "workspace-template"
    names = _scripts(starter / "scripts")
    if _scripts(root / "scripts") != names:
        refuse("host_workspace_runtime_requires_explicit_upgrade", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    for name in names:
        if read_file(root, "scripts/" + name, maximum=2_097_152) != read_file(starter, "scripts/" + name, maximum=2_097_152):
            refuse("host_workspace_runtime_not_the_installed_generation", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
