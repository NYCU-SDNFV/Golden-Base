#!/usr/bin/env python3
"""Publish a sanitized Golden source tag through the shared release service."""

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib import error, parse, request


OWNER = "NYCU-SDNFV"
CONFIG_REPOSITORY = "NYCU-SDNFV/classroom50"
CONFIG_BRANCH = "main"
PAGES_URL = "https://nycu-sdnfv.github.io/classroom50"
DEFAULT_CHANNEL = "newbie"
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
SHA = re.compile(r"[0-9a-f]{40}")
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
CHANNEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
METADATA = ".lab-release.json"
MANIFEST = ".github/policy/manifest.sha256"
LOCAL_PROVENANCE = ".lab-local-provenance.json"
IDENTITY = [
    "-c", "user.name=Golden release[bot]",
    "-c", "user.email=golden-release@users.noreply.github.com",
]
COAUTHOR = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
MANUAL_POLICY = (
    "Student repositories were not modified. Students update through the existing "
    "`make check-update` and `make update` flow."
)


class ReleaseError(Exception):
    pass


class APIError(ReleaseError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def helper(name):
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"golden_release_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, document):
    Path(path).write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def parse_source_repository(value):
    if not isinstance(value, str) or not re.fullmatch(
            rf"{OWNER}/Golden-[A-Za-z][A-Za-z0-9-]*", value):
        raise ReleaseError(
            "Source repository must be NYCU-SDNFV/Golden-<Name>"
        )
    return value.split("/", 1)[1][len("Golden-"):]


def load_config(source, source_repository):
    expected_name = parse_source_repository(source_repository)
    config = load_json(Path(source) / ".release.json")
    fixed = {
        "schema": 1,
        "owner": OWNER,
        "config_repository": CONFIG_REPOSITORY,
        "config_branch": CONFIG_BRANCH,
        "pages_url": PAGES_URL,
        "default_channel": DEFAULT_CHANNEL,
    }
    if not isinstance(config, dict) or any(config.get(key) != value for key, value in fixed.items()):
        raise ReleaseError("Unsupported .release.json fixed routing or schema")
    if config.get("name") != expected_name or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9-]*", expected_name):
        raise ReleaseError("Config name does not match the private source repository")
    if not all(isinstance(config.get(key), str) and config[key].strip()
               for key in ("title", "description")):
        raise ReleaseError("Release title and description are required")
    mode = config.get("student_updates", "manual")
    if mode not in ("manual", "pull_request"):
        raise ReleaseError("student_updates must be manual or pull_request")
    config["student_updates"] = mode
    channels = config.get("channels")
    if (not isinstance(channels, dict) or DEFAULT_CHANNEL not in channels
            or not channels):
        raise ReleaseError("Config must define the newbie channel")
    pairs, templates = set(), set()
    for channel, route in channels.items():
        if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
            raise ReleaseError(f"Invalid release channel: {channel!r}")
        if not isinstance(route, dict) or set(route) != {
                "classroom", "slug", "template"}:
            raise ReleaseError(f"Invalid route for channel {channel}")
        classroom, slug, template = (
            route["classroom"], route["slug"], route["template"])
        if (not isinstance(classroom, str) or not SAFE_COMPONENT.fullmatch(classroom)
                or not isinstance(slug, str) or not SAFE_COMPONENT.fullmatch(slug)):
            raise ReleaseError(f"Invalid classroom or slug for channel {channel}")
        if template != f"Golden-{channel}-{expected_name}":
            raise ReleaseError(
                f"Channel {channel} template must be Golden-{channel}-{expected_name}"
            )
        if (classroom, slug) in pairs or template in templates:
            raise ReleaseError("Channel classroom/slug and template mappings must be unique")
        pairs.add((classroom, slug))
        templates.add(template)
    return config


def parse_tag(config, tag):
    if not isinstance(tag, str) or not tag or len(tag) > 128:
        raise ReleaseError("Release tag must be a nonempty string of at most 128 characters")
    channels = config["channels"]
    if tag in channels:
        return tag, "0.1.0"
    if re.fullmatch(r"v" + VERSION, tag):
        parts = re.fullmatch(r"v" + VERSION, tag)
        return config["default_channel"], ".".join(parts.group(i) for i in (1, 2, 3))
    for channel in sorted(channels, key=len, reverse=True):
        match = re.fullmatch(re.escape(channel) + r"-v" + VERSION, tag)
        if match:
            return channel, ".".join(match.group(i) for i in (1, 2, 3))
    raise ReleaseError(
        "Invalid tag; use <channel>-vX.Y.Z, vX.Y.Z for the default channel, "
        "or a configured bare channel for its initial 0.1.0 release"
    )


