import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]


def load_module(name):
    path = ROOT / "tools/release" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELEASE = load_module("release")
STRIP = load_module("strip")
BUNDLE = load_module("bundle")


def git(repo, *args, check=True):
    if "commit" in args:
        args = (*args, "-m", RELEASE.COAUTHOR)
    result = subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c",
         "user.email=fixture@example.invalid", "-c", "gc.auto=0",
         "-c", "maintenance.auto=false", *args],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=check,
    )
    return result.stdout.decode("utf-8").strip()


def put(root, name, content, executable=False):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)
    return path


def commit(repo, message="fixture"):
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def config(name="VRouter", updates="manual"):
    return {
        "schema": 1,
        "name": name,
        "title": f"{name} lab",
        "description": f"Canonical {name} exercise.",
        "owner": "NYCU-SDNFV",
        "config_repository": "NYCU-SDNFV/classroom50",
        "config_branch": "main",
        "pages_url": "https://nycu-sdnfv.github.io/classroom50",
        "default_channel": "newbie",
        "student_updates": updates,
        "channels": {
            "newbie": {
                "classroom": "winlab-newbies",
                "slug": "lab3-vrouter" if name == "VRouter" else "lab0-toolchain",
                "template": f"Golden-newbie-{name}",
            },
            "115-1": {
                "classroom": "sdnfv-115-1",
                "slug": "lab3-vrouter" if name == "VRouter" else "lab0-toolchain",
                "template": f"Golden-115-1-{name}",
            },
        },
    }


def metadata(cfg, version="0.1.0", source="a" * 40, tag=None):
    return RELEASE.release_metadata(
        cfg, tag or f"newbie-v{version}", source)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def repo(self, name, bare=False):
        path = self.root / name
        path.mkdir()
        args = ["init", "--quiet"]
        if bare:
            args.append("--bare")
        else:
            args.extend(["-b", "master"])
        git(path, *args)
        return path

    def write_config(self, cfg):
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        RELEASE.write_json(source / ".release.json", cfg)
        return source


