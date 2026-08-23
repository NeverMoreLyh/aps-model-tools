import contextlib
import hashlib
import io
import json
import subprocess
import threading
import time
import tempfile
import unittest
import zipfile
from pathlib import Path

import apsgraph.cli as cli_module
from apsgraph.cli import build_parser
from apsgraph.maven import (
    MavenError,
    MavenJdkConfig,
    MavenJdkRule,
    build_workspace,
    collect_dependencies,
    discover_maven_projects,
    import_maven_dependencies,
    maven_jdk_config_from_payload,
    resolve_jdk_selections,
    scan_workspace_with_dependencies,
)
from apsgraph.scanner import import_jar_models, scan_workspace
from apsgraph.store import connect

WORKSPACE_MODEL = """<?xml version="1.0"?>
<schema id="WorkTables" package="work">
  <table id="work_user" name="work_user">
    <fields><field id="name" type="string" nullable="false"/></fields>
  </table>
</schema>
"""

JAR_MODEL = """<?xml version="1.0"?>
<schema id="FrameworkTypes" package="framework">
  <restrictionType id="U_NAME" base="string" maxLength="80"/>
</schema>
"""

OLD_JAR_MODEL = """<?xml version="1.0"?>
<schema id="OldFrameworkTypes" package="old">
  <restrictionType id="U_OLD" base="string" maxLength="1"/>
</schema>
"""


def write_pom(path: Path, artifact_id: str, dependencies=(), modules=(), packaging="jar", parent=None,
              group="demo", parent_group="demo", omit_group=False):
    dependency_xml = "".join(
        f"<dependency><groupId>{group}</groupId>"
        f"<artifactId>{artifact}</artifactId><version>{version}</version>"
        f"<scope>{scope}</scope></dependency>"
        for group, artifact, version, scope in dependencies
    )
    module_xml = ''.join(f'<module>{module}</module>' for module in modules)
    packaging_xml = f'<packaging>{packaging}</packaging>'
    parent_xml = ''
    if parent:
        parent_xml = (f'<parent><groupId>{parent_group}</groupId>'
                      f'<artifactId>{parent}</artifactId><version>1.0.0</version></parent>')
    group_xml = '' if omit_group else f'<groupId>{group}</groupId>'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<project xmlns="http://maven.apache.org/POM/4.0.0">'
        f'<modelVersion>4.0.0</modelVersion>'
        f'{parent_xml}{group_xml}<artifactId>{artifact_id}</artifactId>{packaging_xml}'
        f'<version>1.0.0</version><modules>{module_xml}</modules><dependencies>{dependency_xml}</dependencies>'
        f'</project>',
        encoding="utf-8",
    )


