import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import apsgraph.cli as cli_module
from apsgraph.cli import build_parser
from apsgraph.maven import (
    MavenError,
    build_workspace,
    collect_dependencies,
    discover_maven_projects,
    import_maven_dependencies,
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


def write_pom(path: Path, artifact_id: str, dependencies=(), modules=()):
    dependency_xml = "".join(
        f"<dependency><groupId>{group}</groupId>"
        f"<artifactId>{artifact}</artifactId><version>{version}</version>"
        f"<scope>{scope}</scope></dependency>"
        for group, artifact, version, scope in dependencies
    )
    module_xml = ''.join(f'<module>{module}</module>' for module in modules)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<project xmlns="http://maven.apache.org/POM/4.0.0">'
        f'<modelVersion>4.0.0</modelVersion>'
        f'<groupId>demo</groupId><artifactId>{artifact_id}</artifactId>'
        f'<version>1.0.0</version><modules>{module_xml}</modules><dependencies>{dependency_xml}</dependencies>'
        f'</project>',
        encoding="utf-8",
    )


def write_model_jar(path: Path, model=JAR_MODEL, entry="models/Framework.u_schema.xml"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, model)


class FakeMaven:
    def __init__(self, dependencies, jars, fail_build=None):
        # dependencies maps project artifact id to Maven dependency-list lines.
        self.dependencies = dependencies
        # jars maps artifact coordinate (group:artifact:version) to optional model text.
        self.jars = jars
        self.fail_build = fail_build
        self.builds = []
        self.commands = []

    def __call__(self, project: Path, command, log: Path):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("$ " + " ".join(command) + "\n", encoding="utf-8")
        self.commands.append(list(command))
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
                else:
                    write_model_jar(path, model)
            return subprocess.CompletedProcess(command, 0, "", "")
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

    def test_dependency_version_conflict_fails_closed(self):
        scan_workspace(self.root, self.db)
        before = self.digest(self.db)
        runner = self.runner()
        runner.dependencies["app"][1] = "com.example:shared:jar:2.0.0:compile"
        runner.jars["com.example:shared:2.0.0"] = JAR_MODEL
        with self.assertRaisesRegex(MavenError, "shared.*1.0.0.*2.0.0"):
            scan_workspace_with_dependencies(self.root, self.db, runner=runner)
        self.assertEqual(before, self.digest(self.db))

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
