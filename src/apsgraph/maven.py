"""Maven workspace integration for APSGraph.

The integration deliberately remains optional: ordinary ``apsgraph scan`` is a
standard-library XML scan and never invokes Maven.  With ``--include-deps`` or
``import-maven-deps`` APSGraph builds workspace projects in reactor dependency
order, copies resolved dependency JARs, and imports their APS model XML before
atomically publishing the SQLite index.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .scanner import import_jar_models, recognized_suffix, scan_workspace
from .store import connect

DEP_SCOPES = ("compile", "runtime", "test")
_MAVEN_SCOPES = {"compile", "provided", "runtime", "system", "test"}
_EXCLUDED_DIRECTORIES = {
    ".git", ".apsgraph", ".codegraph", ".idea", "target", "node_modules", ".hermes"
}
DEFAULT_EXCLUDED_PROJECTS = ("*dist",)


class MavenError(ValueError):
    """A Maven workspace operation failed and the index must not be published."""


@dataclass(frozen=True)
class MavenProject:
    path: Path
    artifact_id: str
    dependencies: Tuple[str, ...]
    modules: Tuple[str, ...] = ()


@dataclass
class MavenBuildReport:
    projects: List[str]
    build_order: List[str]
    succeeded: List[str]
    failed: List[str]
    commands: List[List[str]]
    logs: List[str]


@dataclass(frozen=True)
class MavenDependency:
    group_id: str
    artifact_id: str
    version: str
    scope: str
    dependency_type: str
    classifier: str
    project: str


@dataclass
class ImportedJar:
    path: str
    sha256: str
    artifacts: List[str] = field(default_factory=list)
    model_files: int = 0


@dataclass
class DependencyInventory:
    dependencies: List[MavenDependency]
    jars: List[Path]
    jar_records: List[ImportedJar]
    duplicates: List[str]
    reactor_dependencies_excluded: List[str]
    jars_with_models: List[str]
    jars_without_models: List[str]
    manifest: Path
    scope: str
    projects_excluded: List[str]
    projects_analyzed: int

    def summary(self) -> Dict[str, object]:
        return {
            "dependency_files": len(self.dependencies),
            "jars_discovered": len(self.jars),
            "projects_analyzed": self.projects_analyzed,
            "projects_excluded": len(self.projects_excluded),
            "duplicates": len(self.duplicates),
            "reactor_dependencies_excluded": len(self.reactor_dependencies_excluded),
            "jars_with_models": len(self.jars_with_models),
            "jars_without_models": len(self.jars_without_models),
            "manifest": str(self.manifest),
        }


@dataclass
class FullScanResult:
    scan: Dict[str, object]
    build: Dict[str, object]
    dependencies: Dict[str, object]
    import_result: Dict[str, object]
    database: str


CommandRunner = Callable[[Path, Sequence[str], Path], subprocess.CompletedProcess]


def _relative(workspace_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _project_slug(workspace: Path, project: Path) -> str:
    relative = _relative(workspace, project)
    if relative == ".":
        return "root"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", relative)


def _resolve_cache(workspace: Path, cache_dir: Path | str) -> Path:
    cache = Path(cache_dir)
    return cache if cache.is_absolute() else workspace / cache


def default_runner(project: Path, command: Sequence[str], log_path: Path) -> subprocess.CompletedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        list(command),
        cwd=str(project),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(
        "$ " + " ".join(command) + "\n\n" + (result.stdout or ""),
        encoding="utf-8",
    )
    return result


def _pom_project(pom: Path) -> MavenProject:
    try:
        root = ET.parse(pom).getroot()
    except ET.ParseError as exc:
        raise MavenError(f"invalid Maven pom.xml {pom}: {exc}") from exc

    namespace = root.tag.rsplit("}", 1)[0].strip("{") if "}" in root.tag else ""

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}" if namespace else name

    artifact = root.findtext(tag("artifactId"), default="").strip()
    if not artifact:
        raise MavenError(f"Maven pom.xml has no artifactId: {pom}")
    dependencies: List[str] = []
    dependencies_root = root.find(tag("dependencies"))
    if dependencies_root is not None:
        for dependency in dependencies_root.findall(tag("dependency")):
            dependency_artifact = dependency.findtext(tag("artifactId"), default="").strip()
            if dependency_artifact:
                dependencies.append(dependency_artifact)
    modules: List[str] = []
    modules_root = root.find(tag("modules"))
    if modules_root is not None:
        for module in modules_root.findall(tag("module")):
            module_value = (module.text or "").strip()
            if module_value:
                modules.append(module_value)

    return MavenProject(
        pom.parent,
        artifact,
        tuple(dict.fromkeys(dependencies)),
        tuple(dict.fromkeys(modules)),
    )


def discover_maven_projects(workspace: Path | str) -> List[MavenProject]:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"workspace directory does not exist: {root}")
    projects: List[MavenProject] = []
    seen: Set[Path] = set()
    for pom in sorted(root.rglob("pom.xml")):
        relative = pom.relative_to(root)
        if any(part in _EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
            continue
        if pom.parent in seen:
            continue
        seen.add(pom.parent)
        projects.append(_pom_project(pom))
    if not projects:
        raise MavenError(f"workspace contains no Maven projects: {root}")
    return projects


def _topological_projects(projects: Sequence[MavenProject]) -> List[MavenProject]:
    by_artifact: Dict[str, MavenProject] = {}
    for project in projects:
        if project.artifact_id in by_artifact:
            raise MavenError(
                f"duplicate Maven artifactId {project.artifact_id}: "
                f"{by_artifact[project.artifact_id].path} and {project.path}"
            )
        by_artifact[project.artifact_id] = project

    graph: Dict[str, Set[str]] = {project.artifact_id: set() for project in projects}
    for project in projects:
        for dependency in project.dependencies:
            if dependency in by_artifact and dependency != project.artifact_id:
                graph[project.artifact_id].add(dependency)

    ordered: List[MavenProject] = []
    visiting: Set[str] = set()
    visited: Set[str] = set()

    def visit(artifact: str) -> None:
        if artifact in visited:
            return
        if artifact in visiting:
            raise MavenError(f"Maven project dependency cycle involving {artifact}")
        visiting.add(artifact)
        for dependency in sorted(graph[artifact]):
            visit(dependency)
        visiting.remove(artifact)
        visited.add(artifact)
        ordered.append(by_artifact[artifact])

    for artifact in sorted(graph):
        visit(artifact)
    return ordered


def _build_units(projects: Sequence[MavenProject]) -> List[Path]:
    """Return Maven reactor roots rather than invoking every nested module.

    Some APS Maven plugins resolve repository-relative files such as
    ``./scripts/validation.xml`` from the process working directory.  Building
    a nested module directly therefore fails even though building its
    aggregator from the aggregator directory succeeds.
    """
    referenced_paths: Set[Path] = set()
    for project in projects:
        for module in project.modules:
            referenced_paths.add((project.path / module).resolve())

    roots = [project.path for project in projects if project.path.resolve() not in referenced_paths]
    if not roots:
        # Defensive fallback: a module cycle would otherwise leave no roots.
        roots = [project.path for project in projects]

    path_to_root: Dict[Path, Path] = {}
    def owner_root(path: Path) -> Path:
        resolved = path.resolve()
        if resolved in path_to_root:
            return path_to_root[resolved]
        candidates = [root for root in roots if resolved == root.resolve()]
        if candidates:
            result = candidates[0]
        else:
            # Nested modules inherit the nearest ancestor reactor root.  If a
            # module is outside every root, Maven resolves it independently.
            candidates = [
                root for root in roots
                if resolved.is_relative_to(root.resolve())
            ]
            candidates.sort(key=lambda root: len(root.parts), reverse=True)
            result = candidates[0] if candidates else resolved
        path_to_root[resolved] = result
        return result

    artifact_to_root: Dict[str, Set[Path]] = {}
    for project in projects:
        artifact_to_root.setdefault(project.artifact_id, set()).add(owner_root(project.path))

    graph: Dict[Path, Set[Path]] = {root: set() for root in roots}
    for project in projects:
        source_root = owner_root(project.path)
        for dependency_artifact in project.dependencies:
            if dependency_artifact == project.artifact_id:
                continue
            for target_root in artifact_to_root.get(dependency_artifact, set()):
                if target_root != source_root:
                    graph[source_root].add(target_root)

    ordered: List[Path] = []
    visiting: Set[Path] = set()
    visited: Set[Path] = set()

    def visit(root_path: Path) -> None:
        resolved = root_path.resolve()
        if resolved in visited:
            return
        if resolved in visiting:
            raise MavenError(f"Maven workspace build-unit cycle involving {root_path}")
        visiting.add(resolved)
        for dependency in sorted(graph[resolved], key=lambda item: str(item)):
            visit(dependency)
        visiting.remove(resolved)
        visited.add(resolved)
        ordered.append(resolved)

    for root_path in sorted(roots, key=lambda item: str(item)):
        visit(root_path)
    return ordered

def build_workspace(
    workspace: Path | str,
    goals: Sequence[str] = ("install",),
    skip_tests: bool = True,
    maven: str = "mvn",
    cache_dir: Path | str = ".apsgraph",
    runner: CommandRunner = default_runner,
) -> Tuple[List[MavenProject], MavenBuildReport]:
    root = Path(workspace).resolve()
    normalized_goals = tuple(goals or ())
    if not normalized_goals:
        raise MavenError("at least one Maven goal is required")
    projects = _topological_projects(discover_maven_projects(root))
    build_units = _build_units(projects)
    logs = _resolve_cache(root, cache_dir) / "logs" / "maven"
    report = MavenBuildReport(
        projects=[str(project) for project in build_units],
        build_order=[],
        succeeded=[],
        failed=[],
        commands=[],
        logs=[],
    )
    for project in build_units:
        command = [maven, "-B"]
        if skip_tests:
            command.append("-DskipTests")
        command.extend(normalized_goals)
        log = logs / f"{_project_slug(root, project)}.log"
        result = runner(project, command, log)
        report.build_order.append(str(project))
        report.commands.append(list(command))
        report.logs.append(str(log))
        if result.returncode != 0:
            report.failed.append(str(project))
            raise MavenError(
                f"Maven build failed for {project} (exit {result.returncode}); log: {log}"
            )
        report.succeeded.append(str(project))
    return projects, report


def _selected_scopes(scope: str) -> Set[str]:
    if scope == "compile":
        return {"compile", "provided", "system"}
    if scope == "runtime":
        return {"compile", "runtime"}
    if scope == "test":
        return {"compile", "provided", "runtime", "system", "test"}
    raise MavenError(f"dependency scope must be one of {', '.join(DEP_SCOPES)}: {scope}")


def _parse_dependency_list(path: Path) -> List[MavenDependency]:
    dependencies: List[MavenDependency] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("The following files"):
            continue
        parts = line.split(":")
        if len(parts) == 5:
            group, artifact, dependency_type, version, scope_text = parts
            classifier = ""
        elif len(parts) == 6:
            group, artifact, dependency_type, classifier, version, scope_text = parts
        else:
            continue
        scope_match = re.search(r"\b(compile|provided|runtime|system|test)\b", scope_text)
        if not scope_match:
            continue
        dependencies.append(MavenDependency(
            group_id=group,
            artifact_id=artifact,
            version=version,
            scope=scope_match.group(1),
            dependency_type=dependency_type,
            classifier=classifier,
            project="",
        ))
    return dependencies


def _jar_filename(dependency: MavenDependency) -> str:
    suffix = f"-{dependency.classifier}.jar" if dependency.classifier else ".jar"
    return f"{dependency.artifact_id}-{dependency.version}{suffix}"


def _find_copied_jar(directory: Path, dependency: MavenDependency) -> Optional[Path]:
    expected_name = _jar_filename(dependency)
    expected = directory / expected_name
    if expected.is_file():
        return expected
    stem = expected.stem
    candidates = [path for path in directory.glob("*.jar") if path.stem == stem]
    return candidates[0] if len(candidates) == 1 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jar_has_models(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return any(
                recognized_suffix(Path(name))
                for name in archive.namelist()
                if not name.endswith("/")
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise MavenError(f"dependency is not a readable jar: {path}: {exc}") from exc


def _project_exclusion_values(workspace: Path, project: MavenProject) -> Set[str]:
    relative = _relative(workspace, project.path)
    return {
        project.artifact_id,
        project.path.name,
        relative,
        Path(relative).stem,
    }


def _selected_projects(
    workspace: Path,
    projects: Sequence[MavenProject],
    excluded_projects: Sequence[str],
) -> Tuple[List[MavenProject], List[str]]:
    selected: List[MavenProject] = []
    excluded: List[str] = []
    for project in projects:
        values = _project_exclusion_values(workspace, project)
        matching = [
            pattern for pattern in excluded_projects
            if any(fnmatch.fnmatch(value, pattern) for value in values)
        ]
        if matching:
            excluded.append(
                f"{project.artifact_id} ({_relative(workspace, project.path)}); patterns: "
                f"{', '.join(matching)}"
            )
        else:
            selected.append(project)
    return selected, excluded

def collect_dependencies(
    workspace: Path | str,
    projects: Sequence[MavenProject],
    scope: str = "runtime",
    maven: str = "mvn",
    cache_dir: Path | str = ".apsgraph",
    excluded_projects: Sequence[str] = DEFAULT_EXCLUDED_PROJECTS,
    runner: CommandRunner = default_runner,
) -> DependencyInventory:
    root = Path(workspace).resolve()
    cache = _resolve_cache(root, cache_dir)
    deps_dir = cache / "deps"
    logs_dir = cache / "logs" / "maven-dependencies"
    if deps_dir.exists():
        shutil.rmtree(deps_dir)
    deps_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    analysis_projects, excluded_project_records = _selected_projects(
        root, projects, excluded_projects
    )
    allowed_scopes = _selected_scopes(scope)
    project_artifacts = {project.artifact_id for project in projects}
    all_dependencies: List[MavenDependency] = []
    excluded_reactor: Set[str] = set()
    project_dependencies: Dict[str, List[MavenDependency]] = {}

    for project in analysis_projects:
        slug = _project_slug(root, project.path)
        list_log = logs_dir / f"{slug}.list.log"
        list_output = logs_dir / f"{slug}.dependencies.txt"
        if list_output.exists():
            list_output.unlink()
        list_command = [
            maven, "-B", "dependency:list",
            f"-DoutputFile={list_output.resolve()}",
            "-DoutputAbsoluteArtifactFilename=false",
        ]
        result = runner(project.path, list_command, list_log)
        if result.returncode != 0:
            raise MavenError(
                f"Maven dependency:list failed for {project.path} "
                f"(exit {result.returncode}); log: {list_log}"
            )
        if not list_output.is_file():
            raise MavenError(f"Maven dependency:list did not create output: {list_output}")

        selected: List[MavenDependency] = []
        for parsed in _parse_dependency_list(list_output):
            dependency = replace(parsed, project=str(project.path))
            identity = f"{dependency.group_id}:{dependency.artifact_id}:{dependency.version}"
            if dependency.artifact_id in project_artifacts:
                excluded_reactor.add(identity)
                continue
            if dependency.scope not in allowed_scopes or dependency.dependency_type != "jar":
                continue
            all_dependencies.append(dependency)
            selected.append(dependency)
        project_dependencies[str(project.path)] = selected

        output_dir = deps_dir / slug
        output_dir.mkdir(parents=True, exist_ok=True)
        copy_command = [
            maven, "-B", "dependency:copy-dependencies",
            f"-DincludeScope={scope}",
            f"-DoutputDirectory={output_dir.resolve()}",
            "-Doverwrite=true",
        ]
        copy_log = logs_dir / f"{slug}.copy.log"
        result = runner(project.path, copy_command, copy_log)
        if result.returncode != 0:
            raise MavenError(
                f"Maven dependency:copy-dependencies failed for {project.path} "
                f"(exit {result.returncode}); log: {copy_log}"
            )

    # The user chose fail-closed conflict handling.  Do not try to select the
    # nearest/newest version and do not import a partial graph.
    artifact_versions: Dict[str, Set[str]] = {}
    artifact_sources: Dict[str, Set[str]] = {}
    for dependency in all_dependencies:
        artifact_versions.setdefault(dependency.artifact_id, set()).add(dependency.version)
        artifact_sources.setdefault(dependency.artifact_id, set()).add(
            f"{dependency.group_id}:{dependency.version}:{dependency.project}"
        )
    conflicts = {
        artifact_id: sorted(versions)
        for artifact_id, versions in artifact_versions.items()
        if len(versions) > 1
    }
    if conflicts:
        details = "; ".join(
            f"{artifact_id} -> {', '.join(versions)} "
            f"(sources: {', '.join(sorted(artifact_sources[artifact_id]))})"
            for artifact_id, versions in sorted(conflicts.items())
        )
        raise MavenError(f"conflicting Maven dependency versions are not allowed: {details}")

    unique_dependencies: Dict[Tuple[str, str, str, str], MavenDependency] = {}
    for dependency in all_dependencies:
        key = (dependency.group_id, dependency.artifact_id, dependency.version, dependency.classifier)
        unique_dependencies[key] = dependency

    jar_by_hash: Dict[str, Path] = {}
    jar_by_coordinate: Dict[str, ImportedJar] = {}
    duplicates: List[str] = []
    missing: List[str] = []
    for dependency in sorted(
        unique_dependencies.values(),
        key=lambda item: (item.group_id, item.artifact_id, item.version, item.classifier),
    ):
        output_dir = deps_dir / _project_slug(root, Path(dependency.project))
        copied = _find_copied_jar(output_dir, dependency)
        if copied is None:
            missing.append(
                f"{dependency.group_id}:{dependency.artifact_id}:"
                f"{dependency.version}:{dependency.classifier or 'jar'}"
            )
            continue
        digest = _sha256(copied)
        coordinate = f"{dependency.group_id}:{dependency.artifact_id}:{dependency.version}"
        existing = jar_by_coordinate.get(coordinate)
        if existing is not None and existing.sha256 != digest:
            raise MavenError(f"same Maven artifact resolved to different jar content: {coordinate}")
        if digest in jar_by_hash:
            duplicates.append(f"{coordinate} == {jar_by_hash[digest].name}")
            if existing is not None:
                existing.artifacts.append(coordinate)
            continue
        jar_by_hash[digest] = copied
        jar_by_coordinate[coordinate] = ImportedJar(
            path=str(copied),
            sha256=digest,
            artifacts=[coordinate],
        )

    if missing:
        raise MavenError(f"Maven did not copy resolved dependencies: {', '.join(sorted(missing))}")

    records = list(jar_by_coordinate.values())
    jars = [Path(record.path) for record in records]
    with_models: List[str] = []
    without_models: List[str] = []
    for jar in jars:
        (with_models if _jar_has_models(jar) else without_models).append(str(jar))

    manifest = cache / "dependency-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scope": scope,
        "dependencies": [asdict(dependency) for dependency in sorted(
            all_dependencies,
            key=lambda item: (
                item.group_id, item.artifact_id, item.version, item.classifier, item.project
            ),
        )],
        "jars": [asdict(record) for record in records],
        "reactor_dependencies_excluded": sorted(excluded_reactor),
        "projects_analyzed": len(analysis_projects),
        "projects_excluded": excluded_project_records,
    }
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return DependencyInventory(
        dependencies=all_dependencies,
        jars=jars,
        jar_records=records,
        duplicates=duplicates,
        reactor_dependencies_excluded=sorted(excluded_reactor),
        jars_with_models=with_models,
        jars_without_models=without_models,
        manifest=manifest,
        scope=scope,
        projects_excluded=excluded_project_records,
        projects_analyzed=len(analysis_projects),
    )


def _annotate_manifest_models(inventory: DependencyInventory, import_result: Mapping[str, object]) -> None:
    per_jar = import_result.get("per_jar", {})
    if not isinstance(per_jar, Mapping):
        return
    for record in inventory.jar_records:
        value = per_jar.get(record.path, per_jar.get(str(Path(record.path).resolve()), 0))
        record.model_files = int(value)
    inventory.manifest.write_text(
        json.dumps(
            {
                "scope": inventory.scope,
                "dependencies": [asdict(item) for item in inventory.dependencies],
                "jars": [asdict(item) for item in inventory.jar_records],
                "reactor_dependencies_excluded": inventory.reactor_dependencies_excluded,
                "projects_analyzed": inventory.projects_analyzed,
                "projects_excluded": inventory.projects_excluded,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def _copy_database(source: Path, destination: Path) -> None:
    source_conn = connect(source, read_only=True)
    destination_conn = sqlite3.connect(str(destination))
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def _clear_imported_jars(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute("delete from model_files where path like 'jar:%'")
    finally:
        conn.close()


def scan_workspace_with_dependencies(
    workspace: Path | str,
    db_path: Path | str,
    goals: Sequence[str] = ("install",),
    skip_tests: bool = True,
    dependency_scope: str = "runtime",
    maven: str = "mvn",
    cache_dir: Path | str = ".apsgraph",
    excluded_projects: Sequence[str] = DEFAULT_EXCLUDED_PROJECTS,
    fail_on_parse_error: bool = False,
    runner: CommandRunner = default_runner,
) -> FullScanResult:
    root = Path(workspace).resolve()
    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    projects, build_report = build_workspace(
        root, goals=goals, skip_tests=skip_tests, maven=maven,
        cache_dir=cache_dir, runner=runner,
    )
    inventory = collect_dependencies(
        root, projects, scope=dependency_scope, maven=maven,
        cache_dir=cache_dir, excluded_projects=excluded_projects, runner=runner,
    )

    fd, staging_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".include-deps.tmp", dir=str(target.parent)
    )
    os.close(fd)
    staging = Path(staging_name)
    staging.unlink()
    try:
        scan_summary = scan_workspace(root, staging, fail_on_parse_error=fail_on_parse_error)
        import_result = import_jar_models(staging, inventory.jars)
        if import_result["failed_files"]:
            raise MavenError(
                f"{import_result['failed_files']} dependency model file(s) failed to parse"
            )
        _annotate_manifest_models(inventory, import_result)
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
    return FullScanResult(
        scan=asdict(scan_summary),
        build=asdict(build_report),
        dependencies=inventory.summary(),
        import_result=import_result,
        database=str(target),
    )


def import_maven_dependencies(
    workspace: Path | str,
    db_path: Path | str,
    goals: Sequence[str] = ("install",),
    skip_tests: bool = True,
    dependency_scope: str = "runtime",
    build: bool = False,
    maven: str = "mvn",
    cache_dir: Path | str = ".apsgraph",
    excluded_projects: Sequence[str] = DEFAULT_EXCLUDED_PROJECTS,
    runner: CommandRunner = default_runner,
) -> Dict[str, object]:
    root = Path(workspace).resolve()
    target = Path(db_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"index database does not exist: {target}")

    if build:
        projects, build_report = build_workspace(
            root, goals=goals, skip_tests=skip_tests, maven=maven,
            cache_dir=cache_dir, runner=runner,
        )
    else:
        projects = _topological_projects(discover_maven_projects(root))
        build_report = MavenBuildReport(
            projects=[str(project.path) for project in projects],
            build_order=[str(project.path) for project in projects],
            succeeded=[],
            failed=[],
            commands=[],
            logs=[],
        )
    inventory = collect_dependencies(
        root, projects, scope=dependency_scope, maven=maven,
        cache_dir=cache_dir, excluded_projects=excluded_projects, runner=runner,
    )

    fd, staging_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".import-deps.tmp", dir=str(target.parent)
    )
    os.close(fd)
    staging = Path(staging_name)
    try:
        _copy_database(target, staging)
        _clear_imported_jars(staging)
        import_result = import_jar_models(staging, inventory.jars)
        if import_result["failed_files"]:
            raise MavenError(
                f"{import_result['failed_files']} dependency model file(s) failed to parse"
            )
        _annotate_manifest_models(inventory, import_result)
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
    return {
        "build": asdict(build_report),
        "dependencies": inventory.summary(),
        "import": import_result,
        "database": str(target),
    }
