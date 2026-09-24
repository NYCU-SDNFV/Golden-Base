import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.runtime.golden.grading import (
    DEVELOPMENT,
    MANUAL_UPDATE_HELP,
    Grader,
)
from tools.runtime.golden.environment import (
    EnvironmentError as BootstrapEnvironmentError,
    GoldenEnvironmentError,
)


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools" / "runtime"
PROFILES = (
    {
        "schema": 1,
        "name": "Toolchain",
        "lab": 0,
        "assignment": "lab0-toolchain",
        "checks": 8,
        "environment": "toolchain",
    },
    {
        "schema": 1,
        "name": "Controller",
        "lab": 1,
        "assignment": "lab1-controller",
        "checks": 16,
        "environment": "controller",
    },
    {
        "schema": 1,
        "name": "Measure",
        "lab": 2,
        "assignment": "lab2-measure",
        "checks": 12,
        "environment": "measurement",
    },
    {
        "schema": 1,
        "name": "VRouter",
        "lab": 3,
        "assignment": "lab3-vrouter",
        "checks": 20,
        "environment": "vrouter",
    },
)
INTEGRITY_POLICY = """\
import hashlib
from pathlib import Path, PurePosixPath
import re


def load_manifest(path):
    entries = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError(f"{path}:{number}: invalid SHA-256 manifest entry")
        digest, name = match.groups()
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts
                or str(relative) != name or "\\\\" in name or name in entries):
            raise ValueError(f"{path}:{number}: unsafe or duplicate protected path")
        entries[name] = digest
    if not entries:
        raise ValueError(f"{path}: protected-file manifest is empty")
    return entries


def check_integrity(workspace, entries):
    workspace = Path(workspace).resolve()
    failures = []
    for name, expected in entries.items():
        path = workspace / name
        if path.is_symlink():
            failures.append(f"protected path is a symbolic link: {name}")
        elif not path.is_file():
            failures.append(f"protected file is missing: {name}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            failures.append(f"protected file differs from the required release: {name}")
    return failures
"""


def metadata(profile, version="1.2.3", channel="newbie-115-1",
             minimum_version=None, tag=None, source="a"):
    minimum_version = version if minimum_version is None else minimum_version
    tag = f"{channel}-v{version}" if tag is None else tag
    return {
        "schema": 1,
        "channel": channel,
        "version": version,
        "tag": tag,
        "template": f"NYCU-SDNFV/Golden-{channel}-{profile['name']}",
        "source_sha": source * 40,
        "minimum_version": minimum_version,
    }


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8", newline="\n")
    return path


class WorkingDirectory:
    def __init__(self, path):
        self.path = Path(path)
        self.previous = None

    def __enter__(self):
        self.previous = Path.cwd()
        os.chdir(self.path)
        return self.path

    def __exit__(self, *_):
        os.chdir(self.previous)


