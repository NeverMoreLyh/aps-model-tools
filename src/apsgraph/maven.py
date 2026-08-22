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
import inspect
import json
import os
import concurrent.futures
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
JDK_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ProgressCallback = Callable[[str], None]


class MavenError(ValueError):
    """A Maven workspace operation failed and the index must not be published."""


@dataclass(frozen=True)
class MavenProject:
    path: Path
    artifact_id: str
    group_id: str
    dependencies: Tuple[str, ...]
    dependency_coordinates: Tuple[str, ...] = ()
    modules: Tuple[str, ...] = ()
    packaging: str = "jar"
    parent_artifact_id: str = ""
    parent_group_id: str = ""


@dataclass
class MavenBuildReport:
    projects: List[str]
    build_order: List[str]
    succeeded: List[str]
    failed: List[str]
    commands: List[List[str]]
    logs: List[str]
    jdk_profiles: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class MavenJdkRule:
    match: Tuple[str, ...]
    jdk: str


@dataclass(frozen=True)
class MavenJdkConfig:
    """Workspace-level Maven JDK selection policy."""

    default_profile: Optional[str] = None
    java_homes: Mapping[str, str] = field(default_factory=dict)
    rules: Tuple[MavenJdkRule, ...] = ()


@dataclass(frozen=True)
class MavenJdkSelection:
    profile: Optional[str]
    java_home: Optional[Path]

    def environment(self) -> Optional[Dict[str, str]]:
        if self.java_home is None:
            return None
        path = os.environ.get("PATH", "")
        return {
            **os.environ,
            "JAVA_HOME": str(self.java_home),
            "PATH": f"{self.java_home / 'bin'}{os.pathsep}{path}",
        }

    def summary(self) -> str:
        if self.profile is None:
            return "inherited"
        if self.java_home is None:
            return self.profile
        return f"{self.profile} ({self.java_home})"


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


CommandRunner = Callable[..., subprocess.CompletedProcess]


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


