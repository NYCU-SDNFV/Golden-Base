#!/usr/bin/env python3
"""Build the canonical grader bundle without executing source-provided code."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys


class BundleError(Exception):
    pass


def runtime():
    path = Path(__file__).resolve().parent / "runtime.py"
    spec = importlib.util.spec_from_file_location("golden_bundle_runtime", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded.helper()


def regular(path, label):
    if not path.is_file() or path.is_symlink():
        raise BundleError(f"required {label} is missing or not a regular file: {path}")


def copy_file(source, target):
    regular(source, "bundle input")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def validate_rubric(path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read canonical rubric: {exc}") from exc
    tests = document.get("tests") if isinstance(document, dict) else None
    if (not isinstance(tests, list) or not tests
            or any(not isinstance(test, dict)
                   or not isinstance(test.get("name"), str)
                   or not test["name"].strip()
                   or not isinstance(test.get("run"), str)
                   or not test["run"].strip()
                   or type(test.get("points")) is not int
                   or test["points"] < 0
                   or type(test.get("timeout")) is not int
                   or not 0 < test["timeout"] <= 600
                   for test in tests)
            or len({test["name"] for test in tests}) != len(tests)
            or sum(test["points"] for test in tests) != 100):
        raise BundleError(
            "canonical rubric must have uniquely named run tests with valid "
            "integer points/timeouts totaling 100"
        )
    return tests


def build(source, student, output):
    source = Path(source).resolve()
    student = Path(student).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise BundleError("bundle output already exists; choose a fresh path")
    distribution = runtime()
    try:
        distribution.validate(source)
    except ValueError as exc:
        raise BundleError(f"invalid shared runtime contract: {exc}") from exc
    tests = student / "tests"
    if not tests.is_dir() or tests.is_symlink():
        raise BundleError("sanitized student tree has no regular tests directory")
    output.mkdir(parents=True)
    shutil.copytree(tests, output / "tests", copy_function=shutil.copy2)
    mapping = {
        source / ".github/grade/tests.json": output / "tests.json",
        student / ".lab-release.json": output / "release.json",
        student / ".github/policy/00_layout.sh": output / "policy/00_layout.sh",
        student / ".github/policy/01_integrity.sh": output / "policy/01_integrity.sh",
        student / ".github/policy/integrity.py": output / "policy/integrity.py",
        student / ".github/policy/manifest.sha256": output / "policy/manifest.sha256",
        student / ".github/tests/lib.sh": output / "policy/lib.sh",
    }
    for source_path, target_path in mapping.items():
        copy_file(source_path, target_path)
    try:
        profile = distribution.inject_bundle(source, student, output)
    except ValueError as exc:
        raise BundleError(f"cannot distribute shared runtime: {exc}") from exc
    if len(validate_rubric(output / "tests.json")) != profile["checks"]:
        raise BundleError("canonical rubric count must match the protected runtime profile")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--student", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        build(args.source, args.student, args.output)
    except (OSError, BundleError) as exc:
        print(f"bundle: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
