#!/usr/bin/env python3
"""Validate built distributions the same way in CI and at release time.

One validator, two callers. The CI ``package`` job and the release gate used to
carry their own copies of the installed-wheel smoke, which had already drifted:
the release copy probed the round-trip YAML dependency and the CI copy did not,
and neither ever installed anything derived from the sdist. This tool is the
single path both invoke:

1. **Archive membership.** Every member of the wheel and the sdist is checked
   against the public packaging policy: required assets must be present and
   internal material (reports, planning documents, caches, environments, build
   output) must be absent. The sdist's broad ``include`` list is not evidence of
   a leak on its own; the archive is.
2. **Installed wheel.** The wheel is installed into a fresh virtual environment
   and exercised from there: package identity, that imports resolve inside the
   fresh install rather than the checkout, pinned runtime dependencies and the
   YAML round-trip behaviour this package relies on, deploy, pack refresh,
   orchestration start/next/status, the required-asset manifest, the result
   schema, and the managed failure/resume smoke.
3. **Installed sdist.** The sdist is unpacked into an isolated directory, a
   wheel is built from it, and that wheel goes through exactly the same
   installed checks. A distribution that only works because the wheel was built
   from the checkout fails here.

The summary printed at the end records the artifact names, their SHA-256
digests, the version, and each check's result, so a release can cite the exact
bytes it verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import zipfile
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import resources  # noqa: E402 - the checkout's manifest names what the archives must carry.
from tools._qualification_process import CommandRunner, interruptible, positive_seconds  # noqa: E402

_RUNNER = CommandRunner()
_OPTIONS = argparse.Namespace(command_timeout=900, journey_timeout=5400, case_timeout=1200,
                              journey_workers=1, active_artifact="artifacts", evidence=None)

#: Every asset the package contract requires, as archive-relative paths.
REQUIRED_ASSET_PATHS = tuple(
    relative for group in resources.required_asset_manifest().values() for relative in group
)

#: Members that must never ship, as path prefixes relative to the archive's
#: project root. Matched against the sdist's members after its leading
#: ``<name>-<version>/`` directory is stripped, and against wheel members as-is.
FORBIDDEN_PREFIXES = (
    ".git/",
    ".github/",
    ".llm-wiki/",
    ".pytest_cache/",
    ".research-cache/",
    ".ruff_cache/",
    ".venv/",
    "build/",
    "dist/",
    "docs/CR/",
    "docs/llm_wiki/",
    "htmlcov/",
    "pilot-workspaces/",
    "reports/",
    "temp/",
    "temp_codebase_project/",
    "temp_workspace/",
    "venv/",
)

#: Individual top-level files that are maintainer-local and must not ship.
FORBIDDEN_TOP_LEVEL_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "RELEASING.md",
    ".coverage",
    ".env",
    ".research-handoff-secret",
)

#: Path components that mark cache or environment residue anywhere in a tree.
FORBIDDEN_COMPONENTS = ("__pycache__", ".venv", ".research-cache")
FORBIDDEN_SUFFIXES = (".pyc", ".pyo")

#: Members every wheel must carry: the package plus every asset the contract names.
REQUIRED_WHEEL_MEMBERS = (
    "evidence_wiki/__init__.py",
    "evidence_wiki/cli.py",
    "evidence_wiki/agent.py",
    "evidence_wiki/frameworks.py",
    "evidence_wiki/pi_bridge.py",
    "evidence_wiki/_pack_io.py",
    "evidence_wiki/pack_catalog.py",
    "evidence_wiki/pack_commands.py",
    "evidence_wiki/pack_decisions.py",
    "evidence_wiki/pack_discovery.py",
    "evidence_wiki/source_commands.py",
    "evidence_wiki/source_contracts.py",
    "evidence_wiki/source_delivery.py",
    "evidence_wiki/source_inputs.py",
    "evidence_wiki/source_inspection.py",
    "evidence_wiki/source_probe.py",
    "evidence_wiki/source_readiness.py",
    "evidence_wiki/source_routing.py",
    "evidence_wiki/host_capabilities.py",
    "evidence_wiki/agent_resources.py",
    "evidence_wiki/_agent_catalog.py",
    "evidence_wiki/onboarding_schemas.py",
    "evidence_wiki/onboarding_contract.py",
    *("evidence_wiki/" + name + ".py" for name in (
        "onboarding", "onboarding_mcp", "onboarding_tools", "_onboarding_scope", "_onboarding_operations",
        "extension_contracts", "extension_commands", "pack_migrations", "pack_composition", "fleet_revisions",
        "host_transitions", "local_journal", "local_artifacts", "runtime_identity", "native_instructions", "capability_recipes")),
    *(f"evidence_wiki/assets/{relative}" for relative in REQUIRED_ASSET_PATHS),
)

#: Members every sdist must carry: what a from-source build and its checks need,
#: including every asset the wheel built from it will have to force-include.
REQUIRED_SDIST_MEMBERS = (
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "src/evidence_wiki/__init__.py",
    "tools/smoke_installed_orchestration.py",
    "tools/validate_installed_artifacts.py",
    "tools/_qualification_process.py",
    "tools/qualify_journeys.py",
    "tools/sync_agent_resources.py",
    "tools/probe_installed_extensions.py",
    "tests/_docx_fixture.py",
    "tests/_publication_fixture.py",
    "tests/fixtures/fake_codex_cli.py",
    "tests/fixtures/madrid-autonomo-workspace/AGENTS.md",
    "examples/urban-heat-resilience-workspace/AGENTS.md",
    *REQUIRED_ASSET_PATHS,
)

SDIST_ROOT_RE = re.compile(r"^[^/]+/")


class ValidationError(SystemExit):
    """A failed check; the message names the artifact and the reason."""

    def __init__(self, message: str) -> None:
        super().__init__(f"artifact validation failed: {message}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    """Return exactly one wheel and one sdist, refusing an ambiguous directory."""
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValidationError(
            f"{dist_dir} must contain exactly one wheel and one sdist; "
            f"found {[path.name for path in wheels]} and {[path.name for path in sdists]}"
        )
    return wheels[0], sdists[0]


def wheel_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def sdist_members(path: Path) -> list[str]:
    """Members relative to the project root, with the ``<name>-<version>/`` prefix removed."""
    with tarfile.open(path, "r:gz") as archive:
        names = [member.name for member in archive.getmembers() if member.isfile()]
    stripped: list[str] = []
    for name in names:
        if SDIST_ROOT_RE.match(name) is None:
            raise ValidationError(f"{path.name}: sdist member is not below a root directory: {name}")
        stripped.append(SDIST_ROOT_RE.sub("", name, count=1))
    return sorted(stripped)


def forbidden_members(members: list[str]) -> list[str]:
    """Members the public packaging policy forbids, in archive order."""
    flagged: list[str] = []
    for member in members:
        posix = PurePosixPath(member)
        if member.startswith(FORBIDDEN_PREFIXES):
            flagged.append(member)
        elif member in FORBIDDEN_TOP_LEVEL_FILES:
            flagged.append(member)
        elif any(component in FORBIDDEN_COMPONENTS for component in posix.parts):
            flagged.append(member)
        elif posix.suffix in FORBIDDEN_SUFFIXES:
            flagged.append(member)
    return flagged


def missing_members(members: list[str], required: tuple[str, ...]) -> list[str]:
    present = set(members)
    return [member for member in required if member not in present]


def check_archive_membership(wheel: Path, sdist: Path) -> dict[str, object]:
    wheel_names = wheel_members(wheel)
    sdist_names = sdist_members(sdist)
    problems: list[str] = []
    for label, names, required in (
        (wheel.name, wheel_names, REQUIRED_WHEEL_MEMBERS),
        (sdist.name, sdist_names, REQUIRED_SDIST_MEMBERS),
    ):
        flagged = forbidden_members(names)
        if flagged:
            problems.append(f"{label} ships internal material: {', '.join(flagged[:10])}")
        missing = missing_members(names, required)
        if missing:
            problems.append(f"{label} is missing required members: {', '.join(missing)}")
    if problems:
        raise ValidationError("; ".join(problems))
    return {"wheel_members": len(wheel_names), "sdist_members": len(sdist_names)}


def command_label(argv):
    if "-c" in argv:
        program = argv[argv.index("-c") + 1]
        return next((key.lower().removesuffix("_probe") for key, value in globals().items()
                     if key.endswith("_PROBE") and value == program), "python-command")
    if "-m" in argv:
        return argv[argv.index("-m") + 1]
    if "python" in Path(argv[0]).name:
        return next((Path(arg).name for arg in argv[1:] if arg.endswith(".py")), "python-command")
    return " ".join([Path(argv[0]).name, *argv[1:3]])


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout=None, label=None, stream_stderr=False) -> str:
    environment = dict(os.environ if env is None else env)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "GIT_DIR", "GIT_WORK_TREE"):
        environment.pop(key, None)
    environment.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    stage = _OPTIONS.active_artifact + "/" + (label or command_label(argv))
    try:
        process = _RUNNER.run(argv, label=stage, cwd=cwd, env=environment,
                              timeout=timeout or _OPTIONS.command_timeout, stream_stderr=stream_stderr)
    except subprocess.TimeoutExpired as error:
        raise ValidationError(f"{stage} exceeded {error.timeout:g}s; inspect retained command logs") from error
    if process.returncode != 0:
        raise ValidationError(
            f"{stage} returned {process.returncode}\nstdout (tail):\n{process.stdout[-8192:]}\nstderr (tail):\n{process.stderr[-8192:]}"
        )
    return process.stdout


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_cli(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "evidence-wiki.exe"
    return venv / "bin" / "evidence-wiki"


def create_venv_with_wheel(root: Path, wheel: Path) -> Path:
    venv = root / "venv"
    run([sys.executable, "-m", "venv", str(venv)])
    python = venv_python(venv)
    run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", str(wheel)])
    return venv


def fixture_members():
    members = ["tools/smoke_installed_orchestration.py", "tools/qualify_journeys.py",
               "workspace-template/workspace-system.yml",
               "tools/probe_installed_extensions.py", "tests/_docx_fixture.py",
               "tools/_journey_cases.py", "tools/_journey_driver.py", "tools/_journey_authoring.py", "tools/_qualification_process.py",
               "tests/fixtures/onboarding-journeys/cases.json", "tests/_computation_fixture.py", "tests/fixtures/fake_codex_cli.py",
               "tests/fixtures/strict-evidence/review-cases.json",
               "tests/fixtures/workspace-init-profile.yml", "tests/_publication_fixture.py",
               *["tests/_" + name + "_fixture.py" for name in
                 ("execution", "usage", "snapshot", "temporal", "market", "historical", "simulation", "assessment")]]
    packet_root = REPO_ROOT / "tests/fixtures/codebase-intake/native-packets"
    members.extend(path.relative_to(REPO_ROOT).as_posix() for path in packet_root.rglob("*") if path.is_file())
    return sorted(members)


def isolated_fixtures(scratch: Path) -> Path:
    """Copy explicitly named qualification inputs, without source-package imports."""
    root = scratch / "qualification-inputs"
    members = fixture_members()
    for name in members:
        source, target = REPO_ROOT / name, root / name
        if source.is_symlink() or not source.is_file() or any(parent.is_symlink() for parent in source.parents if parent.is_relative_to(REPO_ROOT)):
            raise ValidationError("unsafe or missing qualification input: " + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    hashes = {name: sha256_of(root / name) for name in sorted(members)}
    (root / "inputs.json").write_text(json.dumps(hashes, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return root


INSTALLED_PROBE = textwrap.dedent(
    '''
    import json
    import sys
    from importlib.metadata import version
    from io import StringIO
    from pathlib import Path

    import evidence_wiki
    import pypdf
    import ruamel.yaml
    from evidence_wiki import resources
    from evidence_wiki.orchestration import ORCHESTRATION_RESULT_SCHEMA

    expected, pack_refresh_path = sys.argv[1], sys.argv[2]

    if evidence_wiki.__version__ != version("evidence-wiki"):
        raise SystemExit("Installed package metadata and module versions differ")
    if expected and evidence_wiki.__version__ != expected:
        raise SystemExit(
            f"Installed wheel version {evidence_wiki.__version__!r} does not match expected version {expected!r}"
        )
    installed_path = Path(evidence_wiki.__file__).resolve()
    if "site-packages" not in installed_path.parts:
        raise SystemExit(f"EvidenceWiki was not imported from the installed wheel: {installed_path}")
    if not pypdf.__version__.startswith("6."):
        raise SystemExit(f"Installed wheel resolved unsupported pypdf version {pypdf.__version__!r}")
    if not ruamel.yaml.__version__.startswith("0.19."):
        raise SystemExit(f"Installed wheel resolved unsupported ruamel.yaml version {ruamel.yaml.__version__!r}")

    round_trip_yaml = ruamel.yaml.YAML(typ="rt", pure=True)
    round_trip_yaml.preserve_quotes = True
    round_trip_source = '# operator note\\nquoted: "preserve me"\\n'
    round_trip_document = round_trip_yaml.load(round_trip_source)
    round_trip_output = StringIO()
    round_trip_yaml.dump(round_trip_document, round_trip_output)
    if round_trip_output.getvalue() != round_trip_source:
        raise SystemExit("Installed ruamel.yaml dependency did not preserve YAML comments and quotes")

    pack_refresh = json.loads(Path(pack_refresh_path).read_text(encoding="utf-8"))
    if pack_refresh.get("status") != "no_changes" or pack_refresh.get("log_appended") is not False:
        raise SystemExit(f"Installed-wheel bundled pack refresh was not a no-op: {pack_refresh!r}")
    if not (Path(pack_refresh["target"]) / "domain-packs" / ".evidence-wiki-state.yml").is_file():
        raise SystemExit("Installed-wheel pack initialization did not create lifecycle state")

    properties = ORCHESTRATION_RESULT_SCHEMA["properties"]
    if properties["schema_version"] != {"type": "string", "enum": ["1.0"]}:
        raise SystemExit("Installed wheel contains an incompatible orchestration result schema")
    if any("type" not in definition for definition in properties.values()):
        raise SystemExit("Installed wheel result schema contains an untyped property")

    with resources.assets_root() as assets:
        missing = resources.missing_required_assets(assets)
        if missing:
            raise SystemExit(f"Installed wheel is missing required assets: {missing}")
        required = (
            "workspace-template/docs/orchestration.md",
            "workspace-template/docs/orchestrator-handoff.md",
            "workspace-template/docs/run-controller.md",
            "workspace-template/scripts/_domain_pack_lifecycle.py",
            "workspace-template/skills/research-run.md",
            "workspace-template/skills/research-discover.md",
            "workspace-template/skills/research-acquire.md",
            "workspace-template/skills/research-verify.md",
            "orchestrator/skills/research-orchestrate.md",
        )
        absent = [relative for relative in required if not (assets / relative).is_file()]
        if absent:
            raise SystemExit(f"Installed wheel is missing orchestration assets: {absent}")
    print(json.dumps({"version": evidence_wiki.__version__, "installed_from": str(installed_path)}))
    '''
)


NATIVE_INITIALIZATION_PROBE = textwrap.dedent(r'''
    import copy
    import hashlib
    import json
    import stat
    import subprocess
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    import yaml
    import evidence_wiki

    cli, fixture, root = map(Path, sys.argv[1:])
    assert Path(evidence_wiki.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    root.mkdir()
    template = yaml.safe_load(fixture.read_text())
    declaration = {'acquisition': 'delegated', 'acquirer_agent_id': 'external-acquirer', 'max_attempts_per_request': 3}

    def initialize(profile, *flags):
        return subprocess.run([str(cli), 'init', '--profile', str(profile), '--scope-root', str(root), *flags],
                              text=True, capture_output=True, timeout=60)

    def snapshot():
        return {str(p.relative_to(root)): (stat.S_IMODE(p.stat().st_mode), p.stat().st_ino, p.stat().st_mtime_ns,
                    p.read_bytes() if p.is_file() else None) for p in [root, *root.rglob('*')]}

    for name, nested in [('direct', False), ('nested', True), ('planned', None)]:
        workspace = root/name
        if nested is None:
            def command(*args):
                result = subprocess.run([str(cli), 'agent', *map(str, args)], cwd=root,
                    text=True, capture_output=True, timeout=300)
                assert result.returncode == 0, result.stdout + result.stderr
                return json.loads(result.stdout)
            schema = command('plan-schemas', '--schema-id', 'evidence-research-setup/v1')
            assert 'orchestration' in schema['properties']['decisions']['properties']
            guide = command('plan-guide')['content'].split('### Complete delegated setup example', 1)[1]
            request = json.loads(guide.split('```json\n', 1)[1].split('\n```', 1)[0])
            request['request']['payload']['target'] = {'writable_root':str(root), 'relative_path':name}
            request['request']['payload']['authority']['writable_roots'] = [str(root)]
            source, saved = root/'request.json', root/'plan.json'
            source.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            plan = command('plan', '--from-file', source, '--output', saved)
            assert plan['setup_ready'] and not plan['research_ready'] and not plan['actions_executed']
            before = snapshot()
            assert command('plan-check', '--from-file', saved)['status'] == 'current'
            assert snapshot() == before and not workspace.exists()
            applied = command('apply', '--from-file', saved)
            assert applied['setup_ready'] and not applied['research_complete'] and not applied['claims_verified']
            before = {str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
            assert command('apply', '--from-file', saved)['transaction_id'] == applied['transaction_id']
            assert before == {str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
            assert yaml.safe_load((workspace/'research.yml').read_bytes()) == plan['initialization']['effective_config']
            frozen = (workspace/'docs/research-requirements.json').read_bytes()
            assert json.loads(frozen)['decisions']['orchestration'] == declaration
            policy = plan['initialization']['effective_config']['strict_evidence']
            assert policy['instructions']['docs/research-requirements.json'] == 'sha256:'+hashlib.sha256(frozen).hexdigest()
            selected = request['request']['payload']['strict_evidence']
            assert (policy['policy_id'], policy['revision'], policy['assurance']) == (
                selected['policy_id'], selected['policy_revision'], selected['assurance'])
            metadata = yaml.safe_load((workspace/'wiki/questions/needs-evidence.md').read_text(encoding='utf-8').split('---')[1])['metadata']
            assert metadata['original_text'] == request['request']['payload']['questions'][0]['text']
            assert plan['questions']['rows'][0]['original_ids'] == ['needs-evidence']
        else:
            profile = copy.deepcopy(template)
            profile['workspace_init'].update(target_path=str(workspace),
                raw={'immutable':True, 'source_roots':['raw/data']},
                questions=[{'id':'needs-evidence', 'question':'What does the supplier quote?', 'priority':'high'}])
            destination = profile['workspace_init'].setdefault('research_yml', {}) if nested else profile['workspace_init']
            destination['orchestration'] = declaration
            path = root/(name+'.yml')
            path.write_text(yaml.safe_dump(profile, sort_keys=False))
            before = snapshot()
            preview = initialize(path, '--dry-run')
            assert preview.returncode == 0, preview.stderr
            assert snapshot() == before
            created = initialize(path)
            assert created.returncode == 0, created.stderr
        config_bytes = (workspace/'research.yml').read_bytes()
        config = yaml.safe_load(config_bytes)
        assert config['orchestration'] == declaration
        assert config['integrations']['acquisition']['enabled'] is False

        def script(module, *args):
            result = subprocess.run([sys.executable, '-B', str(workspace/'scripts'/(module+'.py')),
                '--project-root', str(workspace), *args, '--format', 'json'], text=True, capture_output=True, timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        script('question_claim', 'claim', '--slug', 'needs-evidence', '--agent-id', 'research-agent')
        request = script('source_requests', 'add', '--kind', 'other', '--query-or-identifier', 'Supplier quote',
            '--rationale', 'The question needs retained evidence.', '--priority', 'high', '--question-slug', 'needs-evidence')
        request_id = request['request']['request_id']
        script('question_resolve', 'block', '--slug', 'needs-evidence', '--agent-id', 'research-agent',
            '--blocked-reason', 'Evidence has not arrived.', '--request-id', request_id)
        session = script('orchestration_controller', 'start', '--orchestration-id', 'native-profile', '--agent-id', 'parent')
        assert session['acquisition_mode'] == 'delegated' and session['acquirer_agent_id'] == declaration['acquirer_agent_id']
        assert session['max_attempts_per_request'] == 3
        assert session['provider_policy']['acquisition'] == {'enabled':False, 'providers':[]}
        order = script('orchestration_controller', 'next', '--orchestration-id', 'native-profile')
        assert order['phase'] == 'acquisition' and order['acquisition_mode'] == 'delegated'
        assert order['assigned_agent_id'] == declaration['acquirer_agent_id']
        assert order['scope']['request_ids'] == [request_id]
        payload = workspace/'raw/data/quote.csv'
        payload.write_text('supplier,currency,unit_price\nacme,EUR,12.50\nglobex,EUR,13.75\n', encoding='utf-8', newline='\n')
        provenance = {'origin_url':'https://example.test/quote.csv', 'license':'CC-BY-4.0',
            'retrieved_at':datetime.now(timezone.utc).isoformat(), 'retrieved_by':declaration['acquirer_agent_id'],
            'request_id':request_id, 'checksum':'sha256:'+hashlib.sha256(payload.read_bytes()).hexdigest()}
        payload.with_name(payload.name+'.provenance.yml').write_text(yaml.safe_dump(provenance))
        script('source_inventory', '--report')
        script('normalize_sources', '--all')
        records = [json.loads(line) for line in (workspace/config['sources']['manifest_path']).read_text().splitlines() if line.strip()]
        source_id = next(row['id'] for row in records if 'raw/data/quote.csv' in row.get('raw_paths', []))
        script('source_requests', 'fulfill', '--request-id', request_id, '--source-id', source_id)
        script('question_resolve', 'reopen', '--slug', 'needs-evidence', '--agent-id', declaration['acquirer_agent_id'],
            '--source-id', source_id, '--request-id', request_id)
        result_file = root/(name+'-result.json')
        result_file.write_text(json.dumps({'schema_version':'1.0', 'action_id':order['action_id'], 'outcome':'completed',
            'summary':'Delivered the requested quote.', 'artifacts':['raw/data/quote.csv']}))
        completed = script('orchestration_controller', 'submit', '--orchestration-id', 'native-profile',
            '--action-id', order['action_id'], '--result-file', str(result_file))
        assert completed['last_completed_action_id'] == order['action_id'] and completed['phase'] == 'research'
        assert (workspace/'research.yml').read_bytes() == config_bytes

    invalid = [False, {'unsupported':{}}, {'orchestration':{'acquisition':'delegated'}},
        {'orchestration':declaration, 'integrations':{'acquisition':{'enabled':True, 'providers':['arxiv']}}}]
    for index, value in enumerate(invalid):
        for existing in (False, True):
            workspace = root/('refused-'+str(index)+'-'+str(existing))
            if existing:
                workspace.mkdir()
            profile = copy.deepcopy(template)
            profile['workspace_init'].update(target_path=str(workspace), research_yml=value)
            path = root/'refused.yml'
            path.write_text(yaml.safe_dump(profile, sort_keys=False))
            before = snapshot()
            for flags in ([], ['--dry-run']):
                refused = initialize(path, *flags)
                assert refused.returncode == 2, refused.stdout + refused.stderr
                assert 'Traceback' not in refused.stderr
                assert snapshot() == before
    print(json.dumps({'native_initialization':{'direct_profile':'passed', 'nested_profile':'passed',
        'no_write_refusals':'passed', 'controller_submission':'passed'},
        'planned_delegation':{'schema_discovery':'passed', 'plan_check':'passed', 'apply_replay':'passed',
            'strict_bindings':'passed', 'controller_submission':'passed'}}))
''')


PUBLICATION_PROBE = textwrap.dedent(
    '''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    import yaml
    import evidence_wiki
    from evidence_wiki import Workspace
    from evidence_wiki.errors import PublicationError, RevisionError

    cli, fixture_path, profile_path, target = map(Path, sys.argv[1:])
    assert Path(evidence_wiki.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    spec = importlib.util.spec_from_file_location("publication_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    profile = yaml.safe_load(profile_path.read_text())
    profile["workspace_init"]["target_path"] = str(target)
    profile["workspace_init"]["questions"] = [{"id": "vendor-product-spec", "question": "What is the product spec?", "priority": "high"}]
    local_profile = target.parent / "publication-profile.yml"
    local_profile.write_text(yaml.safe_dump(profile))
    subprocess.run([str(cli), "init", "--profile", str(local_profile)], check=True, capture_output=True, text=True, timeout=60)
    fixture.write_ship_ready_vendor_fixture(target)
    question = target / "wiki/questions/vendor-product-spec.md"
    (question.parent / "awaiting-review.md").write_text(question.read_text().replace("status: answered", "status: human_review"))
    def contents():
        return {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    before = contents()
    command = [str(cli), "publication", "--target", str(target), "--format", "json", "--question"]
    with Workspace.open(target) as workspace:
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            try:
                workspace.publish_selected(["vendor-product-spec"])
            except RevisionError as error:
                assert error.error_code == "EVIDENCE_REVISION_UNSUPPORTED"
            else:
                raise AssertionError("unsupported capture was accepted")
            result = subprocess.run([*command, "vendor-product-spec"], capture_output=True, text=True, timeout=60)
            assert result.returncode == 2 and json.loads(result.stderr)["error_code"] == "EVIDENCE_REVISION_UNSUPPORTED"
            outcome = "unsupported-refusal-verified"
        else:
            document = workspace.publish_selected(["vendor-product-spec"])
            result = subprocess.run([*command, "vendor-product-spec"], capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr + result.stdout
            rendered = json.loads(result.stdout)
            assert document["verdict"] == "ship"
            for key in ("revision", "producer_id", "gate_scope", "question_slugs", "verdict"):
                assert document[key] == rendered[key], key
            assert document["export"]["questions"] == rendered["export"]["questions"]
            try:
                workspace.publish_selected(["absent"])
            except PublicationError as error:
                refusal = {key: getattr(error, key) for key in ("error_code", "message", "details", "recoverable", "remediation")}
            else:
                raise AssertionError("unknown question was accepted")
            result = subprocess.run([*command, "absent"], capture_output=True, text=True, timeout=60)
            rendered_refusal = json.loads(result.stderr)
            assert result.returncode == 2
            assert all(rendered_refusal.get(key, {}) == value for key, value in refusal.items())
            outcome = "passed"
    assert before == contents(), "publication changed the live workspace"
    print(json.dumps({"selected_publication": outcome}))
    '''
)


PACKET_PROBE = textwrap.dedent(
    '''
    import hashlib
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    import yaml
    from evidence_wiki import Workspace, contract

    cli, fixtures, root = map(Path, sys.argv[1:])
    profile = "qualified_context_packet/v1"
    record = {"id": "codebase:sample", "kind": "codebase_architecture", "raw_paths": [],
        "raw_fingerprint": "sha256:synthetic", "metadata": {}}
    folder = root / "sources/code_wikis/codebase--sample"
    folder.mkdir(parents=True)
    config = {"sources": {"manifest_path": "sources/manifest.jsonl"}, "integrations": {"codebase_analysis": {"intake_profile": profile}}}
    (root / "research.yml").write_text(yaml.safe_dump(config))
    (root / "sources/manifest.jsonl").write_text(json.dumps(record) + "\\n")
    normalized = root / "sources/normalized/codebase--sample.md"
    normalized.parent.mkdir()
    headings = ["Citation Metadata", "Abstract", "Outline", "Extracted Text", "Figures and Tables",
        "Links", "Raw Source Paths", "Parse Warnings"]
    body = "\\n".join("\\n## " + heading + "\\n\\n- None recorded.\\n" for heading in headings)

    def write_record(report, producer):
        frontmatter = {"type": "normalized_source", "normalized_format": 1, "source_id": record["id"],
            "source_kind": record["kind"], "status": "content_extracted", "evidence_usable": True,
            "created": "2026-09-10", "updated": "2026-09-10", "raw_paths": [],
            "manifest_path": "sources/manifest.jsonl", "raw_fingerprint": record["raw_fingerprint"],
            "normalizer": {"name": producer, "version": "1"}, "parse_warnings": []}
        if report is not None:
            frontmatter["qualified_context"] = report
        normalized.write_text("---\\n" + yaml.safe_dump(frontmatter) + "---\\n" + body)

    cases = []
    with Workspace.open(root) as workspace:
        assert workspace.normalize.profiles() == contract()["intake_profiles"]
        for fixture in sorted(fixtures.glob("*.json")):
            packet = fixture.read_bytes()
            manifest = {"schema_version": "1", "artifact_kind": "codebase_evidence", "source_id": record["id"],
                "intake_profile": profile, "packet_path": "packet.json", "generated_at": "2026-09-10T00:00:00Z",
                "producer": {"name": "agent-wiki-cli", "version": "1.8.0"},
                "invocation": {"executed_by": "external_worker", "argv": ["llm-wiki", "context"],
                    "plugins_enabled": False, "hooks_enabled": False, "network_access": False},
                "files": [{"path": "packet.json", "size_bytes": len(packet), "sha256": hashlib.sha256(packet).hexdigest()}]}
            (folder / "packet.json").write_bytes(packet)
            (folder / "artifact-manifest.json").write_text(json.dumps(manifest))
            report = workspace.normalize.validate_packet(record["id"])
            result = subprocess.run([str(cli), "normalize", "packet", "--target", str(root), "--source-id", record["id"]],
                capture_output=True, text=True, timeout=60)
            assert json.loads(result.stdout) == report, result.stderr
            if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
                assert not report["valid"] and report["reason"] == "delivery_capture_unsupported"
                assert result.returncode == 1
            else:
                assert report["valid"] and report["policy_satisfied"] and result.returncode == 0, report
                assert report["packet_id"] == json.loads(packet)["packet_id"]
                assert report["worker_authentication"] == "not_established"
                for producer in ["normalize_sources.py", "external-tool"]:
                    write_record(report, producer)
                    verification = workspace.normalize.verify()
                    assert verification["overall_result"] == "verified", verification
                    checked = subprocess.run([str(cli), "normalize", "verify", "--target", str(root), "--format", "json"],
                        capture_output=True, text=True, timeout=60)
                    assert checked.returncode == 0 and json.loads(checked.stdout)["overall_result"] == "verified", checked.stderr
                    write_record(None, producer)
                    assert workspace.normalize.verify()["overall_result"] == "not_verified"
                write_record(report, "external-tool")
            cases.append(fixture.stem)
        config["integrations"]["codebase_analysis"]["require_live_reconciliation"] = True
        (root / "research.yml").write_text(yaml.safe_dump(config))
        report = workspace.normalize.validate_packet(record["id"])
        assert not report.get("policy_satisfied")
    assert cases, "native packet corpus is absent"
    print(json.dumps({"qualified_packet_intake": cases, "required_live_policy": "refused"}))
    '''
)


EXECUTION_PROBE = textwrap.dedent(
    '''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    from evidence_wiki import Workspace, contract

    cli, fixture, root = map(Path, sys.argv[1:])
    spec = importlib.util.spec_from_file_location("laboratory_fixture", fixture)
    data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(data)
    authority = root.parent / "laboratory-authority.json"
    data.host_policy(authority)
    os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"] = str(authority.resolve())
    _config, record, folder = data.workspace(root)
    with Workspace.open(root) as workspace:
        report = workspace.normalize.validate_execution(record["id"])
        checked = subprocess.run([str(cli), "normalize", "execution", "--target", str(root), "--source-id", record["id"]],
                                 capture_output=True, text=True, timeout=60)
        cli_report = json.loads(checked.stdout)
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            assert not report["valid"] and checked.returncode == 1, report
            calculation = "unsupported_capture_refused"
        else:
            assert report["valid"] and report["verification"]["eligible"], report
            assert cli_report["valid"] and cli_report["verification"]["eligible"] and checked.returncode == 0, cli_report
            assert [item["outcome"] for item in report["records"]] == ["failed", "inconclusive", "passed"]
            files = {path.name: path.read_bytes() for path in folder.iterdir()}
            for observation in report["records"]:
                if observation["record_type"] == "observation":
                    assert data.independently_recalculate(files, observation["payload"])[2] == observation["outcome"]
            policy = json.loads(authority.read_bytes())
            policy["revoked_keys"] = ["evaluator-key"]
            authority.write_bytes(data.canonical(policy))
            revoked = workspace.normalize.validate_execution(record["id"])
            assert revoked["valid"] and not revoked["verification"]["eligible"]
            (folder / "result.txt").write_bytes(b"changed original bytes")
            assert not workspace.normalize.validate_execution(record["id"])["valid"]
            calculation = "independently_recalculated"
        assert "normalize.validate_execution" in contract()["library_api"]["surface"]
    print(json.dumps({"execution_evidence": "validated", "calculation": calculation}))
    '''
)


MARKET_PROBE = textwrap.dedent(
    '''
    import json
    import subprocess
    import sys
    import types
    from pathlib import Path
    import yaml
    from evidence_wiki import Workspace, contract

    cli, fixture, directory = map(Path, sys.argv[1:])
    directory.mkdir()
    package = types.ModuleType("tests")
    package.__path__ = [str(fixture.parent)]
    sys.modules["tests"] = package
    from tests._market_fixture import SOURCE_ID, example, workspace
    checked = []
    for route in ("sec-company-concept", "alpaca-stock-bars"):
        root = directory / route
        _config, _record, originals = workspace(root, example(route)[0])
        with Workspace.open(root) as evidence:
            report = evidence.normalize.validate_market(SOURCE_ID)
            assert report["valid"] and report["completeness"]["complete"], report
            assert report["authority"] == "not_evaluated"
            values = report["data"]["observations"]
            if route == "sec-company-concept":
                assert values[0]["value"] == "1234567890123456789"
            else:
                assert values[0]["close"] == "10.1234567890123456789"
            command = [str(cli), "normalize", "market", "--target", str(root), "--source-id", SOURCE_ID, "--format", "json"]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            assert json.loads(result.stdout) == report
            (originals / "page.json").write_bytes(b"{}")
            assert not evidence.normalize.validate_market(SOURCE_ID)["valid"]
            result = subprocess.run(command, capture_output=True, text=True)
            assert result.returncode == 1 and not json.loads(result.stdout)["valid"]
            checked.append(route)
    assert "normalize.validate_market" in contract()["library_api"]["surface"]
    target = directory / "optional-pack"
    subprocess.run([str(cli), "init", "--target", str(target), "--project-name", "Market evidence",
                    "--project-description", "Bounded observations", "--owner-goal", "Review evidence",
                    "--domain-pack", "capital-markets"], capture_output=True, text=True, check=True)
    config = yaml.safe_load((target / "research.yml").read_text())
    for kind in ("acquisition", "discovery"):
        assert not config["integrations"][kind]["enabled"]
        assert not config["integrations"][kind]["providers"]
    result = subprocess.run([str(cli), "pack", "refresh", "--target", str(target), "--path", "capital-markets",
                             "--dry-run", "--format", "json"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["status"] == "no_changes"
    print(json.dumps({"market_evidence": "passed", "market_routes": checked, "optional_market_pack": "passed"}))
    '''
)


USAGE_PROBE = textwrap.dedent(
    '''
    import base64
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    import types
    from pathlib import Path
    import yaml
    from evidence_wiki import Workspace, contract
    from evidence_wiki.errors import SourceError

    cli, fixture, directory = map(Path, sys.argv[1:])
    directory.mkdir()
    package = types.ModuleType("tests")
    package.__path__ = [str(fixture.parent)]
    sys.modules["tests"] = package
    spec = importlib.util.spec_from_file_location("usage_fixture", fixture)
    data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(data)
    class Environment:
        def setenv(self, name, value):
            os.environ[name] = value
    host = data.UsageFixture(directory, Environment())
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config))
    with Workspace.open(host.root) as workspace:
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            try:
                workspace.usage.status()
            except SourceError as exc:
                assert exc.error_code == "EVIDENCE_USAGE_REFUSED"
            else:
                raise AssertionError("Unsupported host storage was accepted")
            print(json.dumps({"evidence_usage": "unsupported_host_refused"}))
            sys.exit(0)
        assert workspace.usage.status()["initialized"] is False
        command = host.command("initialize")
        host.checkpoint = workspace.usage.transact(command)["checkpoint"]
        body, files = host.source()
        command = host.command("deposit", body)
        request = {"command": command, "artifacts": {path: base64.b64encode(value).decode() for path, value in files.items()}}
        result = subprocess.run([str(cli), "usage", "transact", "--target", str(host.root)],
                                input=json.dumps(request), capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert workspace.usage.transact(command, artifacts=files) == receipt
        host.checkpoint = receipt["checkpoint"]
        decision = workspace.usage.check(body["source_revision"], uses=["training", "export"],
                                         purpose="training-snapshot", consumer="evidence-wiki")
        assert decision["eligible"], decision
        result = workspace.usage.materialize(body["source_revision"])
        assert (host.root / result["path"]).read_bytes() == files["normalized.md"]
        assert not workspace.usage.materialize(body["source_revision"])["changed"]
        command = host.command("revoke", {"source_id": body["source_id"], "source_revision": body["source_revision"],
                                           "scope": "revision", "reason": "owner-withdrawal"})
        receipt = workspace.usage.transact(command)
        assert workspace.usage.transact(command) == receipt
        assert workspace.usage.status(request_id=command["payload"]["request_id"])["receipt"] == receipt
        decision = workspace.usage.check(body["source_revision"], uses=["retrieval"], purpose="research", consumer="evidence-wiki")
        assert not decision["eligible"], decision
        assert workspace.usage.lineage(body["source_revision"])["complete"]
        try:
            workspace.usage.materialize(body["source_revision"])
        except SourceError as exc:
            assert exc.error_code == "EVIDENCE_USAGE_REFUSED"
        else:
            raise AssertionError("Revoked materialization was accepted")
        assert contract()["evidence_usage"]["retention"] == ["host-managed"]
    print(json.dumps({"evidence_usage": "validated", "revocation": "current", "command_retry": "idempotent"}))
    '''
)


SNAPSHOT_PROBE = textwrap.dedent(
    '''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    import types
    from pathlib import Path
    from evidence_wiki import contract, verify_snapshot
    from evidence_wiki.errors import SourceError

    cli, fixture, directory = map(Path, sys.argv[1:])
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        print(json.dumps({"evidence_snapshots": "unsupported_host"}))
        sys.exit(0)
    directory.mkdir()
    package = types.ModuleType("tests")
    package.__path__ = [str(fixture.parent)]
    sys.modules["tests"] = package
    spec = importlib.util.spec_from_file_location("snapshot_fixture", fixture)
    data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(data)
    class Environment:
        def setenv(self, name, value):
            os.environ[name] = value
    host = data.SnapshotFixture(directory, Environment())
    body, _ = host.add_execution()
    selection = host.selection(body)
    raw, preparation, registration, exported = host.export(selection)
    policy = host.policy_path.read_bytes()
    assert verify_snapshot(raw, trust_policy_bytes=policy)["valid"]
    assert host.workspace.snapshots.check(raw)["current_use"] == "authorized"
    result = subprocess.run([str(cli), "snapshot", "verify", "--trust-policy", str(host.policy_path)],
                            input=raw, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"]
    repeated = host.workspace.snapshots.export(selection, registration_request_id=registration["payload"]["request_id"])
    assert not repeated["created"] and repeated["content_hash"] == exported["content_hash"]
    host.revoke(host.module, host.parent)
    assert host.workspace.snapshots.check(raw)["current_use"] == "denied"
    try:
        host.workspace.snapshots.export(selection, registration_request_id=registration["payload"]["request_id"])
    except SourceError as exc:
        assert exc.error_code == "EVIDENCE_SNAPSHOT_REFUSED"
    else:
        raise AssertionError("Revoked snapshot was published")
    host.workspace.close()
    host.root.rename(host.root.with_name("origin-moved"))
    host.policy_path.unlink()
    del os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"]
    del os.environ["EVIDENCE_WIKI_STATE_DIR"]
    assert verify_snapshot(raw, trust_policy_bytes=policy)["valid"]
    assert "verify_snapshot" in contract()["library_api"]["surface"]
    print(json.dumps({"evidence_snapshots": "validated", "offline_origin": "absent", "snapshot_current_use": "revocation_denied"}))
    '''
)


TEMPORAL_PROBE = textwrap.dedent(
    '''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    import types
    from pathlib import Path
    from evidence_wiki import contract, verify_snapshot

    cli, fixture, directory = map(Path, sys.argv[1:])
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        print(json.dumps({"evidence_temporal": "unsupported_host"}))
        sys.exit(0)
    directory.mkdir()
    package = types.ModuleType("tests")
    package.__path__ = [str(fixture.parent)]
    sys.modules["tests"] = package
    spec = importlib.util.spec_from_file_location("temporal_fixture", fixture)
    data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(data)
    class Environment:
        def setenv(self, name, value):
            os.environ[name] = value
        def setitem(self, mapping, key, value):
            mapping[key] = value
    host = data.TemporalFixture(directory, Environment())
    first, _ = host.captured()
    request = host.request()
    original = host.evaluate(request)
    assert original["result"]["selected"][0]["source_revision"] == first["source_revision"]
    result = subprocess.run([str(cli), "temporal", "evaluate", "--target", str(host.root)],
                            input=json.dumps(request).encode(), capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result_id"] == original["result_id"]
    host.set_time("2026-09-10T02:00:00Z")
    host.captured(value="later correction", available="2026-09-10T01:30:00Z", supersedes=first["source_revision"])
    assert host.evaluate(request)["result_id"] == original["result_id"]
    body, _, selection = host.execution_source(mode="historical-available")
    assert not host.evaluate(host.request(body["source_id"]))["result"]["complete"]
    assert host.evaluate(host.request(body["source_id"], mode="historical-available"))["result"]["complete"]
    raw, _, registration, exported = host.export(selection)
    assert json.loads(raw)["schema_version"] == "evidence-snapshot/v2"
    repeated = host.workspace.snapshots.export(selection, registration_request_id=registration["payload"]["request_id"])
    assert not repeated["created"] and repeated["content_hash"] == exported["content_hash"]
    result = subprocess.run([str(cli), "snapshot", "verify", "--trust-policy", str(host.policy_path)],
                            input=raw, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"]
    host.revoke(host.module, host.parent)
    assert host.workspace.snapshots.check(raw)["current_use"] == "denied"
    policy = host.policy_path.read_bytes()
    host.workspace.close()
    host.root.rename(host.root.with_name("origin-moved"))
    host.host.rename(host.host.with_name("host-moved"))
    del os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"]
    del os.environ["EVIDENCE_WIKI_STATE_DIR"]
    assert verify_snapshot(raw, trust_policy_bytes=policy)["valid"]
    assert "temporal.evaluate" in contract()["library_api"]["surface"]
    print(json.dumps({"evidence_temporal": "validated", "temporal_cli_api": "same_revision",
                      "temporal_snapshot": "independent_offline_verification"}))
    '''
)


HISTORICAL_EXECUTION_PROBE = textwrap.dedent(
    '''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    import types
    from pathlib import Path
    from evidence_wiki import contract, verify_snapshot

    cli, fixture, directory = map(Path, sys.argv[1:])
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        print(json.dumps({"historical_execution": "unsupported_host"}))
        sys.exit(0)
    directory.mkdir()
    package = types.ModuleType("tests")
    package.__path__ = [str(fixture.parent)]
    sys.modules["tests"] = package
    spec = importlib.util.spec_from_file_location("historical_fixture", fixture)
    data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(data)
    class Environment:
        def setenv(self, name, value):
            os.environ[name] = value
        def setitem(self, mapping, key, value):
            mapping[key] = value
    host = data.HistoricalFixture(directory, Environment())
    body, files = host.history(mode="historical-available")
    report, assessment = host.assessment(body, files)
    assert assessment["eligible"], assessment
    assert assessment["historical_inputs"]["qualified_source_cutoffs"] == 1
    assert [item["outcome"] for item in report["records"]] == ["failed", "inconclusive", "passed"]
    raw, _, registration, exported = host.export(host.selection(body))
    assert json.loads(raw)["schema_version"] == "evidence-snapshot/v3"
    policy = host.policy_path.read_bytes()
    assert verify_snapshot(raw, trust_policy_bytes=policy)["valid"]
    result = subprocess.run([str(cli), "snapshot", "verify", "--trust-policy", str(host.policy_path)],
                            input=raw, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"]
    repeated = host.workspace.snapshots.export(host.selection(body), registration_request_id=registration["payload"]["request_id"])
    assert not repeated["created"] and repeated["content_hash"] == exported["content_hash"]
    host.revoke(host.module, host.parent)
    assert host.workspace.snapshots.check(raw)["current_use"] == "denied"
    host.workspace.close()
    host.root.rename(host.root.with_name("origin-moved"))
    host.host.rename(host.host.with_name("host-moved"))
    del os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"]
    del os.environ["EVIDENCE_WIKI_STATE_DIR"]
    assert verify_snapshot(raw, trust_policy_bytes=policy)["valid"]
    assert contract()["library_api"]["version"] == "14"
    print(json.dumps({"historical_execution": "validated", "historical_execution_snapshot": "independent_offline_verification"}))
    '''
)


SIMULATION_PROBE = textwrap.dedent(
    '''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    import types
    from pathlib import Path
    from evidence_wiki import contract, verify_snapshot

    cli, fixture, directory = map(Path, sys.argv[1:])
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        print(json.dumps({"market_simulation": "unsupported_host"}))
        sys.exit(0)
    directory.mkdir()
    package = types.ModuleType("tests")
    package.__path__ = [str(fixture.parent)]
    sys.modules["tests"] = package
    spec = importlib.util.spec_from_file_location("simulation_fixture", fixture)
    data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(data)
    class Environment:
        def setenv(self, name, value):
            os.environ[name] = value
        def setitem(self, mapping, key, value):
            mapping[key] = value
    host = data.SimulationFixture(directory, Environment())
    body, files = host.simulation(mode="historical-available")
    report, assessment = host.assessment(body, files)
    assert assessment["eligible"], assessment
    simulation = report["records"][0]["market_simulation"]
    assert simulation["calculation"]["passed"]
    assert simulation["simulated_performance"]["net_profit"] == "-68.81"
    raw, _, _, _ = host.export(host.selection(body))
    assert json.loads(raw)["schema_version"] == "evidence-snapshot/v4"
    assert json.loads(raw)["manifest"]["execution_profiles"] == ["market-simulation/v1"]
    policy = host.policy_path.read_bytes()
    result = subprocess.run([str(cli), "snapshot", "verify", "--trust-policy", str(host.policy_path)],
                            input=raw, capture_output=True, timeout=60)
    assert result.returncode == 0 and json.loads(result.stdout)["valid"], result.stderr
    corrupt = json.loads(raw)
    corrupt["schema_version"] = "evidence-snapshot/v3"
    assert not verify_snapshot(data.canonical(corrupt), trust_policy_bytes=policy)["valid"]
    host.workspace.close()
    host.root.rename(host.root.with_name("origin-moved"))
    host.host.rename(host.host.with_name("host-moved"))
    del os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"]
    del os.environ["EVIDENCE_WIKI_STATE_DIR"]
    assert verify_snapshot(raw, trust_policy_bytes=policy)["valid"]
    assert contract()["evidence_snapshots"]["optional_execution_profiles"] == ["market-simulation/v1"]
    print(json.dumps({"market_simulation": "independently_recalculated", "profiled_snapshot": "independent_offline_verification"}))
    '''
)


ASSESSMENT_PROBE = textwrap.dedent(
    r'''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    import types
    from pathlib import Path
    from evidence_wiki import Workspace, contract
    from evidence_wiki.errors import EvidenceWikiError

    cli, fixture, directory = map(Path, sys.argv[1:])
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        print(json.dumps({"evidence_assessments": "unsupported_host"}))
        sys.exit(0)
    directory.mkdir()
    package = types.ModuleType("tests")
    package.__path__ = [str(fixture.parent)]
    sys.modules["tests"] = package
    spec = importlib.util.spec_from_file_location("assessment_fixture", fixture)
    data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(data)
    class Environment:
        def setenv(self, name, value):
            os.environ[name] = value
    def initialize(profile):
        result = subprocess.run([str(cli), "init", "--profile", str(profile)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
    host = data.AssessmentFixture(directory, Environment(), initialize)
    with Workspace.open(host.root) as workspace:
        host.checkpoint = workspace.usage.transact(host.command("initialize"))["checkpoint"]
        body, files = host.temporal_source()
        host.checkpoint = workspace.usage.transact(host.command("deposit", body), artifacts=files)["checkpoint"]
        def command(operation, value):
            result = subprocess.run([str(cli), "assessments", operation, "--target", str(host.root)],
                                    input=json.dumps(value), capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)
        prepared = command("prepare", host.request())
        envelope = host.sign(prepared["registration"])
        receipt = command("issue", envelope)
        host.checkpoint = receipt["checkpoint"]
        assert workspace.assessments.issue(envelope) == receipt
        assert workspace.assessments.check(envelope)["eligible"]
        assert command("check", envelope)["external_action_authorized"] is False
        corrupt = json.loads(json.dumps(envelope))
        corrupt["payload"]["body"]["assessment"]["publication"]["export"]["questions"][0]["answer_summary"] = "forged"
        before = (host.host / "evidence-state.json").read_bytes()
        try:
            workspace.assessments.issue(corrupt)
        except EvidenceWikiError as exc:
            assert exc.error_code == "EVIDENCE_ASSESSMENT_REFUSED"
        else:
            raise AssertionError("altered assessment was accepted")
        assert (host.host / "evidence-state.json").read_bytes() == before
        revocation = {"source_id": body["source_id"], "scope": "revision", "source_revision": body["source_revision"], "reason": "owner-withdrawal"}
        host.checkpoint = workspace.usage.transact(host.command("revoke", revocation))["checkpoint"]
        assert not workspace.assessments.check(envelope)["eligible"]
        refresh = workspace.assessments.plan_refresh(host.refresh())
        assert refresh["plan"]["coverage"]["complete"] and len(refresh["plan"]["entries"]) == 1
        application = host.sign(refresh["application"])
        applied = command("apply-refresh", application)
        assert workspace.assessments.apply_refresh(application) == applied
        assert workspace.assessments.check(envelope)["reasons"] == ["assessment_invalidated"]
        assert command("plan-refresh", host.refresh())["plan"]["entries"] == []
        assert contract()["library_api"]["version"] == "14"
    print(json.dumps({"evidence_assessments": "authenticated_cli_api_parity", "assessment_refresh": "revocation_and_idempotent_apply"}))
    '''
)


COMPUTATION_PROBE = textwrap.dedent(r'''
    import json
    import subprocess
    import sys
    from pathlib import Path
    import yaml
    from evidence_wiki import contract
    from evidence_wiki.computation import evaluate, execute, schema_document

    cli, root = sys.argv[1], Path(sys.argv[2])
    deployed = subprocess.run([cli, "deploy", "--target", str(root), "--project-name", "computed-evidence",
                               "--project-description", "Declared arithmetic", "--domain-pack", "general-science"],
                              check=False, capture_output=True, text=True)
    assert deployed.returncode == 0, deployed.stderr
    path = root / "research.yml"
    config = yaml.safe_load(path.read_text())
    config["computation"] = {
        "version": "1.0", "arithmetic": {"mode": "exact", "precision": 64, "scale": 2, "rounding": "ROUND_HALF_EVEN"},
        "clock": {"as_of": "2026-09-21T00:00:00Z", "timezone": "UTC", "ambiguous": "refuse", "nonexistent": "refuse", "search_days": 366},
        "tables": {}, "aggregations": {}, "invariants": {}, "cadence": {},
        "graphs": {"worksheet": {"description": "Declared arithmetic", "constants": {}, "inputs": {},
            "nodes": {"total": {"expr": "0.1 + 0.2", "unit": "units"}}, "output_mapping": {"total": "total"},
            "output_page": "wiki/outputs/computed.md"}}
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    expected = evaluate(root)
    assert expected["graphs"]["worksheet"]["outputs"]["total"]["value"] == "0.3"
    assert expected["graphs"]["worksheet"]["outputs"]["total"]["formatted"] == "0.30"
    assert schema_document(expected["schema_version"])["additionalProperties"] is False
    assert contract()["computation"]["capability"] == "declarative-computation/v1"
    for script in ("aggregate_records.py", "evaluate_formulas.py", "verify_assertions.py", "schedule_milestones.py"):
        completed = subprocess.run([sys.executable, str(root / "scripts" / script), "--target", str(root)],
                                   check=True, capture_output=True, text=True)
        assert json.loads(completed.stdout) == expected
    rendered = subprocess.run([cli, "computation", "check", "--target", str(root)],
                              check=True, capture_output=True, text=True)
    assert json.loads(rendered.stdout) == expected
    receipt = execute(root, "write", expected_result_id=expected["result_id"], request_id="installed-output")
    assert receipt["dry_run"] is False
    assert "0.30" in (root / "wiki/outputs/computed.md").read_text()
    assert execute(root, "write", expected_result_id=expected["result_id"], request_id="installed-output")["replayed"] is True
    for candidate in ("sample-benchmark", "sample-portfolio", "sample-filing"):
        assert (root / "docs/computation-examples" / candidate / "research.overlay.yml").is_file()
    print(json.dumps({"declarative_computation": "passed", "copied_computation_scripts": "passed"}))
''')


AGENT_PROBE = textwrap.dedent(r'''
    import hashlib
    import json
    import pathlib
    import subprocess
    import sys
    from evidence_wiki.agent_resources import resource_document, resource_index
    from evidence_wiki.onboarding_contract import decode_document
    from evidence_wiki.onboarding_schemas import schema_document, schema_ids
    from evidence_wiki.frameworks import compatibility, export_bundle, invoke, validate_bundle
    from evidence_wiki._script_host import load_packaged_script, shared_assets_root

    cli = sys.argv[1]
    before = sorted(str(path) for path in pathlib.Path.cwd().rglob('*'))
    result = subprocess.run([cli, 'agent', '--format', 'json'], capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr, result.stderr + result.stdout
    bootstrap = decode_document('onboarding/bootstrap/v2', result.stdout.encode())['payload']
    assert bootstrap['workspace'] == 'absent'
    assert bootstrap['strict_selection']['effective_assurance'] is None
    assert before == sorted(str(path) for path in pathlib.Path.cwd().rglob('*'))
    summary = subprocess.run([cli, 'agent', 'summary', '--format', 'json', '--require', 'strict-evidence/v1',
                              '--require', 'declarative-computation/v1'], capture_output=True, text=True)
    assert summary.returncode == 0, summary.stderr + summary.stdout
    value = decode_document('onboarding/capabilities/v1', summary.stdout.encode())['payload']
    assert len(summary.stdout.encode()) < value['limits']['summary_bytes']
    assert all('host_enforced' not in mode for mode in value['frameworks']['qualified'])
    assert value['strict']['host_probe'] == 'not_run'
    assert value['installation']['package_version'] != ''
    matrix = compatibility()
    assert {row['id'] for row in matrix['frameworks']} == {'pi', 'opencode', 'gemini'}
    assert all(row['modes']['host_enforced']['status'] != 'supported' for row in matrix['frameworks'])
    bundle = json.loads(resource_document('framework/bundle/v1')['content'])
    validate_bundle(bundle)
    if sys.platform != 'win32':
        assert export_bundle(pathlib.Path.cwd() / 'portable-bundle')['status'] == 'created'
    call = {'schema_version':'evidence-framework-call/v1','request_id':'installed-resource',
            'instruction_sha256':bootstrap['guide']['sha256'],'operation':'resource',
            'parameters':{'resource_id':'evidence-framework-call/v1'}}
    native = invoke(json.dumps(call).encode(), target=pathlib.Path.cwd())
    assert native['status'] == 'completed' and native['evidence_acceptance'] == 'not_evaluated'
    assert json.loads(native['result_json'])['payload']['id'] == 'evidence-framework-call/v1'
    for entry in resource_index()['resources']:
        document = resource_document(entry['id'])
        assert hashlib.sha256(document['content'].encode()).hexdigest() == entry['sha256']
    for key in schema_ids():
        assert json.loads(resource_document(key)['content']) == schema_document(key)
    for stem, method in (('_strict_contract', 'schema_documents'), ('_computation_contract', 'schemas')):
        for key, schema in getattr(load_packaged_script(shared_assets_root(), stem), method)().items():
            assert json.loads(resource_document(key)['content']) == schema
    refused = subprocess.run([cli, 'agent', '--format', 'json', '--assurance', 'host_enforced'],
                             capture_output=True, text=True)
    assert refused.returncode == 2 and not refused.stderr
    assert json.loads(refused.stdout)['details']['field'] == 'host_enforcement_not_verified'
    print(json.dumps({'installed_agent_bootstrap': 'passed', 'closed_resources': 'passed',
                      'schema_owner_parity': 'passed', 'portable_framework_bundle':'passed', 'canonical_native_call':'passed'}))
''')


PACK_PROBE = textwrap.dedent(r'''
    import json
    import os
    import pathlib
    import shutil
    import subprocess
    import sys
    from evidence_wiki._script_host import shared_assets_root
    from evidence_wiki.onboarding_contract import _matches
    from evidence_wiki.pack_decisions import schema_document

    cli, directory = sys.argv[1:]
    scratch = pathlib.Path(directory)
    scratch.mkdir()
    def command(*args, expected=0):
        result = subprocess.run([cli, 'pack', *map(str, args)], capture_output=True, text=True)
        assert result.returncode == expected and not result.stderr, result.stdout + result.stderr
        return json.loads(result.stdout)
    before = sorted(scratch.rglob('*'))
    listing = command('list')
    assert listing['bounds'] == {'total': 5, 'returned': 5, 'truncated': False}
    assert sorted(scratch.rglob('*')) == before
    row = command('show', 'bundled:general-science')['pack']
    assert row['state'] == 'available' and row['metadata']['selection']['unknown_fields'] == []
    guide = command('guide')['content']
    value = json.loads(guide.split('```json\n', 1)[1].split('```', 1)[0])
    value['selections'][0]['tree_sha256'] = row['identity']['tree_sha256']
    decision = scratch / 'decision.json'
    decision.write_text(json.dumps(value), encoding='utf-8')
    result = command('decide', '--from-file', decision)
    _matches(result, schema_document('evidence-pack-decision-result/v1'))
    assert result['status'] == 'valid' and not result['research_ready']
    catalog_status = 'unsupported_platform'
    if os.name == 'posix':
        assets = scratch / 'packs'
        assets.mkdir()
        candidate = assets / 'general-science'
        shutil.copytree(shared_assets_root() / 'domain-packs/general-science', candidate)
        catalog = scratch / 'catalog'
        assert command('catalog', 'init', '--catalog', catalog, '--root', 'local=' + str(assets))['status'] == 'created'
        assert command('catalog', 'register', '--catalog', catalog, '--id', 'science', '--root-id', 'local',
                       '--path', 'general-science', '--scope', 'Caller scope')['status'] == 'registered'
        local = command('show', 'local:science', '--catalog', catalog)['pack']
        assert local['validation']['state'] == 'matching_observation'
        command('show', 'general-science', '--catalog', catalog, expected=2)
        (candidate / 'taxonomy.md').write_text('Changed local guidance.\n', encoding='utf-8')
        assert command('show', 'local:science', '--catalog', catalog, expected=1)['pack']['state'] == 'mutated'
        catalog_status = 'passed'
    print(json.dumps({'pack_discovery': 'passed', 'pack_fit': 'passed', 'pack_catalog': catalog_status}))
''')


SOURCE_PROBE = textwrap.dedent(r'''
    import base64
    import hashlib
    import json
    import os
    import pathlib
    import subprocess
    import sys
    import sysconfig
    import yaml

    cli, location = sys.argv[1:]
    root = pathlib.Path(location)
    def command(*args, expected=0):
        result = subprocess.run([cli, 'agent', *map(str, args)], capture_output=True, text=True)
        assert result.returncode == expected and not result.stderr, result.stdout + result.stderr
        return json.loads(result.stdout)
    assert command('inspect', '--target', root)['target']['state'] == 'absent'
    assert command('source-guide')['content'].startswith('# Inspect capabilities')
    schemas = command('source-schemas')['schema_ids']
    assert 'evidence-host-delivery/v1' in schemas and 'evidence-source-inspection/v1' in schemas
    subprocess.run([cli, 'init', '--target', str(root), '--project-name', 'source-observation',
                    '--project-description', 'Observe selected retained text.', '--domain-pack', 'general-science'],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    content = b'A retained source reports a measured reflectance of 0.74.\n'
    profile = {'schema_version':'evidence-host-capture/v1','capture_id':'retained','tool_id':'browser','tool_version':'1',
        'origin_url':'https://example.org/study','title':'Retained observation','retrieved_at':'2026-09-22T10:00:00Z',
        'capture_method':'browser_visible_text','content_format':'markdown','content_kind':'primary','completeness':'complete',
        'completeness_note':'Supplied complete text.','rights':{'status':'allowed','license':'CC0-1.0','terms_url':None,'note':'Fixture declaration.'},
        'scope':{},'request_id':None,'content_sha256':'sha256:'+hashlib.sha256(content).hexdigest(),'content_bytes':len(content)}
    request = root.parent / 'capture.json'
    request.write_text(json.dumps({'schema_version':'evidence-host-delivery/v1','capture':profile,'content_base64':base64.b64encode(content).decode()}))
    if os.name == 'posix':
        result = command('capture', '--target', root, '--path', 'raw/web/retained.md', '--from-file', request)
        assert result['status'] == 'delivered' and not result['request_fulfilled']
        assert command('capture', '--target', root, '--path', 'raw/web/retained.md', '--from-file', request)['status'] == 'already_present'
    else:
        raw = root / 'raw/web/retained.md'
        raw.write_bytes(content)
        raw.with_name(raw.name+'.provenance.yml').write_text(json.dumps({'host_capture':profile,'checksum':profile['content_sha256'],
            'origin_url':profile['origin_url'],'retrieved_at':profile['retrieved_at'],'license':profile['rights']['license']}))
    subprocess.run([sys.executable, str(root/'scripts/source_inventory.py'), '--project-root', str(root)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    record = json.loads((root/'sources/manifest.jsonl').read_text().splitlines()[0])
    subprocess.run([sys.executable, str(root/'scripts/normalize_sources.py'), '--project-root', str(root), '--source-id', record['id']],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    status = command('source-status', '--target', root, '--source-id', record['id'])
    assert status['sources'][0]['usability'] == 'usable', status
    assert status['strict']['effective_assurance'] is None and not status['research_ready']
    assert (root/'raw/web/retained.md').read_bytes() == content
    tools = {'schema_version':'evidence-host-tools/v1','tools':[{'id':'browser','version':'1','kind':'browser','operations':['capture'],
        'scope':[{'kind':'uri_prefix','value':'https://example.org'}],'formats':['markdown'],'credential_refs':[],
        'limits':{'max_requests':2,'max_bytes':10000,'max_cost_usd':'0'},'authorization':'declared','claims':['sandbox'],'basis':'declared'}]}
    tool_file = root.parent / 'host-tools.json'; tool_file.write_text(json.dumps(tools))
    assert command('inspect','--target',root,'--host-tools',tool_file)['host_tools'][0]['host_protection'] == 'not_verified'
    # Publish only a local fixture registration in this disposable installation.
    site = pathlib.Path(sysconfig.get_path('purelib'))
    (site/'source_observation_fixture.py').write_text('\n'.join([
        'import os', "assert 'SOURCE_FIXTURE_TOKEN' not in os.environ", 'class Capabilities:',
        " allowed_domains=('example.org',)", " terms_urls=('https://example.org/terms',)", " license_inference='none'",
        ' captures_raw=True', ' quarantine_on_incomplete=True', ' rate_limit=None',
        " credentials=('SOURCE_FIXTURE_TOKEN',)", " request_kinds=('structured_data',)", 'class Provider:',
        " id='observed-provider'", ' provider_api_version=1', ' capabilities=Capabilities()',
        ' def validate_request(self, request):',
        "  if request.get('symbol') != 'OBSERVATION': raise ValueError('request refused')", '  return dict(request)',
        " def plan_fetch(self, request): raise AssertionError('fetch must not run')",
        " def interpret(self, responses): raise AssertionError('interpret must not run')", '']))
    metadata = site/'source_observation_fixture-1.0.dist-info'; metadata.mkdir()
    (metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: source-observation-fixture\nVersion: 1.0\n')
    (metadata/'entry_points.txt').write_text('[evidence_wiki.acquisition_providers]\nEntryMarker = source_observation_fixture:Provider\n')
    os.environ['SOURCE_FIXTURE_TOKEN']='never-emit-this-fixture-credential'
    observed = command('inspect','--target',root,'--probe-provider','acquisition:source-observation-fixture/EntryMarker')
    provider = next(row for row in observed['providers'] if row['id']=='observed-provider')
    assert provider['probe']['loaded'] and provider['probe']['capabilities']['credentials']==['SOURCE_FIXTURE_TOKEN'], provider
    assert 'never-emit-this-fixture-credential' not in json.dumps(observed)
    config = yaml.safe_load((root/'research.yml').read_text())
    config['integrations']['acquisition']={'enabled':True,'providers':['observed-provider']}
    (root/'research.yml').write_text(yaml.safe_dump(config))
    (root/'provider-request.json').write_text(json.dumps({'symbol':'OBSERVATION'}))
    route = {'schema_version':'evidence-source-routes/v1','request_id':'observe','requirements':[{
        'id':'rows','question_ids':['question'],'kind':'structured_data','query_or_identifier':'OBSERVATION','source_request_id':None,
        'scope':{},'output_format':'csv','content_kinds':['primary'],'needs_complete':True,'source_ids':[]}],
        'budget':{'max_requests':1,'max_bytes':10000,'max_cost_usd':'0'},'preferred_tools':[],
        'registered_requests':[{'requirement_id':'rows','phase':'acquisition','provider_id':'observed-provider',
            'registration':'source-observation-fixture/EntryMarker','request':{'symbol':'OBSERVATION'},'request_path':'provider-request.json'}]}
    route_file = root.parent/'routes.json'; route_file.write_text(json.dumps(route))
    planned = command('routes','--target',root,'--from-file',route_file,'--probe-provider','acquisition:source-observation-fixture/EntryMarker')
    selected = next(row for row in planned['routes'] if row['kind']=='registered_provider')
    assert selected['state']=='ready_to_attempt' and selected['request_validation']=='passed', selected
    assert not selected['network_executed'] and not planned['research_ready']
    route['registered_requests'][0]['request']={'symbol':'REFUSED'}
    (root/'provider-request.json').write_text(json.dumps({'symbol':'REFUSED'})); route_file.write_text(json.dumps(route))
    refused = command('routes','--target',root,'--from-file',route_file,'--probe-provider','acquisition:source-observation-fixture/EntryMarker')
    assert next(row for row in refused['routes'] if row['kind']=='registered_provider')['state']=='blocked'
    for path in metadata.iterdir():
        path.unlink()
    metadata.rmdir()
    (site/'source_observation_fixture.py').unlink()
    print(json.dumps({'source_inspection':'passed','host_capture_pipeline':'passed','source_readiness':'passed',
                      'registered_request_probe':'passed','source_routes':'passed','declared_authority_not_promoted':'passed'}))
''')


PLANNING_PROBE = textwrap.dedent(r'''
    import hashlib
    import json
    import subprocess
    import sys
    from pathlib import Path
    import yaml
    from evidence_wiki.pack_discovery import owner
    from evidence_wiki.planning import compile_plan

    cli, root = Path(sys.argv[1]), Path(sys.argv[2])
    root.mkdir()
    text = '¿Qué muestra la evidencia?\n第二行'
    original = {'schema_version':'2.0','kind':'research_request','request_id':'installed-research','payload':{
        'goal':'Answer using retained evidence','questions':[{'id':'q1','text':text}], 'derived_questions':[],
        'target':{'writable_root':str(root),'relative_path':'workspace'},'outputs':['json'],
        'scope':[{'name':'jurisdiction','value':'Spain'}],
        'domain':{'mode':'none','pack':None,'rationale':'Generic research guidance is sufficient'},
        'sources':[],'host_tools':[], 'authority':{'role':'caller','reference':'local setup',
            'allowed_actions':['local_setup'],'source_scope':[],'writable_roots':[str(root)],'credential_references':[]},
        'budgets':{'questions':5,'source_requests':0,'downloads':0,'bytes':0,'seconds':60},
        'assumptions':[],'open_decisions':[],
        'strict_evidence':{'mode':'strict','assurance':'artifact_checked','policy_id':'installed-policy','policy_revision':'1'}}}
    facet = {'facet_id':'primary','description':'Retained primary evidence','required':True,'evidence_path':'official_guidance',
        'source_policy':'official_primary','freshness_policy':'no_staleness_check','identity_policy':'official_domain_match','min_sources':1}
    criterion = {'facet_id':'primary','source_classes':['official guidance'],'required_scope':['jurisdiction'],
        'time':'Keep observation dates','units':'Keep source units','counterevidence':'Retain contrary evidence',
        'stopping':'Support or explicit gaps for all facets','inference':'Label all derivations','quantitative':None}
    request = {'schema_version':'evidence-research-setup/v1','request':original,
        'decisions':{'question_plans':[{'question_id':'q1','template':None,'facets':[facet],'criteria':[criterion]}]}}
    request_file, saved = root/'request.json', root/'plan.json'
    request_file.write_text(json.dumps(request,ensure_ascii=False))
    def command(*args, expected=0):
        result = subprocess.run([str(cli),'agent',*map(str,args)],cwd=root,text=True,capture_output=True)
        assert result.returncode == expected, result.stdout + result.stderr
        return json.loads(result.stdout)
    plan = command('plan','--from-file',request_file,'--output',saved)
    assert plan['setup_ready'] and not plan['research_ready'] and not plan['actions_executed']
    assert plan['questions']['rows'][0]['original_text'] == text
    assert not (root/'workspace').exists()
    assert compile_plan(json.dumps(plan['request']).encode())['plan_id'] == plan['plan_id']
    assert command('plan-check','--from-file',saved)['status'] == 'current'
    command('plan','--from-file',request_file,'--output',saved,expected=3)
    assert command('plan-guide')['content'].startswith('# Plan a research workspace')
    assert 'evidence-research-setup/v1' in command('plan-schemas')['schema_ids']
    profile = root/'profile.yml'
    profile.write_text(yaml.safe_dump(plan['profile'],allow_unicode=True))
    deployed = subprocess.run([str(cli),'init','--profile',str(profile)],cwd=root,text=True,capture_output=True)
    assert deployed.returncode == 0, deployed.stdout + deployed.stderr
    target = root/'workspace'
    config = yaml.safe_load((target/'research.yml').read_text())
    assert config == plan['initialization']['effective_config']
    frozen = (target/'docs/research-requirements.json').read_bytes()
    assert config['strict_evidence']['instructions']['docs/research-requirements.json'] == 'sha256:' + hashlib.sha256(frozen).hexdigest()
    intake = owner('intake_questions').run_intake_document(target,plan['questions']['batch'],dry_run=True,from_file_label='saved-plan')
    assert intake['counts']['created'] == 1
    command('plan-check','--from-file',saved,expected=3)
    print(json.dumps({'research_planning':'passed','plan_readonly_replay':'passed','plan_staleness':'passed',
        'initializer_frozen_requirements':'passed','planned_question_intake':'passed'}))
''')


PACK_AUTHORING_PROBE = textwrap.dedent(r'''
    import json
    import subprocess
    import sys
    from pathlib import Path

    cli, root, request_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    root.mkdir()
    def command(*args, expected=0):
        result = subprocess.run([str(cli),*map(str,args)],cwd=root,text=True,capture_output=True)
        assert result.returncode == expected, result.stdout + result.stderr
        return json.loads(result.stdout)
    spec = json.loads(command('pack','guide','--topic','specification')['content'])
    spec['unresolved'] = []
    source = root/'spec.json'
    source.write_text(json.dumps(spec))
    draft = root/'draft'
    created = command('pack','scaffold','--from-file',source,'--output',draft)
    observed = command('pack','qualify','--draft',draft)
    assert observed['validation']['ok'] and observed['validation']['semantic_adequacy'] == 'not_evaluated'
    rows = []
    for scenario,region,question,outcome in [('adequate','north','north','pass'),('missing',None,'north','fail'),
            ('conflicting','south','north','fail'),('wrong_scope','north','east','fail')]:
        rows.append({'id':scenario,'requirement_ids':['region'],'scenario':scenario,'kind':'policy',
            'target':'pack:'+spec['name']+'/region-match',
            'inputs':{'structured':{} if region is None else {'region':region},'question':{'metadata':{'region':question}},
                'provenance':{},'origin_host':None,'provider_ids':[],'as_of':'2026-09-22T12:00:00Z'},
            'expected':{'status':'observed','outcome':outcome,'human_review_required':True},'rationale':'Frozen region-equality outcome'})
    suite={'schema_version':'evidence-pack-cases/v1','draft_id':created['draft_id'],'cases':rows,'exceptions':[],
        'limitations':['Synthetic reference values; independent domain review remains required']}
    cases=root/'cases.json';cases.write_text(json.dumps(suite))
    assert not command('pack','freeze-cases','--draft',draft,'--from-file',cases)['gaps']
    assessment=command('pack','assess','--draft',draft)
    assert not assessment['assessment']['gaps'] and assessment['assessment']['mechanical_cases_passed']
    assert assessment['assessment']['independent_review']=='not_verified'
    assert all(row['passed'] for row in assessment['assessment']['reference_basis']['arithmetic_observations'])
    catalog=root/'catalog'
    command('pack','catalog','init','--catalog',catalog,'--root','drafts='+str(root))
    accepted=command('pack','accept','--draft',draft,'--assessment-id',assessment['record']['sha256'],
        '--catalog',catalog,'--root-id','drafts','--id','scoped-one','--scope','Scoped synthetic measurements')
    assert accepted['semantic_adequacy']=='not_certified'
    original=json.loads(request_path.read_text())
    original['request']['payload']['target']={'writable_root':str(root),'relative_path':'workspace'}
    original['request']['payload']['authority']['writable_roots']=[str(root)]
    original['request']['payload']['scope'].append({'name':'region','value':'north'})
    request=root/'research.json';request.write_text(json.dumps(original))
    saved=root/'plan.json'
    plan=command('pack','resume','--from-file',request,'--catalog',catalog,'--id','scoped-one','--output',saved)
    assert plan['setup_ready'] and not plan['research_ready'] and not (root/'workspace').exists()
    assert plan['bindings']['accepted_pack']['assessment_sha256']==assessment['record']['sha256']
    assert command('agent','plan-check','--from-file',saved)['status']=='current'
    Path(created['candidate'],'taxonomy.md').write_text('Changed guidance')
    command('agent','plan-check','--from-file',saved,expected=3)
    print(json.dumps({'local_pack_scaffold':'passed','frozen_pack_cases':'passed','canonical_pack_qualification':'passed',
        'independent_arithmetic_references':'passed','qualified_local_registration':'passed','qualified_plan_resume':'passed',
        'pack_domain_certification':False}))
''')


SETUP_PROBE = PLANNING_PROBE[:PLANNING_PROBE.index("profile = root/'profile.yml'")] + textwrap.dedent(r'''
    result = command('apply','--from-file',saved)
    assert result['status'] == 'ready' and result['setup_ready'] and result['evidence_empty'], result
    assert not result['research_complete'] and not result['strict']['reviewer_authenticated']
    assert result['strict']['effective_assurance'] == 'artifact_checked'
    assert command('setup-guide')['content'].startswith('# Apply and recover')
    assert 'evidence-setup-result/v1' in command('setup-schemas')['schema_ids']
    target = root/'workspace'
    before = {str(path.relative_to(target)):path.read_bytes() for path in target.rglob('*') if path.is_file()}
    replay = command('apply','--from-file',saved)
    assert replay['transaction_id'] == result['transaction_id']
    assert before == {str(path.relative_to(target)):path.read_bytes() for path in target.rglob('*') if path.is_file()}
    assert yaml.safe_load((target/'wiki/questions/q1.md').read_text().split('---')[1])['metadata']['original_text'] == text
    original['payload']['target']['relative_path'] = 'sources-workspace'
    original['payload']['budgets']['bytes'] = 100000
    original['payload']['authority']['source_scope'] = [str(root)]
    source = root/'original.html'
    source.write_text('<html><head><title>Retained observations</title></head><body><h1>Retained observations</h1><p>Relevant measured evidence with dates, population and units.</p></body></html>')
    original['payload']['sources'] = [{'id':'original','kind':'local_file','locator':str(source),'question_ids':['q1']}]
    request['decisions']['source_requirements'] = [{'source_id':'original','output_format':'html','needs_complete':True,'scope':{'jurisdiction':'Spain'}}]
    request_file.write_text(json.dumps(request,ensure_ascii=False))
    source_plan = root/'source-plan.json'
    command('plan','--from-file',request_file,'--output',source_plan)
    observed = command('apply','--from-file',source_plan)
    assert observed['status'] == 'ready' and observed['sources'][0]['usable'], observed
    assert not observed['claims_verified'] and observed['usable_source_count'] == 1
    (target/'user-note.txt').write_text('User changes are retained')
    assert command('apply','--from-file',saved,expected=3)['error_code'] == 'ONBOARDING_OWNERSHIP_CONFLICT'
    assert (target/'user-note.txt').read_text() == 'User changes are retained'
    print(json.dumps({'workspace_application':'passed','setup_replay':'passed','local_source_observation':'passed','setup_conflict_preservation':'passed'}))
''')


REVISION_PROBE = textwrap.dedent(r'''
    import json, shutil, subprocess, sys
    from pathlib import Path
    import yaml
    from evidence_wiki._script_host import shared_assets_root
    from evidence_wiki.pack_discovery import owner
    cli, root = Path(sys.argv[1]), Path(sys.argv[2])
    root.mkdir()
    target, candidate, saved = root/'workspace', root/'candidate/general-science', root/'revision.json'
    def command(*args, expected=0):
        result = subprocess.run([str(cli),*map(str,args)], cwd=root, text=True, capture_output=True)
        assert result.returncode == expected, result.stdout + result.stderr
        return None if args[0] == "init" else json.loads(result.stdout or result.stderr)
    command('init','--target',target,'--project-name','reviewed-research','--project-description','Retain evidence',
            '--owner-goal','Explicit research requirements','--domain-pack','general-science')
    owner('intake_questions').run_intake_document(target, {'schema_version':'1.0','questions':[
        {'id':'q1','question':'What evidence supports the claim?','priority':'high','origin':'caller'}]},
        dry_run=False, from_file_label='caller')
    shutil.copytree(shared_assets_root()/'domain-packs/general-science',candidate)
    overlay=yaml.safe_load((candidate/'research.overlay.yml').read_text())
    overlay['domain_pack']['version']='0.2.0'
    (candidate/'research.overlay.yml').write_text(yaml.safe_dump(overlay,sort_keys=False))
    (candidate/'claims.md').write_text((candidate/'claims.md').read_text()+'\nRetain explicit uncertainty.\n')
    planned=command('pack','revision-plan','--target',target,'--path',candidate,'--rationale','Clarify evidence scope','--output',saved)
    assert planned['owner_plan']['impact']['bounds']['questions_affected']==1
    applied=command('pack','revision-apply','--from-file',saved)
    assert applied['status']=='applied' and applied['research']['pending_questions']==['q1']
    assert command('pack','revision-apply','--from-file',saved)['status']=='already_applied'
    migration={'schema_version':'evidence-pack-reevaluation/v1','revision_id':applied['revision_id'],'slug':'q1',
        'rationale':'Explicit reviewed requirement mapping','retired_facets':[],'request_replacements':{},'computation_migrations':{},
        'template':{'coverage_profile':'scoped','required_facets':[{'facet_id':'evidence','description':'Retained evidence',
            'required':True,'evidence_path':'academic_method_existence','source_policy':'academic_indexed',
            'freshness_policy':'publication_identity','identity_policy':'citation_id_resolves','min_sources':1}],'optional_facets':[]}}
    source=root/'migration.json';source.write_text(json.dumps(migration))
    migrated=command('pack','reevaluate','--target',target,'--from-file',source)
    assert migrated['status']=='migrated' and not migrated['release_accepted']
    assert (target/migrated['archive']).is_file()
    assert command('pack','reevaluate','--target',target,'--from-file',source)['status']=='already_migrated'
    assert command('pack','revision-status','--target',target)['pending_questions']==['q1']
    assert 'evidence-pack-reevaluation/v1' in command('pack','schemas')['schema_ids']
    print(json.dumps({'revision_owner_application':'passed','revision_impact':'passed','coverage_revision_history':'passed',
        'revision_replay':'passed','revision_semantic_certification':False}))
''')


RESEARCH_PROBE = PLANNING_PROBE[:PLANNING_PROBE.index("profile = root/'profile.yml'")].replace(
    "'allowed_actions':['local_setup']", "'allowed_actions':['local_setup','local_research']") + textwrap.dedent(r'''
    setup = command('apply','--from-file',saved)
    target = root/'workspace'
    advice = command('next','--target',target,'--agent-id','current')
    assert not advice['actions_executed'] and not advice['research_complete']
    assert advice['actions'][0]['operation'] == 'start'
    started = command('start','--target',target,'--run-id','research','--agent-id','current')
    assert started['run']['caller_context']['context_id']
    before = {str(p.relative_to(target)):p.read_bytes() for p in target.rglob('*') if p.is_file()}
    resumed = command('resume','--target',target,'--run-id','research','--agent-id','current')
    assert any(row['operation']=='claim' for row in resumed['actions'])
    assert before == {str(p.relative_to(target)):p.read_bytes() for p in target.rglob('*') if p.is_file()}
    command('heartbeat','--target',target,'--run-id','research','--agent-id','current')
    refused = command('heartbeat','--target',target,'--run-id','research','--agent-id','other',expected=3)
    assert refused['error_code'] == 'ONBOARDING_OWNERSHIP_CONFLICT'
    output = command('research-export','--target',target,expected=3)
    assert not output['research_complete'] and output['original_outcomes'][0]['original_text'] == text
    progress = command('progress','--target',target,'--run-id','research')
    assert progress['semantic_evaluation']['unsupported_claim_escapes']['value'] is None
    assert progress['measured']['question_outcomes'] == {'open':1}
    assert command('research-guide')['content'].startswith('# Research with the current caller')
    assert 'evidence-research-action/v1' in command('research-schemas')['schema_ids']
    print(json.dumps({'caller_guidance':'passed','caller_run_binding':'passed','caller_readonly_resume':'passed',
        'caller_ownership_conflict':'passed','original_question_accounting':'passed','local_telemetry_unknown_grading':'passed'}))
''')


def validate_installed(venv: Path, scratch: Path, expected_version: str | None, label: str) -> dict[str, object]:
    if scratch.resolve().is_relative_to(REPO_ROOT.resolve()) or venv.resolve().is_relative_to(REPO_ROOT.resolve()):
        raise ValidationError("installed execution must use an unrelated directory outside the checkout")
    """Exercise one fresh installation from outside the checkout."""
    python = venv_python(venv)
    cli = venv_cli(venv)
    if not cli.is_file():
        raise ValidationError(f"{label}: the installed distribution did not provide the evidence-wiki entry point")
    outside = scratch / "outside-checkout"
    outside.mkdir()
    fixture_root = isolated_fixtures(scratch)
    workspace = scratch / "provider-workspace"
    # Every command runs from a directory that is not the checkout, so a module
    # resolved from the source tree instead of the install would be a failure here.
    run([str(cli), "--version"], cwd=outside)
    agent_probe = run([str(python), "-c", AGENT_PROBE, str(cli)], cwd=outside)
    pack_probe = run([str(python), "-c", PACK_PROBE, str(cli), str(scratch / "pack-discovery")], cwd=outside)
    native_initialization = run([str(python), "-B", "-c", NATIVE_INITIALIZATION_PROBE, str(cli),
        str(fixture_root / "tests/fixtures/workspace-init-profile.yml"), str(scratch / "native-initialization")], cwd=outside)
    contract_path = scratch / "contract.json"
    contract_path.write_text(run([str(cli), "contract"], cwd=outside), encoding="utf-8", newline="\n")
    run(
        [
            str(cli),
            "deploy",
            "--target",
            str(workspace),
            "--project-name",
            "provider-workspace",
            "--project-description",
            f"Installed {label} orchestration smoke",
            "--domain-pack",
            "general-science",
            "--discovery-provider",
            "arxiv",
            "--acquisition-provider",
            "arxiv",
        ],
        cwd=outside,
    )
    pack_refresh_path = scratch / "pack-refresh.json"
    pack_refresh_path.write_text(
        run(
            [str(cli), "pack", "refresh", "--target", str(workspace), "--path", "general-science", "--format", "json"],
            cwd=outside,
        ),
        encoding="utf-8",
        newline="\n",
    )
    session = ["--target", str(workspace), "--orchestration-id", "wheel-smoke", "--agent-id", "artifact-smoke"]
    run([str(cli), "orchestrate", "start", *session, "--format", "json"], cwd=outside)
    run([str(cli), "orchestrate", "next", *session, "--format", "json"], cwd=outside)
    status = json.loads(
        run(
            [str(cli), "orchestrate", "status", "--target", str(workspace), "--orchestration-id", "wheel-smoke", "--format", "json"],
            cwd=outside,
        )
    )
    session_document = status.get("session", status)
    if session_document.get("pending_action_id") != "action-0001":
        raise ValidationError(f"{label}: installed orchestration did not issue its first action: {session_document}")
    probe = run(
        [str(python), "-c", INSTALLED_PROBE, expected_version or "", str(pack_refresh_path)],
        cwd=outside,
    )
    probe_result = json.loads(probe.strip().splitlines()[-1])
    run([str(python), str(fixture_root / "tools/smoke_installed_orchestration.py"), "--cli", str(cli)], cwd=outside)
    publication = run([
        str(python), "-c", PUBLICATION_PROBE, str(cli),
        str(fixture_root / "tests/_publication_fixture.py"),
        str(fixture_root / "tests/fixtures/workspace-init-profile.yml"),
        str(scratch / "publication-workspace"),
    ], cwd=outside)
    packets = run([
        str(python), "-c", PACKET_PROBE, str(cli),
        str(fixture_root / "tests/fixtures/codebase-intake/native-packets"),
        str(scratch / "packet-workspace"),
    ], cwd=outside)
    execution = run([
        str(python), "-c", EXECUTION_PROBE, str(cli), str(fixture_root / "tests/_execution_fixture.py"),
        str(scratch / "execution-workspace"),
    ], cwd=outside)
    usage = run([
        str(python), "-c", USAGE_PROBE, str(cli), str(fixture_root / "tests/_usage_fixture.py"),
        str(scratch / "usage-evidence"),
    ], cwd=outside)
    snapshots = run([
        str(python), "-c", SNAPSHOT_PROBE, str(cli), str(fixture_root / "tests/_snapshot_fixture.py"),
        str(scratch / "snapshot-evidence"),
    ], cwd=outside)
    temporal = run([
        str(python), "-c", TEMPORAL_PROBE, str(cli), str(fixture_root / "tests/_temporal_fixture.py"),
        str(scratch / "temporal-evidence"),
    ], cwd=outside)
    market = run([
        str(python), "-c", MARKET_PROBE, str(cli), str(fixture_root / "tests/_market_fixture.py"),
        str(scratch / "market-evidence"),
    ], cwd=outside)
    historical = run([
        str(python), "-c", HISTORICAL_EXECUTION_PROBE, str(cli), str(fixture_root / "tests/_historical_fixture.py"),
        str(scratch / "historical-execution"),
    ], cwd=outside)
    simulation = run([
        str(python), "-c", SIMULATION_PROBE, str(cli), str(fixture_root / "tests/_simulation_fixture.py"),
        str(scratch / "market-simulation"),
    ], cwd=outside)
    assessments = run([
        str(python), "-c", ASSESSMENT_PROBE, str(cli), str(fixture_root / "tests/_assessment_fixture.py"),
        str(scratch / "assessment-evidence"),
    ], cwd=outside)
    computation = run([str(python), "-c", COMPUTATION_PROBE, str(cli), str(scratch / "computation-workspace")], cwd=outside)
    sources = run([str(python), "-c", SOURCE_PROBE, str(cli), str(scratch / "source-workspace")], cwd=outside)
    planning = run([str(python), "-c", PLANNING_PROBE, str(cli), str(scratch / "research-planning")], cwd=outside)
    research = run([str(python), "-c", RESEARCH_PROBE, str(cli), str(scratch / "caller-research")], cwd=outside)
    revisions = run([str(python), "-c", REVISION_PROBE, str(cli), str(scratch / "pack-revisions")], cwd=outside)
    setup = run([str(python), "-c", SETUP_PROBE, str(cli), str(scratch / "workspace-application")], cwd=outside)
    extensions = run([str(python), "-I", str(fixture_root / "tools/probe_installed_extensions.py"), "--root", str(scratch / "scoped-extensions"),
                      "--docx-fixture", str(fixture_root / "tests/_docx_fixture.py")], cwd=outside) if os.name == "posix" else json.dumps({"scoped_extensions": "unsupported_platform"})
    authoring = run([str(python), "-c", PACK_AUTHORING_PROBE, str(cli), str(scratch / "pack-authoring"),
        str(scratch / "research-planning/request.json")], cwd=outside)
    journey_reports = (_OPTIONS.evidence / label / "journeys") if _OPTIONS.evidence is not None else scratch / "journeys"
    run([str(python), "-B", str(fixture_root / "tools/qualify_journeys.py"), "--output", str(scratch / "journeys"),
         "--report-dir", str(journey_reports), "--workers", str(_OPTIONS.journey_workers),
         "--case-timeout", str(_OPTIONS.case_timeout)], cwd=outside, label="research-journeys",
        timeout=_OPTIONS.journey_timeout, stream_stderr=True)
    journeys = json.loads((journey_reports / "observations.json").read_text(encoding="utf-8"))
    if journeys["status"] != "passed" or not Path(journeys["package_location"]).is_relative_to(venv.resolve()):
        raise ValidationError("installed journeys failed or imported a package outside the isolated environment")
    return {"label": label, "fixture_inputs_sha256": sha256_of(fixture_root / "inputs.json"), "checkout_imports": "disabled", **probe_result, "managed_smoke": "passed", **json.loads(publication),
            **json.loads(packets), **json.loads(execution), **json.loads(usage), **json.loads(snapshots),
            **json.loads(temporal), **json.loads(market), **json.loads(historical), **json.loads(simulation),
            **json.loads(assessments), **json.loads(computation), **json.loads(agent_probe), **json.loads(pack_probe), **json.loads(sources),
            **json.loads(planning), **json.loads(authoring), **json.loads(setup), **json.loads(research), **json.loads(revisions),
            **json.loads(extensions), **json.loads(native_initialization),
            "journeys": journeys}


def build_wheel_from_sdist(sdist: Path, scratch: Path) -> Path:
    """Unpack the sdist and build a wheel from it, away from the checkout."""
    unpack_root = scratch / "sdist-unpacked"
    unpack_root.mkdir()
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            target = (unpack_root / member.name).resolve()
            if unpack_root.resolve() not in target.parents:
                raise ValidationError(f"{sdist.name}: member escapes its root directory: {member.name}")
            if not member.isfile() and not member.isdir():
                raise ValidationError(f"{sdist.name}: links and special archive members are not supported: {member.name}")
        # Explicit filtering avoids changing extraction semantics between Python versions.
        options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        archive.extractall(unpack_root, **options)  # noqa: S202 - contained regular files/directories only.
    roots = [child for child in unpack_root.iterdir() if child.is_dir()]
    if len(roots) != 1:
        raise ValidationError(f"{sdist.name}: expected one project root, found {[root.name for root in roots]}")
    out_dir = scratch / "sdist-dist"
    run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out_dir), str(roots[0])])
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ValidationError(f"{sdist.name}: building from the sdist produced {[path.name for path in wheels]}")
    return wheels[0]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist-dir", type=Path, default=REPO_ROOT / "dist", help="Directory holding one wheel and one sdist.")
    parser.add_argument("--expected-version", default=None, help="Version the installed package must report.")
    parser.add_argument("--artifact", choices=("both", "wheel", "sdist"), default="both",
                        help="Select one independently rerunnable installation; the final gate requires both reports.")
    parser.add_argument("--skip-sdist", action="store_true", help="Validate only the wheel (not for release use).")
    parser.add_argument("--membership-only", action="store_true", help="Check archive contents without installing.")
    parser.add_argument("--evidence-dir", type=Path, help="New private directory retaining full installed inputs, journeys and failed diagnostics.")
    parser.add_argument("--journey-workers", type=int, choices=range(1, 9), default=1)
    parser.add_argument("--command-timeout", type=positive_seconds, default=900)
    parser.add_argument("--journey-timeout", type=positive_seconds, default=5400)
    parser.add_argument("--case-timeout", type=positive_seconds, default=1200)
    parser.add_argument("--heartbeat-seconds", type=positive_seconds, default=30)
    args = parser.parse_args(argv)
    if args.skip_sdist and args.artifact != "both":
        parser.error("--skip-sdist cannot be combined with --artifact")
    if args.skip_sdist:
        args.artifact = "wheel"
    return args


def validation_identity():
    members = ["tools/validate_installed_artifacts.py", *fixture_members()]
    return {name: sha256_of(REPO_ROOT / name) for name in members}


def validate_distributions(args, summary, scratch):
    wheel, sdist = find_artifacts(args.dist_dir.resolve())
    checks: dict[str, object] = {}
    summary.update({
        "wheel": {"name": wheel.name, "sha256": sha256_of(wheel)},
        "sdist": {"name": sdist.name, "sha256": sha256_of(sdist)},
        "expected_version": args.expected_version,
        "artifact": args.artifact,
        "validation_inputs": validation_identity(),
        "workflow": {key: os.environ.get(key) for key in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")},
        "checks": checks,
    })
    checks["membership"] = check_archive_membership(wheel, sdist)
    _RUNNER.event("passed", "archive-membership")
    if not args.membership_only:
        if args.artifact in {"both", "wheel"}:
            _OPTIONS.active_artifact = "wheel"
            wheel_scratch = scratch / "wheel"
            wheel_scratch.mkdir()
            checks["installed_wheel"] = validate_installed(
                create_venv_with_wheel(wheel_scratch, wheel), wheel_scratch, args.expected_version, "wheel"
            )
            _RUNNER.event("passed", "installed-wheel")
        if args.artifact in {"both", "sdist"}:
            _OPTIONS.active_artifact = "sdist"
            sdist_scratch = scratch / "sdist"
            sdist_scratch.mkdir()
            _RUNNER.event("started", "sdist-rebuild")
            rebuilt = build_wheel_from_sdist(sdist, sdist_scratch)
            direct_members = [name for name in wheel_members(wheel) if not name.endswith("RECORD")]
            rebuilt_members = [name for name in wheel_members(rebuilt) if not name.endswith("RECORD")]
            if direct_members != rebuilt_members:
                only_direct = sorted(set(direct_members) - set(rebuilt_members))
                only_rebuilt = sorted(set(rebuilt_members) - set(direct_members))
                raise ValidationError(
                    "the wheel built from the sdist does not match the direct wheel; "
                    f"only in direct: {only_direct[:10]}; only in sdist-built: {only_rebuilt[:10]}"
                )
            checks["installed_sdist"] = validate_installed(
                create_venv_with_wheel(sdist_scratch, rebuilt), sdist_scratch, args.expected_version, "sdist"
            )
            checks["installed_sdist"]["rebuilt_wheel_sha256"] = sha256_of(rebuilt)
            _RUNNER.event("passed", "installed-sdist")
    summary["status"] = "partial" if args.artifact != "both" or args.membership_only else "passed"
    summary["selection_status"] = "passed"


def retain_evidence(scratch, evidence):
    """Copy bounded inputs/results; keep large execution workspaces outside checkout."""
    for label in ("wheel", "sdist"):
        source, target = scratch / label, evidence / label
        if not source.is_dir():
            continue
        for name in ("qualification-inputs", "journeys"):
            directory = source / name
            if not directory.is_dir():
                continue
            if name == "qualification-inputs":
                shutil.copytree(directory, target / name, dirs_exist_ok=True)
            else:
                (target / name).mkdir(parents=True, exist_ok=True)
                for path in directory.glob("*.json"):
                    shutil.copyfile(path, target / name / path.name)


def write_job_summary(summary):
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination:
        return
    rows = sorted(summary.get("commands", []), key=lambda row: row.get("seconds", 0), reverse=True)
    with Path(destination).open("a", encoding="utf-8") as stream:
        stream.write(f"### Installed {summary.get('artifact', 'distribution')} validation: {summary['status']}\n\n")
        stream.write("| Stage | Result | Seconds |\n|---|---|---:|\n")
        for row in rows[:15]:
            stream.write(f"| {row['stage']} | {row['status']} | {row['seconds']:.1f} |\n")
        stream.write("\nFull command logs, progress and case results are retained in the diagnostics artifact.\n")


def main(argv: list[str] | None = None) -> int:
    global _RUNNER, _OPTIONS
    args = parse_args(argv)
    summary = {"status": "incomplete"}
    evidence = args.evidence_dir.resolve() if args.evidence_dir else None
    if evidence is not None:
        evidence.mkdir(parents=True, exist_ok=False)
    previous = _RUNNER, _OPTIONS
    _RUNNER = CommandRunner(evidence / "commands" if evidence is not None else None, heartbeat=args.heartbeat_seconds)
    _OPTIONS = argparse.Namespace(**vars(args), evidence=evidence, active_artifact="artifacts")
    scratch = None
    try:
        manager = (nullcontext(tempfile.mkdtemp(prefix="evidence-wiki-artifacts-")) if evidence
                   else tempfile.TemporaryDirectory(prefix="evidence-wiki-artifacts-"))
        with manager as tmpdir:
            scratch = Path(tmpdir).resolve()
            if scratch.is_relative_to(REPO_ROOT.resolve()):
                raise ValidationError("temporary execution root overlaps the checkout")
            summary["execution_root"] = str(scratch)
            if evidence is not None:
                (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
            validate_distributions(args, summary, scratch)
    except BaseException as error:
        summary.update(status="failed", error_type=type(error).__name__, error=str(error)[:8192])
        raise
    finally:
        try:
            summary["commands"] = _RUNNER.records
            if evidence is not None:
                try:
                    if scratch is not None and scratch.is_dir() and not scratch.is_relative_to(REPO_ROOT.resolve()):
                        retain_evidence(scratch, evidence)
                        summary["evidence_retention"] = "inputs_and_results_retained; external_execution_root_retained"
                except OSError as error:
                    summary.update(status="failed", evidence_retention="failed", retention_error=type(error).__name__)
                    raise
                finally:
                    (evidence / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        finally:
            _RUNNER, _OPTIONS = previous
            write_job_summary(summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    with interruptible():
        raise SystemExit(main())
