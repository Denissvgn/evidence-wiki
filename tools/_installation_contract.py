"""Source-owned distribution layouts and bounded archive admission."""

from __future__ import annotations

import base64
import configparser
import csv
import email.parser
import gzip
import hashlib
import io
import os
import stat
import struct
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 8192
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 65536
DEPENDENCY_MODULES = {"pypdf": "pypdf", "pyyaml": "yaml", "ruamel-yaml": "ruamel.yaml",
                      "tzdata": "tzdata", "typing-extensions": "typing_extensions"}


def tar_stream_bounds(path):
    """Bound raw gzip expansion and hidden extended headers before tar parsing."""
    total, count, ended = 0, 0, False
    with gzip.open(path, "rb") as stream:
        while header := stream.read(512):
            total += len(header)
            if total > MAX_EXPANDED_BYTES or len(header) != 512:
                raise ValueError("raw tar expansion exceeds bound or header is incomplete")
            if not any(header):
                ended = True
                continue
            if ended:
                raise ValueError("nonzero tar data after archive terminator")
            count += 1
            if count > MAX_MEMBERS:
                raise ValueError("raw tar member count exceeds bound")
            try:
                size = int(header[124:136].strip(b"\x00 ") or b"0", 8)
            except ValueError as error:
                raise ValueError("unsupported tar size encoding") from error
            if size < 0 or size > MAX_MEMBER_BYTES or header[156:157] in {b"x", b"g", b"L", b"K"} and size > MAX_METADATA_BYTES:
                raise ValueError("tar body or extended metadata exceeds bound")
            remaining = ((size + 511) // 512) * 512
            if total + remaining > MAX_EXPANDED_BYTES:
                raise ValueError("raw tar expansion exceeds bound")
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("incomplete tar body")
                total += len(chunk)
                remaining -= len(chunk)


def zip_metadata_bounds(path):
    with Path(path).open("rb") as stream:
        stream.seek(max(0, Path(path).stat().st_size - 65557))
        tail = stream.read(65557)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        raise ValueError("ZIP end record missing")
    _, disk, directory_disk, disk_count, count, size, _, comment = struct.unpack("<4s4H2IH", tail[index:index + 22])
    if disk or directory_disk or disk_count != count or count > MAX_MEMBERS or size > 8 * 1024 * 1024 or count == 65535:
        raise ValueError("ZIP metadata/count is outside bounded single-disk policy")
    if len(tail) - index != 22 + comment:
        raise ValueError("ZIP trailing data differs from end record")


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name or "\x00" in name
            or any(part in {".", ".."} for part in name.rstrip("/").split("/"))
            or path.as_posix() != name.rstrip("/")):
        raise ValueError("unsafe archive path: " + repr(name))
    return path


def source_file(root, path):
    root, path = Path(root).resolve(), Path(path).absolute()
    if not path.is_relative_to(root):
        raise ValueError("distribution source escapes candidate")
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ValueError("candidate source contains a link: " + str(item))
    if not path.is_file():
        raise ValueError("candidate source is not a regular file: " + str(path))
    return path