def write_model_jar(
    path: Path,
    model=JAR_MODEL,
    entry="models/Framework.u_schema.xml",
    marker_property="edsp-module",
    marker_value="true",
    group_id="com.example",
    artifact_id="shared",
    version="1.0.0",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    properties = ""
    if marker_property is not None:
        properties = f"<properties><{marker_property}>{marker_value}</{marker_property}></properties>"
    pom = (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<modelVersion>4.0.0</modelVersion>"
        f"<groupId>{group_id}</groupId><artifactId>{artifact_id}</artifactId>"
        f"<version>{version}</version>{properties}</project>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, model)
        archive.writestr(f"META-INF/maven/{group_id}/{artifact_id}/pom.xml", pom)


class FakeMaven:
    def __init__(self, dependencies, jars, fail_build=None):
        # dependencies maps project artifact id to Maven dependency-list lines.
        self.dependencies = dependencies
        # jars maps artifact coordinate (group:artifact:version) to optional model text.
        self.jars = jars
        self.fail_build = fail_build
        self.builds = []
        self.commands = []
        self.environments = []
        self.state_lock = threading.Lock()

    def __call__(self, project: Path, command, log: Path, env=None, metadata=None):
        log.parent.mkdir(parents=True, exist_ok=True)
        lines = ["$ " + " ".join(command)]
        if metadata:
            lines.extend(f"# {key}: {value}" for key, value in metadata.items())
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.state_lock:
            self.commands.append(list(command))
            self.environments.append(dict(env) if env else None)
        if "dependency:list" in command:
            artifact = project.name
            output = Path(next(value.split("=", 1)[1] for value in command if value.startswith("-DoutputFile=")))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("\n".join(self.dependencies[artifact]) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        if "dependency:copy-dependencies" in command:
            output = Path(next(value.split("=", 1)[1] for value in command if value.startswith("-DoutputDirectory=")))
            output.mkdir(parents=True, exist_ok=True)
            for dependency in self.dependencies[project.name]:
                group, artifact, dependency_type, version, scope = dependency.split(":")
                if artifact == "framework":
                    continue
                if scope == "test":
                    continue
                model = self.jars.get(f"{group}:{artifact}:{version}")
                path = output / f"{artifact}-{version}.jar"
                if model is None:
                    path.write_bytes(b"not a model jar but also not a zip")
                elif isinstance(model, dict):
                    write_model_jar(
                        path, JAR_MODEL, group_id=group,
                        version=version, **model,
                    )
                else:
                    write_model_jar(path, model)
            return subprocess.CompletedProcess(command, 0, "", "")
        with self.state_lock:
            self.builds.append(project.name)
        if self.fail_build == project.name:
            return subprocess.CompletedProcess(command, 1, "", "simulated failure")
        return subprocess.CompletedProcess(command, 0, "", "")


class MavenWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / ".apsgraph" / "apsgraph.db"

        write_pom(self.root / "framework/pom.xml", "framework")
        write_pom(
            self.root / "app/pom.xml",
            "app",
            [
                ("demo", "framework", "1.0.0", "compile"),
                ("com.example", "shared", "1.0.0", "compile"),
            ],
        )
        (self.root / "models/Work.tables.xml").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "models/Work.tables.xml").write_text(WORKSPACE_MODEL, encoding="utf-8")
        # Discovery must ignore generated POM copies.
        write_pom(self.root / "target/generated/pom.xml", "generated")

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, fail_build=None):
        return FakeMaven(
            dependencies={
                "framework": ["com.example:shared:jar:1.0.0:compile"],
                "app": [
                    "demo:framework:jar:1.0.0:compile",
                    "com.example:shared:jar:1.0.0:compile",
                ],
            },
            jars={"com.example:shared:1.0.0": JAR_MODEL},
            fail_build=fail_build,
        )

    @staticmethod
    def model_paths(db: Path):
        conn = connect(db, read_only=True)
        try:
            return {row[0] for row in conn.execute("select path from model_files")}
        finally:
            conn.close()

    @staticmethod
    def digest(path: Path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_discovery_and_build_order(self):
        projects = discover_maven_projects(self.root)
        self.assertEqual({"framework", "app"}, {project.artifact_id for project in projects})

        runner = self.runner()
        ordered, report = build_workspace(self.root, runner=runner)
        self.assertEqual(["framework", "app"], [project.artifact_id for project in ordered])
        self.assertEqual(["framework", "app"], runner.builds)
        self.assertEqual([str(project.path) for project in ordered], report.succeeded)
        self.assertIn("-DskipTests", report.commands[0])
        self.assertIn("install", report.commands[0])

    def test_aggregator_is_built_once_from_its_own_directory(self):
        root = self.root / "aggregate"
        write_pom(root / "pom.xml", "aggregate", modules=("framework", "app"))
        write_pom(root / "framework/pom.xml", "framework")
        write_pom(root / "app/pom.xml", "app", [("demo", "framework", "1.0.0", "compile")])
        runner = FakeMaven(dependencies={"aggregate": [], "framework": [], "app": []}, jars={})
        projects, report = build_workspace(root, runner=runner)
        self.assertEqual({project.artifact_id for project in projects}, {"aggregate", "framework", "app"})
        self.assertEqual([root.resolve()], [Path(value).resolve() for value in report.build_order])
        self.assertEqual([root.name], runner.builds)
        self.assertEqual([str(root.resolve())], report.succeeded)

    def test_cross_aggregator_build_order_uses_child_dependencies(self):
        isolated = self.root / "isolated"
        library = isolated / "library"
        application = isolated / "application"
        write_pom(library / "pom.xml", "library", modules=("framework",))
        write_pom(library / "framework/pom.xml", "lib-framework")
        write_pom(application / "pom.xml", "application", modules=("app",))
        write_pom(application / "app/pom.xml", "business-app", [("demo", "lib-framework", "1.0.0", "compile")])
        runner = FakeMaven(dependencies={"library": [], "framework": [], "application": [], "business-app": []}, jars={})
        projects, report = build_workspace(isolated, runner=runner)
        self.assertEqual({project.artifact_id for project in projects}, {"library", "lib-framework", "application", "business-app"})
        self.assertEqual([library.resolve(), application.resolve()], [Path(value).resolve() for value in report.build_order])
        self.assertEqual([library.name, application.name], runner.builds)

    def test_duplicate_project_artifact_and_cycle_fail(self):
        write_pom(self.root / "another/pom.xml", "app")
        with self.assertRaisesRegex(MavenError, "duplicate Maven artifactId app"):
            build_workspace(self.root, runner=self.runner())

        write_pom(self.root / "framework/pom.xml", "framework", [("demo", "app", "1.0.0", "compile")])
        # Replace duplicate with a separate project to create a framework/app cycle.
        write_pom(self.root / "another/pom.xml", "third")
        with self.assertRaisesRegex(MavenError, "dependency cycle"):
            build_workspace(self.root, runner=self.runner())

    def test_include_deps_publishes_workspace_and_framework_models(self):
        result = scan_workspace_with_dependencies(self.root, self.db, runner=self.runner())
        self.assertEqual(1, result.scan["parsed_files"])
        self.assertEqual(1, result.import_result["imported_files"])
        paths = self.model_paths(self.db)
        self.assertIn("models/Work.tables.xml", paths)
        self.assertTrue(any(path.startswith("jar:") and path.endswith("Framework.u_schema.xml") for path in paths))
        self.assertFalse(list(self.db.parent.glob("*.include-deps.tmp")))
        manifest = json.loads((self.root / ".apsgraph/dependency-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(1, manifest["jars"][0]["model_files"])
        self.assertTrue(all(record["artifacts"] == ["com.example:shared:1.0.0"] for record in manifest["jars"]))

    def test_build_failure_preserves_existing_index(self):
        scan_workspace(self.root, self.db)
        before = self.digest(self.db)
        with self.assertRaisesRegex(MavenError, "Maven build failed for .*app"):
            scan_workspace_with_dependencies(self.root, self.db, runner=self.runner(fail_build="app"))
        self.assertEqual(before, self.digest(self.db))
        self.assertEqual({"models/Work.tables.xml"}, self.model_paths(self.db))

    def test_projects_ending_with_dist_are_not_dependency_analyzed(self):
        write_pom(
            self.root / "delivery-dist/pom.xml",
            "delivery-dist",
            [("com.example", "pack-dist", "9.0.0", "compile")],
        )
        runner = self.runner()
        runner.dependencies["delivery-dist"] = ["com.example:pack-dist:jar:9.0.0:compile"]
        inventory = collect_dependencies(self.root, discover_maven_projects(self.root), runner=runner)

        self.assertEqual(2, inventory.projects_analyzed)
        self.assertEqual(1, len(inventory.projects_excluded))
        self.assertIn("delivery-dist", inventory.projects_excluded[0])
        self.assertEqual(4, sum(any("dependency:" in value for value in command) for command in runner.commands))
        self.assertFalse(
            any("delivery-dist.dependencies.txt" in value for command in runner.commands for value in command)
        )
        manifest = json.loads(inventory.manifest.read_text(encoding="utf-8"))
        self.assertEqual(2, manifest["projects_analyzed"])
        self.assertEqual(1, len(manifest["projects_excluded"]))
        self.assertTrue(all(item["artifact_id"] != "pack-dist" for item in manifest["dependencies"]))

    def test_project_exclusion_is_configurable(self):
        write_pom(self.root / "tools/pom.xml", "tools")
        projects = discover_maven_projects(self.root)
        inventory = collect_dependencies(
            self.root,
            projects,
            excluded_projects=["tools", "framework"],
            runner=FakeMaven(dependencies={"framework": [], "app": [], "tools": []}, jars={}),
        )
        self.assertEqual(1, inventory.projects_analyzed)
        self.assertEqual(2, len(inventory.projects_excluded))
        self.assertEqual({"tools", "framework"}, {
            record.split(" ", 1)[0] for record in inventory.projects_excluded
        })

    def test_workspace_config_defines_project_exclusion_rules(self):
        config = self.root / ".apsgraph.json"
        config.write_text(json.dumps({"maven": {"excludeProjects": ["framework"]}}), encoding="utf-8")
        args = build_parser().parse_args([
            "scan", "--include-deps", "--workspace", str(self.root),
            "--exclude-project", "extra",
        ])
        self.assertEqual(["framework", "*dist", "extra"], cli_module._project_excludes(args))

        no_defaults = build_parser().parse_args([
            "scan", "--include-deps", "--workspace", str(self.root),
            "--exclude-project", "extra", "--no-default-project-excludes",
        ])
        self.assertEqual(["framework", "extra"], cli_module._project_excludes(no_defaults))

        config.write_text(json.dumps({"excludeProjects": ["legacy"]}), encoding="utf-8")
        self.assertEqual(
            ["legacy", "*dist"],
            cli_module._project_excludes(build_parser().parse_args([
                "scan", "--include-deps", "--workspace", str(self.root)
            ])),
        )

        config.write_text(json.dumps({"excludeProjects": [1]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "excludeProjects.*array of strings"):
            cli_module._project_excludes(build_parser().parse_args([
                "scan", "--include-deps", "--workspace", str(self.root)
            ]))

    def test_jdk_config_is_parsed_and_profiles_fail_closed(self):
        config = maven_jdk_config_from_payload({
            "jdk": {
                "default": "8",
                "javaHomes": {"8": "auto", "17": "/opt/jdk17"},
                "rules": [{"match": ["api-parent", "api-parent/*"], "jdk": "17"}],
            }
        })
        self.assertEqual("8", config.default_profile)
        self.assertEqual({"8": "auto", "17": "/opt/jdk17"}, config.java_homes)
        self.assertEqual(("api-parent", "api-parent/*"), config.rules[0].match)

        with self.assertRaisesRegex(ValueError, "maven.jdk.rules"):
            maven_jdk_config_from_payload({"jdk": {"rules": [{"match": [], "jdk": "8"}]}})

    def test_project_jdk_rules_select_reactor_profile(self):
        write_pom(self.root / "aggregator/pom.xml", "aggregator", modules=("module",))
        write_pom(self.root / "aggregator/module/pom.xml", "module")
        projects = discover_maven_projects(self.root)
        units = [self.root / "aggregator"]
        java8 = self.root / "jdks" / "8"
        java17 = self.root / "jdks" / "17"
        java8.mkdir(parents=True)
        java17.mkdir()
        config = MavenJdkConfig(
            java_homes={"8": str(java8), "17": str(java17)},
            rules=(MavenJdkRule(("aggregator",), "8"),),
        )
        selections = resolve_jdk_selections(
            self.root, projects, units, config=config, project_profiles={"aggregator": "17"}
        )
        self.assertEqual("17", selections[units[0].resolve()].profile)
        self.assertEqual(java17.resolve(), selections[units[0].resolve()].java_home)

        conflicting = MavenJdkConfig(
            java_homes={"8": str(java8), "17": str(java17)},
            rules=(
                MavenJdkRule(("aggregator",), "8"),
                MavenJdkRule(("aggregator/module",), "17"),
            ),
        )
        with self.assertRaisesRegex(MavenError, "different JDK profiles"):
            resolve_jdk_selections(self.root, projects, units, config=conflicting)

    def test_same_jdk_is_used_for_build_and_dependencies(self):
        java8 = self.root / "jdks" / "8"
        java8.mkdir(parents=True)
        runner = self.runner()
        config = MavenJdkConfig(default_profile="8", java_homes={"8": str(java8)})
        result = scan_workspace_with_dependencies(
            self.root, self.db, runner=runner, jdk_config=config
        )
        self.assertEqual(2, len(result.build["jdk_profiles"]))
        self.assertTrue(all(value == f"8 ({java8.resolve()})" for value in result.build["jdk_profiles"]))
        self.assertEqual(6, len(runner.environments))
        self.assertTrue(all(environment["JAVA_HOME"] == str(java8.resolve()) for environment in runner.environments))
        self.assertTrue(all(environment["PATH"].startswith(str(java8.resolve() / "bin")) for environment in runner.environments))

    def test_scan_progress_reports_key_stages(self):
        messages = []
        scan_workspace_with_dependencies(
            self.root, self.db, runner=self.runner(), progress=messages.append
        )
        self.assertIn("stage 1/7 discover Maven projects", messages)
        self.assertIn("stage 2/7 build Maven workspace", messages)
        self.assertIn("stage 3/7 resolve Maven dependencies", messages)
        self.assertIn("stage 4/7 check dependency versions", messages)
        self.assertIn("stage 5/7 scan workspace XML", messages)
        self.assertIn("stage 6/7 import dependency JAR models", messages)
        self.assertIn("stage 7/7 publish index atomically", messages)
        self.assertTrue(messages[-1].startswith("complete:"))

    def test_jaxb_failure_includes_jdk_diagnostic(self):
        class JaxbFailureMaven(FakeMaven):
            def __call__(self, project, command, log, env=None, metadata=None):
                result = super().__call__(project, command, log, env=env, metadata=metadata)
                build = (
                    project.name == "app"
                    and "dependency:list" not in command
                    and "dependency:copy-dependencies" not in command
                )
                if build:
                    log.write_text(
                        "Caused by: java.lang.ClassNotFoundException: javax.xml.bind.JAXBException\n",
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 1, "", "JAXB failure")
                return result

        runner = JaxbFailureMaven(
            self.runner().dependencies, self.runner().jars, fail_build="app"
        )
        with self.assertRaisesRegex(MavenError, "JAXB compatibility error.*--jdk 8"):
            scan_workspace_with_dependencies(self.root, self.db, runner=runner)

    def test_dependency_version_conflict_fails_closed(self):
        scan_workspace(self.root, self.db)
        before = self.digest(self.db)
        runner = self.runner()
        runner.dependencies["app"][1] = "com.example:shared:jar:2.0.0:compile"
        runner.jars["com.example:shared:2.0.0"] = JAR_MODEL
        with self.assertRaisesRegex(MavenError, "shared.*1.0.0.*2.0.0"):
            scan_workspace_with_dependencies(self.root, self.db, runner=runner)
        self.assertEqual(before, self.digest(self.db))

    def test_dependency_conflict_uses_group_and_artifact_coordinate(self):
        runner = self.runner()
        runner.dependencies["app"] = [
            "org.jetbrains:annotations:jar:23.0.0:compile",
            "com.google.android:annotations:jar:4.1.1.4:runtime",
        ]
        runner.jars["org.jetbrains:annotations:23.0.0"] = JAR_MODEL
        runner.jars["com.google.android:annotations:4.1.1.4"] = JAR_MODEL

        inventory = collect_dependencies(self.root, discover_maven_projects(self.root), runner=runner)
        # Different groupId/artifactId coordinates are independent even when
        # their jar content happens to be identical in this fixture.
        self.assertEqual(1, len(inventory.jars))

        runner.dependencies["app"][1] = "org.jetbrains:annotations:jar:22.0.0:runtime"
        runner.jars["org.jetbrains:annotations:22.0.0"] = JAR_MODEL
        with self.assertRaisesRegex(MavenError, "org.jetbrains:annotations -> 22.0.0, 23.0.0"):
            collect_dependencies(self.root, discover_maven_projects(self.root), runner=runner)

    def test_dependency_jars_are_filtered_by_business_pom_marker(self):
        isolated = self.root / "business-marker"
        write_pom(isolated / "app/pom.xml", "app", [
            ("com.example", "edsp", "1.0.0", "compile"),
            ("com.example", "aps", "1.0.0", "compile"),
            ("com.example", "disabled", "1.0.0", "compile"),
            ("com.example", "unmarked", "1.0.0", "compile"),
        ])
        runner = FakeMaven(
            dependencies={
                "app": [
                    "com.example:edsp:jar:1.0.0:compile",
                    "com.example:aps:jar:1.0.0:compile",
                    "com.example:disabled:jar:1.0.0:compile",
                    "com.example:unmarked:jar:1.0.0:compile",
                ],
            },
            jars={},
        )

        def jar_fixture(marker_property, marker_value="true", artifact="shared"):
            return {
                "marker_property": marker_property,
                "marker_value": marker_value,
                "artifact_id": artifact,
            }

        runner.jars = {
            "com.example:edsp:1.0.0": jar_fixture("edsp-module", artifact="edsp"),
            "com.example:aps:1.0.0": jar_fixture("aps-module", artifact="aps"),
            "com.example:disabled:1.0.0": jar_fixture("edsp-module", "false", "disabled"),
            "com.example:unmarked:1.0.0": jar_fixture(None, artifact="unmarked"),
        }
        inventory = collect_dependencies(isolated, discover_maven_projects(isolated), runner=runner)

        self.assertEqual({"edsp-1.0.0.jar", "aps-1.0.0.jar"}, {path.name for path in inventory.jars})
        self.assertEqual(2, len(inventory.jars_skipped_non_business))
        self.assertIn(
            "com.example:disabled:1.0.0; reason: property disabled",
            inventory.jars_skipped_non_business,
        )
        self.assertIn(
            "com.example:unmarked:1.0.0; reason: property missing",
            inventory.jars_skipped_non_business,
        )
        self.assertEqual(
            {"edsp-module", "aps-module"},
            {record.business_marker for record in inventory.jar_records},
        )

    def test_same_jar_version_from_multiple_projects_is_deduplicated(self):
        # Both framework and app resolve the exact same Maven coordinate.  This
        # is normal reuse and must not be treated as a version conflict.
        inventory = collect_dependencies(self.root, discover_maven_projects(self.root), runner=self.runner())
        self.assertEqual(2, len(inventory.dependencies))
        self.assertEqual(1, len(inventory.jars))
        self.assertEqual("shared-1.0.0.jar", inventory.jars[0].name)

    def test_reactor_matching_uses_group_and_artifact_coordinate(self):
        isolated = self.root / "coordinates"
        write_pom(isolated / "library/pom.xml", "library", modules=("framework",))
        write_pom(isolated / "library/framework/pom.xml", "lib-framework")
        write_pom(isolated / "application/pom.xml", "application", modules=("app",))
        write_pom(
            isolated / "application/app/pom.xml",
            "business-app",
            [("demo", "lib-framework", "1.0.0", "compile")],
        )
        runner = FakeMaven(
            dependencies={
                "library": [], "framework": [], "application": [],
                "app": [
                    "demo:lib-framework:jar:1.0.0:compile",
                    "other:lib-framework:jar:1.0.0:compile",
                ],
            },
            jars={"other:lib-framework:1.0.0": JAR_MODEL},
        )
        projects, report = build_workspace(isolated, runner=runner)
        self.assertEqual(
            [isolated.resolve() / "library", isolated.resolve() / "application"],
            [Path(value).resolve() for value in report.build_order],
        )
        inventory = collect_dependencies(isolated, projects, runner=runner)
        self.assertEqual(["demo:lib-framework:1.0.0"], inventory.reactor_dependencies_excluded)
        self.assertEqual(
            [("other", "lib-framework")],
            [(dependency.group_id, dependency.artifact_id) for dependency in inventory.dependencies],
        )

    def test_project_group_id_may_be_inherited_from_parent(self):
        isolated = self.root / "inherited-group"
        write_pom(isolated / "parent/pom.xml", "standalone-parent")
        write_pom(
            isolated / "child/pom.xml", "inherited-child",
            parent="standalone-parent", omit_group=True,
        )
        write_pom(
            isolated / "consumer/pom.xml", "consumer",
            [("demo", "inherited-child", "1.0.0", "compile")],
        )
        projects, report = build_workspace(isolated, runner=FakeMaven(
            dependencies={"standalone-parent": [], "inherited-child": [], "consumer": []}, jars={}
        ))
        self.assertEqual(
            ["child", "consumer", "parent"],
            [Path(value).name for value in report.build_order],
        )

    def test_workspace_aggregators_skip_but_top_parent_boundary_is_analyzed(self):
        isolated = self.root / "structural"
        write_pom(isolated / "pom.xml", "aggregate", modules=("child",), packaging="pom")
        write_pom(isolated / "child/pom.xml", "child")
        write_pom(isolated / "standalone-parent/pom.xml", "standalone-parent")
        write_pom(isolated / "standalone-child/pom.xml", "standalone-child", parent="standalone-parent")
        projects = discover_maven_projects(isolated)
        runner = FakeMaven(
            dependencies={"aggregate": [], "child": [], "standalone-parent": [], "standalone-child": []},
            jars={},
        )
        inventory = collect_dependencies(isolated, projects, runner=runner)

        self.assertEqual(3, inventory.projects_analyzed)
        self.assertEqual(
            {"aggregate"},
            {record.split(" ", 1)[0] for record in inventory.projects_excluded},
        )
        self.assertEqual(6, sum(any("dependency:" in value for value in command) for command in runner.commands))

    def test_only_top_workspace_parent_boundary_is_dependency_analyzed(self):
        isolated = self.root / "parent-chain"
        write_pom(
            isolated / "ap-parent/pom.xml", "ap-parent",
            modules=("module",), packaging="pom", parent_group="cn.external",
        )
        write_pom(
            isolated / "ap-out-parent/pom.xml", "ap-out-parent",
            parent="ap-parent", packaging="pom",
        )
        write_pom(
            isolated / "prod-parent/pom.xml", "prod-parent",
            parent="ap-out-parent", modules=("app",), packaging="pom",
        )
        write_pom(isolated / "prod-parent/app/pom.xml", "app", parent="prod-parent")
        projects = discover_maven_projects(isolated)
        runner = FakeMaven(
            dependencies={"ap-parent": [], "ap-out-parent": [], "prod-parent": [], "app": []},
            jars={},
        )
        inventory = collect_dependencies(isolated, projects, runner=runner)

        self.assertEqual(2, inventory.projects_analyzed)
        self.assertEqual(
            {"ap-out-parent", "prod-parent"},
            {record.split(" ", 1)[0] for record in inventory.projects_excluded},
        )
        list_outputs = {
            next(value for value in command if value.startswith("-DoutputFile="))
            for command in runner.commands if "dependency:list" in command
        }
        self.assertEqual(
            {"ap-parent.dependencies.txt", "prod-parent_app.dependencies.txt"},
            {Path(value.split("=", 1)[1]).name for value in list_outputs},
        )

    def test_independent_build_units_and_dependencies_run_in_parallel(self):
        isolated = self.root / "parallel"
        write_pom(isolated / "alpha/pom.xml", "alpha")
        write_pom(isolated / "beta/pom.xml", "beta")

        class ConcurrentFakeMaven(FakeMaven):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.lock = threading.Lock()
                self.active = 0
                self.maximum = 0

            def __call__(self, project, command, log, env=None, metadata=None):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                try:
                    time.sleep(0.05)
                    return super().__call__(project, command, log, env=env, metadata=metadata)
                finally:
                    with self.lock:
                        self.active -= 1

        dependencies = {"alpha": [], "beta": []}
        build_runner = ConcurrentFakeMaven(dependencies, {}, fail_build=None)
        _, report = build_workspace(isolated, runner=build_runner, jobs=2)
        self.assertEqual(2, len(report.succeeded))
        self.assertEqual(2, build_runner.maximum)

        dependency_runner = ConcurrentFakeMaven(dependencies, {})
        inventory = collect_dependencies(
            isolated, discover_maven_projects(isolated), runner=dependency_runner, jobs=2
        )
        self.assertEqual(2, inventory.projects_analyzed)
        self.assertEqual(2, dependency_runner.maximum)

    def test_import_maven_deps_replaces_old_jar_models_atomically(self):
        scan_workspace(self.root, self.db)
        old_jar = self.root / "old-framework.jar"
        write_model_jar(old_jar, OLD_JAR_MODEL, "models/Old.u_schema.xml")
        import_jar_models(self.db, [old_jar])
        self.assertTrue(any("old-framework" in path for path in self.model_paths(self.db)))

        result = import_maven_dependencies(self.root, self.db, runner=self.runner())
        paths = self.model_paths(self.db)
        self.assertFalse(any("old-framework" in path for path in paths))
        self.assertTrue(any("shared-1.0.0.jar" in path for path in paths))
        self.assertIn("models/Work.tables.xml", paths)
        self.assertEqual(1, result["import"]["imported_files"])
        self.assertFalse(list(self.db.parent.glob("*.import-deps.tmp")))

    def test_cli_default_behavior_is_install_skip_tests_runtime(self):
        args = build_parser().parse_args(["scan", "--include-deps"])
        self.assertTrue(args.skip_tests)
        self.assertEqual("runtime", args.deps_scope)
        self.assertIsNone(args.maven_goal)
        self.assertEqual([], args.exclude_project)
        self.assertFalse(args.no_default_project_excludes)
        self.assertEqual(
            ["*dist", "packaging/*"],
            cli_module._project_excludes(
                build_parser().parse_args([
                    "scan", "--include-deps", "--exclude-project", "packaging/*"
                ])
            ),
        )
        self.assertEqual(
            ["packaging/*"],
            cli_module._project_excludes(
                build_parser().parse_args([
                    "scan", "--include-deps", "--exclude-project", "packaging/*",
                    "--no-default-project-excludes",
                ])
            ),
        )

        args = build_parser().parse_args(["import-maven-deps", "--maven-goal", "package"])
        self.assertEqual(["package"], args.maven_goal)
        self.assertTrue(args.skip_tests)
        self.assertEqual("runtime", args.deps_scope)


if __name__ == "__main__":
    unittest.main()
