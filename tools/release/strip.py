#!/usr/bin/env python3
"""Create a sanitized student tree from a tagged instructor source tree."""

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


KEY_TAG = re.compile(r"(?:#|//|<!--)\s*KEY\b")
KEY_BEGIN = re.compile(r"(?:#|//|<!--)\s*BEGIN\s+KEY\b")
KEY_END = re.compile(r"(?:#|//|<!--)\s*END\s+KEY\b")
STUDENT_HASH = re.compile(r"^(\s*)(?:#|//)\s*STUDENT:[ ]?(.*)$")
STUDENT_HTML = re.compile(r"^(\s*)<!--\s*STUDENT:[ ]?(.*?)[ ]?-->\s*$")
ANY_MARKER = re.compile(
    r"(?:#|//|<!--)\s*(?:BEGIN\s+KEY|END\s+KEY|KEY\b|STUDENT:)"
)
BANNED_NAME = re.compile(
    r"(^|/)(\.env|id_rsa|.*\.pem)$|\.solution\.|LAB[0-9]+-SOLUTION\.md"
)
DEFAULT_IGNORE = (
    ".studentignore",
    ".github/workflows/",
    ".github/grade/",
    ".github/golden/grading.py",
    "tools/student-build/",
    "tools/release/",
    "INSTRUCTOR-*",
    "TA-BRIEF.md",
    "checkpoint/",
    "rubric/",
    "instructor/",
    "*-tests.json",
)
MANIFEST = ".github/policy/manifest.sha256"
METADATA = ".lab-release.json"


class StripError(Exception):
    pass


def runtime():
    path = Path(__file__).resolve().parent / "runtime.py"
    spec = importlib.util.spec_from_file_location("golden_strip_runtime", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded.helper()


def tracked_files(source):
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
         "-C", str(source), "ls-files", "-z"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=60,
    )
    if result.returncode:
        raise StripError("cannot enumerate tagged source files")
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def load_ignore(source):
    patterns = list(DEFAULT_IGNORE)
    path = Path(source) / ".studentignore"
    if path.is_file() and not path.is_symlink():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def ignored(path, patterns):
    for pattern in patterns:
        if pattern.endswith("/"):
            if path.startswith(pattern) or ("/" + pattern) in ("/" + path):
                return True
        elif fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(
                os.path.basename(path), pattern):
            return True
    return False


def is_text(data):
    if b"\0" in data[:8000]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def strip_text(text, path):
    output = []
    in_key = False
    for number, line in enumerate(text.split("\n"), 1):
        if in_key:
            if KEY_END.search(line):
                in_key = False
            continue
        if KEY_BEGIN.search(line):
            in_key = True
            continue
        if KEY_TAG.search(line):
            continue
        match = STUDENT_HTML.match(line) or STUDENT_HASH.match(line)
        if match:
            output.append(match.group(1) + match.group(2))
        else:
            output.append(line)
    if in_key:
        raise StripError(f"{path}: BEGIN KEY without END KEY")
    return "\n".join(output)


def manifest_paths(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StripError(f"cannot read protected-file manifest: {exc}") from exc
    paths = []
    for line in lines:
        fields = line.split()
        if (len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0])
                or fields[1].startswith("/") or ".." in fields[1].split("/")
                or "\\" in fields[1]):
            raise StripError("invalid protected-file manifest")
        paths.append(fields[1])
    if not paths or len(paths) != len(set(paths)):
        raise StripError("protected-file manifest must be nonempty and unique")
    return paths


def regenerate_manifest(output, source):
    target = Path(output) / MANIFEST
    if not target.is_file() or target.is_symlink():
        raise StripError("student tree is missing the protected-file manifest")
    paths = manifest_paths(target)
    lines = []
    for name in paths:
        original = Path(source) / name
        generated = Path(output) / name
        if (not original.is_file() or original.is_symlink()
                or not generated.is_file() or generated.is_symlink()):
            raise StripError(f"manifest path is not a regular student file: {name}")
        if original.read_bytes() != generated.read_bytes():
            raise StripError(
                f"protected file {name} differs between source and student tree"
            )
        lines.append(f"{hashlib.sha256(generated.read_bytes()).hexdigest()}  {name}\n")
    target.write_text("".join(lines), encoding="utf-8", newline="\n")


def stamp_release(output, metadata):
    root = Path(output)
    manifest = root / MANIFEST
    paths = manifest_paths(manifest)
    metadata_path = root / METADATA
    if METADATA not in paths:
        raise StripError(f"{MANIFEST} must protect {METADATA}")
    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    lines = []
    for name in paths:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise StripError(f"manifest path is not a regular student file: {name}")
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n")
    manifest.write_text("".join(lines), encoding="utf-8", newline="\n")


def guards(output):
    root = Path(output)
    failures = []
    for path in root.rglob("*"):
        if path.is_symlink():
            failures.append(f"symlink leaked: {path.relative_to(root).as_posix()}")
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if BANNED_NAME.search(relative):
            failures.append(f"banned file name: {relative}")
        data = path.read_bytes()
        if is_text(data):
            for number, line in enumerate(data.decode("utf-8").split("\n"), 1):
                if ANY_MARKER.search(line):
                    failures.append(
                        f"marker leaked: {relative}:{number}: {line.strip()[:80]}"
                    )
    for name in (".github/workflows", ".github/grade", ".github/golden/grading.py", "tools/release",
                 "tools/student-build"):
        if (root / name).exists():
            failures.append(f"{name}/ must not exist in the student tree")
    return failures


def build(source, output, metadata):
    source = Path(source).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise StripError("student output already exists; choose a fresh path")
    distribution = runtime()
    try:
        distribution.validate(source)
    except ValueError as exc:
        raise StripError(f"invalid shared runtime contract: {exc}") from exc
    output.mkdir(parents=True)
    patterns = load_ignore(source)
    for relative in tracked_files(source):
        if ignored(relative, patterns):
            continue
        source_path = source / relative
        target_path = output / relative
        if source_path.is_symlink() or not source_path.is_file():
            raise StripError(f"unsupported tracked symlink or submodule: {relative}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        data = source_path.read_bytes()
        if is_text(data):
            target_path.write_text(
                strip_text(data.decode("utf-8"), relative),
                encoding="utf-8", newline="",
            )
        else:
            shutil.copyfile(source_path, target_path)
        shutil.copymode(source_path, target_path)
    try:
        distribution.inject_student(source, output)
    except ValueError as exc:
        raise StripError(f"cannot distribute shared runtime: {exc}") from exc
    regenerate_manifest(output, source)
    stamp_release(output, metadata)
    failures = guards(output)
    if failures:
        raise StripError("unsafe generated student tree: " + "; ".join(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
        build(args.source, args.output, metadata)
    except (OSError, UnicodeError, json.JSONDecodeError, StripError) as exc:
        print(f"strip: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
