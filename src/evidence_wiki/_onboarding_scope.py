"""Host-selected roots and operation grants for lifecycle entry points."""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from ._filesystem import os
from ._pack_io import identity, json_document
from ._script_host import shared_assets_root
from .pack_catalog import _outside_assets, _writer_flags
from .planning_contracts import refuse

ACTIVE_SCOPE = ContextVar("evidence_wiki_onboarding_scope", default=None)


def scoped_operation(function):
    @wraps(function)
    def scoped(self, *args, **kwargs):
        token = ACTIVE_SCOPE.set(self._scope)
        try:
            return function(self, *args, **kwargs)
        except OSError:
            refuse("onboarding_local_environment_unavailable", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        finally:
            ACTIVE_SCOPE.reset(token)
    return scoped


class Scope:
    def __init__(self, roots):
        self.roots, self.descriptors = [], []
        if len(roots) > 16:
            refuse("onboarding_root_bound")
        try:
            for value in roots:
                if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
                    refuse("onboarding_root_invalid")
                supplied = Path(value).expanduser().absolute()
                if supplied.is_symlink():
                    refuse("onboarding_root_link")
                root = supplied.resolve(strict=True)
                descriptor = os.open(root, _writer_flags())
                self.descriptors.append(descriptor)
                self.roots.append((root, identity(descriptor)))
        except (OSError, ValueError, RuntimeError):
            self.close()
            refuse("onboarding_root_unavailable", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        except BaseException:
            self.close()
            raise

    def close(self):
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors = []

    def verify(self):
        for root, expected in self.roots:
            if not root.is_dir() or root.is_symlink() or root.resolve() != root or identity(root) != expected:
                refuse("onboarding_root_changed", "ONBOARDING_PLAN_STALE")

    def path(self, value, *, write=False, installation=False):
        if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
            refuse("onboarding_path_invalid")
        self.verify()
        try:
            supplied = Path(value).expanduser().absolute()
            if supplied.is_symlink():
                refuse("onboarding_path_link")
            resolved = supplied.resolve()
        except (OSError, ValueError, RuntimeError):
            refuse("onboarding_path_invalid")
        roots = [root for root, _ in self.roots]
        if installation and not write:
            roots += [shared_assets_root() / name for name in ("workspace-template", "domain-packs", "orchestrator")]
        if not any(resolved.is_relative_to(root) for root in roots):
            refuse("onboarding_path_outside_host_scope", "ONBOARDING_AUTHORITY_REQUIRED")
        for key in ("EVIDENCE_WIKI_AUTHORITY_FILE", "EVIDENCE_WIKI_STATE_DIR"):
            if os.environ.get(key):
                protected = Path(os.environ[key]).expanduser().resolve()
                if resolved.is_relative_to(protected) or protected.is_relative_to(resolved):
                    refuse("onboarding_path_overlaps_host_authority", "ONBOARDING_AUTHORITY_REQUIRED")
        if write:
            _outside_assets(resolved, additional_roots=(Path(__file__).parent,))
        return resolved

    def setup(self, value):
        from .planning_contracts import PLAN, REQUEST, decode

        raw = value
        shape = json_document(raw)
        doc = decode(raw, PLAN if isinstance(shape, dict) and shape.get("schema_version") == PLAN else REQUEST)
        if doc["schema_version"] == PLAN:
            request = doc["request"]
        else:
            request = doc
        payload = request["request"]["payload"]
        selected = payload["target"]
        # Setup writes its coordination state under the selected writable root too.
        self.path(selected["writable_root"], write=True)
        self.path(Path(selected["writable_root"]) / selected["relative_path"], write=True)
        for scope in payload["authority"]["source_scope"]:
            if not scope.startswith(("https://", "http://")):
                self.path(scope, installation=True)
        for row in payload["sources"]:
            if row["kind"] == "local_file":
                self.path(row["locator"], installation=True)
        pack = payload["domain"]["pack"]
        if pack and pack["origin"] != "bundled":
            self.path(pack["locator"], installation=True)
        action = request.get("decisions", {}).get("accepted_pack")
        if isinstance(action, dict) and action.get("catalog"):
            self.catalog(action["catalog"])
        return doc

    def catalog(self, value):
        from .pack_catalog import _read

        root = self.path(value)
        document = _read(root)
        for row in document["roots"].values():
            self.path(row["path"], installation=True)
        return root

    def revision(self, request):
        if not isinstance(request, dict) or "target" not in request:
            refuse("onboarding_revision_shape")
        self.path(request["target"], write=True)
        if request.get("path"):
            self.path(request["path"], installation=True)
        if request.get("catalog"):
            self.catalog(request["catalog"])

    def builtin_workspace(self, target):
        from .pack_discovery import owner
        from .pack_migrations import _call

        root = self.path(target)
        config = _call(owner("_domain_pack_lifecycle").load_mapping, root / "research.yml", "configuration")
        if not owner("_caller_context").builtin_integrations(config):
            refuse("scoped_onboarding_registered_execution_not_granted", "ONBOARDING_AUTHORITY_REQUIRED")
        return root
