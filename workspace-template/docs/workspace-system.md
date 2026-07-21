# Workspace System Metadata

`workspace-system.yml` records which reusable workspace starter produced a research workspace. It is intentionally separate from `research.yml`: `research.yml` describes a specific research project, while `workspace-system.yml` describes the reusable starter and metadata contract.

Generated workspaces should keep this file at the workspace root so a human or agent can identify starter provenance without running code.

## File Shape

The file uses one top-level mapping:

```yaml
workspace_system:
  starter_version: "0.5.4"
  schema_version: "0.1"
  created: "2026-05-10"
  compatible_research_yml_contract: "0.1"
```

Required fields:

| Field | Meaning |
|-------|---------|
| `starter_version` | Version of the reusable starter content copied or used to generate the workspace. |
| `schema_version` | Version of the `workspace-system.yml` metadata shape. |
| `created` | Date this starter metadata record was created, using `YYYY-MM-DD`. |
| `compatible_research_yml_contract` | `research.yml` contract version this starter metadata is expected to work with. |

Keep all values quoted strings. This keeps the file readable and avoids YAML parsers treating dates or version-like values as numeric or date objects.

## Update Policy

Bump `starter_version` when the reusable starter changes:

- Patch version for documentation fixes, scaffold fixes, or small script/test updates that do not change expected workspace structure.
- Minor version for new starter capabilities, new optional files, or compatible additions to the workspace structure.
- Major version for incompatible structure, script, or operating-contract changes that require migration guidance.

Bump `schema_version` only when the shape or required fields of `workspace-system.yml` changes.

Change `compatible_research_yml_contract` when the starter requires a different `research.yml` contract version. Domain packs should declare their own compatibility separately and should not add domain-specific fields to this metadata file.

## Generated Workspaces

Workspace initialization tools should copy `workspace-system.yml` into created workspaces. Later initialization tasks may add separate workspace-specific provenance, such as a generated workspace creation date or setup profile path, but they should preserve this starter metadata as the system version anchor.
