#!/usr/bin/env python3
"""Non-scoring host checks and isolated active network probes for SDNFV labs."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import uuid

if __package__:
    from .contract import COURSE_HOST_IMAGE, dockerfile_image, load_profile
    from .environment import PROFILE_MODULES
    from . import probe
else:
    from contract import COURSE_HOST_IMAGE, dockerfile_image, load_profile
    from environment import PROFILE_MODULES
    import probe


GUIDE_URL = (
    "https://github.com/NYCU-SDNFV/lab-images/blob/main/README.md"
    "#1-prepare-the-docker-host"
)
COMMAND_TIMEOUT_SECONDS = 10
RESOURCE_TIMEOUT_SECONDS = 20
HOST_VERIFY_TIMEOUT_SECONDS = 30
CONTAINER_TIMEOUT_SECONDS = 120
CONTAINER_LABEL = "edu.nycu.sdnfv.golden.pretest"
SUPPORTED_ARCHITECTURES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
VERSION_COMMANDS = {
    "git": ("git", "--version"),
    "make": ("make", "--version"),
    "bash": ("bash", "--version"),
    "docker": ("docker", "--version"),
}


class Reporter:
    def __init__(self, stream=None):
        self.stream = sys.stdout if stream is None else stream
        self.failures = 0
        self.incomplete = 0

    def emit(
            self, status, title, *, current, required, responsible,
            fix=None, verify=None, guide=GUIDE_URL, incomplete=False):
        print(
            f"[{status}] {title}\n"
            f"  Current: {current}\n"
            f"  Required: {required}\n"
            f"  Responsible: {responsible}",
            file=self.stream,
        )
        if fix:
            print(f"  Fix: {fix}", file=self.stream)
        if verify:
            print(f"  Verify: {verify}", file=self.stream)
        if guide:
            print(f"  Guide: {guide}", file=self.stream)
        if status == "FAIL":
            self.failures += 1
        if incomplete:
            self.incomplete += 1

    def skip(self, title, dependency):
        self.emit(
            "SKIP",
            title,
            current=f"not run because {dependency} did not pass",
            required="complete the prerequisite, then rerun make pretest",
            responsible="the operator responsible for the failed prerequisite",
            verify="make pretest",
            incomplete=True,
        )

    @property
    def returncode(self):
        return 1 if self.failures or self.incomplete else 0

    def finish(self):
        print(
            "Summary: "
            f"{self.failures} FAIL, {self.incomplete} incomplete required "
            "verification(s). This pretest never awards course points.",
            file=self.stream,
        )
        return self.returncode


def _first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "(no output)"


def _last_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


def _marker_line(text, marker):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if marker in line:
            return line
    return _last_line(text)


def _error_text(error):
    parts = []
    for name in ("stdout", "stderr"):
        output = getattr(error, name, None)
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        if output:
            parts.extend(
                line.strip() for line in output.splitlines() if line.strip()
            )
    if not parts:
        return str(error)
    excerpt = " | ".join(parts[-8:])
    return excerpt[-2000:]


def _run(command, *, runner=subprocess.run, timeout=COMMAND_TIMEOUT_SECONDS,
         input_text=None):
    return runner(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )


def _find_project_root(script_path, profile_path):
    profile_parent = Path(profile_path).resolve().parent
    for directory in (script_path.resolve().parent, profile_parent):
        if directory.name == "golden" and directory.parent.name == ".github":
            return directory.parent.parent
    if (profile_parent / "Dockerfile").is_file():
        return profile_parent
    return Path.cwd().resolve()


def _dockerfile_identity(path):
    return dockerfile_image(path)


def _container_command(name, label, arguments, *, privileged=False,
                       network="none", interactive=False):
    command = [
        "docker",
        "run",
        "--pull=never",
        "--rm",
    ]
    if interactive:
        command.append("-i")
    command.extend([
        "--name",
        name,
        "--label",
        f"{CONTAINER_LABEL}={label}",
    ])
    if privileged:
        command.append("--privileged")
    command.extend(["--network", network, *arguments])
    return command


def _cleanup_container(name, *, runner=subprocess.run):
    try:
        result = runner(
            ["docker", "rm", "-f", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=probe.CLEANUP_TIMEOUT_SECONDS,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "No such container" not in output:
            print(
                f"[WARN] Scoped cleanup for {name} returned "
                f"{result.returncode}: {output.strip()}",
                file=sys.stderr,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(
            f"[WARN] Scoped cleanup for {name} failed: {error}",
            file=sys.stderr,
        )


def _run_container(command, name, *, runner=subprocess.run,
                   timeout=CONTAINER_TIMEOUT_SECONDS, input_text=None):
    try:
        return _run(
            command, runner=runner, timeout=timeout, input_text=input_text
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _cleanup_container(name, runner=runner)
        raise


def _host_location(context, endpoint, server_name):
    endpoint_text = endpoint or "(endpoint unavailable)"
    return (
        f"Docker context {context!r}, endpoint {endpoint_text!r}, "
        f"server {server_name!r}"
    )


def _host_fix(command):
    return (
        "On the Linux VM running this Docker Engine (not the PVE hypervisor "
        f"and not inside a lab container), run: {command}"
    )


def _module_fix(environment):
    modules = PROFILE_MODULES[environment]
    if not modules:
        return (
            "No host module preload is required for this userspace OVS "
            "profile; inspect the probe error and Docker privileges."
        )
    commands = " && ".join(
        f"sudo modprobe {shlex.quote(module)}" for module in modules
    )
    return _host_fix(commands)


def _normalize_architecture(value):
    return SUPPORTED_ARCHITECTURES.get(str(value).casefold())


def _load_info(result, description):
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"{description} returned invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} did not return a JSON object")
    return value


def run_pretest(
        profile_path, *, project_root=None, runner=subprocess.run,
        which=shutil.which, environ=None, platform_name=None, stream=None):
    profile_data = load_profile(profile_path)
    reporter = Reporter(stream)
    environ = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    project_root = (
        _find_project_root(Path(__file__), profile_path)
        if project_root is None else Path(project_root).resolve()
    )
    dockerfile = project_root / "Dockerfile"
    environment = profile_data["environment"]

    print(
        "Golden environment pretest (non-scoring; no host configuration changes)\n"
        "Active probes use temporary privileged containers; Linux may autoload "
        "network modules. Use an authorized dedicated lab VM, not a shared production host.\n"
        f"Profile: {profile_data['name']} / {environment}\n"
        f"Required image: {COURSE_HOST_IMAGE}",
        file=reporter.stream,
    )

    if platform_name == "linux":
        reporter.emit(
            "PASS",
            "Client platform",
            current=f"Linux ({platform.platform()})",
            required="a Linux client shell for the validated course workflow",
            responsible="student workstation operator",
        )
    else:
        if platform_name == "win32":
            fix = (
                "Run the repository and Docker CLI from WSL2 connected to a "
                "Linux Docker Engine, or use the course Linux VM."
            )
        else:
            fix = (
                "Use the course Linux VM; Docker Desktop/macOS is not a "
                "certified course environment."
            )
        reporter.emit(
            "WARN",
            "Client platform",
            current=f"{platform_name} is not certified",
            required="Linux; native Windows and macOS are not validated",
            responsible="student workstation operator",
            fix=fix,
            verify="From Linux/WSL2, rerun make pretest",
            incomplete=True,
        )

    current_python = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info >= (3, 8):
        reporter.emit(
            "PASS",
            "Python client",
            current=current_python,
            required="Python 3.8 or newer",
            responsible="student workstation operator",
        )
    else:
        reporter.emit(
            "FAIL",
            "Python client",
            current=current_python,
            required="Python 3.8 or newer",
            responsible="student workstation operator",
            fix="Use the supported course Linux VM or WSL2 environment.",
            verify="python3 --version",
        )

    tool_paths = {}
    for tool in ("git", "make", "bash", "docker"):
        location = which(tool)
        tool_paths[tool] = location
        if location is None:
            reporter.emit(
                "FAIL",
                f"{tool} client",
                current="not found on PATH",
                required=f"{tool} available in the client shell",
                responsible="student workstation operator",
                fix=(
                    "Use the pre-provisioned course Linux VM; pretest does "
                    "not install client software."
                ),
                verify=f"{tool} --version",
            )
            continue
        try:
            version = _run(
                list(VERSION_COMMANDS[tool]), runner=runner
            ).stdout
        except (OSError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as error:
            reporter.emit(
                "FAIL",
                f"{tool} client",
                current=_error_text(error),
                required=f"a working {tool} executable",
                responsible="student workstation operator",
                fix=(
                    "Repair the client shell or use the pre-provisioned "
                    "course Linux VM."
                ),
                verify=f"{tool} --version",
            )
        else:
            reporter.emit(
                "PASS",
                f"{tool} client",
                current=f"{location}: {_first_line(version)}",
                required=f"a working {tool} executable",
                responsible="student workstation operator",
            )

    dockerfile_ok = True
    try:
        actual_base = _dockerfile_identity(dockerfile)
    except (OSError, UnicodeError, ValueError) as error:
        dockerfile_ok = False
        reporter.emit(
            "FAIL",
            "Protected Dockerfile image",
            current=str(error),
            required=f"exactly one FROM line using {COURSE_HOST_IMAGE}",
            responsible="course maintainer / assignment updater",
            fix=(
                "Do not substitute another image. Restore protected files "
                "with the assignment's documented make update workflow or "
                "contact course staff."
            ),
            verify=f"grep -n '^FROM ' {shlex.quote(str(dockerfile))}",
        )
    else:
        reporter.emit(
            "PASS",
            "Protected Dockerfile image",
            current=actual_base,
            required=COURSE_HOST_IMAGE,
            responsible="course maintainer",
        )

    if tool_paths["docker"] is None:
        for title in (
                "Docker Compose", "Docker engine selection",
                "Immutable course image", "Image resource API",
                "Docker host resources", "Independent environment probe"):
            reporter.skip(title, "the Docker client")
        return reporter.finish()

    try:
        compose = _run(
            ["docker", "compose", "version"], runner=runner
        ).stdout
    except (OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        reporter.emit(
            "FAIL",
            "Docker Compose",
            current=_error_text(error),
            required="Docker Compose v2 available as docker compose",
            responsible="Docker client operator",
            fix=(
                "Use the course Linux VM with the Docker Compose plugin; "
                "pretest does not install packages."
            ),
            verify="docker compose version",
        )
    else:
        reporter.emit(
            "PASS",
            "Docker Compose",
            current=_first_line(compose),
            required="Docker Compose v2 available as docker compose",
            responsible="Docker client operator",
        )

    try:
        context = _run(["docker", "context", "show"], runner=runner).stdout.strip()
        endpoint_override = (
            environ.get("DOCKER_HOST") if not environ.get("DOCKER_CONTEXT") else None
        )
        endpoint = endpoint_override
        if endpoint is None:
            endpoint_result = _run(
                [
                    "docker", "context", "inspect", context,
                    "--format", "{{json .Endpoints.docker.Host}}",
                ],
                runner=runner,
            )
            endpoint_value = json.loads(endpoint_result.stdout)
            if not isinstance(endpoint_value, str) or not endpoint_value:
                raise ValueError("Docker context has no engine endpoint")
            endpoint = endpoint_value
        info_result = _run(
            ["docker", "info", "--format", "{{json .}}"], runner=runner
        )
        info = _load_info(info_result, "docker info")
        server_os = str(info.get("OSType", ""))
        server_arch_raw = str(info.get("Architecture", ""))
        server_arch = _normalize_architecture(server_arch_raw)
        server_name = str(info.get("Name", "(unnamed)"))
        server_version = str(info.get("ServerVersion", "(unknown)"))
    except (OSError, ValueError, json.JSONDecodeError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        reporter.emit(
            "FAIL",
            "Docker engine selection",
            current=_error_text(error),
            required="a reachable Linux Docker Engine and inspectable context",
            responsible="Docker client and engine operator",
            fix=(
                "Select the intended Linux engine with docker context use "
                "<context>, then verify that the daemon is running."
            ),
            verify=(
                "docker context show && docker info --format "
                "'{{.Name}} {{.OSType}} {{.Architecture}}'"
            ),
        )
        for title in (
                "Immutable course image", "Image resource API",
                "Docker host resources", "Independent environment probe"):
            reporter.skip(title, "Docker engine selection")
        return reporter.finish()

    location = _host_location(context, endpoint, server_name)
    if server_os != "linux":
        engine_ok = False
        reporter.emit(
            "FAIL",
            "Docker engine selection",
            current=(
                f"{location}; OS={server_os!r}, arch={server_arch_raw!r}, "
                f"version={server_version}"
            ),
            required="a Linux Docker Engine (not Windows containers)",
            responsible="Docker engine operator",
            fix=(
                "Switch Docker to a Linux engine. On Windows, use WSL2 or "
                "the course Linux VM."
            ),
            verify="docker info --format '{{.OSType}} {{.Architecture}}'",
        )
    elif server_arch is None:
        engine_ok = False
        reporter.emit(
            "FAIL",
            "Docker engine selection",
            current=f"{location}; unsupported architecture={server_arch_raw!r}",
            required="linux/amd64 or linux/arm64",
            responsible="Docker engine operator",
            fix="Use a supported course Linux VM.",
            verify="docker info --format '{{.OSType}} {{.Architecture}}'",
        )
    else:
        engine_ok = True
        status = "PASS" if server_arch == "amd64" else "WARN"
        reporter.emit(
            status,
            "Docker engine selection",
            current=(
                f"{location}; OS=linux, arch={server_arch}, "
                f"version={server_version}"
            ),
            required=(
                "Linux Docker Engine; full course validation is linux/amd64"
            ),
            responsible="Docker engine operator",
            fix=(
                None if server_arch == "amd64"
                else "Use the course linux/amd64 VM for certified lab results."
            ),
            verify="docker info --format '{{.Name}} {{.OSType}} {{.Architecture}}'",
        )

    if not dockerfile_ok or not engine_ok:
        dependency = (
            "the protected Dockerfile image"
            if not dockerfile_ok else "the Linux Docker engine"
        )
        for title in (
                "Immutable course image", "Image resource API",
                "Docker host resources", "Independent environment probe"):
            reporter.skip(title, dependency)
        return reporter.finish()

    try:
        image_result = _run(
            [
                "docker", "image", "inspect", COURSE_HOST_IMAGE,
                "--format", "{{json .}}",
            ],
            runner=runner,
        )
        image_info = _load_info(image_result, "docker image inspect")
        image_os = str(image_info.get("Os", ""))
        image_arch = _normalize_architecture(image_info.get("Architecture", ""))
        repo_digests = image_info.get("RepoDigests", [])
        if (not isinstance(repo_digests, list)
                or COURSE_HOST_IMAGE not in repo_digests):
            raise ValueError(
                "the inspected image did not report the required RepoDigest"
            )
        if image_os != "linux" or image_arch != server_arch:
            raise ValueError(
                f"image platform is {image_os}/{image_arch}; "
                f"server is linux/{server_arch}"
            )
    except (OSError, ValueError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        reporter.emit(
            "FAIL",
            "Immutable course image",
            current=_error_text(error),
            required=(
                f"{COURSE_HOST_IMAGE} present on the selected Docker engine "
                f"for linux/{server_arch}"
            ),
            responsible=f"administrator of {location}",
            fix=_host_fix(f"docker pull {shlex.quote(COURSE_HOST_IMAGE)}"),
            verify=(
                "docker image inspect "
                f"{shlex.quote(COURSE_HOST_IMAGE)} --format "
                "'{{json .RepoDigests}}'"
            ),
        )
        for title in (
                "Image resource API", "Docker host resources",
                "Independent environment probe"):
            reporter.skip(title, "the immutable course image")
        return reporter.finish()

    reporter.emit(
        "PASS",
        "Immutable course image",
        current=(
            f"id={image_info.get('Id', '(unknown)')}, "
            f"platform=linux/{image_arch}, digest={COURSE_HOST_IMAGE}"
        ),
        required=COURSE_HOST_IMAGE,
        responsible=f"administrator of {location}",
    )

    label = uuid.uuid4().hex
    resource_name = f"golden-pretest-{label[:12]}-resource"
    resource_script = (
        "import lab_resources as r; "
        "assert callable(r.prepare_host) and callable(r.verify_host) "
        "and callable(r.initialize_container); "
        "print(r.__file__)"
    )
    resource_command = _container_command(
        resource_name,
        label,
        [
            "--entrypoint", "python3", COURSE_HOST_IMAGE,
            "-c", resource_script,
        ],
    )
    try:
        resource_result = _run_container(
            resource_command, resource_name, runner=runner,
            timeout=RESOURCE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        reporter.emit(
            "FAIL",
            "Image resource API",
            current=_error_text(error),
            required=(
                "the pinned image's lab_resources prepare/verify/container API"
            ),
            responsible="course image maintainer",
            fix=(
                "Keep the exact pinned image and report this image integrity "
                "failure to course staff; do not substitute another image."
            ),
            verify=shlex.join(resource_command),
        )
        reporter.skip("Docker host resources", "the image resource API")
        reporter.skip("Independent environment probe", "the image resource API")
        return reporter.finish()

    reporter.emit(
        "PASS",
        "Image resource API",
        current=_first_line(resource_result.stdout),
        required="lab_resources prepare/verify/container API",
        responsible="course image maintainer",
    )

    verify_name = f"golden-pretest-{label[:12]}-host-verify"
    verify_command = _container_command(
        verify_name,
        label,
        [
            "--entrypoint", "python3", COURSE_HOST_IMAGE,
            "-m", "lab_resources", "host-verify", "--profile", "course",
        ],
        network="host",
    )
    try:
        verify_result = _run_container(
            verify_command, verify_name, runner=runner,
            timeout=HOST_VERIFY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        prepare_command = (
            "docker run --rm --privileged --network host "
            f"--entrypoint python3 {shlex.quote(COURSE_HOST_IMAGE)} "
            "-m lab_resources host-prepare --profile course"
        )
        reporter.emit(
            "FAIL",
            "Docker host resources",
            current=_error_text(error),
            required="the pinned image's course host resource profile",
            responsible=f"administrator of {location}",
            fix=_host_fix(prepare_command),
            verify=shlex.join(verify_command),
        )
        reporter.skip(
            "Independent environment probe", "Docker host resource verification"
        )
        return reporter.finish()

    reporter.emit(
        "PASS",
        "Docker host resources",
        current=_marker_line(
            verify_result.stdout, "Course host prerequisites verified."
        ),
        required="the pinned image's course host resource profile",
        responsible=f"administrator of {location}",
    )

    try:
        probe_result = probe.run_probe(
            ("docker",),
            COURSE_HOST_IMAGE,
            environment,
            timeout=CONTAINER_TIMEOUT_SECONDS,
            runner=runner,
            capture_output=True,
            label=label,
        )
    except (OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as error:
        reporter.emit(
            "FAIL",
            "Independent environment probe",
            current=_error_text(error),
            required=(
                f"known-good {environment} topology, actual datapath, "
                "zero-loss ping, TCP transfer, and profile capabilities"
            ),
            responsible=f"administrator of {location}",
            fix=_module_fix(environment),
            verify="make pretest",
        )
    else:
        reporter.emit(
            "PASS",
            "Independent environment probe",
            current=_marker_line(
                probe_result.stdout, "GOLDEN_PROBE_RESULT"
            ),
            required=(
                f"known-good independent {environment} probe on the exact "
                "course image"
            ),
            responsible=f"administrator of {location}",
        )

    return reporter.finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).with_name("profile.json"),
        help="protected profile JSON (default: profile.json next to this script)",
    )
    args = parser.parse_args(argv)
    try:
        return run_pretest(args.profile)
    except (OSError, ValueError) as error:
        print(
            "[FAIL] Golden profile\n"
            f"  Current: {error}\n"
            "  Required: the protected adjacent Golden profile.json\n"
            "  Responsible: course maintainer / assignment updater\n"
            "  Fix: Restore protected assignment files; do not invent or edit "
            "the profile.\n"
            "  Verify: make pretest\n"
            f"  Guide: {GUIDE_URL}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