def version_key(version):
    if not isinstance(version, str) or not re.fullmatch(VERSION, version):
        raise ReleaseError(f"Invalid release version: {version!r}")
    return tuple(map(int, version.split(".")))


def release_metadata(config, tag, source_sha):
    channel, version = parse_tag(config, tag)
    if not isinstance(source_sha, str) or not SHA.fullmatch(source_sha):
        raise ReleaseError("Source SHA must be a full 40-character commit hash")
    return {
        "schema": 1,
        "channel": channel,
        "version": version,
        "tag": tag,
        "template": f"{OWNER}/{config['channels'][channel]['template']}",
        "source_sha": source_sha,
        "minimum_version": version,
    }


def validate_metadata(config, metadata):
    expected_keys = {
        "schema", "channel", "version", "tag", "template", "source_sha",
        "minimum_version",
    }
    if not isinstance(metadata, dict) or set(metadata) != expected_keys:
        raise ReleaseError("Missing or malformed release metadata")
    channel, version = parse_tag(config, metadata.get("tag"))
    expected_template = f"{OWNER}/{config['channels'][channel]['template']}"
    if (type(metadata["schema"]) is not int or metadata["schema"] != 1
            or metadata["channel"] != channel
            or metadata["version"] != version
            or metadata["minimum_version"] != version
            or metadata["template"] != expected_template
            or not isinstance(metadata["source_sha"], str)
            or not SHA.fullmatch(metadata["source_sha"])):
        raise ReleaseError("Inconsistent release metadata")
    return metadata


def git_environment(token):
    environment = os.environ.copy()
    header = "AUTHORIZATION: basic " + base64.b64encode(
        f"x-access-token:{token}".encode()).decode()
    environment.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_1": header,
        "GIT_CONFIG_KEY_2": "credential.helper",
        "GIT_CONFIG_VALUE_2": "",
    })
    for key in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE", "GIT_TRACE_PACKET"):
        environment.pop(key, None)
    return environment


def git(cwd, *args, env=None, data=None, check=True):
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd, env=env, input=data, timeout=300,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(
            f"git {args[0]} timed out; inspect remote state and rerun the same tag"
        ) from exc
    if check and result.returncode:
        raise ReleaseError(
            f"git {args[0]} failed (exit {result.returncode}); check repository "
            "access or concurrent changes. No force push was attempted."
        )
    return result


def text_git(cwd, *args, **kwargs):
    return git(cwd, *args, **kwargs).stdout.decode("utf-8").strip()


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReleaseError("Unexpected HTTP redirect from GitHub")


