"""Facade namespaces hung off a :class:`evidence_wiki.workspace.Workspace`.

Each namespace groups the operations of one workspace concern behind an
attribute on the handle (``ws.coverage.evaluate(...)``,
``ws.orchestrate.session(...)``). Every namespace lives in its own module --
deliberately, not for size but for isolation: separate work units fill
different namespaces in parallel, and a single shared module would put them all
in one another's way. Add operations to the namespace's own module; keep this
package's ``__init__`` a re-export surface only.

Every namespace subclasses :class:`._base.Namespace`, which holds the owning
handle as ``self._workspace`` and reaches scripts through ``self._script(stem)``.
"""

from __future__ import annotations

from ._base import Namespace
from .assessments import AssessmentsNamespace
from .coverage import CoverageNamespace
from .diagnostics import DiagnosticsNamespace
from .grounding import GroundingNamespace
from .mutations import MutationsNamespace
from .normalize import NormalizeNamespace
from .orchestrate import OrchestrateNamespace, OrchestrationSession
from .questions import QuestionsNamespace
from .research import ResearchNamespace
from .requirements import RequirementsNamespace
from .snapshots import SnapshotsNamespace
from .source_requests import SourceRequestsNamespace
from .sources import SourcesNamespace
from .strict import StrictNamespace
from .telemetry import TelemetryNamespace
from .temporal import TemporalNamespace
from .usage import UsageNamespace

__all__ = [
    "AssessmentsNamespace",
    "CoverageNamespace",
    "DiagnosticsNamespace",
    "GroundingNamespace",
    "Namespace",
    "MutationsNamespace",
    "NormalizeNamespace",
    "OrchestrateNamespace",
    "OrchestrationSession",
    "QuestionsNamespace",
    "ResearchNamespace",
    "RequirementsNamespace",
    "TelemetryNamespace",
    "SnapshotsNamespace",
    "SourceRequestsNamespace",
    "SourcesNamespace",
    "StrictNamespace",
    "TemporalNamespace",
    "UsageNamespace",
]