class Fixture:
    def __init__(self, case, profile=PROFILES[0]):
        self.profile = dict(profile)
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "student"
        self.bundle = self.root / "bundle"
        self.workspace.mkdir()
        (self.bundle / "policy").mkdir(parents=True)
        write(self.bundle / "policy" / "integrity.py", INTEGRITY_POLICY)
        self.profile_raw = (
            json.dumps(self.profile, indent=2) + "\n"
        ).encode()
        write(
            self.bundle / "golden" / "profile.json",
            self.profile_raw,
        )
        write(
            self.workspace / ".github" / "golden" / "profile.json",
            self.profile_raw,
        )
        self.grader = Grader(self.profile)
        self.prepare = mock.Mock()
        self.grader.prepare_course_host = self.prepare
        self.protected = write(self.workspace / "Makefile", "protected\n")
        self.updater = write(
            self.workspace / ".github" / "release" / "upgrade.py",
            "raise SystemExit('copied only')\n",
        )
        self.set_rubric()
        self.stamp(metadata(self.profile))

    def rubric(self, runs=None):
        count = self.profile["checks"]
        points = [10, 15] + [0] * (count - 3) + [75]
        if runs is None:
            runs = ["true"] * count
        return {
            "tests": [
                {
                    "name": f"check {number:02d}",
                    "run": runs[number],
                    "points": points[number],
                    "timeout": 30,
                }
                for number in range(count)
            ]
        }

    def set_rubric(self, document=None):
        document = self.rubric() if document is None else document
        write(
            self.bundle / "tests.json",
            json.dumps(document, indent=2) + "\n",
        )
        return document

    def stamp(self, canonical, student=None, include_updater=True):
        student = canonical if student is None else student
        canonical_raw = (json.dumps(canonical, indent=2) + "\n").encode()
        student_raw = (json.dumps(student, indent=2) + "\n").encode()
        write(self.bundle / "release.json", canonical_raw)
        write(self.workspace / ".lab-release.json", student_raw)
        common = {
            "Makefile": hashlib.sha256(
                self.protected.read_bytes()
            ).hexdigest(),
            ".github/golden/profile.json": hashlib.sha256(
                self.profile_raw
            ).hexdigest(),
        }
        if include_updater:
            common[".github/release/upgrade.py"] = hashlib.sha256(
                self.updater.read_bytes()
            ).hexdigest()
        canonical_entries = {
            **common,
            ".lab-release.json": hashlib.sha256(canonical_raw).hexdigest(),
        }
        student_entries = {
            **common,
            ".lab-release.json": hashlib.sha256(student_raw).hexdigest(),
        }
        canonical_manifest = "".join(
            f"{digest}  {name}\n"
            for name, digest in canonical_entries.items()
        )
        student_manifest = "".join(
            f"{digest}  {name}\n"
            for name, digest in student_entries.items()
        )
        write(
            self.bundle / "policy" / "manifest.sha256",
            canonical_manifest,
        )
        write(
            self.workspace / ".github" / "policy" / "manifest.sha256",
            student_manifest,
        )

    def grade(self, execute=None):
        if execute is None:
            execute = lambda *_: (True, "fixture")
        return self.grader.grade(
            self.workspace, self.bundle, execute=execute
        )


