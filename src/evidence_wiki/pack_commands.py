"""CLI transport for explicit pack discovery, caller catalogs and domain decisions."""

from __future__ import annotations

import json
from pathlib import Path

from ._pack_io import MAX_OUTPUT, read_file, refuse
from .agent import _Parser
from .errors import EvidenceWikiError
from .pack_decisions import decide, schema_document, schemas
from .pack_discovery import inventory, select


def contract_index():
    return {"capability": "pack-discovery/v1", "schema_ids": sorted(schemas()),
            "schema_command": "evidence-wiki pack schemas --schema-id ID",
            "guide_command": "evidence-wiki pack guide", "resource_command": "evidence-wiki pack show --resource ID",
            "origins": ["bundled", "local", "installed", "path"],
            "commands": ["pack list", "pack show", "pack catalog init", "pack catalog register", "pack catalog list", "pack decide"],
            "limits": {"roots": 8, "local_revisions": 32, "packs": 64, "files_per_pack": 256,
                       "file_bytes": 1048576, "tree_bytes": 8388608, "output_bytes": MAX_OUTPUT},
            "catalog_authority": "caller-local revision and validation observations; workspace lifecycle remains authoritative",
            "provider_enablement": False, "semantic_fit": "caller_declared", "multi_pack_composition": False,
            "catalog_write_platform": "POSIX descriptor-relative operations and an established workspace lock"}


def _summary(row):
    metadata = row.get("metadata") or {}
    result = {key: value for key, value in row.items() if key != "metadata"}
    result.update(version=metadata.get("version"), compatible=metadata.get("compatible"),
                  description=metadata.get("description"), human_gated=metadata.get("human_gated"),
                  human_review_policies=metadata.get("human_review_policies"),
                  unknown_selection_fields=metadata.get("selection", {}).get("unknown_fields"))
    if isinstance(result["description"], str) and len(result["description"]) > 1024:
        result["description"] = result["description"][:1024]
        result["truncated_fields"] = ["description"]
    return result


def main(operation: str, argv: list[str]) -> int:
    parser = _Parser(prog="evidence-wiki pack " + operation)
    if operation == "catalog":
        parser.add_argument("action", choices=("init", "register", "list"))
        parser.add_argument("--catalog", required=True)
        parser.add_argument("--root", action="append", default=[], metavar="ID=PATH")
        parser.add_argument("--root-id")
        parser.add_argument("--id", dest="revision")
        parser.add_argument("--path")
        parser.add_argument("--scope")
        parser.add_argument("--derived-from")
        parser.add_argument("--derived-sha256")
    elif operation in {"list", "show", "decide"}:
        parser.add_argument("--target")
        parser.add_argument("--catalog")
        if operation == "show":
            parser.add_argument("selector", nargs="?")
            parser.add_argument("--path")
            parser.add_argument("--resource")
        elif operation == "list":
            parser.add_argument("--limit", type=int, default=64)
            parser.add_argument("--origin", choices=("bundled", "local", "installed"))
        else:
            parser.add_argument("--from-file", required=True)
    elif operation == "schemas":
        parser.add_argument("--schema-id")
    elif operation == "guide":
        parser.add_argument("--topic", choices=("selection", "authoring", "specification", "references"), default="selection")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    try:
        args = parser.parse_args(argv)
        code = 0
        if operation == "guide":
            from ._script_host import shared_assets_root

            relative = {"selection": "pack-selection.md", "authoring": "pack-authoring.md",
                        "specification": "pack-authoring-example.json", "references": "pack-assessment-references.json"}[args.topic]
            content = read_file(shared_assets_root(), "workspace-template/docs/" + relative).decode()
            if args.format == "text":
                print(content, end="")
                return 0
            result = {"schema_version": "evidence-pack-guide/v1", "content": content}
        elif operation == "schemas":
            from .pack_authoring_contracts import contract_index as authoring_index
            from .pack_authoring_contracts import schema_document as authoring_schema
            from .pack_authoring_contracts import schemas as authoring_schemas

            if args.schema_id:
                result = authoring_schema(args.schema_id) if args.schema_id in authoring_schemas() else schema_document(args.schema_id)
            else:
                result = contract_index()
                result["authoring"] = authoring_index()
                result["schema_ids"].extend(authoring_schemas())
        elif operation == "show":
            row, _ = select(args.selector, target=args.target, catalog=args.catalog, path=args.path, resource=args.resource)
            result = {"schema_version": "evidence-pack-inspection/v1", "pack": row}
            code = 0 if row["state"] == "available" else 1
        elif operation == "list":
            if not 1 <= args.limit <= 64:
                refuse("pack_list_limit")
            result = inventory(target=args.target, catalog=args.catalog)
            rows = [row for row in result["packs"] if args.origin is None or row["origin"] == args.origin]
            result["packs"] = [_summary(row) for row in rows[:args.limit]]
            result["bounds"] = {"total": len(rows), "returned": len(result["packs"]), "truncated": len(rows) > args.limit}
        elif operation == "decide":
            path = Path(args.from_file).expanduser().absolute()
            result = decide(read_file(path.parent, path.name), target=args.target, catalog=args.catalog)
            code = 2 if result["status"] == "unsupported" else 0
        else:
            from . import pack_catalog

            selected = Path(args.catalog)
            if args.action == "init":
                if any((args.root_id, args.revision, args.path, args.scope, args.derived_from, args.derived_sha256)):
                    refuse("catalog_options")
                roots = {}
                for item in args.root:
                    if "=" not in item:
                        refuse("catalog_root_argument")
                    key, path = item.split("=", 1)
                    if key in roots:
                        refuse("catalog_root_duplicate")
                    roots[key] = path
                result = pack_catalog.initialize(selected, roots)
            elif args.action == "register":
                if args.root or not all((args.root_id, args.revision, args.path, args.scope)) or bool(args.derived_from) != bool(args.derived_sha256):
                    refuse("catalog_options")
                derived = {"selector": args.derived_from, "tree_sha256": args.derived_sha256, "basis": "caller_declared"} if args.derived_from else None
                result = pack_catalog.register(selected, revision=args.revision, root_id=args.root_id,
                                               relative=args.path, scope=args.scope, derived_from=derived)
            else:
                if any((args.root, args.root_id, args.revision, args.path, args.scope, args.derived_from, args.derived_sha256)):
                    refuse("catalog_options")
                result = {"schema_version": "evidence-pack-catalog-view/v1", "packs": [_summary(row) for row in pack_catalog.entries(selected)]}
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode()) + 1 > MAX_OUTPUT:
            refuse("pack_output_bound")
        if args.format == "text" and operation in {"list", "show"}:
            for row in result.get("packs", [result.get("pack")]):
                metadata = row.get("metadata") or row
                print(f"{row['selector']}  version={metadata.get('version') or 'unknown'}  state={row['state']}")
                print(f"  Compatible: {metadata.get('compatible')}; human-gated: {metadata.get('human_gated')}; newer revision: unknown")
            if result.get("bounds", {}).get("truncated"):
                print("Inventory truncated; omitted entries are not negative evidence.")
        else:
            print(encoded)
        return code
    except (EvidenceWikiError, OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        print(json.dumps({"schema_version": "1.0", "error_code": getattr(error, "error_code", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE"),
                          "message": "Pack discovery request refused.", "recoverable": getattr(error, "recoverable", False),
                          "remediation": "Use an explicit current origin/revision and a supported local catalog.",
                          "details": getattr(error, "details", {"field": "pack_environment"})}))
        return getattr(error, "exit_code", 2)