class RoutingTests(TempCase):
    def test_vrouter_and_toolchain_profiles_are_generic(self):
        for name in ("VRouter", "Toolchain"):
            with self.subTest(name=name):
                source = self.write_config(config(name))
                loaded = RELEASE.load_config(
                    source, f"NYCU-SDNFV/Golden-{name}")
                self.assertEqual(name, loaded["name"])
                self.assertEqual(
                    f"Golden-115-1-{name}",
                    loaded["channels"]["115-1"]["template"],
                )
                shutil.rmtree(source)

    def test_tags_are_data_driven_and_strict(self):
        cfg = config()
        self.assertEqual(("newbie", "0.1.0"), RELEASE.parse_tag(cfg, "newbie"))
        self.assertEqual(("115-1", "0.1.0"), RELEASE.parse_tag(cfg, "115-1"))
        self.assertEqual(("newbie", "2.3.4"), RELEASE.parse_tag(cfg, "v2.3.4"))
        self.assertEqual(("115-1", "12.30.4"),
                         RELEASE.parse_tag(cfg, "115-1-v12.30.4"))
        for value in (
            "", "master", "newbie-v01.0.0", "v1.0", "v1.0.0-rc1",
            "unknown-v1.0.0", "../v1.0.0", "v1.0.0\n", None,
        ):
            with self.subTest(value=value), self.assertRaises(RELEASE.ReleaseError):
                RELEASE.parse_tag(cfg, value)

    def test_wrong_source_template_and_duplicate_routes_are_rejected(self):
        source = self.write_config(config())
        with self.assertRaisesRegex(RELEASE.ReleaseError, "does not match"):
            RELEASE.load_config(source, "NYCU-SDNFV/Golden-Toolchain")
        wrong = config()
        wrong["channels"]["newbie"]["template"] = "Golden-newbie-Controller"
        RELEASE.write_json(source / ".release.json", wrong)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "must be"):
            RELEASE.load_config(source, "NYCU-SDNFV/Golden-VRouter")
        duplicate = config()
        duplicate["channels"]["115-1"]["classroom"] = "winlab-newbies"
        RELEASE.write_json(source / ".release.json", duplicate)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "unique"):
            RELEASE.load_config(source, "NYCU-SDNFV/Golden-VRouter")

    def test_update_modes_default_manual_and_reject_unknown(self):
        cfg = config()
        cfg.pop("student_updates")
        source = self.write_config(cfg)
        self.assertEqual(
            "manual",
            RELEASE.load_config(
                source, "NYCU-SDNFV/Golden-VRouter")["student_updates"],
        )
        cfg["student_updates"] = "best-effort"
        RELEASE.write_json(source / ".release.json", cfg)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "manual or pull_request"):
            RELEASE.load_config(source, "NYCU-SDNFV/Golden-VRouter")

    def test_pull_request_mode_fails_before_api_or_git_mutation(self):
        source = self.write_config(config(updates="pull_request"))
        report = {}
        with mock.patch.object(RELEASE, "GitHub") as api, \
                mock.patch.object(RELEASE, "git") as command:
            with self.assertRaisesRegex(RELEASE.ReleaseError, "before mutation"):
                RELEASE.publish(
                    source, "newbie-v0.1.0", "secret", report,
                    "NYCU-SDNFV/Golden-VRouter",
                )
        api.assert_not_called()
        command.assert_not_called()
        self.assertEqual({}, report)

    def test_manual_publisher_has_no_pr_or_actions_api_paths(self):
        publisher = (
            ROOT / "tools/release/release.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("/pulls", publisher)
        self.assertNotIn("/actions", publisher)

    def test_workflow_rejects_unknown_auth_and_separates_tokens(self):
        workflow = (
            ROOT / ".github/workflows/shared-release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('case "$AUTH_MODE" in', workflow)
        self.assertIn("auth_mode must be token or app", workflow)
        self.assertIn(r"^[0-9a-f]{40}$", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("SOURCE_REPOSITORY: ${{ github.repository }}", workflow)
        self.assertNotIn("secrets: inherit", workflow)
        self.assertEqual(2, workflow.count("          RELEASE_TOKEN: ${{"))

    def test_base_ci_runs_publisher_and_parent_bootstrap_suites(self):
        workflow = (
            ROOT / ".github/workflows/test-shared-release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'python3 -m unittest discover -s tools/release/tests '
            '-p "test_*.py" -v',
            workflow,
        )
        self.assertIn(
            'python3 -m unittest discover -s tests '
            '-p "test_golden_bootstrap.py"',
            workflow,
        )


class RepositoryChecks(TempCase):
    def test_source_metadata_requires_private_non_template_expected_repo(self):
        good = {
            "full_name": "NYCU-SDNFV/Golden-VRouter",
            "private": True,
            "is_template": False,
            "archived": False,
            "disabled": False,
            "default_branch": "master",
        }
        api = mock.Mock()
        api.call.return_value = good
        RELEASE.verify_source_repository(api, good["full_name"])
        for field, value in (
            ("private", False), ("is_template", True), ("archived", True),
            ("disabled", True), ("default_branch", "main"),
            ("full_name", "NYCU-SDNFV/Golden-Other"),
        ):
            with self.subTest(field=field):
                api.call.return_value = {**good, field: value}
                with self.assertRaises(RELEASE.ReleaseError):
                    RELEASE.verify_source_repository(api, good["full_name"])

    def tagged_source(self):
        remote = self.repo("source.git", bare=True)
        source = self.repo("checkout")
        put(source, "README.md", "source\n")
        first = commit(source)
        git(source, "remote", "add", "origin", str(remote))
        git(source, "tag", "newbie-v0.1.0")
        git(source, "push", "--quiet", "origin", "master", "newbie-v0.1.0")
        return source, remote, first

    def test_exact_remote_tag_ancestor_and_moved_tag_checks(self):
        source, remote, first = self.tagged_source()
        self.assertEqual(
            first,
            RELEASE.verify_source(
                source, "newbie-v0.1.0", config(), None),
        )
        put(source, "README.md", "new master\n")
        second = commit(source, "second")
        git(source, "push", "--quiet", "origin", "master")
        git(source, "push", "--quiet", "--force", "origin",
            f"{second}:refs/tags/newbie-v0.1.0")
        git(source, "reset", "--hard", "--quiet", first)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "moved"):
            RELEASE.verify_source(
                source, "newbie-v0.1.0", config(), None)

    def test_non_ancestor_tag_is_rejected(self):
        source, remote, first = self.tagged_source()
        git(source, "checkout", "--quiet", "--orphan", "side")
        git(source, "rm", "-r", "--quiet", ".", check=False)
        put(source, "side.txt", "side\n")
        side = commit(source, "side")
        git(source, "tag", "newbie-v0.1.1")
        git(source, "push", "--quiet", "origin", "newbie-v0.1.1")
        self.assertNotEqual(first, side)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "not an ancestor"):
            RELEASE.verify_source(
                source, "newbie-v0.1.1", config(), None)

    def test_generated_source_is_rejected(self):
        source = self.root / "generated"
        source.mkdir()
        put(source, RELEASE.LOCAL_PROVENANCE, "{}\n")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "local-provenance"):
            RELEASE.require_development_source(source)