class GitHub:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, body=None, missing_ok=False):
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ReleaseError("GitHub API path must be relative")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_object = request.Request(
            "https://api.github.com" + path,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with request.build_opener(NoRedirect).open(
                    request_object, timeout=45) as response:
                content = response.read()
        except error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise APIError(
                exc.code,
                f"GitHub {method} {path.split('?')[0]} returned HTTP {exc.code}; "
                "check release credential access and permissions",
            ) from exc
        except (error.URLError, TimeoutError) as exc:
            raise ReleaseError("GitHub API connection failed; rerun the same tag") from exc
        try:
            return json.loads(content) if content else None
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("GitHub API returned invalid JSON") from exc


def repository_url(full_name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
        raise ReleaseError("Invalid repository name")
    return f"https://github.com/{full_name}.git"


def require_development_source(source):
    marker = Path(source) / LOCAL_PROVENANCE
    if marker.exists() or marker.is_symlink():
        raise ReleaseError(
            "Refusing generated local-provenance source; release only the original "
            "private development tree"
        )


def verify_source_repository(api, source_repository):
    repository = api.call("GET", f"/repos/{source_repository}")
    if (not isinstance(repository, dict)
            or repository.get("full_name") != source_repository
            or repository.get("private") is not True
            or repository.get("is_template") is not False
            or repository.get("archived") is True
            or repository.get("disabled") is True
            or repository.get("default_branch") != "master"):
        raise ReleaseError(
            "Source must be the expected active private non-template Golden repository "
            "with default branch master"
        )


def verify_source(source, tag, config, env):
    require_development_source(source)
    parse_tag(config, tag)
    ref = f"refs/tags/{tag}"
    source_sha = text_git(source, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if source_sha != text_git(source, "rev-parse", "HEAD"):
        raise ReleaseError("Source checkout must be the exact existing release tag")
    if text_git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("Tracked source files are dirty")
    remote = text_git(
        source, "ls-remote", "--tags", "origin", ref, ref + "^{}", env=env)
    refs = dict(line.split()[::-1] for line in remote.splitlines() if line)
    if refs.get(ref + "^{}", refs.get(ref)) != source_sha:
        raise ReleaseError("Source tag is missing or moved on origin")
    git(source, "fetch", "--quiet", "--no-tags", "origin",
        "refs/heads/master:refs/remotes/origin/master", env=env)
    if git(source, "merge-base", "--is-ancestor", source_sha,
           "refs/remotes/origin/master", check=False).returncode:
        raise ReleaseError("Tagged source is not an ancestor of origin/master")
    return source_sha


def classroom_preflight(config_dir, config, channel):
    route = config["channels"][channel]
    root = Path(config_dir) / route["classroom"]
    required = (root / "classroom.json", root / "assignments.json")
    if not all(path.is_file() and not path.is_symlink() for path in required):
        raise ReleaseError(
            f"Classroom {route['classroom']} is not provisioned. A teacher must first "
            "commit classroom.json and assignments.json to classroom50/main. "
            "No template or assignment was published."
        )
    classroom, assignments = map(load_json, required)
    if (not isinstance(classroom, dict)
            or classroom.get("schema") != "classroom50/classroom/v1"
            or classroom.get("short_name") != route["classroom"]
            or classroom.get("org") != OWNER
            or classroom.get("active") is False
            or not isinstance(classroom.get("team"), dict)
            or type(classroom["team"].get("id")) is not int
            or classroom["team"]["id"] <= 0):
        raise ReleaseError("Classroom is invalid, archived, or unprovisioned")
    secret = classroom.get("secret", "")
    if not isinstance(secret, str) or (
            secret and not re.fullmatch(r"[a-z0-9]{4,64}", secret)):
        raise ReleaseError("Classroom has an invalid Pages secret")
    if (not isinstance(assignments, dict)
            or assignments.get("schema") != "classroom50/assignments/v1"
            or not isinstance(assignments.get("assignments"), list)
            or any(not isinstance(row, dict)
                   or not isinstance(row.get("slug"), str)
                   for row in assignments["assignments"])):
        raise ReleaseError("Malformed assignments.json")
    slugs = [row["slug"] for row in assignments["assignments"]]
    if len(slugs) != len(set(slugs)):
        raise ReleaseError("Duplicate assignment slugs")
    return classroom, assignments


def upsert_assignment(assignments, config, metadata):
    result = copy.deepcopy(assignments)
    route = config["channels"][metadata["channel"]]
    matches = [row for row in result["assignments"] if row["slug"] == route["slug"]]
    if len(matches) > 1:
        raise ReleaseError("Duplicate target assignment")
    if matches:
        assignment = matches[0]
        incompatible = []
        existing_template = assignment.get("template")
        if existing_template is not None and (
                not isinstance(existing_template, dict)
                or existing_template.get("owner") != OWNER
                or existing_template.get("repo") != route["template"]):
            incompatible.append("template belongs to another source")
        if assignment.get("mode") not in (None, "", "individual"):
            incompatible.append("mode")
        if ("max_group_size" in assignment
                and (type(assignment["max_group_size"]) is not int
                     or assignment["max_group_size"] != 0)):
            incompatible.append("max_group_size")
        if assignment.get("team_formation") not in (None, ""):
            incompatible.append("team_formation")
        for flag in ("empty_repo", "no_autograder", "init_shim"):
            if flag in assignment and assignment[flag] is not False:
                incompatible.append(flag)
        if incompatible:
            raise ReleaseError(
                f"Assignment {route['slug']} has incompatible settings: "
                + ", ".join(incompatible)
                + ". Explicitly migrate it before publishing; no student repository "
                  "was converted."
            )
    else:
        assignment = {
            "slug": route["slug"],
            "name": config["title"],
            "description": config["description"],
            "copy_about": True,
            "copy_topics": True,
        }
        result["assignments"].append(assignment)
    assignment.update({
        "template": {
            "owner": OWNER, "repo": route["template"], "branch": "main",
        },
        "mode": "individual",
        "repo_visibility": "private",
        "feedback_pr": True,
        "autograder": "default",
        "pass_threshold": 100,
    })
    if assignment.get("available_from") in (None, ""):
        assignment["available_from"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    assignment.pop("tests", None)
    assignment.pop("test_defaults", None)
    return result, assignment


def tree_hashes(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ReleaseError("Publication trees must not contain symlinks")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return result


def source_file_modes(source):
    modes = {}
    raw = git(source, "ls-tree", "-r", "-z", "HEAD").stdout.decode("utf-8")
    for entry in raw.split("\0"):
        if entry:
            details, name = entry.split("\t", 1)
            modes[name] = details.split()[0]
    return modes


def set_index_modes(repo, modes):
    if any(mode not in ("100644", "100755") for mode in modes.values()):
        raise ReleaseError("Tagged publication contains an unsupported file mode")
    for mode, flag in (("100644", "-x"), ("100755", "+x")):
        names = [name for name, expected in modes.items() if expected == mode]
        if names:
            git(repo, "update-index", f"--chmod={flag}", "--", *names)


def bundle_file_modes(bundle, source_modes):
    managed = helper("runtime").helper().canonical_modes()
    aliases = {
        "autograder.py": ".github/grade/autograder.py",
        "tests.json": ".github/grade/tests.json",
        "release.json": METADATA,
        "policy/lib.sh": ".github/tests/lib.sh",
    }
    modes = {}
    for name in tree_hashes(bundle):
        if name in managed:
            modes[name] = managed[name]
            continue
        original = aliases.get(
            name, ".github/" + name if name.startswith("policy/") else name)
        if original not in source_modes:
            raise ReleaseError(f"Bundle input is not in the tagged source: {name}")
        modes[name] = source_modes[original]
    return modes


def build_student(source, output, metadata):
    require_development_source(source)
    strip = helper("strip")
    try:
        strip.build(source, output, metadata)
    except (OSError, strip.StripError) as exc:
        raise ReleaseError(str(exc)) from exc


def build_bundle(source, student, output):
    bundle = helper("bundle")
    try:
        bundle.build(source, student, output)
    except (OSError, bundle.BundleError) as exc:
        raise ReleaseError(str(exc)) from exc


def read_git_metadata(repo, ref, config):
    blob = text_git(repo, "show", f"{ref}:{METADATA}")
    try:
        return validate_metadata(config, json.loads(blob))
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"Invalid release metadata at {ref}") from exc


def inventory_template(repo, metadata, config):
    tags = text_git(repo, "tag", "--list").splitlines()
    if not tags:
        raise ReleaseError("Existing template has no managed release tags")
    releases, versions = [], set()
    for tag in tags:
        prior = read_git_metadata(repo, f"refs/tags/{tag}", config)
        if prior["tag"] != tag or prior["channel"] != metadata["channel"]:
            raise ReleaseError("Template contains a mismatched release tag")
        key = version_key(prior["version"])
        if key in versions:
            raise ReleaseError("Template contains colliding aliases for one version")
        versions.add(key)
        releases.append(prior)
    latest = max(releases, key=lambda item: version_key(item["version"]))
    if text_git(repo, "rev-parse", "HEAD") != text_git(
            repo, "rev-parse", f"refs/tags/{latest['tag']}^{{commit}}"):
        raise ReleaseError("Template main is not the latest immutable release")
    for prior in releases:
        if git(repo, "merge-base", "--is-ancestor",
               f"refs/tags/{prior['tag']}", "HEAD", check=False).returncode:
            raise ReleaseError("Template release tags do not form an ancestor history")
    new_key, latest_key = version_key(metadata["version"]), version_key(latest["version"])
    if new_key < latest_key:
        raise ReleaseError(f"Downgrade refused: current template is {latest['tag']}")
    if new_key == latest_key and latest != metadata:
        raise ReleaseError("Immutable release collision; choose a new version")
    return latest, new_key == latest_key


def copy_release_tree(student, repo, source_modes=None):
    git(repo, "rm", "-r", "--quiet", "--ignore-unmatch", "--", ".")
    shutil.copytree(student, repo, dirs_exist_ok=True)
    git(repo, "add", "--all")
    expected = tree_hashes(student)
    if source_modes is not None:
        if any(name not in source_modes for name in expected):
            raise ReleaseError("Generated template contains an untagged file")
        set_index_modes(repo, {name: source_modes[name] for name in expected})
    staged = {}
    raw = git(repo, "ls-files", "--stage", "-z").stdout.decode("utf-8")
    for entry in raw.split("\0"):
        if not entry:
            continue
        details, name = entry.split("\t", 1)
        mode, _, stage = details.split()
        if stage != "0" or mode not in ("100644", "100755"):
            raise ReleaseError("Generated template index contains an unsupported entry")
        staged[name] = hashlib.sha256(git(repo, "show", f":{name}").stdout).hexdigest()
        if name in expected:
            executable = (
                source_modes[name] == "100755" if source_modes is not None
                else bool((Path(student) / name).stat().st_mode & 0o111)
            )
            if executable != (mode == "100755"):
                raise ReleaseError(f"Generated template changed executable mode: {name}")
    if staged != expected:
        raise ReleaseError("Generated template index differs from sanitized tree")


def prepare_template(api, config, metadata, student, work, env, source_modes=None):
    full_name = metadata["template"]
    existing = api.call("GET", f"/repos/{full_name}", missing_ok=True)
    repo = Path(work) / "template"
    latest, rerun = None, False
    if existing is not None:
        if (existing.get("full_name") != full_name
                or existing.get("private") is not False
                or existing.get("is_template") is not True
                or existing.get("archived") is True
                or existing.get("disabled") is True):
            raise ReleaseError(
                "Existing target must be the expected active public channel template"
            )
        refs = text_git(work, "ls-remote", repository_url(full_name), env=env)
        if refs:
            if existing.get("default_branch") != "main":
                raise ReleaseError("Existing template default branch must be main")
            git(work, "clone", "--quiet", "--branch", "main",
                repository_url(full_name), str(repo), env=env)
            git(repo, "fetch", "--quiet", "--tags", "origin", env=env)
            latest, rerun = inventory_template(repo, metadata, config)
        else:
            repo.mkdir()
            git(repo, "init", "--quiet", "-b", "main")
            git(repo, "remote", "add", "origin", repository_url(full_name))
    else:
        repo.mkdir()
        git(repo, "init", "--quiet", "-b", "main")
        git(repo, "remote", "add", "origin", repository_url(full_name))
    copy_release_tree(student, repo, source_modes)
    if rerun:
        if git(repo, "diff", "--cached", "--quiet", check=False).returncode:
            raise ReleaseError("Same release/source produced a different tree")
    else:
        git(repo, *IDENTITY, "commit", "--quiet", "-m",
            f"{config['title']} {metadata['tag']}\n\n"
            f"Source: {metadata['source_sha']}\n\n{COAUTHOR}")
        git(repo, "tag", metadata["tag"])
    return repo, existing is None, latest, rerun


def publish_template(api, repo, config, metadata, create, rerun, env):
    if create:
        created = api.call("POST", f"/orgs/{OWNER}/repos", {
            "name": metadata["template"].split("/", 1)[1],
            "private": False,
            "is_template": True,
            "has_issues": False,
            "has_wiki": False,
            "has_projects": False,
            "description": config["description"],
        })
        if (not created or created.get("private") is not False
                or created.get("is_template") is not True):
            raise ReleaseError("GitHub did not create the requested public template")
    if not rerun:
        git(repo, "push", "--quiet", "--atomic", "origin",
            "HEAD:refs/heads/main", f"refs/tags/{metadata['tag']}", env=env)
    remote = text_git(repo, "ls-remote", "origin", "refs/heads/main",
                      f"refs/tags/{metadata['tag']}", env=env)
    refs = dict(line.split()[::-1] for line in remote.splitlines())
    head = text_git(repo, "rev-parse", "HEAD")
    if (refs.get("refs/heads/main") != head
            or refs.get(f"refs/tags/{metadata['tag']}") != head):
        raise ReleaseError("Template refs changed during publication")
    updated = api.call("GET", f"/repos/{metadata['template']}")
    if updated.get("default_branch") != "main":
        api.call("PATCH", f"/repos/{metadata['template']}",
                 {"default_branch": "main"})


def publish_config(config_dir, config, metadata, assignments, bundle, env,
                   source_modes=None):
    route = config["channels"][metadata["channel"]]
    root = Path(config_dir) / route["classroom"]
    target = root / "autograders" / route["slug"]
    if target.is_symlink():
        raise ReleaseError("Assignment bundle path is a symlink")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, target)
    write_json(root / "assignments.json", assignments)
    relative_assignment = f"{route['classroom']}/assignments.json"
    relative_bundle = f"{route['classroom']}/autograders/{route['slug']}"
    git(config_dir, "add", "--", relative_assignment, relative_bundle)
    if source_modes is not None:
        prefix = relative_bundle + "/"
        set_index_modes(config_dir, {
            prefix + name: mode
            for name, mode in bundle_file_modes(bundle, source_modes).items()
        })
    if git(config_dir, "diff", "--cached", "--quiet", check=False).returncode:
        git(config_dir, *IDENTITY, "commit", "--quiet", "-m",
            f"{route['classroom']}/{route['slug']}: publish "
            f"{metadata['tag']}\n\n{COAUTHOR}")
        git(config_dir, "push", "--quiet", "origin",
            "HEAD:refs/heads/main", env=env)
    return text_git(config_dir, "rev-parse", "HEAD")


def public_bytes(url):
    request_object = request.Request(
        url, headers={"Cache-Control": "no-cache", "Accept": "*/*"})
    try:
        with request.build_opener(NoRedirect).open(
                request_object, timeout=30) as response:
            data = response.read(25 * 1024 * 1024 + 1)
    except error.HTTPError as exc:
        raise APIError(
            exc.code, f"Public publication endpoint returned HTTP {exc.code}"
        ) from exc
    except (error.URLError, TimeoutError) as exc:
        raise APIError(0, "Public publication endpoint is unavailable") from exc
    if len(data) > 25 * 1024 * 1024:
        raise ReleaseError("Published payload exceeds the size limit")
    return data


def archive_entries(payload, slug):
    result, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                prefix = slug + "/"
                if (not member.isfile() or not member.name.startswith(prefix)
                        or ".." in member.name.split("/")
                        or member.size > 10 * 1024 * 1024):
                    raise ReleaseError("Unsafe published autograder archive")
                total += member.size
                if total > 50 * 1024 * 1024:
                    raise ReleaseError("Published archive exceeds expanded size limit")
                name = member.name[len(prefix):]
                if name in result:
                    raise ReleaseError("Duplicate entry in published archive")
                mode = member.mode & 0o777
                if mode not in (0o644, 0o755):
                    raise ReleaseError("Published archive has an unsupported file mode")
                result[name] = {
                    "sha256": hashlib.sha256(
                        archive.extractfile(member).read()).hexdigest(),
                    "mode": f"100{mode:o}",
                }
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ReleaseError("Published autograder archive is invalid") from exc
    return result


def archive_hashes(payload, slug):
    return {
        name: entry["sha256"]
        for name, entry in archive_entries(payload, slug).items()
    }


def wait_for_pages(config, classroom, metadata, assignment, bundle, timeout=600,
                   fetch=public_bytes, sleep=time.sleep, clock=time.monotonic,
                   expected_modes=None):
    route = config["channels"][metadata["channel"]]
    prefix = PAGES_URL + "/" + route["classroom"]
    if classroom.get("secret"):
        prefix += "/" + classroom["secret"]
    if expected_modes is None:
        expected_modes = {
            path.relative_to(bundle).as_posix():
            ("100755" if path.stat().st_mode & 0o111 else "100644")
            for path in Path(bundle).rglob("*") if path.is_file()
        }
    expected = {
        name: {
            "sha256": digest,
            "mode": expected_modes.get(name),
        }
        for name, digest in tree_hashes(bundle).items()
    }
    deadline, attempt, reason = clock() + timeout, 0, "publication has not appeared"
    while True:
        attempt += 1
        query = "?" + parse.urlencode(
            {"golden_release": metadata["tag"], "attempt": attempt})
        try:
            published = json.loads(fetch(prefix + "/assignments.json" + query))
            matches = [
                row for row in published.get("assignments", [])
                if row.get("slug") == route["slug"]
            ]
            if matches != [assignment]:
                reason = "Pages has a different assignment/template configuration"
            elif archive_entries(
                    fetch(prefix + "/autograders/" + route["slug"] + ".tar.gz" + query),
                    route["slug"]) != expected:
                reason = "Pages has a different autograder bundle"
            elif json.loads(fetch(
                    f"https://raw.githubusercontent.com/{metadata['template']}"
                    f"/main/{METADATA}{query}")) != metadata:
                reason = "public template metadata has not propagated"
            else:
                return
        except APIError as exc:
            if exc.status not in (0, 404, 429, 500, 502, 503, 504):
                raise
            reason = str(exc)
        except (UnicodeError, json.JSONDecodeError) as exc:
            reason = f"publication returned invalid JSON ({type(exc).__name__})"
        if clock() >= deadline:
            raise ReleaseError(
                f"Pages publication was not verified: {reason}. Inspect classroom50 "
                "Pages, then rerun the same tag."
            )
        sleep(min(15, max(0, deadline - clock())))


def publish(source, tag, token, report, source_repository, pages_timeout=600):
    source = Path(source).resolve()
    config = load_config(source, source_repository)
    if config["student_updates"] == "pull_request":
        raise ReleaseError(
            "student_updates=pull_request is not enabled in the shared publisher: "
            "the deployed release credential is Contents/Administration-only and must "
            "not call Pull requests or Actions APIs. Select manual explicitly; this "
            "check runs before mutation and no repository was changed."
        )
    channel, _ = parse_tag(config, tag)
    environment, api = git_environment(token), GitHub(token)
    verify_source_repository(api, source_repository)
    source_sha = verify_source(source, tag, config, environment)
    modes = source_file_modes(source)
    distribution = helper("runtime").helper()
    modes.update({name: "100644" for name in distribution.public_paths()})
    metadata = release_metadata(config, tag, source_sha)
    report.update({
        "release": metadata,
        "runtime_toolkit_ref": distribution.toolkit_ref(),
        "template_published": False,
        "pages_verified": False,
        "student_updates": {"mode": "manual", "policy": MANUAL_POLICY},
    })
    work_parent = os.environ.get("RUNNER_TEMP")
    with tempfile.TemporaryDirectory(
            prefix="golden-release-", dir=work_parent) as temporary:
        work = Path(temporary)
        config_dir = work / "classroom50"
        git(work, "clone", "--quiet", "--branch", CONFIG_BRANCH,
            repository_url(CONFIG_REPOSITORY), str(config_dir), env=environment)
        classroom, assignments = classroom_preflight(config_dir, config, channel)
        assignments, assignment = upsert_assignment(assignments, config, metadata)
        student, bundle = work / "student-tree", work / "bundle"
        build_student(source, student, metadata)
        build_bundle(source, student, bundle)
        bundle_modes = bundle_file_modes(bundle, modes)
        template, create, previous, rerun = prepare_template(
            api, config, metadata, student, work, environment, modes)
        report["previous_tag"] = previous["tag"] if previous else None
        publish_template(
            api, template, config, metadata, create, rerun, environment)
        report["template_published"] = True
        report["config_commit"] = publish_config(
            config_dir, config, metadata, assignments, bundle, environment, modes)
        wait_for_pages(
            config, classroom, metadata, assignment, bundle, timeout=pages_timeout,
            expected_modes=bundle_modes)
        report["pages_verified"] = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source-repository")
    parser.add_argument("--pages-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    report = {"status": "failed"}
    try:
        token = os.environ.get("RELEASE_TOKEN", "")
        source_repository = (
            args.source_repository or os.environ.get("SOURCE_REPOSITORY", ""))
        if not token:
            raise ReleaseError(
                "RELEASE_TOKEN is required with scoped Contents and Administration write"
            )
        if not source_repository:
            raise ReleaseError(
                "SOURCE_REPOSITORY or --source-repository is required"
            )
        if not 1 <= args.pages_timeout <= 1800:
            raise ReleaseError("--pages-timeout must be between 1 and 1800 seconds")
        publish(
            args.source, args.tag, token, report, source_repository,
            args.pages_timeout,
        )
        report["status"] = "published"
    except (ReleaseError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        report["error"] = str(exc)
        print(f"release: {exc}", file=sys.stderr)
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.report, report)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "### Golden release\n\n```json\n"
                    + json.dumps(report, indent=2) + "\n```\n"
                )
    return 0 if report["status"] == "published" else 1


if __name__ == "__main__":
    sys.exit(main())
