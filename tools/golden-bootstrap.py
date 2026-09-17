#!/usr/bin/env python3
"""Create/enroll a private Golden source in the shared publication service."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys


OWNER = "NYCU-SDNFV"
TEAM = "instructors"
SECRETS = {"token": "GOLDEN_RELEASE_TOKEN", "app": "GOLDEN_RELEASE_APP_KEY"}


class BootstrapError(ValueError):
    pass


def validate_name(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,47}", name) or name in ("Base",):
        raise BootstrapError("name must be an instructor Lab name, not Base or a channel snapshot")


def validate_config(config, name):
    if (not isinstance(config, dict) or config.get("schema") != 1
            or config.get("name") != name or config.get("owner") != OWNER
            or config.get("default_channel") != "newbie"
            or config.get("config_repository") != f"{OWNER}/classroom50"
            or config.get("config_branch") != "main"
            or config.get("pages_url") != "https://nycu-sdnfv.github.io/classroom50"):
        raise BootstrapError("release configuration does not identify the expected instructor source")
    if not all(isinstance(config.get(field), str) and config[field].strip()
               for field in ("title", "description")):
        raise BootstrapError("release title and description are required")
    channels = config.get("channels")
    if not isinstance(channels, dict) or "newbie" not in channels:
        raise BootstrapError("channels must include newbie")
    pairs = set()
    for channel, route in channels.items():
        if channel != "newbie" and not re.fullmatch(r"[1-9][0-9]{1,3}-[12]", channel):
            raise BootstrapError("unsupported release channel")
        classroom = "winlab-newbies" if channel == "newbie" else f"sdnfv-{channel}"
        if (not isinstance(route, dict) or route.get("classroom") != classroom
                or route.get("template") != f"Golden-{channel}-{name}"
                or not isinstance(route.get("slug"), str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", route["slug"])):
            raise BootstrapError("invalid classroom, slug or student template route")
        pair = (classroom, route["slug"])
        if pair in pairs:
            raise BootstrapError("duplicate assignment route")
        pairs.add(pair)
    if config.get("student_updates") not in ("manual", "pull_request"):
        raise BootstrapError("student_updates must explicitly be manual or pull_request")
    return config


def new_configuration(name, slug, title, description, semester):
    config = {
        "schema": 1, "name": name, "title": title, "description": description,
        "owner": OWNER, "config_repository": f"{OWNER}/classroom50",
        "config_branch": "main", "pages_url": "https://nycu-sdnfv.github.io/classroom50",
        "default_channel": "newbie", "student_updates": "manual",
        "channels": {
            "newbie": {"classroom": "winlab-newbies", "slug": slug,
                       "template": f"Golden-newbie-{name}"},
            semester: {"classroom": f"sdnfv-{semester}", "slug": slug,
                       "template": f"Golden-{semester}-{name}"},
        },
    }
    return validate_config(config, name)


def render_workflow(toolkit_ref, poc_workflow, auth_mode):
    if not re.fullmatch(r"[0-9a-f]{40}", toolkit_ref):
        raise BootstrapError("toolkit_ref must pin the tested Golden-Base commit")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml", poc_workflow)
            or poc_workflow == "release.yml"):
        raise BootstrapError("poc_workflow must be a local verification workflow filename")
    if auth_mode not in SECRETS:
        raise BootstrapError("auth_mode must be token or app")
    authentication = (
        "      release_token: ${{ secrets.GOLDEN_RELEASE_TOKEN }}\n"
        if auth_mode == "token" else
        "      app_private_key: ${{ secrets.GOLDEN_RELEASE_APP_KEY }}\n")
    app_input = "      app_id: ${{ vars.GOLDEN_RELEASE_APP_ID }}\n" if auth_mode == "app" else ""
    return (
        "name: release\n"
        "on:\n"
        "  push:\n"
        "    tags: [\"newbie\", \"[0-9]*-[12]\", \"v*\", \"newbie-v*\", \"[0-9]*-[12]-v*\"]\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      tag:\n"
        "        description: Existing immutable source tag\n"
        "        required: true\n"
        "        type: string\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  verify:\n"
        f"    uses: ./.github/workflows/{poc_workflow}\n"
        "    with:\n"
        "      source_ref: refs/tags/${{ inputs.tag || github.ref_name }}\n"
        "  release:\n"
        "    needs: verify\n"
        f"    uses: {OWNER}/Golden-Base/.github/workflows/shared-release.yml@{toolkit_ref}\n"
        "    with:\n"
        "      tag: ${{ inputs.tag || github.ref_name }}\n"
        f"      toolkit_ref: {toolkit_ref}\n"
        f"      auth_mode: {auth_mode}\n"
        f"{app_input}"
        "    secrets:\n"
        f"{authentication}"
    )


def list_pages(api, path, container=None):
    rows = []
    separator = "&" if "?" in path else "?"
    page = 1
    while True:
        document = api.call("GET", f"{path}{separator}per_page=100&page={page}")
        batch = document[container] if container else document
        rows.extend(batch)
        if len(batch) < 100:
            return rows
        page += 1


def verify_instructor_boundary(api, repo):
    full_name = repo["full_name"]
    if (repo.get("private") is not True or repo.get("is_template")
            or repo.get("archived") or repo.get("fork")
            or not repo.get("permissions", {}).get("admin")):
        raise BootstrapError("only an active, private, non-template instructor repository is eligible")
    if repo.get("size", 0) and repo.get("default_branch") != "master":
        raise BootstrapError("existing instructor source must use master; no branch is changed automatically")
    if api.call("GET", f"/repos/{full_name}/contents/.classroom50.yaml", missing_ok=True) is not None:
        raise BootstrapError("a Classroom student checkout cannot receive the publishing credential")
    for team in list_pages(api, f"/repos/{full_name}/teams"):
        if team.get("slug") != TEAM:
            raise BootstrapError(f"non-instructor team has source access: {team.get('slug')}")
    for collaborator in list_pages(api, f"/repos/{full_name}/collaborators"):
        login = collaborator["login"]
        membership = api.call("GET", f"/orgs/{OWNER}/memberships/{login}", missing_ok=True)
        if membership and membership.get("state") == "active" and membership.get("role") == "admin":
            continue
        instructor = api.call("GET", f"/orgs/{OWNER}/teams/{TEAM}/memberships/{login}", missing_ok=True)
        if not instructor or instructor.get("state") != "active":
            raise BootstrapError(f"non-instructor collaborator has source access: {login}")


def enroll(api, name, auth_mode="token", apply=False):
    validate_name(name)
    if auth_mode not in SECRETS:
        raise BootstrapError("unsupported authentication mode")
    secret_name = SECRETS[auth_mode]
    secret_path = f"/orgs/{OWNER}/actions/secrets/{secret_name}"
    secret = api.call("GET", secret_path, missing_ok=True)
    if secret is None or secret.get("visibility") != "selected":
        raise BootstrapError(f"{secret_name} must first exist as a selected-repository organization secret")
    full_name = f"{OWNER}/Golden-{name}"
    repo = api.call("GET", f"/repos/{full_name}", missing_ok=True)
    if repo is not None:
        if repo.get("full_name") != full_name:
            raise BootstrapError("repository identity mismatch")
        verify_instructor_boundary(api, repo)
    plan = {"repository": full_name, "create": repo is None, "auth_mode": auth_mode,
            "organization_secret": secret_name, "applied": False}
    if not apply:
        return plan
    if repo is None:
        repo = api.call("POST", f"/orgs/{OWNER}/repos", {
            "name": f"Golden-{name}", "private": True, "is_template": False,
            "auto_init": False, "has_issues": False, "has_projects": False, "has_wiki": False,
            "description": "Private instructor source. Not a Classroom student template.",
        })
        if repo.get("full_name") != full_name or not repo.get("private"):
            raise BootstrapError("GitHub did not create the requested private source")
        repo = api.call("GET", f"/repos/{full_name}")
        verify_instructor_boundary(api, repo)
    api.call("PUT", f"/orgs/{OWNER}/teams/{TEAM}/repos/{full_name}", {"permission": "admin"})
    api.call("PUT", f"{secret_path}/repositories/{repo['id']}")
    selected = list_pages(api, secret_path + "/repositories", container="repositories")
    if repo["id"] not in {item["id"] for item in selected}:
        raise BootstrapError("organization-secret enrollment did not take effect")
    plan.update({"applied": True, "repository_id": repo["id"]})
    return plan


def configure(api, source, name, toolkit_ref, poc_workflow, auth_mode,
              apply=False, replace_workflow=False, new_config=None):
    source = Path(source).resolve()
    validate_name(name)
    if not source.is_dir() or (source / ".lab-local-provenance.json").exists():
        raise BootstrapError("source must be the original instructor working directory")
    config_path = source / ".release.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.setdefault("student_updates", "manual")
    elif new_config is not None:
        config = dict(new_config)
    else:
        raise BootstrapError("a Lab-specific .release.json is required before enrollment")
    validate_config(config, name)
    workflow = render_workflow(toolkit_ref, poc_workflow, auth_mode)
    poc = source / ".github/workflows" / poc_workflow
    if not poc.is_file() or poc.is_symlink():
        raise BootstrapError("the local Lab verification workflow must exist")
    destination = source / ".github/workflows/release.yml"
    if (destination.exists() and destination.read_text(encoding="utf-8") != workflow
            and not replace_workflow):
        raise BootstrapError("release workflow differs; use --replace-workflow after reviewing the migration")
    if destination.is_symlink() or config_path.is_symlink():
        raise BootstrapError("bootstrap will not write through symbolic links")
    plan = enroll(api, name, auth_mode, apply=apply)
    plan.update({"source": str(source), "toolkit_ref": toolkit_ref,
                 "workflow": str(destination), "student_updates": config["student_updates"]})
    if apply:
        for path, content in (
                (config_path, json.dumps(config, indent=2) + "\n"),
                (destination, workflow)):
            temporary = path.with_name(path.name + ".bootstrap-tmp")
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary.replace(path)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--toolkit-ref")
    parser.add_argument("--poc-workflow")
    parser.add_argument("--auth-mode", choices=SECRETS, default="token")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-workflow", action="store_true")
    parser.add_argument("--slug", help="assignment slug when generating a new .release.json")
    parser.add_argument("--title", help="student-facing title for a new Lab")
    parser.add_argument("--description", help="student-facing description for a new Lab")
    parser.add_argument("--semester", default="115-1")
    args = parser.parse_args()
    token = os.environ.get("GOLDEN_ADMIN_TOKEN")
    if not token:
        parser.error("GOLDEN_ADMIN_TOKEN is required only for local administrator bootstrap; never commit it")
    location = Path(__file__).resolve().parent / "release/release.py"
    spec = importlib.util.spec_from_file_location("golden_release_core", location)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    api = module.GitHub(token)
    try:
        if args.source:
            if not args.toolkit_ref or not args.poc_workflow:
                raise BootstrapError("--source requires --toolkit-ref and --poc-workflow")
            generated = None
            if not (args.source / ".release.json").exists():
                generated = new_configuration(args.name, args.slug, args.title,
                                              args.description, args.semester)
            result = configure(api, args.source, args.name, args.toolkit_ref,
                               args.poc_workflow, args.auth_mode, args.apply,
                               args.replace_workflow, generated)
        else:
            result = enroll(api, args.name, args.auth_mode, args.apply)
    except (BootstrapError, OSError, ValueError, module.ReleaseError) as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
