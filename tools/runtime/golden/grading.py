"""Canonical grading orchestration shared by every Golden Lab."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import subprocess
import sys
import tempfile
from types import MappingProxyType

from . import environment
from .contract import load_profile, read_json, validate_profile


DEVELOPMENT = {"schema": 1, "version": "development"}
MARKER = ".lab-release.json"
MANIFEST_TARGET = ".github/policy/manifest.sha256"
PROFILE_TARGET = ".github/golden/profile.json"
UPDATER = ".github/release/upgrade.py"
MAX_METADATA_BYTES = 16 * 1024
MAX_LOG_BYTES = 12000
RELEASE_FIELDS = {
    "schema", "channel", "version", "tag", "template", "source_sha",
    "minimum_version",
}
CLASSROOM_ENVIRONMENT = {
    "classroom": "CLASSROOM",
    "assignment": "ASSIGNMENT",
    "owner": "OWNER",
    "submission": "SUBMISSION_TAG",
    "commit": "COMMIT_URL",
    "release": "RELEASE_URL",
    "review": "REVIEW_URL",
    "assignment_type": "ASSIGNMENT_TYPE",
}
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
CHANNEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
MANUAL_UPDATE_HELP = (
    "Manual update required: run `make check-update`, then commit or stash all "
    "work, including untracked files, before running `make update`. Review and "
    "merge the resulting `instructor/update-<tag>` branch into your Classroom "
    "default branch. Run `make test`, then push the Classroom default branch to "
    "resubmit; pushing only the update branch is not a submission. Updates are "
    "manual; no pull request is created automatically. Do not edit protected "
    "files or their hashes."
)


def _version(value):
    if (not isinstance(value, str) or len(value) > 128
            or not SEMVER.fullmatch(value)):
        raise ValueError("release version must be stable MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in value.split("."))


def _one_line(value):
    return " ".join(str(value).splitlines())


def _table_cell(value):
    return _one_line(value).replace("\\", "\\\\").replace("|", "\\|")


class Grader:
    """Grade one Lab selected by a strict, data-only runtime profile."""

    def __init__(self, profile):
        self.profile = MappingProxyType(validate_profile(profile))

    @property
    def title(self):
        return f"Lab {self.profile['lab']}"

    def prepare_course_host(self, profile=None):
        selected = dict(self.profile) if profile is None else profile
        return environment.prepare_course_host(selected)

    def load_tests(self, path):
        document = read_json(path)
        tests = document.get("tests") if isinstance(document, dict) else None
        if not isinstance(tests, list) or not tests:
            raise ValueError(
                f"{self.title} tests.json must contain a nonempty tests list"
            )
        names = set()
        loaded = []
        for test in tests:
            if (not isinstance(test, dict)
                    or not isinstance(test.get("name"), str)
                    or not test["name"].strip()
                    or test["name"] in names
                    or not isinstance(test.get("run"), str)
                    or not test["run"].strip()
                    or type(test.get("points")) is not int
                    or test["points"] < 0
                    or type(test.get("timeout")) is not int
                    or not 1 <= test["timeout"] <= 600):
                raise ValueError("tests.json contains an invalid rubric item")
            names.add(test["name"])
            loaded.append(dict(test))
        if sum(test["points"] for test in loaded) != 100:
            raise ValueError(f"{self.title} rubric must total exactly 100 points")
        if len(loaded) != self.profile["checks"]:
            raise ValueError(
                f"{self.title} tests.json must contain exactly "
                f"{self.profile['checks']} rubric checks"
            )
        return loaded

    def run_test(self, test, workspace, bundle):
        workspace = Path(workspace)
        bundle = Path(bundle)
        child_environment = dict(
            os.environ,
            CLASSROOM50_BUNDLE_DIR=str(bundle.resolve()),
            PYTHONDONTWRITEBYTECODE="1",
        )
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                ["bash", "-o", "pipefail", "-c", test["run"]],
                cwd=workspace,
                env=child_environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code = process.wait(timeout=test["timeout"])
                passed = code == 0
                message = f"command exited {code}"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                passed = False
                message = f"timed out after {test['timeout']} seconds"
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - MAX_LOG_BYTES))
            output = log.read().decode("utf-8", errors="replace")
        return passed, f"{message}\n{output}".rstrip()

    def validate_release_metadata(self, data):
        if not isinstance(data, dict):
            raise ValueError("release metadata must be a JSON object")
        if (set(data) == set(DEVELOPMENT)
                and type(data.get("schema")) is int
                and data == DEVELOPMENT):
            return dict(data)
        if (set(data) != RELEASE_FIELDS
                or type(data.get("schema")) is not int
                or data["schema"] != 1
                or any(not isinstance(data[field], str)
                       for field in RELEASE_FIELDS - {"schema"})):
            raise ValueError(
                "release metadata has missing, unexpected, or non-string fields"
            )
        channel = data["channel"]
        if not CHANNEL.fullmatch(channel):
            raise ValueError("invalid release channel")
        version = _version(data["version"])
        minimum = _version(data["minimum_version"])
        if minimum > version:
            raise ValueError("minimum_version exceeds the release version")
        tags = {f"{channel}-v{data['version']}"}
        if version == (0, 1, 0):
            tags.add(channel)
        if channel == "newbie":
            tags.add(f"v{data['version']}")
        if data["tag"] not in tags:
            raise ValueError("release tag does not match its channel and version")
        template = (
            f"NYCU-SDNFV/Golden-{channel}-{self.profile['name']}"
        )
        if data["template"] != template:
            raise ValueError(
                "release template does not match the selected Lab profile"
            )
        if not SOURCE_SHA.fullmatch(data["source_sha"]):
            raise ValueError(
                "release source_sha must be 40 lowercase hexadecimal digits"
            )
        return dict(data)

    def read_release_metadata(self, path):
        path = Path(path)
        data = self.validate_release_metadata(
            read_json(path, limit=MAX_METADATA_BYTES)
        )
        with path.open("rb") as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise ValueError("release metadata exceeds 16 KiB")
        return data, raw

    def release_failures(self, workspace, required):
        try:
            current, _ = self.read_release_metadata(
                Path(workspace) / MARKER
            )
        except (OSError, ValueError) as exc:
            return [
                f"Cannot verify {self.title} {self.profile['name']} starter "
                f"release: {_one_line(exc)}. {MANUAL_UPDATE_HELP}"
            ]
        if current == required:
            return []
        if current == DEVELOPMENT or required == DEVELOPMENT:
            return [
                "Development and published release markers are not "
                f"interchangeable. {MANUAL_UPDATE_HELP}"
            ]
        if ((current["channel"], current["template"])
                != (required["channel"], required["template"])):
            return [
                f"Wrong starter channel/template; this assignment requires "
                f"{required['template']}. Use the correct Classroom assignment, "
                f"not a cross-channel update. {MANUAL_UPDATE_HELP}"
            ]
        current_version = _version(current["version"])
        required_version = _version(required["version"])
        if current_version < required_version:
            return [
                f"Outdated {self.title} {self.profile['name']} starter: "
                f"{current['channel']} {current['version']}; canonical "
                f"{required['version']} is required. {MANUAL_UPDATE_HELP}"
            ]
        return [
            f"Starter provenance does not exactly match canonical "
            f"{required['channel']} {required['version']}. "
            f"{MANUAL_UPDATE_HELP}"
        ]

    def _integrity_runtime(self, bundle):
        namespace = runpy.run_path(
            str(Path(bundle) / "policy" / "integrity.py")
        )
        load_manifest = namespace.get("load_manifest")
        check_integrity = namespace.get("check_integrity")
        if not callable(load_manifest) or not callable(check_integrity):
            raise ValueError(
                "canonical integrity policy lacks the required API"
            )
        return load_manifest, check_integrity

    @staticmethod
    def _integrity_failures(check_integrity, workspace, entries):
        failures = check_integrity(workspace, entries)
        if (not isinstance(failures, list)
                or any(not isinstance(item, str) or not item
                       for item in failures)):
            raise ValueError(
                "canonical integrity policy returned invalid failures"
            )
        return failures

    def _student_gate(self, workspace, required, check_integrity, entries):
        return (
            self.release_failures(workspace, required)
            + self._integrity_failures(
                check_integrity, workspace, entries
            )
        )

    def grade(self, workspace, bundle, execute=None):
        workspace = Path(workspace)
        bundle = Path(bundle)
        tests = self.load_tests(bundle / "tests.json")
        load_manifest, check_integrity = self._integrity_runtime(bundle)
        manifest = bundle / "policy" / "manifest.sha256"
        entries = load_manifest(manifest)
        if not isinstance(entries, dict):
            raise ValueError(
                "canonical integrity policy returned an invalid manifest"
            )
        entries = dict(entries)
        entries[MANIFEST_TARGET] = hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest()
        entries = MappingProxyType(entries)

        profile_path = bundle / "golden" / "profile.json"
        try:
            canonical_profile = validate_profile(read_json(profile_path))
        except ValueError as exc:
            raise ValueError(
                f"canonical runtime profile is invalid: {exc}"
            ) from exc
        profile_raw = profile_path.read_bytes()
        if canonical_profile != dict(self.profile):
            raise ValueError(
                "canonical runtime profile does not match the initialized grader"
            )
        if (entries.get(PROFILE_TARGET)
                != hashlib.sha256(profile_raw).hexdigest()):
            raise ValueError(
                "canonical manifest must protect the exact Golden runtime profile"
            )

        try:
            required, release_raw = self.read_release_metadata(
                bundle / "release.json"
            )
        except ValueError as exc:
            raise ValueError(
                f"canonical release metadata is invalid: {exc}"
            ) from exc
        if entries.get(MARKER) != hashlib.sha256(release_raw).hexdigest():
            raise ValueError(
                "canonical manifest must protect the exact bundle release.json"
            )
        if UPDATER not in entries:
            raise ValueError(
                "canonical manifest must protect the student updater"
            )

        failures = self._student_gate(
            workspace, required, check_integrity, entries
        )
        if not failures:
            self.prepare_course_host(dict(self.profile))
        runner = self.run_test if execute is None else execute
        outcomes = []
        for test in tests:
            if not failures:
                failures = self._student_gate(
                    workspace, required, check_integrity, entries
                )
            if failures:
                passed = False
                detail = (
                    "Not awarded: release or protected-file integrity "
                    "gate failed."
                )
            else:
                result = runner(test, workspace, bundle)
                if (not isinstance(result, tuple) or len(result) != 2
                        or type(result[0]) is not bool
                        or not isinstance(result[1], str)):
                    raise ValueError(
                        "test runner must return a (bool, str) pair"
                    )
                passed, detail = result
            outcomes.append({
                "test-name": test["name"],
                "passed": passed,
                "score": test["points"] if passed else 0,
                "max-score": test["points"],
                "detail": detail,
            })
            if not failures:
                failures = self._student_gate(
                    workspace, required, check_integrity, entries
                )
        if failures:
            for outcome in outcomes:
                outcome["passed"] = False
                outcome["score"] = 0
                outcome["detail"] = (
                    "Whole-lab release/integrity gate: no points awarded."
                )
        return outcomes, failures

    def result_context(self, local):
        if local:
            return {
                "classroom": "local",
                "assignment": self.profile["assignment"],
                "owner": "instructor",
                "submission": "submit/local",
                "commit": "local",
                "release": "local",
                "review": "local",
                "assignment_type": "individual",
            }
        missing = [
            name for name in CLASSROOM_ENVIRONMENT.values()
            if not os.environ.get(name)
        ]
        if missing:
            raise ValueError(
                "missing Classroom 50 environment: " + ", ".join(missing)
            )
        return {
            field: os.environ[name]
            for field, name in CLASSROOM_ENVIRONMENT.items()
        }

    def build_result(self, context, outcomes):
        return dict(
            context,
            schema="classroom50/result/v1",
            datetime=datetime.datetime.now(
                datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            score=sum(row["score"] for row in outcomes),
            **{"max-score": sum(row["max-score"] for row in outcomes)},
            tests=[
                {
                    key: row[key]
                    for key in (
                        "test-name", "passed", "score", "max-score"
                    )
                }
                for row in outcomes
            ],
        )

    def release_body(self, result, outcomes, failures):
        lines = [
            f"### {self.title}: "
            f"{result['score']}/{result['max-score']}",
            "",
        ]
        if failures:
            lines.extend([
                f"**Release/protected-file integrity failed: the entire "
                f"{self.title} rubric scores 0/100.**",
                MANUAL_UPDATE_HELP,
                "",
            ])
            lines.extend(
                f"- {_one_line(message)}" for message in failures
            )
            lines.append("")
        lines.extend(["| Item | Score |", "|---|---|"])
        for row in outcomes:
            lines.append(
                f"| {_table_cell(row['test-name'])} | "
                f"{row['score']}/{row['max-score']} |"
            )
        if not failures:
            for row in outcomes:
                if not row["passed"]:
                    lines.extend([
                        "",
                        f"**{_one_line(row['test-name'])}**",
                        "",
                    ])
                    details = row["detail"].splitlines() or [""]
                    lines.extend(f"    {line}" for line in details)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _remove_stale_output(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def main(self, argv=None, default_bundle=None):
        parser = argparse.ArgumentParser(
            description="Run the canonical Golden Lab grader."
        )
        parser.add_argument("--local", action="store_true")
        parser.add_argument("--require-full-score", action="store_true")
        args = parser.parse_args(argv)
        workspace = Path.cwd()
        outputs = (
            workspace / "result.json",
            workspace / "release-body.md",
        )
        try:
            for path in outputs:
                self._remove_stale_output(path)
            configured = os.environ.get("CLASSROOM50_BUNDLE_DIR")
            if configured is not None and not configured:
                raise ValueError(
                    "CLASSROOM50_BUNDLE_DIR must not be empty"
                )
            if configured is None:
                if default_bundle is None:
                    raise ValueError(
                        "canonical bundle directory was not provided"
                    )
                bundle = Path(default_bundle)
            else:
                bundle = Path(configured)
            context = self.result_context(args.local)
            outcomes, failures = self.grade(workspace, bundle)
            result = self.build_result(context, outcomes)
            body = self.release_body(result, outcomes, failures)
            outputs[0].write_text(
                json.dumps(result, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            outputs[1].write_text(
                body,
                encoding="utf-8",
                newline="\n",
            )
        except (
                OSError,
                ValueError,
                KeyError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
        ) as exc:
            for path in outputs:
                try:
                    self._remove_stale_output(path)
                except OSError as cleanup_error:
                    print(
                        f"{self.title} {self.profile['name']} autograder "
                        "configuration/runtime error: "
                        f"{exc}; failed to remove stale output {path}: "
                        f"{cleanup_error}",
                        file=sys.stderr,
                    )
                    return 2
            print(
                f"{self.title} {self.profile['name']} autograder "
                f"configuration/runtime error: {exc}",
                file=sys.stderr,
            )
            return 2
        print(body)
        complete = (
            result["score"] == 100
            and all(row["passed"] for row in outcomes)
        )
        return (
            1
            if args.require_full_score and not complete
            else 0
        )