class ProfileAndRubricTests(unittest.TestCase):
    def test_all_profiles_enforce_counts_and_keep_lab_identity(self):
        for profile in PROFILES:
            with self.subTest(profile=profile["name"]):
                fixture = Fixture(self, profile)
                tests = fixture.grader.load_tests(
                    fixture.bundle / "tests.json"
                )
                self.assertEqual(profile["checks"], len(tests))
                outcomes, failures = fixture.grade()
                self.assertEqual([], failures)
                self.assertEqual(100, sum(row["score"] for row in outcomes))
                fixture.prepare.assert_called_once_with(profile)
                context = fixture.grader.result_context(True)
                self.assertEqual(profile["assignment"], context["assignment"])
                result = fixture.grader.build_result(context, outcomes)
                self.assertTrue(
                    fixture.grader.release_body(
                        result, outcomes, failures
                    ).startswith(f"### Lab {profile['lab']}: 100/100\n")
                )

    def test_data_only_profile_supports_new_labs_and_rejects_bad_schema(self):
        profile = {
            "schema": 1,
            "name": "Demo",
            "lab": 9,
            "assignment": "lab9-demo",
            "checks": 1,
            "environment": "toolchain",
        }
        fixture = Fixture(self, profile)
        fixture.set_rubric({
            "tests": [{
                "name": "demo contract",
                "run": "true",
                "points": 100,
                "timeout": 30,
            }],
        })

        tests = fixture.grader.load_tests(fixture.bundle / "tests.json")
        outcomes, failures = fixture.grade()

        self.assertEqual(1, len(tests))
        self.assertEqual([], failures)
        self.assertEqual(100, sum(row["score"] for row in outcomes))
        self.assertEqual("Lab 9", fixture.grader.title)
        self.assertEqual(
            "lab9-demo",
            fixture.grader.result_context(True)["assignment"],
        )
        fixture.prepare.assert_called_once_with(profile)

        with self.assertRaisesRegex(
                ValueError, "invalid Golden environment profile"):
            Grader(dict(profile, schema=2))

    def test_rubric_rejects_bad_count_sum_names_commands_points_and_timeouts(self):
        fixture = Fixture(self)
        mutations = {}

        wrong_count = fixture.rubric()
        wrong_count["tests"].pop(2)
        wrong_count["tests"][-1]["points"] += 0
        mutations["count"] = wrong_count

        wrong_sum = fixture.rubric()
        wrong_sum["tests"][0]["points"] += 1
        mutations["sum"] = wrong_sum

        duplicate = fixture.rubric()
        duplicate["tests"][1]["name"] = duplicate["tests"][0]["name"]
        mutations["duplicate"] = duplicate

        empty_command = fixture.rubric()
        empty_command["tests"][0]["run"] = " "
        mutations["command"] = empty_command

        boolean_points = fixture.rubric()
        boolean_points["tests"][0]["points"] = True
        mutations["points"] = boolean_points

        for value in (0, 601, True):
            bad_timeout = fixture.rubric()
            bad_timeout["tests"][0]["timeout"] = value
            mutations[f"timeout-{value}"] = bad_timeout

        for name, document in mutations.items():
            with self.subTest(name=name):
                fixture.set_rubric(document)
                with self.assertRaises(ValueError):
                    fixture.grader.load_tests(
                        fixture.bundle / "tests.json"
                    )

    def test_zero_point_checks_are_valid_and_counted(self):
        fixture = Fixture(self)
        tests = fixture.grader.load_tests(fixture.bundle / "tests.json")
        self.assertGreater(
            sum(test["points"] == 0 for test in tests), 0
        )
        self.assertEqual(fixture.profile["checks"], len(tests))

    def test_duplicate_json_fields_and_symlinked_rubric_are_rejected(self):
        fixture = Fixture(self)
        write(
            fixture.bundle / "tests.json",
            '{"tests":[],"tests":[]}\n',
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
            fixture.grader.load_tests(fixture.bundle / "tests.json")
        target = fixture.root / "rubric-target.json"
        write(target, json.dumps(fixture.rubric()))
        path = fixture.bundle / "tests.json"
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        with self.assertRaisesRegex(ValueError, "regular JSON file"):
            fixture.grader.load_tests(path)


class ReleaseMetadataTests(unittest.TestCase):
    def test_configured_release_channels_follow_shared_publisher_contract(self):
        from tools.release.release import CHANNEL as publisher_channel
        from tools.runtime.golden.grading import CHANNEL as grader_channel

        self.assertEqual(publisher_channel.pattern, grader_channel.pattern)
        grader = Grader(PROFILES[0])
        document = metadata(PROFILES[0], channel="summer-validation")
        self.assertEqual(document, grader.validate_release_metadata(document))
        for channel in ("", "Summer", "../newbie", "newbie--validation"):
            with self.subTest(channel=channel), self.assertRaisesRegex(
                    ValueError, "invalid release channel"):
                grader.validate_release_metadata(metadata(PROFILES[0], channel=channel))

    def test_stable_and_legacy_release_tags_match_measure_contract(self):
        fixture = Fixture(self, PROFILES[2])
        accepted = (
            ("newbie", "0.1.0", "newbie"),
            ("newbie", "0.1.0", "newbie-v0.1.0"),
            ("newbie", "1.2.3", "v1.2.3"),
            ("newbie-115-1", "0.1.0", "newbie-115-1"),
            ("newbie-115-1", "1.2.3", "newbie-115-1-v1.2.3"),
            ("115-1", "0.1.0", "115-1"),
            ("115-1", "1.2.3", "115-1-v1.2.3"),
            ("116-2", "3.4.5", "116-2-v3.4.5"),
        )
        for channel, version, tag in accepted:
            document = metadata(
                fixture.profile,
                version=version,
                channel=channel,
                tag=tag,
            )
            with self.subTest(tag=tag):
                self.assertEqual(
                    document,
                    fixture.grader.validate_release_metadata(document),
                )

    def test_release_metadata_is_exact_and_profile_bound(self):
        fixture = Fixture(self, PROFILES[2])
        valid = metadata(fixture.profile)
        invalid = (
            {**valid, "schema": True},
            {**valid, "channel": "newbie-115-3"},
            {**valid, "version": "01.2.3"},
            {**valid, "tag": "v1.2.3"},
            {**valid, "template": valid["template"].replace(
                "Measure", "VRouter")},
            {**valid, "source_sha": "g" * 40},
            {**valid, "minimum_version": "2.0.0"},
            {**valid, "extra": "field"},
            {"schema": True, "version": "development"},
        )
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(
                    ValueError):
                fixture.grader.validate_release_metadata(document)
        self.assertEqual(
            DEVELOPMENT,
            fixture.grader.validate_release_metadata(DEVELOPMENT),
        )

    def test_canonical_malformed_unprotected_or_missing_updater_is_infrastructure_error(self):
        for mode in (
                "malformed", "duplicate", "oversized",
                "unprotected-release", "unprotected-profile",
                "missing-updater"):
            with self.subTest(mode=mode):
                fixture = Fixture(self)
                if mode == "malformed":
                    write(fixture.bundle / "release.json", "not JSON\n")
                elif mode == "duplicate":
                    write(
                        fixture.bundle / "release.json",
                        '{"schema":1,"schema":1}\n',
                    )
                elif mode == "oversized":
                    write(
                        fixture.bundle / "release.json",
                        b"{" + b"x" * (16 * 1024) + b"}",
                    )
                else:
                    lines = (
                        fixture.bundle
                        / "policy"
                        / "manifest.sha256"
                    ).read_text(encoding="utf-8").splitlines()
                    suffix = (
                        ".lab-release.json"
                        if mode == "unprotected-release"
                        else (
                            ".github/golden/profile.json"
                            if mode == "unprotected-profile"
                            else ".github/release/upgrade.py"
                        )
                    )
                    manifest = "\n".join(
                        line for line in lines
                        if not line.endswith(suffix)
                    ) + "\n"
                    write(
                        fixture.bundle / "policy" / "manifest.sha256",
                        manifest,
                    )
                    write(
                        fixture.workspace
                        / ".github"
                        / "policy"
                        / "manifest.sha256",
                        manifest,
                    )
                with self.assertRaises((OSError, ValueError)):
                    fixture.grade()
                fixture.prepare.assert_not_called()

    def test_canonical_profile_must_match_grader_and_exact_manifest_hash(self):
        fixture = Fixture(self)
        different = dict(PROFILES[1])
        write(
            fixture.bundle / "golden" / "profile.json",
            json.dumps(different, indent=2) + "\n",
        )
        with self.assertRaisesRegex(ValueError, "initialized grader"):
            fixture.grade()
        fixture.prepare.assert_not_called()

        fixture = Fixture(self)
        path = fixture.bundle / "golden" / "profile.json"
        write(path, path.read_text(encoding="utf-8") + "\n")
        with self.assertRaisesRegex(ValueError, "protect the exact"):
            fixture.grade()
        fixture.prepare.assert_not_called()

    def test_symlinked_canonical_release_is_infrastructure_error(self):
        fixture = Fixture(self)
        release = fixture.bundle / "release.json"
        target = fixture.root / "canonical-release.json"
        release.replace(target)
        try:
            release.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        with self.assertRaisesRegex(ValueError, "canonical release"):
            fixture.grade()
        fixture.prepare.assert_not_called()

    def test_stale_student_is_zero_with_manual_update_flow_and_no_execution(self):
        fixture = Fixture(self)
        old = metadata(fixture.profile, version="1.2.2", source="b")
        fixture.stamp(metadata(fixture.profile), student=old)
        execute = mock.Mock(
            side_effect=AssertionError("stale submission executed")
        )
        outcomes, failures = fixture.grade(execute)
        execute.assert_not_called()
        fixture.prepare.assert_not_called()
        self.assertEqual(0, sum(row["score"] for row in outcomes))
        self.assertTrue(any("Outdated Lab 0" in item for item in failures))
        body = fixture.grader.release_body(
            fixture.grader.build_result(
                fixture.grader.result_context(True), outcomes
            ),
            outcomes,
            failures,
        )
        for phrase in (
                "make check-update",
                "commit or stash",
                "untracked files",
                "make update",
                "Review and merge",
                "instructor/update-<tag>",
                "default branch",
                "make test",
                "push the Classroom default branch",
                "no pull request is created automatically"):
            self.assertIn(phrase, body)
        self.assertIn(MANUAL_UPDATE_HELP, body)

    def test_minimum_version_never_weakens_exact_canonical_equality(self):
        fixture = Fixture(self)
        required = metadata(
            fixture.profile,
            version="1.2.0",
            minimum_version="1.0.0",
        )
        current = metadata(
            fixture.profile,
            version="1.1.0",
            minimum_version="1.0.0",
            source="b",
        )
        fixture.stamp(required, student=current)
        outcomes, failures = fixture.grade()
        self.assertTrue(failures)
        self.assertEqual(0, sum(row["score"] for row in outcomes))
        fixture.prepare.assert_not_called()

    def test_malformed_oversized_and_symlinked_student_markers_are_zero(self):
        values = (
            b'{"schema":1,"schema":1}\n',
            b"\xff",
            b"x" * (16 * 1024 + 1),
        )
        for raw in values:
            with self.subTest(raw=raw[:30]):
                fixture = Fixture(self)
                write(fixture.workspace / ".lab-release.json", raw)
                outcomes, failures = fixture.grade()
                self.assertEqual(0, sum(row["score"] for row in outcomes))
                self.assertIn("Cannot verify Lab 0", failures[0])
                fixture.prepare.assert_not_called()

        fixture = Fixture(self)
        marker = fixture.workspace / ".lab-release.json"
        target = fixture.root / "student-release.json"
        marker.replace(target)
        try:
            marker.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        outcomes, failures = fixture.grade()
        self.assertEqual(0, sum(row["score"] for row in outcomes))
        self.assertIn("regular JSON file", failures[0])
        fixture.prepare.assert_not_called()


class OrchestrationTests(unittest.TestCase):
    def test_bootstrap_exception_is_a_configuration_value_error(self):
        self.assertTrue(issubclass(BootstrapEnvironmentError, ValueError))
        self.assertIs(GoldenEnvironmentError, BootstrapEnvironmentError)
    def test_prepare_method_delegates_the_selected_profile(self):
        grader = Grader(PROFILES[0])
        with mock.patch(
                "tools.runtime.golden.grading.environment.prepare_course_host",
                return_value=None,
        ) as prepare:
            self.assertIsNone(grader.prepare_course_host())
        prepare.assert_called_once_with(dict(PROFILES[0]))

    def test_normal_failure_preserves_partial_credit(self):
        fixture = Fixture(self)

        def execute(test, *_):
            return test["name"] != "check 01", "normal result"

        outcomes, failures = fixture.grade(execute)
        self.assertEqual([], failures)
        self.assertEqual(85, sum(row["score"] for row in outcomes))
        self.assertEqual(
            fixture.profile["checks"], len(outcomes)
        )

    def test_initial_and_post_command_integrity_failures_revoke_every_point(self):
        fixture = Fixture(self)
        fixture.protected.write_text(
            "tampered\n", encoding="utf-8", newline="\n"
        )
        execute = mock.Mock(
            side_effect=AssertionError("tampered submission executed")
        )
        outcomes, failures = fixture.grade(execute)
        execute.assert_not_called()
        fixture.prepare.assert_not_called()
        self.assertTrue(failures)
        self.assertEqual(0, sum(row["score"] for row in outcomes))

        fixture = Fixture(self)
        calls = []

        def mutate(test, *_):
            calls.append(test["name"])
            if len(calls) == 3:
                fixture.protected.write_text(
                    "changed during grading\n",
                    encoding="utf-8",
                    newline="\n",
                )
            return True, "passed before the gate"

        outcomes, failures = fixture.grade(mutate)
        self.assertEqual(3, len(calls))
        self.assertTrue(failures)
        self.assertEqual(0, sum(row["score"] for row in outcomes))
        self.assertTrue(all(not row["passed"] for row in outcomes))

    def test_manifest_is_loaded_once_and_cannot_be_replaced_during_grading(self):
        fixture = Fixture(self)
        calls = 0
        manifest = fixture.bundle / "policy" / "manifest.sha256"

        def mutate_manifest(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                fixture.protected.write_text(
                    "changed\n", encoding="utf-8", newline="\n"
                )
                digest = hashlib.sha256(
                    fixture.protected.read_bytes()
                ).hexdigest()
                write(manifest, f"{digest}  Makefile\n")
            return True, "attempted manifest replacement"

        outcomes, failures = fixture.grade(mutate_manifest)
        self.assertEqual(1, calls)
        self.assertTrue(failures)
        self.assertEqual(0, sum(row["score"] for row in outcomes))

    def test_instance_methods_are_resolved_at_call_time(self):
        fixture = Fixture(self)
        runner = mock.Mock(return_value=(True, "patched runner"))
        with mock.patch.object(fixture.grader, "run_test", runner):
            outcomes, failures = fixture.grader.grade(
                fixture.workspace, fixture.bundle
            )
        self.assertEqual([], failures)
        self.assertEqual(fixture.profile["checks"], runner.call_count)
        self.assertEqual(100, sum(row["score"] for row in outcomes))
        fixture.prepare.assert_called_once_with(fixture.profile)

    def test_prepare_failure_runs_no_student_command_and_main_removes_stale_results(self):
        failures = (
            subprocess.CalledProcessError(1, ["docker"]),
            subprocess.TimeoutExpired(["docker"], 180),
            OSError("docker unavailable"),
            GoldenEnvironmentError("host verification failed"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                fixture = Fixture(self)
                fixture.prepare.side_effect = failure
                runner = mock.Mock()
                fixture.grader.run_test = runner
                write(fixture.workspace / "result.json", "stale\n")
                write(fixture.workspace / "release-body.md", "stale\n")
                with WorkingDirectory(fixture.workspace), mock.patch.dict(
                        os.environ,
                        {
                            "CLASSROOM50_BUNDLE_DIR": str(fixture.bundle),
                            "GITHUB_ACTIONS": "false",
                        },
                        clear=True):
                    status = fixture.grader.main(["--local"])
                self.assertEqual(2, status)
                runner.assert_not_called()
                self.assertFalse(
                    (fixture.workspace / "result.json").exists()
                )
                self.assertFalse(
                    (fixture.workspace / "release-body.md").exists()
                )

    def test_configuration_error_returns_two_without_result_files(self):
        fixture = Fixture(self)
        write(fixture.bundle / "tests.json", "{}\n")
        write(fixture.workspace / "result.json", "stale\n")
        write(fixture.workspace / "release-body.md", "stale\n")
        with WorkingDirectory(fixture.workspace), mock.patch.dict(
                os.environ,
                {
                    "CLASSROOM50_BUNDLE_DIR": str(fixture.bundle),
                    "GITHUB_ACTIONS": "false",
                },
                clear=True):
            status = fixture.grader.main(["--local"])
        self.assertEqual(2, status)
        self.assertFalse((fixture.workspace / "result.json").exists())
        self.assertFalse((fixture.workspace / "release-body.md").exists())
        fixture.prepare.assert_not_called()

    def test_output_write_failure_removes_partial_result_files(self):
        fixture = Fixture(self)
        fixture.grader.run_test = mock.Mock(
            return_value=(True, "student command passed")
        )
        original_write_text = Path.write_text

        def fail_release_body(path, data, *args, **kwargs):
            if path.name == "release-body.md":
                raise OSError("release body is not writable")
            return original_write_text(path, data, *args, **kwargs)

        with WorkingDirectory(fixture.workspace), mock.patch.dict(
                os.environ,
                {
                    "CLASSROOM50_BUNDLE_DIR": str(fixture.bundle),
                    "GITHUB_ACTIONS": "false",
                },
                clear=True), mock.patch.object(
                    Path, "write_text", fail_release_body):
            status = fixture.grader.main(["--local"])
        self.assertEqual(2, status)
        self.assertFalse((fixture.workspace / "result.json").exists())
        self.assertFalse((fixture.workspace / "release-body.md").exists())

    def test_local_and_classroom_result_contexts_are_not_confused(self):
        fixture = Fixture(self, PROFILES[3])
        local = fixture.grader.result_context(True)
        self.assertEqual("lab3-vrouter", local["assignment"])
        self.assertEqual("submit/local", local["submission"])
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
                ValueError, "missing Classroom 50"):
            fixture.grader.result_context(False)
        values = {
            "CLASSROOM": "formal",
            "ASSIGNMENT": "official-assignment",
            "OWNER": "student",
            "SUBMISSION_TAG": "submit/tag",
            "COMMIT_URL": "https://example.invalid/commit",
            "RELEASE_URL": "https://example.invalid/release",
            "REVIEW_URL": "https://example.invalid/review",
            "ASSIGNMENT_TYPE": "individual",
        }
        with mock.patch.dict(os.environ, values, clear=True):
            context = fixture.grader.result_context(False)
        self.assertEqual("official-assignment", context["assignment"])
        self.assertEqual("formal", context["classroom"])

    def test_untrusted_logs_are_indented_and_table_names_are_escaped(self):
        fixture = Fixture(self)
        outcomes = [{
            "test-name": "unsafe | item",
            "passed": False,
            "score": 0,
            "max-score": 10,
            "detail": "```\n| injected | table\n### forged heading\n<script>",
        }]
        result = fixture.grader.build_result(
            fixture.grader.result_context(True), outcomes
        )
        body = fixture.grader.release_body(result, outcomes, [])
        self.assertIn("| unsafe \\| item | 0/10 |", body)
        for line in (
                "```",
                "| injected | table",
                "### forged heading",
                "<script>"):
            self.assertIn(f"    {line}", body)
            self.assertNotIn(f"\n{line}\n", body)

    @unittest.skipIf(
        os.name == "nt" or shutil.which("bash") is None,
        "POSIX process-group timeout requires bash",
    )
    def test_native_timeout_kills_the_scoped_process_group(self):
        fixture = Fixture(self)
        passed, detail = fixture.grader.run_test(
            {"run": "sleep 10", "timeout": 1},
            fixture.workspace,
            fixture.bundle,
        )
        self.assertFalse(passed)
        self.assertIn("timed out after 1 seconds", detail)

    @unittest.skipUnless(
        shutil.which("bash"),
        "bounded command execution requires bash",
    )
    def test_native_log_capture_keeps_only_the_last_12000_bytes(self):
        fixture = Fixture(self)
        command = (
            "i=0; while [ \"$i\" -lt 13000 ]; do printf x; "
            "i=$((i + 1)); done"
        )
        passed, detail = fixture.grader.run_test(
            {"run": command, "timeout": 30},
            fixture.workspace,
            fixture.bundle,
        )
        self.assertTrue(passed)
        self.assertLessEqual(
            len(detail.encode("utf-8")),
            12000 + len("command exited 0\n"),
        )
        self.assertTrue(detail.startswith("command exited 0\n"))


class EntrypointTests(unittest.TestCase):
    def copy_runtime(self, target, profile):
        target.mkdir(parents=True, exist_ok=True)
        for name in (
                "__init__.py", "contract.py", "environment.py", "grading.py",
                "probe.py"):
            shutil.copy2(RUNTIME / "golden" / name, target / name)
        write(
            target / "profile.json",
            json.dumps(profile, indent=2) + "\n",
        )

    def canonical_fixture(self):
        fixture = Fixture(self, PROFILES[0])
        golden = fixture.bundle / "golden"
        self.copy_runtime(golden, fixture.profile)
        shutil.copy2(
            RUNTIME / "autograder.py",
            fixture.bundle / "autograder.py",
        )
        malicious = fixture.workspace / "golden"
        malicious.mkdir()
        write(
            malicious / "__init__.py",
            "raise RuntimeError('student CWD package imported')\n",
        )
        return fixture

    def test_fixed_adapter_supports_source_runtime_layout(self):
        fixture = Fixture(self, PROFILES[1])
        source = fixture.root / "source"
        grade = source / ".github" / "grade"
        grade.mkdir(parents=True)
        self.copy_runtime(
            source / ".github" / "golden", fixture.profile
        )
        adapter_path = grade / "autograder.py"
        shutil.copy2(RUNTIME / "autograder.py", adapter_path)
        specification = importlib.util.spec_from_file_location(
            f"source_adapter_{fixture.root.name}", adapter_path
        )
        adapter = importlib.util.module_from_spec(specification)
        with WorkingDirectory(fixture.workspace):
            specification.loader.exec_module(adapter)
        self.assertEqual("Controller", adapter.GRADER.profile["name"])
        self.assertEqual(
            "lab1-controller",
            adapter.result_context(True)["assignment"],
        )
        self.assertFalse(list(source.rglob("__pycache__")))

    def test_invalid_source_profile_returns_two_without_result_files(self):
        fixture = Fixture(self)
        source = fixture.root / "invalid-source"
        grade = source / ".github" / "grade"
        grade.mkdir(parents=True)
        golden = source / ".github" / "golden"
        self.copy_runtime(golden, fixture.profile)
        write(golden / "profile.json", "{}\n")
        adapter_path = grade / "autograder.py"
        shutil.copy2(RUNTIME / "autograder.py", adapter_path)
        write(fixture.workspace / "result.json", "stale\n")
        write(fixture.workspace / "release-body.md", "stale\n")
        complete = subprocess.run(
            [sys.executable, str(adapter_path), "--local"],
            cwd=fixture.workspace,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(2, complete.returncode, complete.stderr)
        self.assertIn(
            "autograder configuration/runtime error", complete.stderr
        )
        self.assertFalse((fixture.workspace / "result.json").exists())
        self.assertFalse((fixture.workspace / "release-body.md").exists())
        self.assertFalse(list(source.rglob("__pycache__")))

    def test_fixed_adapter_uses_trusted_runtime_without_requiring_bash(self):
        fixture = self.canonical_fixture()
        specification = importlib.util.spec_from_file_location(
            f"canonical_adapter_{fixture.root.name}",
            fixture.bundle / "autograder.py",
        )
        adapter = importlib.util.module_from_spec(specification)
        with WorkingDirectory(fixture.workspace):
            specification.loader.exec_module(adapter)
        self.assertEqual("Toolchain", adapter.GRADER.profile["name"])
        adapter.GRADER.prepare_course_host = mock.Mock()
        adapter.GRADER.run_test = mock.Mock(
            return_value=(True, "mocked canonical command")
        )
        outcomes, failures = adapter.grade(
            fixture.workspace, fixture.bundle
        )
        self.assertEqual([], failures)
        self.assertEqual(100, sum(row["score"] for row in outcomes))
        adapter.GRADER.prepare_course_host.assert_called_once_with(
            fixture.profile
        )
        self.assertEqual(
            fixture.profile["checks"],
            adapter.GRADER.run_test.call_count,
        )
        self.assertFalse(list(fixture.bundle.rglob("__pycache__")))
        self.assertFalse(list(fixture.workspace.rglob("__pycache__")))

    @unittest.skipUnless(
        shutil.which("bash"),
        "canonical CLI execution requires bash",
    )
    def test_fixed_cli_imports_only_trusted_runtime_and_enforces_full_score(self):
        fixture = self.canonical_fixture()
        command = [
            sys.executable,
            str(fixture.bundle / "autograder.py"),
            "--local",
            "--require-full-score",
        ]
        environment = dict(
            os.environ,
            GITHUB_ACTIONS="false",
            CLASSROOM50_BUNDLE_DIR=str(fixture.bundle),
            PYTHONDONTWRITEBYTECODE="1",
        )
        complete = subprocess.run(
            command,
            cwd=fixture.workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(0, complete.returncode, complete.stderr)
        result = json.loads(
            (fixture.workspace / "result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(100, result["score"])
        self.assertTrue(all(row["passed"] for row in result["tests"]))
        self.assertFalse(list(fixture.bundle.rglob("__pycache__")))
        self.assertFalse(list(fixture.workspace.rglob("__pycache__")))
        self.assertNotIn(
            b"\r",
            (fixture.workspace / "result.json").read_bytes(),
        )
        self.assertNotIn(
            b"\r",
            (fixture.workspace / "release-body.md").read_bytes(),
        )

        document = fixture.rubric()
        zero = next(
            item for item in document["tests"] if item["points"] == 0
        )
        zero["run"] = "printf 'untrusted | output'; exit 7"
        fixture.set_rubric(document)
        normal = subprocess.run(
            command[:-1],
            cwd=fixture.workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        strict = subprocess.run(
            command,
            cwd=fixture.workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(0, normal.returncode, normal.stderr)
        self.assertEqual(1, strict.returncode, strict.stderr)
        result = json.loads(
            (fixture.workspace / "result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(100, result["score"])
        self.assertTrue(any(not row["passed"] for row in result["tests"]))

    def test_main_reports_invalid_canonical_profile_as_exit_two(self):
        fixture = Fixture(self)
        fixture.grader.prepare_course_host = mock.Mock()
        write(fixture.bundle / "golden" / "profile.json", "{}\n")
        with WorkingDirectory(fixture.workspace), mock.patch.dict(
                os.environ,
                {
                    "CLASSROOM50_BUNDLE_DIR": str(fixture.bundle),
                    "GITHUB_ACTIONS": "false",
                },
                clear=True):
            self.assertEqual(2, fixture.grader.main(["--local"]))
        fixture.grader.prepare_course_host.assert_not_called()
        self.assertFalse((fixture.workspace / "result.json").exists())
        self.assertFalse((fixture.workspace / "release-body.md").exists())


if __name__ == "__main__":
    unittest.main()