def source_layout(root):
    """Read the pinned backend's file selection, without building or running hooks."""
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder

    root = Path(root).resolve()
    for name in ("src", "tools", "tests", "docs", "workspace-template", "domain-packs", "orchestrator", "examples"):
        for directory, children, _ in os.walk(root / name, followlinks=False):
            parent = Path(directory)
            children[:] = [child for child in children if child not in {"__pycache__", ".venv", ".pytest_cache", ".research-cache", ".ruff_cache"}
                           and not (parent == root / "docs" and child in {"CR", "llm_wiki"})]
            if parent.is_symlink() or any((parent / child).is_symlink() for child in children):
                raise ValueError("candidate selection contains a linked directory")
    builders = {"wheel": WheelBuilder(str(root)), "sdist": SdistBuilder(str(root))}
    maps, sources = {}, set()
    for kind, builder in builders.items():
        members = {}
        build_data = builder.get_default_build_data()
        builder.set_build_data_defaults(build_data)
        with builder.config.set_build_data(build_data):
            for member in builder.recurse_included_files():
                distribution_path = Path(member.distribution_path).as_posix()
                safe_name(distribution_path)
                path = source_file(root, member.path)
                if distribution_path in members:
                    raise ValueError("duplicate configured distribution target")
                members[distribution_path] = path.relative_to(root).as_posix()
                sources.add(path)
        if len(members) > MAX_MEMBERS:
            raise ValueError("candidate distribution file count exceeds bound")
        maps[kind] = members
    builder = builders["wheel"]
    core = builder.metadata.core
    info = builder.artifact_project_id + ".dist-info/"
    generated_wheel = [info + name for name in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")]
    # The reviewed project license is a regular source file; metadata copies
    # have a distinct wheel locator but must retain these exact source bytes.
    maps["wheel"][info + "licenses/LICENSE"] = "LICENSE"
    sources.add(source_file(root, root / "LICENSE"))
    project = {"name": core.name, "version": core.version, "requires_python": core.requires_python,
               "dependencies": core.dependencies, "optional_dependencies": core.optional_dependencies,
               "scripts": core.scripts}
    return {"maps": maps, "generated": {"wheel": generated_wheel, "sdist": ["PKG-INFO"]},
            "project": project}, sources


def admitted_archive(path, kind):
    """Inspect every member before reading bodies or extracting any archive."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive is unavailable, linked or exceeds compressed byte bound")
    rows, roots, total = {}, set(), 0
    if kind == "wheel":
        zip_metadata_bounds(path)
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS:
                raise ValueError("archive member count exceeds bound")
            for member in members:
                safe_name(member.filename)
                if member.filename in rows:
                    raise ValueError("duplicate archive member: " + member.filename)
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1:
                    raise ValueError("archive contains a link, special or encrypted member")
                if (member.is_dir() and member.file_size != 0 or stat.S_IFMT(mode) == stat.S_IFDIR and not member.is_dir()
                        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or len(member.extra) + len(member.comment) > MAX_METADATA_BYTES):
                    raise ValueError("ZIP directory data or metadata is unsupported")
                if member.file_size > MAX_MEMBER_BYTES:
                    raise ValueError("archive member expanded size exceeds bound")
                total += member.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("archive expanded byte total exceeds bound")
                rows[member.filename] = {"size": member.file_size, "directory": member.is_dir()}
    elif kind == "sdist":
        tar_stream_bounds(path)
        # Streaming iteration permits an early count/size refusal; getmembers
        # could first materialize an attacker-controlled unbounded inventory.
        with tarfile.open(path, "r|gz") as archive:
            for index, member in enumerate(archive, 1):
                if index > MAX_MEMBERS:
                    raise ValueError("archive member count exceeds bound")
                parts = safe_name(member.name).parts
                roots.add(parts[0])
                if len(roots) != 1 or len(parts) < 2 and not member.isdir():
                    raise ValueError("source archive requires one shared project root")
                if member.name in rows:
                    raise ValueError("duplicate archive member: " + member.name)
                if not member.isfile() and not member.isdir():
                    raise ValueError("archive contains a link or special member")
                if member.isdir() and member.size:
                    raise ValueError("tar directory data is unsupported")
                if member.size > MAX_MEMBER_BYTES:
                    raise ValueError("archive member expanded size exceeds bound")
                total += member.size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("archive expanded byte total exceeds bound")
                rows[member.name] = {"size": member.size, "directory": member.isdir()}
    else:
        raise ValueError("unknown archive kind")
    return rows


def archive_bytes(path, kind):
    admitted = admitted_archive(path, kind)
    values = {}
    if kind == "wheel":
        with zipfile.ZipFile(path) as archive:
            for name, row in admitted.items():
                if not row["directory"]:
                    values[name] = archive.read(name)
    else:
        with tarfile.open(path, "r|gz") as archive:
            for member in archive:
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        values[member.name.split("/", 1)[1]] = stream.read(MAX_MEMBER_BYTES + 1)
    return values


def normalized_requirement(value):
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name

    requirement = Requirement(value)
    requirement.name = canonicalize_name(requirement.name)
    return str(requirement)


def project_requirements(project):
    values = list(project["dependencies"])
    for extra, requirements in project["optional_dependencies"].items():
        values.extend(row + '; extra == "' + extra + '"' for row in requirements)
    return sorted(map(normalized_requirement, values))


def check_metadata(data, project, entries=None):
    message = email.parser.BytesParser().parsebytes(data)
    for key, expected in (("Name", project["name"]), ("Version", project["version"]), ("Requires-Python", project["requires_python"])):
        if message.get_all(key) != [expected]:
            raise ValueError("distribution metadata differs from source: " + key)
    if sorted(map(normalized_requirement, message.get_all("Requires-Dist", []))) != project_requirements(project):
        raise ValueError("distribution dependency declarations differ from source")
    if entries is not None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(entries.decode("utf-8"))
        if parser.sections() != ["console_scripts"] or dict(parser["console_scripts"]) != project["scripts"]:
            raise ValueError("distribution entry points differ from source")


def check_source_archives(wheel, sdist, source, layout):
    for kind, path in (("wheel", wheel), ("sdist", sdist)):
        values = archive_bytes(path, kind)
        expected = set(layout["maps"][kind]) | set(layout["generated"][kind])
        if set(values) != expected:
            raise ValueError(f"{kind} exact member inventory differs; missing={sorted(expected-set(values))[:8]}, extra={sorted(set(values)-expected)[:8]}")
        allowed_directories = {parent.as_posix() for name in expected for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
        for name, row in admitted_archive(path, kind).items():
            if row["directory"]:
                relative = name.rstrip("/") if kind == "wheel" else name.rstrip("/").split("/", 1)[1] if "/" in name.rstrip("/") else None
                if relative is not None and relative not in allowed_directories:
                    raise ValueError("unbound archive directory: " + name)
        for member, relative in layout["maps"][kind].items():
            if relative not in source or hashlib.sha256(values[member]).hexdigest() != source[relative]:
                raise ValueError(kind + " bytes differ from candidate: " + member)
        if kind == "wheel":
            info = layout["generated"][kind][0].rsplit("/", 1)[0] + "/"
            check_metadata(values[info + "METADATA"], layout["project"], values[info + "entry_points.txt"])
            wheel_metadata = email.parser.BytesParser().parsebytes(values[info + "WHEEL"])
            if (wheel_metadata.get_all("Wheel-Version") != ["1.0"] or wheel_metadata.get_all("Root-Is-Purelib") != ["true"]
                    or wheel_metadata.get_all("Tag") != ["py3-none-any"] or wheel_metadata.get_all("Generator") != ["hatchling 1.30.1"]):
                raise ValueError("wheel runtime/tag/build metadata differs from frozen pure-Python contract")
            rows = list(csv.reader(io.StringIO(values[info + "RECORD"].decode("utf-8"))))
            if len(rows) != len(values) or any(len(row) != 3 for row in rows) or {row[0] for row in rows} != set(values):
                raise ValueError("wheel RECORD inventory differs")
            for name, checksum, size in rows:
                expected_hash = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(values[name]).digest()).decode().rstrip("=")
                if name == info + "RECORD":
                    if checksum or size:
                        raise ValueError("wheel RECORD self entry must be un-hashed")
                elif checksum != expected_hash or size != str(len(values[name])):
                    raise ValueError("wheel RECORD hash/size differs")
        else:
            check_metadata(values["PKG-INFO"], layout["project"])


def dependency_rules():
    """Freeze complete source-environment core declarations, including markers."""
    from importlib.metadata import distribution

    result = {}
    for name in ("pypdf", "pyyaml", "ruamel-yaml", "tzdata"):
        item = distribution(name)
        result[name] = {"requires_dist": sorted(item.requires or []), "requires_python": item.metadata.get("Requires-Python")}
    # This reviewed core closure has one conditional Python compatibility
    # dependency. It is admitted only when a frozen owner edge requires it.
    result["typing-extensions"] = {"requires_dist": []}
    return result


def validate_resolved_metadata(resolved, project, rules, runtime):
    """Evaluate real versions, Python limits and the active transitive closure."""
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import canonicalize_name
    from pip._vendor.packaging.version import Version

    if not isinstance(resolved, dict) or "evidence-wiki" not in resolved:
        raise ValueError("resolved distribution metadata missing")
    markers = runtime["marker_environment"]
    if (markers.get("python_full_version") != runtime["python_version"]
            or markers.get("python_version") != ".".join(runtime["python_version"].split(".")[:2])
            or markers.get("platform_system") != runtime["system"]
            or markers.get("platform_machine") != runtime["machine"]
            or markers.get("platform_python_implementation") != runtime["implementation"]):
        raise ValueError("dependency marker facts contradict selected runtime")
    environment = default_environment()
    if set(markers) != set(environment):
        raise ValueError("dependency marker inventory differs from observed runtime contract")
    environment.update(markers, extra="")
    package = resolved["evidence-wiki"]
    if (package.get("version") != project["version"] or package.get("requires_python") != project["requires_python"]
            or sorted(map(normalized_requirement, package.get("requires_dist", []))) != project_requirements(project)
            or package.get("entry_points") != project["scripts"]):
        raise ValueError("installed project metadata differs from frozen source")
    visited, pending = set(), [Requirement("evidence-wiki==" + project["version"])]
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate(environment=environment):
            continue
        if requirement.url or requirement.extras:
            raise ValueError("core closure selects an unreviewed URL or extra")
        name = canonicalize_name(requirement.name)
        if name not in resolved or name != "evidence-wiki" and name not in rules:
            raise ValueError("unreviewed or missing core dependency: " + name)
        metadata = resolved[name]
        version = Version(metadata["version"])
        if not requirement.specifier.contains(version, prereleases=False):
            raise ValueError("resolved dependency version violates frozen constraint: " + name)
        if metadata.get("requires_python") and not SpecifierSet(metadata["requires_python"]).contains(Version(runtime["python_version"]), prereleases=False):
            raise ValueError("resolved dependency does not support selected Python: " + name)
        if name != "evidence-wiki":
            rule = rules[name]
            if sorted(metadata.get("requires_dist", [])) != rule["requires_dist"]:
                raise ValueError("resolved dependency declarations differ from frozen owner: " + name)
            if "requires_python" in rule and metadata.get("requires_python") != rule["requires_python"]:
                raise ValueError("resolved dependency Python declaration differs from frozen owner: " + name)
        if name in visited:
            continue
        visited.add(name)
        for row in metadata.get("requires_dist", []):
            edge = Requirement(row)
            if edge.marker is None or edge.marker.evaluate(environment=environment):
                pending.append(edge)
    if set(resolved) != visited:
        raise ValueError("installed dependency inventory contains an inactive extra or undeclared distribution")
    return visited


def validate_dependency_origins(resolved, origins, prefix, *, read_files=False):
    expected = set(resolved) - {"evidence-wiki"}
    if not isinstance(origins, dict) or set(origins) != expected:
        raise ValueError("imported dependency origin inventory differs from active core closure")
    prefix = Path(prefix)
    if not prefix.is_absolute():
        raise ValueError("dependency environment prefix is unavailable")
    for name, value in origins.items():
        path = Path(value["origin"])
        if (value.get("module") != DEPENDENCY_MODULES[name] or not path.is_absolute() or not path.is_relative_to(prefix)
                or any(part in {".", ".."} for part in path.parts)
                or name != "typing-extensions" and value.get("version") != resolved[name]["version"]
                or value.get("version") is not None and value["version"] != resolved[name]["version"]
                or not isinstance(value.get("sha256"), str) or len(value["sha256"]) != 64
                or type(value.get("bytes")) is not int or not 0 < value["bytes"] <= MAX_MEMBER_BYTES):
            raise ValueError("imported dependency origin/version differs from selected installation: " + name)
        if read_files and (not path.is_file() or path.is_symlink() or path.stat().st_size != value["bytes"]
                           or hashlib.sha256(path.read_bytes()).hexdigest() != value["sha256"]):
            raise ValueError("imported dependency bytes differ from observed origin")
    return origins


def check_review_producer_inventory(root, layout):
    """Require complete owned source selection and both archive projections."""
    from evidence_wiki import _review_producer as producer

    root = Path(root)
    roots = (("package/evidence_wiki", root / "src/evidence_wiki"),
             *(("assets/" + name, root / name) for name in producer.ASSET_NAMES))
    observed = producer.observe_roots(roots, read=True)
    inventory = producer.inventory_document(observed[3][producer.INVENTORY_PATH])
    expected = set(inventory["paths"])
    if set(observed[0]) != expected:
        raise ValueError("review producer source inventory differs")

    def coordinate(path, kind):
        package_prefix = "evidence_wiki/" if kind == "wheel" else "src/evidence_wiki/"
        asset_prefix = "evidence_wiki/assets/" if kind == "wheel" else ""
        for name in producer.ASSET_NAMES:
            prefix = asset_prefix + name + "/"
            if path.startswith(prefix):
                return "assets/" + name + "/" + path[len(prefix):]
        if path.startswith(package_prefix):
            return "package/evidence_wiki/" + path[len(package_prefix):]
        return None

    for kind in ("wheel", "sdist"):
        projected = {}
        for member, source in layout["maps"][kind].items():
            logical = coordinate(member, kind)
            if logical is not None:
                producer.validate_path(logical)
                if logical in projected:
                    raise ValueError("duplicate review producer archive projection")
                projected[logical] = source
        if set(projected) != expected:
            raise ValueError(kind + " review producer owned membership differs from inventory")
        for logical, source in projected.items():
            expected_source = ("src/evidence_wiki/" + logical[len("package/evidence_wiki/"):]
                               if logical.startswith("package/") else logical[len("assets/"):])
            if source != expected_source:
                raise ValueError("review producer archive maps to an unrelated source")
    return inventory
