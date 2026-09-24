#!/usr/bin/env python3
"""Synchronize and validate the runtime supplied by a pinned Golden toolkit."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
PROFILE = ".github/golden/profile.json"
VERSION = ".github/golden/runtime.json"
ADAPTER = ".github/grade/autograder.py"
MANIFEST = ".github/policy/manifest.sha256"
LIBRARIES = (
    "__init__.py", "contract.py", "environment.py", "grading.py",
    "pretest.py", "probe.py", "README.md",
)
PUBLIC_LIBRARIES = tuple(name for name in LIBRARIES if name != "grading.py")
EXPECTED_ENVIRONMENTS = {
    "Toolchain": "toolchain", "Controller": "controller",
    "Measure": "measurement", "VRouter": "vrouter",
}
SHA = re.compile(r"[0-9a-f]{40}")


class RuntimeContractError(ValueError):
    pass


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


CONTRACT = module("golden_distribution_contract", ROOT / "golden/contract.py")


def regular(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if (not path.is_file()
            or any(part.is_symlink() for part in (path, *path.parents)
                   if part == root or root in part.parents)):
        raise RuntimeContractError(f"runtime input must be a regular file: {relative}")
    return path


def toolkit_ref():
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    value = result.stdout.strip()
    if not SHA.fullmatch(value):
        raise RuntimeContractError("toolkit checkout must have a full commit SHA")
    return value


def require_clean_toolkit():
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(ROOT.parent.parent),
         "status", "--porcelain", "--untracked-files=all", "--",
         "tools/runtime", "tools/release"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    if result.stdout.strip():
        raise RuntimeContractError(
            "commit and validate the toolkit before stamping a consumer runtime pin")


def public_paths():
    return tuple(f".github/golden/{name}" for name in PUBLIC_LIBRARIES) + (PROFILE, VERSION)


def source_assets():
    return {
        **{f".github/golden/{name}": ROOT / "golden" / name for name in LIBRARIES},
        ADAPTER: ROOT / "autograder.py",
    }


def expected_files(ref=None):
    ref = toolkit_ref() if ref is None else ref
    if not isinstance(ref, str) or not SHA.fullmatch(ref):
        raise RuntimeContractError("runtime toolkit_ref must be a full lowercase commit SHA")
    files = {}
    for name, path in source_assets().items():
        if not path.is_file() or path.is_symlink():
            raise RuntimeContractError(f"trusted toolkit runtime asset is missing: {path}")
        files[name] = path.read_bytes()
    provenance = {
        "schema": 1,
        "toolkit_ref": ref,
        "files": {name: hashlib.sha256(raw).hexdigest()
                  for name, raw in sorted(files.items())},
    }
    files[VERSION] = (json.dumps(provenance, indent=2) + "\n").encode("utf-8")
    return files


def manifest_paths(source):
    strip = module("golden_distribution_strip", ROOT.parent / "release/strip.py")
    try:
        return strip.manifest_paths(regular(source, MANIFEST))
    except strip.StripError as exc:
        raise RuntimeContractError(str(exc)) from exc


def validate_caller(source, ref):
    caller = regular(source, ".github/workflows/release.yml").read_text(encoding="utf-8")
    uses = re.findall(
        r"(?m)^\s*uses:\s*NYCU-SDNFV/Golden-Base/"
        r"\.github/workflows/shared-release\.yml@([0-9a-f]{40})\s*$", caller)
    refs = re.findall(r"(?m)^\s*toolkit_ref:\s*([0-9a-f]{40})\s*$", caller)
    if uses != [ref] or refs != [ref]:
        raise RuntimeContractError(
            "release caller uses/toolkit_ref and runtime must pin the same Golden-Base commit")


def validate_profile_source(source):
    profile = CONTRACT.load_profile(regular(source, PROFILE))
    release = CONTRACT.read_json(regular(source, ".release.json"))
    if not isinstance(release, dict) or release.get("name") != profile["name"]:
        raise RuntimeContractError("runtime profile name must match .release.json")
    routes = release.get("channels")
    if (not isinstance(routes, dict)
            or not any(isinstance(route, dict) and route.get("slug") == profile["assignment"]
                       for route in routes.values())):
        raise RuntimeContractError("runtime assignment must identify a configured release route")
    expected = EXPECTED_ENVIRONMENTS.get(profile["name"])
    if expected is not None and profile["environment"] != expected:
        raise RuntimeContractError(f"{profile['name']} requires the {expected} environment profile")
    try:
        CONTRACT.dockerfile_image(regular(source, "Dockerfile"))
    except ValueError as exc:
        raise RuntimeContractError(
            "Dockerfile must use the runtime's exact immutable course image: " + str(exc)) from exc
    makefile = regular(source, "Makefile").read_text(encoding="utf-8")
    if not re.search(r"(?m)^pretest:\s*\n\t@?python3 -B \.github/golden/pretest\.py\s*$", makefile):
        raise RuntimeContractError("Makefile must expose the shared non-scoring pretest target")
    return profile


def validate(source, ref=None):
    source = Path(source).resolve()
    profile = validate_profile_source(source)
    ref = toolkit_ref() if ref is None else ref
    validate_caller(source, ref)
    files = expected_files(ref)
    for name, raw in files.items():
        if regular(source, name).read_bytes() != raw:
            raise RuntimeContractError(
                f"outdated or edited shared runtime: {name}; run the pinned toolkit's runtime sync")
    protected = set(manifest_paths(source))
    required = set(public_paths()) | {"Dockerfile", "Makefile", ".lab-release.json",
                                     ".github/release/upgrade.py"}
    if not required <= protected:
        raise RuntimeContractError(
            "manifest must protect the complete runtime contract: " + ", ".join(sorted(required - protected)))
    strip = module("golden_distribution_tracking", ROOT.parent / "release/strip.py")
    try:
        tracked = set(strip.tracked_files(source))
    except strip.StripError as exc:
        raise RuntimeContractError(str(exc)) from exc
    if not set(files).union({PROFILE}) <= tracked:
        raise RuntimeContractError("shared runtime assets must be tracked in the source Git index")
    managed = {name for name in tracked if name.startswith(".github/golden/")}
    if managed != {name for name in files if name.startswith(".github/golden/")} | {PROFILE}:
        raise RuntimeContractError("source contains unexpected files in the managed runtime directory")
    return profile


def write_file(source, relative, raw):
    source = Path(source).resolve()
    path = source / relative
    if any(part.is_symlink() for part in (path, *path.parents)
           if part == source or source in part.parents):
        raise RuntimeContractError(f"refusing to write through a symbolic link: {relative}")
    if path.exists() and not path.is_file():
        raise RuntimeContractError(f"runtime destination is not a regular file: {relative}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o644)


def synchronize(source, ref=None):
    source = Path(source).resolve()
    validate_profile_source(source)
    files = expected_files(ref)
    paths = list(dict.fromkeys([*manifest_paths(source), *public_paths()]))
    for name, raw in files.items():
        write_file(source, name, raw)
    lines = []
    for name in paths:
        path = regular(source, name)
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n")
    write_file(source, MANIFEST, "".join(lines).encode("utf-8"))
    return sorted(files)


def inject_student(source, output):
    validate(source)
    files = expected_files()
    for name in public_paths():
        raw = regular(source, name).read_bytes() if name == PROFILE else files[name]
        write_file(output, name, raw)


def inject_bundle(source, student, output):
    profile = validate(source)
    files = expected_files()
    for name in LIBRARIES:
        write_file(output, f"golden/{name}", files[f".github/golden/{name}"])
    for name in (PROFILE, VERSION):
        raw = regular(student, name).read_bytes()
        if raw != regular(source, name).read_bytes():
            raise RuntimeContractError(f"student runtime contract changed during sanitization: {name}")
        write_file(output, f"golden/{Path(name).name}", raw)
    write_file(output, "autograder.py", files[ADAPTER])
    return profile


def canonical_modes():
    return {
        **{f"golden/{name}": "100644" for name in LIBRARIES},
        "golden/profile.json": "100644", "golden/runtime.json": "100644",
        "autograder.py": "100644",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--write", action="store_true",
                        help="replace managed runtime assets and regenerate the protected manifest")
    args = parser.parse_args(argv)
    try:
        if args.write:
            require_clean_toolkit()
            ref = toolkit_ref()
            validate_caller(args.source, ref)
            for name in synchronize(args.source, ref):
                print(f"updated {name}")
            print("Stage and review these generated files and the manifest, then rerun without --write.")
        else:
            profile = validate(args.source)
            print(f"PASS: {profile['name']} uses Golden runtime {toolkit_ref()} ({profile['environment']})")
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"runtime sync: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
