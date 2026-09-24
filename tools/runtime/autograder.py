#!/usr/bin/env python3
"""Thin entrypoint for the trusted shared Golden grading runtime."""

import sys

sys.dont_write_bytecode = True

import hashlib
import importlib
import importlib.util
from pathlib import Path


_ENTRY_DIRECTORY = Path(__file__).resolve().parent
if (
        _ENTRY_DIRECTORY.name == "grade"
        and _ENTRY_DIRECTORY.parent.name == ".github"
):
    _RUNTIME_DIRECTORY = _ENTRY_DIRECTORY.parent / "golden"
else:
    _RUNTIME_DIRECTORY = _ENTRY_DIRECTORY / "golden"


def _load_grading():
    identity = hashlib.sha256(
        str(_RUNTIME_DIRECTORY).encode("utf-8")
    ).hexdigest()[:16]
    package_name = f"_golden_runtime_{identity}"
    if package_name not in sys.modules:
        specification = importlib.util.spec_from_file_location(
            package_name,
            _RUNTIME_DIRECTORY / "__init__.py",
            submodule_search_locations=[str(_RUNTIME_DIRECTORY)],
        )
        if specification is None or specification.loader is None:
            raise ImportError("cannot load the trusted Golden runtime package")
        package = importlib.util.module_from_spec(specification)
        sys.modules[package_name] = package
        specification.loader.exec_module(package)
    return importlib.import_module(f"{package_name}.grading")


_GRADING = _load_grading()
Grader = _GRADING.Grader
_GRADER_ERROR = None
try:
    GRADER = Grader(
        _GRADING.load_profile(_RUNTIME_DIRECTORY / "profile.json")
    )
except (OSError, ValueError, KeyError) as error:
    GRADER = None
    _GRADER_ERROR = error


def _unavailable(*_args, **_kwargs):
    raise _GRADER_ERROR


if GRADER is None:
    load_tests = _unavailable
    run_test = _unavailable
    grade = _unavailable
    result_context = _unavailable
    build_result = _unavailable
    release_body = _unavailable
    validate_release_metadata = _unavailable
    read_release_metadata = _unavailable
    release_failures = _unavailable
else:
    load_tests = GRADER.load_tests
    run_test = GRADER.run_test
    grade = GRADER.grade
    result_context = GRADER.result_context
    build_result = GRADER.build_result
    release_body = GRADER.release_body
    validate_release_metadata = GRADER.validate_release_metadata
    read_release_metadata = GRADER.read_release_metadata
    release_failures = GRADER.release_failures


def main(argv=None, default_bundle=_ENTRY_DIRECTORY):
    if _GRADER_ERROR is not None:
        cleanup_error = None
        for name in ("result.json", "release-body.md"):
            try:
                (Path.cwd() / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                cleanup_error = error
        message = str(_GRADER_ERROR)
        if cleanup_error is not None:
            message += f"; failed to remove stale output: {cleanup_error}"
        print(
            f"Golden autograder configuration/runtime error: {message}",
            file=sys.stderr,
        )
        return 2
    return GRADER.main(argv, default_bundle=default_bundle)


if __name__ == "__main__":
    sys.exit(main())
