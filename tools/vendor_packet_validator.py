"""Reproduce the MIT-licensed validator closure from exact pinned local source bytes."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import symtable
from pathlib import Path

PARSER = argparse.ArgumentParser(description="Regenerate the pinned offline packet validator without importing producer code.")
PARSER.add_argument("--producer-root", type=Path, required=True)
PARSER.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / "workspace-template/scripts")
MODE = PARSER.add_mutually_exclusive_group()
MODE.add_argument("--check", action="store_true")
MODE.add_argument("--describe", action="store_true", help="Print the static closure for pin review without writing files.")
ARGS = PARSER.parse_args()
SOURCE = ARGS.producer_root.resolve()
DEST = ARGS.destination
PIN = json.loads(Path(__file__).with_name("packet-validator-spec.json").read_text(encoding="utf-8"))
for item in ([] if ARGS.describe else PIN["modules"]):
    candidate = SOURCE.joinpath(*item["module"].split(".")[1:]).with_suffix(".py")
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit("Producer source does not match the pinned validator: " + item["module"])

CACHE = {}
SELECTED = {}
IMPORTS = {}
MISSING = set()


def path_for(module):
    relative = Path(*module.split('.')[1:])
    path = SOURCE / relative.with_suffix('.py')
    return path if path.is_file() else SOURCE / relative / '__init__.py'


def internal(module):
    return module == 'llm_wiki_cli' or module.startswith('llm_wiki_cli.')


def resolved_imports(module, node):
    if isinstance(node, ast.Import):
        for alias in node.names:
            if internal(alias.name) and '.' in alias.name and alias.asname is None:
                raise ValueError(
                    f'Unaliased dotted producer import requires an explicit alias: '
                    f'{module}:{node.lineno}: {alias.name}'
                )
        return [(alias, alias.name, None) for alias in node.names]
    parent = '.'.join(module.split('.')[:-node.level]) if node.level else ''
    imported = '.'.join(part for part in (parent, node.module) if part)
    result = []
    for alias in node.names:
        if alias.name == '*':
            raise ValueError(f'Unbounded star import: {module}:{node.lineno}')
        candidate = imported + '.' + alias.name
        if internal(imported) and path_for(candidate).is_file():
            result.append((alias, candidate, None))
        else:
            result.append((alias, imported, alias.name))
    return result


def read(module):
    if module in CACHE:
        return CACHE[module]
    path = path_for(module)
    data = path.read_text(encoding="utf-8")
    tree = ast.parse(data)
    names = {}
    imports = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            names[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                for child in ast.walk(target):
                    if isinstance(child, ast.Name):
                        names[child.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias, origin, symbol in resolved_imports(module, node):
                name = alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)
                imports[name] = (node, alias, origin, symbol)
    CACHE[module] = data, tree, names, imports
    return CACHE[module]


def refs(node):
    code = 'from __future__ import annotations\n' + ast.unparse(node)
    table = symtable.symtable(code, '<node>', 'exec')
    result = set()
    source_names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}
    def visit(current):
        for symbol in current.get_symbols():
            if (symbol.is_global() and symbol.is_referenced()
                    and symbol.get_name() in source_names
                    and not getattr(symbol, 'is_comp_iter', lambda: False)()):
                result.add(symbol.get_name())
        for child in current.get_children():
            visit(child)
    visit(table)
    for child in ast.walk(node):
        annotation = child.annotation if isinstance(child, (ast.arg, ast.AnnAssign)) else child.returns if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if annotation:
            result.update(value.id for value in ast.walk(annotation) if isinstance(value, ast.Name))
    return result


def select_module_references(module, alias, origin, node):
    parents = {id(child): parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    for child in ast.walk(node):
        if not isinstance(child, ast.Name) or not isinstance(child.ctx, ast.Load) or child.id != alias:
            continue
        parent = parents.get(id(child))
        if not isinstance(parent, ast.Attribute) or parent.value is not child:
            raise ValueError(f'Unbounded module reference: {module}.{alias}:{child.lineno}')
        select(origin, parent.attr)


def select(module, name):
    data, tree, names, imports = read(module)
    if name in SELECTED.setdefault(module, set()):
        return
    if name in imports:
        node, alias, origin, symbol = imports[name]
        IMPORTS.setdefault(module, set()).add(name)
        if internal(origin):
            if symbol is None:
                return
            select(origin, symbol)
        return
    if name not in names:
        import builtins
        if not hasattr(builtins, name):
            MISSING.add((module, name))
        return
    SELECTED[module].add(name)
    node = names[name]
    for dependency in refs(node):
        select(module, dependency)
        if dependency in imports:
            _original, _alias, origin, symbol = imports[dependency]
            if internal(origin) and symbol is None:
                select_module_references(module, dependency, origin, node)
    for child in ast.walk(node):
        if not isinstance(child, (ast.Import, ast.ImportFrom)):
            continue
        for alias, origin, symbol in resolved_imports(module, child):
            if internal(origin):
                if symbol is not None:
                    select(origin, symbol)
                else:
                    select_module_references(module, alias.asname or alias.name, origin, node)


def local(module):
    return '_packet_vendor_' + '_'.join(module.split('.')[1:])


def import_binding(origin, symbol):
    """Name an isolated dependency without colliding with producer locals."""
    identity = origin + ':' + (symbol or '')
    return '_packet_import_' + hashlib.sha256(identity.encode()).hexdigest()[:16]


def render_bound_import(module, node):
    """Keep local bindings available after the sibling import window closes."""
    statements = []
    for alias, origin, symbol in resolved_imports(module, node):
        if internal(origin):
            name = alias.asname or alias.name
            statements.append(f'{name} = {import_binding(origin, symbol)}')
        else:
            narrowed = copy.deepcopy(node)
            narrowed.names = [alias]
            statements.append(ast.unparse(narrowed))
    return '; '.join(statements)


def deferred_self_annotations(node):
    """Normalize redundant self-reference quotes under deferred annotations."""
    if not isinstance(node, ast.ClassDef):
        return []
    result = []
    for member in node.body:
        if not isinstance(member, ast.AnnAssign):
            continue
        annotation = member.annotation
        if isinstance(annotation, ast.Constant) and annotation.value in {node.name, node.name + ' | None'}:
            result.append(annotation)
        elif (isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name)
                and annotation.value.id == 'Mapping'):
            result.extend(child for child in ast.walk(annotation.slice)
                          if isinstance(child, ast.Constant) and child.value == node.name)
    return result


def rewritten_source(module, data, node):
    """Copy pinned source with isolated imports and deferred annotations."""
    lines = data.encode().splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start_line = min([node.lineno, *(decorator.lineno for decorator in getattr(node, 'decorator_list', []))])
    start = offsets[start_line - 1]
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    piece = data.encode()[start:end]
    edits = []
    for child in ast.walk(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)) and any(
            internal(origin) for _alias, origin, _symbol in resolved_imports(module, child)
        ):
            first = offsets[child.lineno - 1] + child.col_offset - start
            last = offsets[child.end_lineno - 1] + child.end_col_offset - start
            edits.append((first, last, render_bound_import(module, child).encode()))
    for annotation in deferred_self_annotations(node):
        first = offsets[annotation.lineno - 1] + annotation.col_offset - start
        last = offsets[annotation.end_lineno - 1] + annotation.end_col_offset - start
        edits.append((first, last, annotation.value.encode()))
    for first, last, replacement in sorted(edits, reverse=True):
        piece = piece[:first] + replacement + piece[last:]
    return piece.decode()


select('llm_wiki_cli.services.context_packet', 'validate_context_packet')
if MISSING:
    raise SystemExit('Unresolved validation dependency: ' + repr(sorted(MISSING)))
license_text = PIN['license_text']
inventory = []
outputs = {}
for module in sorted(SELECTED):
    data, tree, names, imports = read(module)
    source_id = hashlib.sha256(data.encode()).hexdigest()
    preface = f'#!/usr/bin/env python3\n# ruff: noqa: I001, S101, UP007, UP035, UP045\n# Preserve the pinned upstream validation implementation.\n"""Offline packet validation from agent-wiki-cli {PIN["version"]}: {module}.\n\nOriginal source SHA-256: {source_id}\nOnly the validation dependency closure is included; source discovery, producer\nexecution, persistence, plugins and live reconciliation are excluded.\n\n{license_text}\n"""\n\nfrom __future__ import annotations\n\n'
    imported = []
    for name in sorted(IMPORTS.get(module, ())):
        node, alias, origin, symbol = imports[name]
        if internal(origin):
            if symbol is None:
                imported.append(f'import {local(origin)} as {name}')
            else:
                imported.append(f'from {local(origin)} import {symbol}' + (f' as {name}' if name != symbol else ''))
        else:
            narrowed = copy.deepcopy(node)
            narrowed.names = [alias]
            imported.append(ast.unparse(narrowed))
    selected_nodes = {id(names[name]) for name in SELECTED[module]}
    bindings = {}
    for node in tree.body:
        if id(node) not in selected_nodes:
            continue
        for child in ast.walk(node):
            if not isinstance(child, (ast.Import, ast.ImportFrom)):
                continue
            for _alias, origin, symbol in resolved_imports(module, child):
                if not internal(origin):
                    continue
                binding = import_binding(origin, symbol)
                if binding in names or binding in imports:
                    raise ValueError(f'Import binding conflicts with producer declaration: {module}.{binding}')
                bindings[binding] = (origin, symbol)
    for binding, (origin, symbol) in sorted(bindings.items()):
        if symbol is None:
            imported.append(f'import {local(origin)} as {binding}')
        else:
            imported.append(f'from {local(origin)} import {symbol} as {binding}')
    pieces = []
    for node in tree.body:
        if id(node) in selected_nodes:
            pieces.append(rewritten_source(module, data, node))
    rendered = preface + '\n'.join(imported) + '\n\n\n' + '\n\n\n'.join(pieces) + '\n'
    destination = DEST / (local(module) + '.py')
    outputs[destination] = rendered
    inventory.append({'module': module, 'sha256': source_id, 'symbols': sorted(SELECTED[module]), 'lines': len(rendered.splitlines())})
if ARGS.describe:
    print(json.dumps(inventory, indent=2))
    raise SystemExit(0)
if [{key: row[key] for key in ("module", "sha256", "symbols")} for row in inventory] != PIN["modules"]:
    raise SystemExit("Validation dependency closure differs from the reviewed pin")
if not ARGS.check:
    DEST.mkdir(parents=True, exist_ok=True)
for destination, rendered in outputs.items():
    if ARGS.check:
        if not destination.is_file() or destination.read_text(encoding="utf-8") != rendered:
            raise SystemExit('Vendored validator differs: ' + destination.name)
    else:
        destination.write_text(rendered, encoding="utf-8", newline="\n")
print(f"Pinned offline validator {'checked' if ARGS.check else 'generated'}: {len(inventory)} modules")