class ClassroomTests(TempCase):
    def provision(self):
        room = self.root / "classroom50/winlab-newbies"
        room.mkdir(parents=True)
        RELEASE.write_json(room / "classroom.json", {
            "schema": "classroom50/classroom/v1",
            "short_name": "winlab-newbies",
            "org": "NYCU-SDNFV",
            "active": True,
            "team": {"id": 17},
            "secret": "abcd1234",
        })
        RELEASE.write_json(room / "assignments.json", {
            "schema": "classroom50/assignments/v1",
            "assignments": [],
        })
        return room

    def test_wrong_classroom_and_duplicate_slug_are_rejected(self):
        room = self.provision()
        document = RELEASE.load_json(room / "classroom.json")
        document["short_name"] = "wrong"
        RELEASE.write_json(room / "classroom.json", document)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "invalid"):
            RELEASE.classroom_preflight(
                self.root / "classroom50", config(), "newbie")
        document["short_name"] = "winlab-newbies"
        RELEASE.write_json(room / "classroom.json", document)
        RELEASE.write_json(room / "assignments.json", {
            "schema": "classroom50/assignments/v1",
            "assignments": [{"slug": "same"}, {"slug": "same"}],
        })
        with self.assertRaisesRegex(RELEASE.ReleaseError, "Duplicate"):
            RELEASE.classroom_preflight(
                self.root / "classroom50", config(), "newbie")

    def test_assignment_dates_are_populated_once_and_due_is_preserved(self):
        cfg = config()
        data = metadata(cfg)
        document = {
            "schema": "classroom50/assignments/v1",
            "assignments": [{
                "slug": "lab3-vrouter",
                "mode": "individual",
                "available_from": None,
                "due": "2026-12-01T15:59:00Z",
                "tests": [{"points": 1}],
            }],
        }
        with mock.patch.object(RELEASE, "datetime") as clock:
            clock.now.return_value.isoformat.return_value = (
                "2026-09-17T12:00:00+00:00")
            updated, entry = RELEASE.upsert_assignment(document, cfg, data)
        self.assertEqual("2026-09-17T12:00:00Z", entry["available_from"])
        self.assertEqual("2026-12-01T15:59:00Z", entry["due"])
        self.assertNotIn("tests", entry)
        self.assertIsNone(document["assignments"][0]["available_from"])
        again, second = RELEASE.upsert_assignment(updated, cfg, data)
        self.assertEqual(updated, again)
        self.assertEqual(entry["available_from"], second["available_from"])


