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
PARSER.add_argument("--check", action="store_true")
ARGS = PARSER.parse_args()
SOURCE = ARGS.producer_root.resolve()
DEST = ARGS.destination
PIN = json.loads(Path(__file__).with_name("packet-validator-spec.json").read_text())
for item in PIN["modules"]:
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


def read(module):
    if module in CACHE:
        return CACHE[module]
    path = path_for(module)
    data = path.read_text()
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
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split('.')[0]] = (node, alias, alias.name, None)
        elif isinstance(node, ast.ImportFrom):
            parent = '.'.join(module.split('.')[:-node.level]) if node.level else ''
            imported = '.'.join(part for part in (parent, node.module) if part)
            for alias in node.names:
                candidate = imported + '.' + alias.name
                if node.module is None or path_for(candidate).is_file() and imported.startswith('llm_wiki_cli'):
                    imports[alias.asname or alias.name] = (node, alias, candidate, None)
                else:
                    imports[alias.asname or alias.name] = (node, alias, imported, alias.name)
    CACHE[module] = data, tree, names, imports
    return CACHE[module]


def refs(node):
    code = 'from __future__ import annotations\n' + ast.unparse(node)
    table = symtable.symtable(code, '<node>', 'exec')
    result = set()
    def visit(current):
        for symbol in current.get_symbols():
            if symbol.is_global() and symbol.is_referenced():
                result.add(symbol.get_name())
        for child in current.get_children():
            visit(child)
    visit(table)
    for child in ast.walk(node):
        annotation = child.annotation if isinstance(child, (ast.arg, ast.AnnAssign)) else child.returns if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if annotation:
            result.update(value.id for value in ast.walk(annotation) if isinstance(value, ast.Name))
    return result


def select(module, name):
    data, tree, names, imports = read(module)
    if name in SELECTED.setdefault(module, set()):
        return
    if name in imports:
        node, alias, origin, symbol = imports[name]
        IMPORTS.setdefault(module, set()).add(name)
        if origin.startswith('llm_wiki_cli'):
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
            if origin.startswith('llm_wiki_cli') and symbol is None:
                found = False
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id == dependency:
                        select(origin, child.attr)
                        found = True
                if not found:
                    raise ValueError(f'Unbounded module reference: {module}.{dependency}')


def local(module):
    return '_packet_vendor_' + '_'.join(module.split('.')[1:])


select('llm_wiki_cli.services.context_packet', 'validate_context_packet')
if not ARGS.check:
    DEST.mkdir(parents=True, exist_ok=True)
license_text = PIN['license_text']
inventory = []
for module in sorted(SELECTED):
    data, tree, names, imports = read(module)
    source_id = hashlib.sha256(data.encode()).hexdigest()
    preface = f'# ruff: noqa: I001, S101, UP007, UP035, UP045\n# Preserve the pinned upstream validation implementation.\n"""Offline packet validation from agent-wiki-cli 1.8.0: {module}.\n\nOriginal source SHA-256: {source_id}\nOnly the validation dependency closure is included; source discovery, producer\nexecution, persistence, plugins and live reconciliation are excluded.\n\n{license_text}\n"""\n\nfrom __future__ import annotations\n\n'
    imported = []
    for name in sorted(IMPORTS.get(module, ())):
        node, alias, origin, symbol = imports[name]
        if origin.startswith('llm_wiki_cli'):
            if symbol is None:
                imported.append(f'import {local(origin)} as {name}')
            else:
                imported.append(f'from {local(origin)} import {symbol}' + (f' as {name}' if name != symbol else ''))
        else:
            narrowed = copy.deepcopy(node)
            narrowed.names = [alias]
            imported.append(ast.unparse(narrowed))
    selected_nodes = {id(names[name]) for name in SELECTED[module]}
    pieces = []
    for node in tree.body:
        if id(node) in selected_nodes:
            start = min([node.lineno, *(decorator.lineno for decorator in getattr(node, 'decorator_list', []))])
            pieces.append('\n'.join(data.splitlines()[start - 1:node.end_lineno]))
    rendered = preface + '\n'.join(imported) + '\n\n\n' + '\n\n\n'.join(pieces) + '\n'
    destination = DEST / (local(module) + '.py')
    if ARGS.check:
        if not destination.is_file() or destination.read_text() != rendered:
            raise SystemExit('Vendored validator differs: ' + destination.name)
    else:
        destination.write_text(rendered)
    inventory.append({'module': module, 'sha256': source_id, 'symbols': sorted(SELECTED[module]), 'lines': len(rendered.splitlines())})
if [{key: row[key] for key in ("module", "sha256", "symbols")} for row in inventory] != PIN["modules"]:
    raise SystemExit("Validation dependency closure differs from the reviewed pin")
print(f"Pinned offline validator {'checked' if ARGS.check else 'generated'}: {len(inventory)} modules")
