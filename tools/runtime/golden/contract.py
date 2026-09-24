"""Data-only contract shared by the publisher, pretest, and canonical grader."""

import json
from pathlib import Path
import re


SCHEMA = 1
COURSE_HOST_IMAGE = (
    "ghcr.io/nycu-sdnfv/lab-base@sha256:"
    "c9b6ee4a5271038225a7ca41f541fd005e3766abf4bd70f9f924a61e24984a19"
)
ENVIRONMENTS = ("toolchain", "controller", "measurement", "vrouter")
PROFILE_FIELDS = {"schema", "name", "lab", "assignment", "checks", "environment"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def read_json(path, limit=16384):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular JSON file: {path}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"JSON file exceeds {limit} bytes: {path}")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def validate_profile(profile):
    if (not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS
            or type(profile["schema"]) is not int or profile["schema"] != SCHEMA
            or not isinstance(profile["name"], str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,47}", profile["name"])
            or type(profile["lab"]) is not int or not 0 <= profile["lab"] <= 99
            or not isinstance(profile["assignment"], str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", profile["assignment"])
            or type(profile["checks"]) is not int or not 1 <= profile["checks"] <= 100
            or profile["environment"] not in ENVIRONMENTS):
        raise ValueError("invalid Golden environment profile")
    return dict(profile)


def load_profile(path):
    return validate_profile(read_json(path))


def dockerfile_image(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular Dockerfile: {path}")
    text = path.read_text(encoding="utf-8")
    stages = re.findall(r"(?im)^\s*(FROM\b[^\r\n]*)$", text)
    if len(stages) != 1 or stages[0].split()[1:] != [COURSE_HOST_IMAGE]:
        raise ValueError(
            f"Dockerfile must contain exactly one plain FROM {COURSE_HOST_IMAGE}; "
            f"found {stages!r}")
    return COURSE_HOST_IMAGE