class SanitizationAndBundleTests(TempCase):
    def source_tree(self, points=(40, 60)):
        source = self.repo("instructor")
        cfg = config()
        RELEASE.write_json(source / ".release.json", cfg)
        RELEASE.write_json(source / ".lab-release.json", {
            "schema": 1, "version": "development",
        })
        put(source, "README.md",
            "visible\n# KEY instructor secret\n#STUDENT: student task\n")
        put(source, "solution.txt", "hidden\n")
        put(source, ".studentignore", "solution.txt\n")
        put(source, "tests/00_env.sh", "#!/bin/sh\nexit 0\n", executable=True)
        put(source, ".github/tests/lib.sh", "#!/bin/sh\ntrue\n", executable=True)
        put(source, ".github/policy/00_layout.sh", "#!/bin/sh\nexit 0\n",
            executable=True)
        put(source, ".github/policy/01_integrity.sh", "#!/bin/sh\nexit 0\n",
            executable=True)
        put(source, ".github/policy/integrity.py", "pass\n")
        put(source, ".github/release/upgrade.py",
            "raise RuntimeError('must only be copied')\n")
        put(source, ".github/grade/autograder.py",
            "raise RuntimeError('must never execute while publishing')\n")
        tests = [
            {"name": f"test-{number}", "run": "true", "points": value,
             "timeout": 30}
            for number, value in enumerate(points)
        ]
        RELEASE.write_json(source / ".github/grade/tests.json", {"tests": tests})
        put(source, "tools/student-build/evil.py",
            "raise RuntimeError('untrusted release code')\n")
        protected = (
            ".lab-release.json",
            ".github/policy/00_layout.sh",
            ".github/policy/01_integrity.sh",
            ".github/policy/integrity.py",
            ".github/release/upgrade.py",
        )
        put(source, RELEASE.MANIFEST, "".join(
            f"{hashlib.sha256((source / name).read_bytes()).hexdigest()}  {name}\n"
            for name in protected
        ))
        commit(source)
        git(source, "update-index", "--chmod=+x", "--",
            "tests/00_env.sh", ".github/tests/lib.sh",
            ".github/policy/00_layout.sh",
            ".github/policy/01_integrity.sh")
        git(source, "commit", "--quiet", "--amend", "--no-edit")
        return source, cfg

    def test_trusted_strip_and_bundle_remove_keys_preserve_assets_and_modes(self):
        source, cfg = self.source_tree()
        student, bundle = self.root / "student", self.root / "bundle"
        data = metadata(cfg)
        RELEASE.build_student(source, student, data)
        RELEASE.build_bundle(source, student, bundle)
        self.assertEqual("visible\nstudent task\n",
                         (student / "README.md").read_text(encoding="utf-8"))
        self.assertFalse((student / "solution.txt").exists())
        self.assertFalse((student / "tools/student-build").exists())
        self.assertFalse((student / ".github/grade").exists())
        self.assertTrue((student / ".github/release/upgrade.py").is_file())
        self.assertEqual(
            (source / ".github/grade/autograder.py").read_bytes(),
            (bundle / "autograder.py").read_bytes(),
        )
        self.assertEqual(
            (student / RELEASE.MANIFEST).read_bytes(),
            (bundle / "policy/manifest.sha256").read_bytes(),
        )
        modes = RELEASE.source_file_modes(source)
        self.assertEqual("100755", modes["tests/00_env.sh"])
        self.assertEqual("100755",
                         RELEASE.bundle_file_modes(bundle, modes)["tests/00_env.sh"])
        self.assertEqual(2, len(BUNDLE.validate_rubric(bundle / "tests.json")))

    def test_rubric_total_and_policy_manifest_are_enforced(self):
        source, cfg = self.source_tree(points=(99,))
        student, output = self.root / "student", self.root / "bundle"
        RELEASE.build_student(source, student, metadata(cfg))
        with self.assertRaisesRegex(RELEASE.ReleaseError, "totaling 100"):
            RELEASE.build_bundle(source, student, output)
        manifest = source / RELEASE.MANIFEST
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                ".lab-release.json", "../secret"),
            encoding="utf-8", newline="\n",
        )
        with self.assertRaisesRegex(RELEASE.ReleaseError, "manifest"):
            RELEASE.build_student(
                source, self.root / "second-student", metadata(cfg))

    def test_marker_and_banned_secret_guards(self):
        tree = self.root / "unsafe"
        put(tree, "visible.txt", "// KEY secret\n")
        put(tree, "id_rsa", b"private")
        failures = STRIP.guards(tree)
        self.assertTrue(any("marker leaked" in item for item in failures))
        self.assertTrue(any("banned file" in item for item in failures))

    def test_archive_rejects_traversal_and_hashes_exact_bytes(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            content = b"canonical\n"
            info = tarfile.TarInfo("lab/file.txt")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        self.assertEqual(
            hashlib.sha256(b"canonical\n").hexdigest(),
            RELEASE.archive_hashes(payload.getvalue(), "lab")["file.txt"],
        )
        self.assertEqual(
            "100644",
            RELEASE.archive_entries(
                payload.getvalue(), "lab")["file.txt"]["mode"],
        )
        bad = io.BytesIO()
        with tarfile.open(fileobj=bad, mode="w:gz") as archive:
            info = tarfile.TarInfo("lab/../secret")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        with self.assertRaisesRegex(RELEASE.ReleaseError, "Unsafe"):
            RELEASE.archive_hashes(bad.getvalue(), "lab")


class TemplateHistoryTests(TempCase):
    def setUp(self):
        super().setUp()
        self.remote = self.repo("template.git", bare=True)
        self.cfg = config()
        self.exists = False
        self.api = mock.Mock()

        def call(method, path, body=None, missing_ok=False):
            if method == "POST":
                self.exists = True
            if self.exists:
                return {
                    "private": False, "is_template": True,
                    "default_branch": "main",
                    "full_name": metadata(self.cfg)["template"],
                    "archived": False, "disabled": False,
                }
            return None

        self.api.call.side_effect = call
        patch = mock.patch.object(
            RELEASE, "repository_url", return_value=str(self.remote))
        patch.start()
        self.addCleanup(patch.stop)
        self.number = 0

    def student(self, root, data):
        root.mkdir()
        RELEASE.write_json(root / RELEASE.METADATA, data)
        put(root, "README.md", "sanitized\n")
        return root

    def publish(self, data, changed=False):
        self.number += 1
        work = self.root / f"attempt-{self.number}"
        work.mkdir()
        tree = self.student(work / "student", data)
        if changed:
            put(tree, "README.md", "changed\n")
        repo, create, prior, rerun = RELEASE.prepare_template(
            self.api, self.cfg, data, tree, work, None)
        RELEASE.publish_template(
            self.api, repo, self.cfg, data, create, rerun, None)
        return repo, prior, rerun

    def test_clean_initial_root_append_rerun_and_no_force(self):
        first, _, rerun = self.publish(metadata(self.cfg))
        first_sha = git(first, "rev-parse", "HEAD")
        self.assertFalse(rerun)
        self.assertEqual("1", git(first, "rev-list", "--count", "HEAD"))
        second, prior, rerun = self.publish(
            metadata(self.cfg, "0.1.1", "b" * 40))
        self.assertEqual("newbie-v0.1.0", prior["tag"])
        self.assertEqual(first_sha, git(second, "rev-parse", "newbie-v0.1.0"))
        latest = git(second, "rev-parse", "HEAD")
        again, _, rerun = self.publish(
            metadata(self.cfg, "0.1.1", "b" * 40))
        self.assertTrue(rerun)
        self.assertEqual(latest, git(again, "rev-parse", "HEAD"))

    def test_downgrade_collision_alias_and_changed_rerun_fail(self):
        self.publish(metadata(self.cfg))
        candidates = (
            metadata(self.cfg, source="c" * 40),
            metadata(self.cfg, tag="v0.1.0"),
            metadata(self.cfg, "0.0.9"),
        )
        for data in candidates:
            with self.subTest(data=data), self.assertRaises(RELEASE.ReleaseError):
                self.publish(data)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "different tree"):
            self.publish(metadata(self.cfg), changed=True)

    def test_wrong_target_privacy_or_template_state_is_rejected(self):
        self.exists = True
        for field, value in (("private", True), ("is_template", False),
                             ("archived", True), ("disabled", True)):
            with self.subTest(field=field):
                self.api.call.side_effect = None
                self.api.call.return_value = {
                    "private": False, "is_template": True,
                    "default_branch": "main",
                    "full_name": metadata(self.cfg)["template"],
                    "archived": False, "disabled": False,
                    field: value,
                }
                work = self.root / f"wrong-{field}"
                work.mkdir()
                tree = self.student(work / "student", metadata(self.cfg))
                with self.assertRaisesRegex(RELEASE.ReleaseError, "public channel"):
                    RELEASE.prepare_template(
                        self.api, self.cfg, metadata(self.cfg), tree, work, None)


class AssignmentOwnershipTests(unittest.TestCase):
    def test_shared_publisher_cannot_take_over_another_labs_assignment(self):
        cfg = config()
        assignments = {"assignments": [{
            "slug": "lab3-vrouter", "mode": "individual",
            "template": {"owner": "NYCU-SDNFV", "repo": "Golden-newbie-Measure", "branch": "main"},
        }]}
        before = copy.deepcopy(assignments)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "template belongs to another source"):
            RELEASE.upsert_assignment(assignments, cfg, metadata(cfg))
        self.assertEqual(before, assignments)


if __name__ == "__main__":
    unittest.main()