def default_runner(
    project: Path,
    command: Sequence[str],
    log_path: Path,
    env: Optional[Mapping[str, str]] = None,
    metadata: Optional[Mapping[str, object]] = None,
) -> subprocess.CompletedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    java_version = ""
    java_home = (env or {}).get("JAVA_HOME")
    if java_home:
        version_result = subprocess.run(
            [str(Path(java_home) / "bin" / "java"), "-version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        java_version = (version_result.stdout or "").strip()
    result = subprocess.run(
        list(command),
        cwd=str(project),
        env=dict(env) if env is not None else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    lines = ["$ " + " ".join(command)]
    for key, value in (metadata or {}).items():
        lines.append(f"# {key}: {value}")
    if java_version:
        lines.append("# java version: " + java_version.replace("\n", " | "))
    log_path.write_text(
        "\n".join(lines) + "\n\n" + (result.stdout or ""),
        encoding="utf-8",
    )
    return result


def _run_maven(
    runner: CommandRunner,
    project: Path,
    command: Sequence[str],
    log_path: Path,
    selection: Optional[MavenJdkSelection] = None,
    workspace: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    env = selection.environment() if selection else None
    metadata: Dict[str, object] = {"command": " ".join(command)}
    if workspace:
        metadata["workspace"] = str(workspace)
    metadata["project"] = str(project)
    if selection:
        metadata["jdk profile"] = selection.summary()
        if selection.java_home:
            metadata["JAVA_HOME"] = str(selection.java_home)
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        signature = inspect.Signature()

    if env is not None and not _accepts_keyword(signature, "env"):
        raise MavenError(
            f"Maven runner {runner!r} must accept an optional 'env' keyword to use JDK profiles"
        )
    keywords = {}
    if env is not None and _accepts_keyword(signature, "env"):
        keywords["env"] = env
    if _accepts_keyword(signature, "metadata"):
        keywords["metadata"] = metadata
    if not keywords:
        return runner(project, command, log_path)
    if env is not None and "env" not in keywords:
        raise MavenError(
            f"Maven runner {runner!r} must accept an optional 'env' keyword to use JDK profiles"
        )
    return runner(project, command, log_path, **keywords)
    return runner(project, command, log_path, env=env, metadata=metadata)


def _failure_message(message: str, log_path: Path, result: subprocess.CompletedProcess) -> str:
    output = result.stdout or ""
    try:
        output += "\n" + log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    if "ClassNotFoundException: javax.xml.bind.JAXBException" in output:
        return message + (
            "\nDetected JAXB compatibility error. JDK 11+ removed javax.xml.bind; "
            "legacy APS Maven plugins may require JDK 8. Configure maven.jdk in "
            ".apsgraph.json or run with --jdk 8."
        )
    return message


def _accepts_keyword(signature: inspect.Signature, name: str) -> bool:
    parameter = signature.parameters.get(name)
    if parameter is None:
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    return parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


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
    parent_root = root.find(tag("parent"))
    parent_group = ""
    if parent_root is not None:
        parent_group = parent_root.findtext(tag("groupId"), default="").strip()
    # A project may omit groupId and inherit it from its parent.  Although this
    # is a lightweight POM parse, retaining that inherited identity is enough
    # to distinguish Maven coordinates without invoking Maven during discovery.
    group = root.findtext(tag("groupId"), default=parent_group).strip()
    dependencies: List[str] = []
    dependency_coordinates: List[str] = []
    dependencies_root = root.find(tag("dependencies"))
    if dependencies_root is not None:
        for dependency in dependencies_root.findall(tag("dependency")):
            dependency_artifact = dependency.findtext(tag("artifactId"), default="").strip()
            dependency_group = dependency.findtext(tag("groupId"), default="").strip()
            if dependency_artifact:
                dependencies.append(dependency_artifact)
                dependency_coordinates.append(
                    f"{dependency_group}:{dependency_artifact}" if dependency_group else f":{dependency_artifact}"
                )
    modules: List[str] = []
    modules_root = root.find(tag("modules"))
    if modules_root is not None:
        for module in modules_root.findall(tag("module")):
            module_value = (module.text or "").strip()
            if module_value:
                modules.append(module_value)
    parent_artifact = ""
    if parent_root is not None:
        parent_artifact = parent_root.findtext(tag("artifactId"), default="").strip()

    return MavenProject(
        pom.parent,
        artifact,
        group,
        tuple(dict.fromkeys(dependencies)),
        tuple(dict.fromkeys(dependency_coordinates)),
        tuple(dict.fromkeys(modules)),
        (root.findtext(tag("packaging"), default="jar").strip() or "jar").lower(),
        parent_artifact,
        parent_group,
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


def maven_jdk_config_from_payload(payload: Mapping[str, object]) -> MavenJdkConfig:
    value = payload.get("jdk")
    if value is None:
        return MavenJdkConfig()
    if not isinstance(value, dict):
        raise ValueError("maven.jdk must be a JSON object")

    default_profile = value.get("default")
    if default_profile is not None and not isinstance(default_profile, str):
        raise ValueError("maven.jdk.default must be a string")

    raw_homes = value.get("javaHomes", {})
    if not isinstance(raw_homes, dict):
        raise ValueError("maven.jdk.javaHomes must be a JSON object")
    java_homes: Dict[str, str] = {}
    for profile, home in raw_homes.items():
        if not isinstance(profile, str) or not JDK_PROFILE_PATTERN.match(profile):
            raise ValueError(f"invalid JDK profile name: {profile!r}")
        if not isinstance(home, str) or not home.strip():
            raise ValueError(f"maven.jdk.javaHomes.{profile} must be a non-empty string")
        java_homes[profile] = home

    raw_rules = value.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError("maven.jdk.rules must be an array")
    rules: List[MavenJdkRule] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise ValueError("each maven.jdk.rules entry must be a JSON object")
        raw_match = raw_rule.get("match", [])
        if isinstance(raw_match, str):
            raw_match = [raw_match]
        if (
            not isinstance(raw_match, list) or not raw_match
            or not all(isinstance(item, str) and item for item in raw_match)
        ):
            raise ValueError("maven.jdk.rules[].match must be a string or array of strings")
        jdk = raw_rule.get("jdk")
        if not isinstance(jdk, str) or not jdk:
            raise ValueError("maven.jdk.rules[].jdk must be a non-empty string")
        unknown = set(raw_rule) - {"match", "jdk"}
        if unknown:
            raise ValueError(f"unknown maven.jdk.rules fields: {', '.join(sorted(unknown))}")
        rules.append(MavenJdkRule(tuple(raw_match), jdk))

    unknown = set(value) - {"default", "javaHomes", "rules"}
    if unknown:
        raise ValueError(f"unknown maven.jdk fields: {', '.join(sorted(unknown))}")
    return MavenJdkConfig(default_profile, java_homes, tuple(rules))


def load_maven_jdk_config(workspace: Path | str) -> MavenJdkConfig:
    config_path = Path(workspace) / ".apsgraph.json"
    if not config_path.is_file():
        return MavenJdkConfig()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{config_path} must contain a JSON object")
    maven_payload = payload.get("maven")
    if maven_payload is None:
        return MavenJdkConfig()
    if not isinstance(maven_payload, dict):
        raise ValueError("maven configuration must be a JSON object")
    return maven_jdk_config_from_payload(maven_payload)


def _project_match_values(workspace: Path, project: MavenProject) -> Set[str]:
    relative = _relative(workspace, project.path)
    return _project_exclusion_values(workspace, project) | {
        relative,
        f"{relative}/*",
    }


def _matched_profile(
    patterns: Mapping[str, str],
    workspace: Path,
    project: MavenProject,
) -> Optional[str]:
    values = _project_match_values(workspace, project)
    matches = [
        (pattern, profile) for pattern, profile in patterns.items()
        if any(fnmatch.fnmatch(value, pattern) for value in values)
    ]
    if len({profile for _, profile in matches}) > 1:
        raise MavenError(
            f"conflicting JDK overrides for {project.path}: "
            + ", ".join(f"{pattern}={profile}" for pattern, profile in matches)
        )
    return matches[0][1] if matches else None


def _configured_profile_for_project(
    config: MavenJdkConfig,
    workspace: Path,
    project: MavenProject,
) -> Optional[str]:
    values = _project_match_values(workspace, project)
    for rule in config.rules:
        if any(fnmatch.fnmatch(value, pattern) for value in values for pattern in rule.match):
            return rule.jdk
    return None


def _resolve_java_home(profile: str, configured_homes: Mapping[str, str]) -> Optional[Path]:
    configured = configured_homes.get(profile)
    if configured and configured != "auto":
        path = Path(configured).expanduser()
        if not path.is_dir():
            raise MavenError(f"JDK {profile} JAVA_HOME does not exist or is not a directory: {path}")
        return path.resolve()

    environment_name = f"JAVA_HOME_{profile}"
    environment_home = os.environ.get(environment_name)
    if environment_home:
        path = Path(environment_home).expanduser()
        if not path.is_dir():
            raise MavenError(
                f"{environment_name} does not exist or is not a directory: {path}"
            )
        return path.resolve()

    if configured == "auto":
        result = subprocess.run(
            ["/usr/libexec/java_home", "-v", profile],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            path = Path(result.stdout.strip())
            if path.is_dir():
                return path.resolve()
    raise MavenError(
        f"cannot resolve JDK {profile}; configure maven.jdk.javaHomes.{profile}, "
        f"set JAVA_HOME_{profile}, or install the JDK"
    )


def _project_coordinate(project: MavenProject) -> str:
    """Return the lightweight Maven identity used for workspace matching."""
    return (
        f"{project.group_id}:{project.artifact_id}"
        if project.group_id else f":{project.artifact_id}"
    )


def _parent_coordinate(project: MavenProject) -> Optional[str]:
    """Return a parent coordinate when this lightweight parser can identify one."""
    if not project.parent_artifact_id:
        return None
    return (
        f"{project.parent_group_id}:{project.parent_artifact_id}"
        if project.parent_group_id else f":{project.parent_artifact_id}"
    )


def _topological_projects(projects: Sequence[MavenProject]) -> List[MavenProject]:
    by_artifact: Dict[str, MavenProject] = {}
    for project in projects:
        if project.artifact_id in by_artifact:
            raise MavenError(
                f"duplicate Maven artifactId {project.artifact_id}: "
                f"{by_artifact[project.artifact_id].path} and {project.path}"
            )
        by_artifact[project.artifact_id] = project

    by_coordinate: Dict[str, MavenProject] = {
        _project_coordinate(project): project for project in projects
    }
    graph: Dict[str, Set[str]] = {project.artifact_id: set() for project in projects}
    for project in projects:
        for coordinate in project.dependency_coordinates:
            target = by_coordinate.get(coordinate)
            if target is not None and target.artifact_id != project.artifact_id:
                graph[project.artifact_id].add(target.artifact_id)

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


def _build_unit_graph(
    projects: Sequence[MavenProject],
) -> Tuple[List[Path], Dict[Path, Set[Path]], Dict[Path, Path]]:
    """Return Maven reactor roots, their dependency graph, and project owners.

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

    coordinate_to_root: Dict[str, Set[Path]] = {}
    for project in projects:
        coordinate_to_root.setdefault(_project_coordinate(project), set()).add(
            owner_root(project.path)
        )

    graph: Dict[Path, Set[Path]] = {root: set() for root in roots}
    for project in projects:
        source_root = owner_root(project.path)
        for dependency_coordinate in project.dependency_coordinates:
            for target_root in coordinate_to_root.get(dependency_coordinate, set()):
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
    return ordered, graph, {
        project.path.resolve(): owner_root(project.path).resolve()
        for project in projects
    }


def _build_units(projects: Sequence[MavenProject]) -> List[Path]:
    """Return Maven reactor roots in dependency order."""
    return _build_unit_graph(projects)[0]


def _owner_build_unit(project: Path, build_units: Sequence[Path]) -> Path:
    resolved = project.resolve()
    candidates = [
        root for root in build_units
        if resolved == root.resolve() or resolved.is_relative_to(root.resolve())
    ]
    candidates.sort(key=lambda root: len(root.parts), reverse=True)
    return candidates[0] if candidates else resolved


def resolve_jdk_selections(
    workspace: Path | str,
    projects: Sequence[MavenProject],
    build_units: Sequence[Path],
    config: Optional[MavenJdkConfig] = None,
    global_profile: Optional[str] = None,
    project_profiles: Optional[Mapping[str, str]] = None,
) -> Dict[Path, MavenJdkSelection]:
    """Resolve one JDK for every Maven reactor/build unit.

    CLI project overrides win over the CLI global override, which wins over
    workspace rules and the workspace default.  A reactor containing projects
    that require different profiles is rejected because one Maven process can
    have only one ``JAVA_HOME``.
    """
    root = Path(workspace).resolve()
    config = config or MavenJdkConfig()
    overrides = dict(project_profiles or {})
    projects_by_unit: Dict[Path, List[MavenProject]] = {
        unit.resolve(): [] for unit in build_units
    }
    for project in projects:
        projects_by_unit.setdefault(_owner_build_unit(project.path, build_units).resolve(), []).append(project)

    selections: Dict[Path, MavenJdkSelection] = {}
    for unit in build_units:
        resolved_unit = unit.resolve()
        unit_projects = projects_by_unit.get(resolved_unit, [])
        if not unit_projects:
            selections[resolved_unit] = MavenJdkSelection(None, None)
            continue

        cli_profiles: Set[Optional[str]] = set()
        for project in unit_projects:
            cli_profiles.add(_matched_profile(overrides, root, project))
        cli_profiles.discard(None)
        if len(cli_profiles) > 1:
            raise MavenError(
                f"build unit {unit} resolves to conflicting CLI JDK profiles: "
                f"{', '.join(sorted(str(value) for value in cli_profiles))}"
            )

        root_project = next(
            (project for project in unit_projects if project.path.resolve() == resolved_unit),
            unit_projects[0],
        )
        root_config_profile = _configured_profile_for_project(config, root, root_project)
        selected_profiles: Dict[Path, Optional[str]] = {}
        for project in unit_projects:
            if cli_profiles:
                selected_profiles[project.path.resolve()] = next(iter(cli_profiles))
            elif global_profile:
                selected_profiles[project.path.resolve()] = global_profile
            else:
                selected_profiles[project.path.resolve()] = (
                    _configured_profile_for_project(config, root, project)
                    or root_config_profile
                    or config.default_profile
                )

        required = {profile for profile in selected_profiles.values() if profile is not None}
        if len(required) > 1:
            details = ", ".join(
                f"{_relative(root, project)}={profile}"
                for project, profile in selected_profiles.items()
                if profile is not None
            )
            raise MavenError(
                f"build unit {unit} contains projects requiring different JDK profiles; "
                f"one Maven reactor cannot use multiple JAVA_HOME values: {details}"
            )
        profile = next(iter(required), None)
        java_home = _resolve_java_home(profile, config.java_homes) if profile else None
        selections[resolved_unit] = MavenJdkSelection(profile, java_home)
    return selections

def build_workspace(
    workspace: Path | str,
    goals: Sequence[str] = ("install",),
    skip_tests: bool = True,
    maven: str = "mvn",
    cache_dir: Path | str = ".apsgraph",
    runner: CommandRunner = default_runner,
    jdk_config: Optional[MavenJdkConfig] = None,
    jdk_profile: Optional[str] = None,
    project_jdk: Optional[Mapping[str, str]] = None,
    progress: Optional[ProgressCallback] = None,
    jobs: int = 1,
) -> Tuple[List[MavenProject], MavenBuildReport]:
    root = Path(workspace).resolve()
    normalized_goals = tuple(goals or ())
    if not normalized_goals:
        raise MavenError("at least one Maven goal is required")
    if jobs < 1:
        raise MavenError("jobs must be >= 1")
    projects = _topological_projects(discover_maven_projects(root))
    build_units, unit_graph, _ = _build_unit_graph(projects)
    jdk_selections = resolve_jdk_selections(
        root, projects, build_units, config=jdk_config,
        global_profile=jdk_profile, project_profiles=project_jdk,
    )
    logs = _resolve_cache(root, cache_dir) / "logs" / "maven"
    report = MavenBuildReport(
        projects=[str(project) for project in build_units],
        build_order=[],
        succeeded=[],
        failed=[],
        commands=[],
        logs=[],
        jdk_profiles=[],
    )

    reverse_graph: Dict[Path, Set[Path]] = {unit: set() for unit in build_units}
    for unit, dependencies in unit_graph.items():
        for dependency in dependencies:
            reverse_graph.setdefault(dependency, set()).add(unit)
    pending: Dict[Path, Set[Path]] = {
        unit: set(dependencies) for unit, dependencies in unit_graph.items()
    }
    completed_units: Set[Path] = set()
    completed_count = 0
    unit_order = {unit.resolve(): index for index, unit in enumerate(build_units)}
    completed_records: List[Tuple[int, List[str], str, Path]] = []

    def run_unit(project: Path) -> Tuple[Path, subprocess.CompletedProcess, List[str], str, Path]:
        selection = jdk_selections[project.resolve()]
        if progress:
            progress(
                f"build start {_relative(root, project)} using JDK {selection.summary()}"
            )
        command = [maven, "-B"]
        if skip_tests:
            command.append("-DskipTests")
        command.extend(normalized_goals)
        log = logs / f"{_project_slug(root, project)}.log"
        result = _run_maven(
            runner, project, command, log, selection=selection, workspace=root
        )
        if result.returncode != 0:
            raise MavenError(
                _failure_message(
                    f"Maven build failed for {project} (exit {result.returncode}); log: {log}",
                    log,
                    result,
                )
            )
        return project, result, list(command), selection.summary(), log

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=jobs, thread_name_prefix="apsgraph-build"
    ) as executor:
        futures: Dict[concurrent.futures.Future, Path] = {}
        active_units: Set[Path] = set()

        def schedule_ready() -> None:
            for unit in build_units:
                resolved = unit.resolve()
                if (
                    resolved in pending
                    and not pending[resolved]
                    and resolved not in completed_units
                    and resolved not in active_units
                ):
                    future = executor.submit(run_unit, unit)
                    futures[future] = resolved
                    active_units.add(resolved)

        schedule_ready()
        while futures:
            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_EXCEPTION
            )
            failures: List[BaseException] = []
            failed_unit: Optional[Path] = None
            for future in done:
                unit = futures.pop(future)
                active_units.discard(unit)
                try:
                    project, result, command, jdk_profile, log = future.result()
                except BaseException as exc:
                    failures.append(exc)
                    failed_unit = failed_unit or unit
                    continue
                completed_count += 1
                if progress:
                    progress(
                        f"build complete [{completed_count}/{len(build_units)}] "
                        f"{_relative(root, project)} using JDK {jdk_profile}"
                    )
                completed_records.append((unit_order[unit], list(command), jdk_profile, log))
                completed_units.add(unit)
                for dependent in reverse_graph.get(unit, set()):
                    pending[dependent].discard(unit)
            if failures:
                for future in futures:
                    future.cancel()
                if failed_unit:
                    report.failed.append(str(failed_unit))
                raise failures[0]
            schedule_ready()

    for _, command, jdk_profile, log in sorted(completed_records, key=lambda item: item[0]):
        report.commands.append(command)
        report.logs.append(str(log))
        report.jdk_profiles.append(jdk_profile)
    report.build_order = [str(unit) for unit in build_units]
    report.succeeded = list(report.build_order)
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


def _dependency_analysis_projects(
    projects: Sequence[MavenProject],
) -> Tuple[List[MavenProject], List[str]]:
    """Select runtime modules plus the top boundary of workspace parent chains.

    ``dependency:list`` on every intermediate parent duplicates inherited
    dependencies and can run for several minutes without adding a coordinate.
    A referenced workspace parent whose own parent is also local is only an
    intermediate link and is skipped.  The topmost local parent is analyzed so
    dependencies introduced at the workspace/external-parent boundary remain
    visible even when it has ``packaging=pom`` or aggregator modules.  Ordinary
    packaging/aggregator POMs that are not parent-chain boundaries are skipped.
    """
    project_coordinates = {_project_coordinate(project) for project in projects}
    referenced_parents = {
        coordinate for coordinate in map(_parent_coordinate, projects)
        if coordinate is not None
    }
    selected: List[MavenProject] = []
    excluded: List[str] = []
    for project in projects:
        coordinate = _project_coordinate(project)
        is_workspace_parent = coordinate in referenced_parents
        has_workspace_parent = _parent_coordinate(project) in project_coordinates
        reason = ""
        if is_workspace_parent and has_workspace_parent:
            reason = "intermediate workspace parent"
        elif not is_workspace_parent and project.packaging == "pom":
            reason = "packaging=pom"
        elif not is_workspace_parent and project.modules:
            reason = "Maven aggregator with modules"
        if reason:
            excluded.append(
                f"{project.artifact_id} ({project.path}); reason: {reason}"
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
    jdk_selections: Optional[Mapping[Path, MavenJdkSelection]] = None,
    progress: Optional[ProgressCallback] = None,
    jobs: int = 1,
) -> DependencyInventory:
    root = Path(workspace).resolve()
    cache = _resolve_cache(root, cache_dir)
    deps_dir = cache / "deps"
    logs_dir = cache / "logs" / "maven-dependencies"
    if jobs < 1:
        raise MavenError("jobs must be >= 1")
    if deps_dir.exists():
        shutil.rmtree(deps_dir)
    deps_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    explicit_projects, excluded_project_records = _selected_projects(
        root, projects, excluded_projects
    )
    analysis_projects, structural_excluded = _dependency_analysis_projects(
        explicit_projects
    )
    excluded_project_records.extend(structural_excluded)
    allowed_scopes = _selected_scopes(scope)
    project_coordinates = {_project_coordinate(project) for project in projects}
    all_dependencies: List[MavenDependency] = []
    excluded_reactor: Set[str] = set()

    def jdk_for(project: MavenProject) -> Optional[MavenJdkSelection]:
        if not jdk_selections:
            return None
        resolved = project.path.resolve()
        candidates = [
            (unit, selection) for unit, selection in jdk_selections.items()
            if resolved == unit.resolve() or resolved.is_relative_to(unit.resolve())
        ]
        candidates.sort(key=lambda item: len(item[0].parts), reverse=True)
        return candidates[0][1] if candidates else None

    def resolve_project(
        project: MavenProject,
    ) -> Tuple[MavenProject, List[MavenDependency], List[str]]:
        selection = jdk_for(project)
        jdk_label = f" using JDK {selection.summary()}" if selection else ""
        if progress:
            progress(
                f"dependencies start {_relative(root, project.path)}{jdk_label}"
            )
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
        result = _run_maven(
            runner, project.path, list_command, list_log,
            selection=selection, workspace=root,
        )
        if result.returncode != 0:
            raise MavenError(
                _failure_message(
                    f"Maven dependency:list failed for {project.path} "
                    f"(exit {result.returncode}); log: {list_log}",
                    list_log,
                    result,
                )
            )
        if not list_output.is_file():
            raise MavenError(f"Maven dependency:list did not create output: {list_output}")

        selected: List[MavenDependency] = []
        reactor_excluded: List[str] = []
        for parsed in _parse_dependency_list(list_output):
            dependency = replace(parsed, project=str(project.path))
            identity = f"{dependency.group_id}:{dependency.artifact_id}:{dependency.version}"
            dependency_coordinate = f"{dependency.group_id}:{dependency.artifact_id}"
            if dependency_coordinate in project_coordinates:
                reactor_excluded.append(identity)
                continue
            if dependency.scope not in allowed_scopes or dependency.dependency_type != "jar":
                continue
            selected.append(dependency)

        output_dir = deps_dir / slug
        output_dir.mkdir(parents=True, exist_ok=True)
        copy_command = [
            maven, "-B", "dependency:copy-dependencies",
            f"-DincludeScope={scope}",
            f"-DoutputDirectory={output_dir.resolve()}",
            "-Doverwrite=true",
        ]
        copy_log = logs_dir / f"{slug}.copy.log"
        result = _run_maven(
            runner, project.path, copy_command, copy_log,
            selection=selection, workspace=root,
        )
        if result.returncode != 0:
            raise MavenError(
                _failure_message(
                    f"Maven dependency:copy-dependencies failed for {project.path} "
                    f"(exit {result.returncode}); log: {copy_log}",
                    copy_log,
                    result,
                )
            )
        return project, selected, reactor_excluded

    completed_count = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(jobs, len(analysis_projects) or 1),
        thread_name_prefix="apsgraph-dependencies",
    ) as executor:
        future_to_project = {
            executor.submit(resolve_project, project): project
            for project in analysis_projects
        }
        failures: List[BaseException] = []
        for future in concurrent.futures.as_completed(future_to_project):
            try:
                project, selected, reactor = future.result()
            except BaseException as exc:
                failures.append(exc)
                continue
            completed_count += 1
            all_dependencies.extend(selected)
            excluded_reactor.update(reactor)
            if progress:
                progress(
                    f"dependencies complete [{completed_count}/{len(analysis_projects)}] "
                    f"{_relative(root, project.path)}"
                )
        if failures:
            raise failures[0]

    if progress:
        progress("stage 4/7 check dependency versions")
    # Conflict detection uses the Maven coordinate groupId:artifactId. Two
    # different groups may legitimately reuse an artifactId (for example both
    # org.jetbrains:annotations and com.google.android:annotations).
    artifact_versions: Dict[Tuple[str, str], Set[str]] = {}
    artifact_sources: Dict[Tuple[str, str], Set[str]] = {}
    for dependency in all_dependencies:
        coordinate = (dependency.group_id, dependency.artifact_id)
        artifact_versions.setdefault(coordinate, set()).add(dependency.version)
        artifact_sources.setdefault(coordinate, set()).add(
            f"{dependency.group_id}:{dependency.version}:{dependency.project}"
        )
    conflicts = {
        coordinate: sorted(versions)
        for coordinate, versions in artifact_versions.items()
        if len(versions) > 1
    }
    if conflicts:
        details = "; ".join(
            f"{group}:{artifact} -> {', '.join(versions)} "
            f"(sources: {', '.join(sorted(artifact_sources[(group, artifact)]))})"
            for (group, artifact), versions in sorted(conflicts.items())
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
    jdk_config: Optional[MavenJdkConfig] = None,
    jdk_profile: Optional[str] = None,
    project_jdk: Optional[Mapping[str, str]] = None,
    progress: Optional[ProgressCallback] = None,
    jobs: int = 1,
) -> FullScanResult:
    root = Path(workspace).resolve()
    target = Path(db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"workspace: {root}")
        progress("stage 1/7 discover Maven projects")
        progress("stage 2/7 build Maven workspace")
    projects, build_report = build_workspace(
        root, goals=goals, skip_tests=skip_tests, maven=maven,
        cache_dir=cache_dir, runner=runner, jdk_config=jdk_config,
        jdk_profile=jdk_profile, project_jdk=project_jdk, progress=progress,
        jobs=jobs,
    )
    build_units = _build_units(projects)
    jdk_selections = resolve_jdk_selections(
        root, projects, build_units, config=jdk_config,
        global_profile=jdk_profile, project_profiles=project_jdk,
    )
    if progress:
        progress(f"discovered {len(projects)} POMs, {len(build_units)} build units")
        progress("stage 3/7 resolve Maven dependencies")
    inventory = collect_dependencies(
        root, projects, scope=dependency_scope, maven=maven,
        cache_dir=cache_dir, excluded_projects=excluded_projects, runner=runner,
        jdk_selections=jdk_selections, progress=progress, jobs=jobs,
    )

    fd, staging_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".include-deps.tmp", dir=str(target.parent)
    )
    os.close(fd)
    staging = Path(staging_name)
    staging.unlink()
    try:
        if progress:
            progress("stage 5/7 scan workspace XML")
        scan_summary = scan_workspace(root, staging, fail_on_parse_error=fail_on_parse_error)
        if progress:
            progress("stage 6/7 import dependency JAR models")
        import_result = import_jar_models(staging, inventory.jars)
        if import_result["failed_files"]:
            raise MavenError(
                f"{import_result['failed_files']} dependency model file(s) failed to parse"
            )
        _annotate_manifest_models(inventory, import_result)
        if progress:
            progress("stage 7/7 publish index atomically")
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
    if progress:
        progress(f"complete: {target}")
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
    jdk_config: Optional[MavenJdkConfig] = None,
    jdk_profile: Optional[str] = None,
    project_jdk: Optional[Mapping[str, str]] = None,
    progress: Optional[ProgressCallback] = None,
    jobs: int = 1,
) -> Dict[str, object]:
    root = Path(workspace).resolve()
    target = Path(db_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"index database does not exist: {target}")

    if progress:
        progress(f"workspace: {root}")
        progress("stage 1/4 discover Maven projects")
    if build:
        if progress:
            progress("stage 2/4 build Maven workspace")
        projects, build_report = build_workspace(
            root, goals=goals, skip_tests=skip_tests, maven=maven,
            cache_dir=cache_dir, runner=runner, jdk_config=jdk_config,
            jdk_profile=jdk_profile, project_jdk=project_jdk, progress=progress,
            jobs=jobs,
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
            jdk_profiles=[],
        )
    build_units = _build_units(projects)
    jdk_selections = resolve_jdk_selections(
        root, projects, build_units, config=jdk_config,
        global_profile=jdk_profile, project_profiles=project_jdk,
    )
    if progress:
        progress("stage 3/4 resolve Maven dependencies")
    inventory = collect_dependencies(
        root, projects, scope=dependency_scope, maven=maven,
        cache_dir=cache_dir, excluded_projects=excluded_projects, runner=runner,
        jdk_selections=jdk_selections, progress=progress, jobs=jobs,
    )

    fd, staging_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".import-deps.tmp", dir=str(target.parent)
    )
    os.close(fd)
    staging = Path(staging_name)
    try:
        if progress:
            progress("stage 4/4 replace JAR models atomically")
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
