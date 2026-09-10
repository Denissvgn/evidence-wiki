# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.knowledge_freshness.

Original source SHA-256: bc7e62322087f294888f4155c8bfd4d625317ee73b99efc45b038965f7c36748
Only the validation dependency closure is included; source discovery, producer
execution, persistence, plugins and live reconciliation are excluded.

MIT License

Copyright (c) 2026 Denis Sivagin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

"""

from __future__ import annotations

from types import MappingProxyType


REASON_LIVE_EVALUATION_NOT_PERFORMED = "live-evaluation-not-performed"


REASON_RECORDED_BASIS_MATCHES_LIVE_EVALUATION = "recorded-basis-matches-live-evaluation"


REASON_SOURCE_BYTES_CHANGED_CONCEPT_OBSERVATION_UNCHANGED = (
    "source-bytes-changed-concept-observation-unchanged"
)


REASON_CONCEPT_OBSERVATION_CHANGED = "concept-observation-changed"


REASON_EXTRACTOR_VERSION_CHANGED = "extractor-version-changed"


REASON_EXTRACTOR_CONFIGURATION_CHANGED = "extractor-configuration-changed"


REASON_RELIABLY_MAPPED_SOURCE_MISSING = "reliably-mapped-source-missing"


REASON_MISSING_SOURCE_HAS_NO_RELIABLE_RECORDED_BASIS = (
    "missing-source-has-no-reliable-recorded-basis"
)


REASON_RECORDED_BASIS_UNAVAILABLE = "recorded-basis-unavailable"


REASON_LIVE_BASIS_UNAVAILABLE = "live-basis-unavailable"


REASON_FRESHNESS_NOT_MODELED = "freshness-not-modeled"


REASON_SCHEMA_VERSION_CHANGED = "knowledge-schema-version-changed"


REASON_GENERATION_OPTIONS_CHANGED = "generation-options-changed"


REASON_TOOL_ID_CHANGED = "producer-tool-id-changed"


REASON_TOOL_VERSION_CHANGED = "producer-tool-version-changed"


REASON_TOOL_CONFIGURATION_CHANGED = "producer-tool-configuration-changed"


REASON_TOOL_CONFIGURATION_UNKNOWN = "producer-tool-configuration-unknown"


REASON_TOOL_LIMITATIONS_CHANGED = "producer-tool-limitations-changed"


REASON_VERSION_UNKNOWN = "version-unknown"


REASON_EXTRACTOR_SELECTION_CHANGED = "extractor-selection-changed"


REASON_LIVE_EXTRACTOR_UNAVAILABLE = "live-extractor-unavailable"


REASON_EXTRACTOR_LIMITATIONS_CHANGED = "extractor-limitations-changed"


REASON_EXTRACTOR_CONFIGURATION_UNKNOWN = "extractor-configuration-unknown"


REASON_PLUGIN_SET_CHANGED = "plugin-set-changed"


REASON_PLUGIN_VERSION_CHANGED = "plugin-version-changed"


REASON_PLUGIN_CONFIGURATION_CHANGED = "plugin-configuration-changed"


REASON_PLUGIN_LIMITATIONS_CHANGED = "plugin-limitations-changed"


REASON_PLUGIN_CONFIGURATION_UNKNOWN = "plugin-configuration-unknown"


REASON_SOURCE_MAPPING_CHANGED = "source-mapping-changed"


REASON_OBSERVATION_SCOPE_CHANGED = "observation-scope-changed"


REASON_IDENTICAL_SOURCE_OBSERVATION_MISMATCH = "identical-source-observation-mismatch"


_REASON_DESCRIPTIONS = MappingProxyType(
    {
        REASON_LIVE_EVALUATION_NOT_PERFORMED: "live evaluation was not performed",
        REASON_RECORDED_BASIS_MATCHES_LIVE_EVALUATION: ("unchanged since observation"),
        REASON_SOURCE_BYTES_CHANGED_CONCEPT_OBSERVATION_UNCHANGED: (
            "concept observation is unchanged since observation; source bytes changed"
        ),
        REASON_CONCEPT_OBSERVATION_CHANGED: (
            "concept observation changed since observation"
        ),
        REASON_EXTRACTOR_VERSION_CHANGED: "extractor version changed",
        REASON_EXTRACTOR_CONFIGURATION_CHANGED: "extractor configuration changed",
        REASON_RELIABLY_MAPPED_SOURCE_MISSING: ("reliably mapped source is missing"),
        REASON_MISSING_SOURCE_HAS_NO_RELIABLE_RECORDED_BASIS: (
            "a missing source cannot be asserted without a reliable recorded basis"
        ),
        REASON_RECORDED_BASIS_UNAVAILABLE: (
            "a reliable recorded concept basis is unavailable"
        ),
        REASON_LIVE_BASIS_UNAVAILABLE: ("a reliable live concept basis is unavailable"),
        REASON_FRESHNESS_NOT_MODELED: (
            "live structural freshness is not modeled for this concept"
        ),
        REASON_SCHEMA_VERSION_CHANGED: "knowledge schema version changed",
        REASON_GENERATION_OPTIONS_CHANGED: "generation options changed",
        REASON_TOOL_ID_CHANGED: "producer tool identity changed",
        REASON_TOOL_VERSION_CHANGED: "producer tool version changed",
        REASON_TOOL_CONFIGURATION_CHANGED: "producer tool configuration changed",
        REASON_TOOL_CONFIGURATION_UNKNOWN: (
            "producer tool configuration basis is unknown"
        ),
        REASON_TOOL_LIMITATIONS_CHANGED: "producer tool limitations changed",
        REASON_VERSION_UNKNOWN: (
            "a contributing producer component version is unknown"
        ),
        REASON_EXTRACTOR_SELECTION_CHANGED: "selected extractor changed",
        REASON_LIVE_EXTRACTOR_UNAVAILABLE: (
            "the recorded extractor is unavailable in the live producer basis"
        ),
        REASON_EXTRACTOR_LIMITATIONS_CHANGED: "extractor limitations changed",
        REASON_EXTRACTOR_CONFIGURATION_UNKNOWN: (
            "extractor configuration basis is unknown"
        ),
        REASON_PLUGIN_SET_CHANGED: "contributing plugin set changed",
        REASON_PLUGIN_VERSION_CHANGED: "contributing plugin version changed",
        REASON_PLUGIN_CONFIGURATION_CHANGED: (
            "contributing plugin configuration changed"
        ),
        REASON_PLUGIN_LIMITATIONS_CHANGED: ("contributing plugin limitations changed"),
        REASON_PLUGIN_CONFIGURATION_UNKNOWN: (
            "contributing plugin configuration basis is unknown"
        ),
        REASON_SOURCE_MAPPING_CHANGED: "concept source mapping changed",
        REASON_OBSERVATION_SCOPE_CHANGED: "concept observation scope changed",
        REASON_IDENTICAL_SOURCE_OBSERVATION_MISMATCH: (
            "identical source under an identical basis produced a different "
            "observation; the record or producer may be corrupt or nondeterministic"
        ),
    }
)


KNOWN_FRESHNESS_REASON_CODES = frozenset(_REASON_DESCRIPTIONS)
