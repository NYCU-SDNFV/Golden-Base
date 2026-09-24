"""Trusted hosted-runner preparation for the shared Golden runtime."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

if __package__:
    from .contract import COURSE_HOST_IMAGE, validate_profile
    from . import probe
else:
    from contract import COURSE_HOST_IMAGE, validate_profile
    import probe


DOCKER_SOCKET = "unix:///var/run/docker.sock"
DOCKER_ENVIRONMENT_OVERRIDES = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)
RUNNER_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))
COURSE_OWNER = "nycu-sdnfv"
OVERALL_TIMEOUT_SECONDS = 240
HOST_PREPARE_TIMEOUT_SECONDS = 60
HOST_VERIFY_TIMEOUT_SECONDS = 30
MODULE_TIMEOUT_SECONDS = 12
PROBE_TIMEOUT_SECONDS = 120
CONTAINER_LABEL = "edu.nycu.sdnfv.golden.environment"
PROFILE_MODULES = probe.PROFILE_MODULES


class GoldenEnvironmentError(ValueError):
    """The trusted runner is not the native disposable host it claims to be."""


EnvironmentError = GoldenEnvironmentError


def _trusted_hosted_runner(environ=None, platform_name=None):
    environ = os.environ if environ is None else environ
    platform_name = sys.platform if platform_name is None else platform_name
    return (
        environ.get("GITHUB_ACTIONS") == "true"
        and environ.get("GITHUB_REPOSITORY_OWNER", "").casefold()
        == COURSE_OWNER
        and environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and environ.get("RUNNER_OS") == "Linux"
        and platform_name == "linux"
    )


def _clean_docker_environment(environ=None):
    cleaned = dict(os.environ if environ is None else environ)
    for name in DOCKER_ENVIRONMENT_OVERRIDES:
        cleaned.pop(name, None)
    return cleaned


def _docker_prefix():
    return ("docker", f"--host={DOCKER_SOCKET}")


def _remaining_timeout(deadline, maximum, command):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, OVERALL_TIMEOUT_SECONDS)
    return min(maximum, max(1, remaining))


def _log_operation(operation, command, *, image=None, module=None):
    fields = [f"operation={operation}"]
    if image is not None:
        fields.append(f"image={image}")
    if module is not None:
        fields.append(f"module={module}")
    fields.append(f"command={shlex.join(command)}")
    print("GOLDEN_ENV START " + " ".join(fields), flush=True)


def _run_checked(command, *, operation, deadline, maximum, env, image=None,
                 module=None, input_text=None):
    _log_operation(
        operation, command, image=image, module=module
    )
    result = subprocess.run(
        command,
        check=True,
        text=True,
        input=input_text,
        env=env,
        timeout=_remaining_timeout(deadline, maximum, command),
    )
    print(
        f"GOLDEN_ENV PASS operation={operation}"
        + (f" image={image}" if image is not None else "")
        + (f" module={module}" if module is not None else ""),
        flush=True,
    )
    return result


def _cleanup_container(name, *, env):
    command = [*_docker_prefix(), "rm", "-f", name]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=probe.CLEANUP_TIMEOUT_SECONDS,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "No such container" not in output:
            print(
                "GOLDEN_ENV CLEANUP_ERROR "
                f"container={name} rc={result.returncode}: {output.strip()}",
                file=sys.stderr,
                flush=True,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(
            f"GOLDEN_ENV CLEANUP_ERROR container={name}: {error}",
            file=sys.stderr,
            flush=True,
        )


def _host_container_command(name, label, *, privileged, action):
    command = [
        *_docker_prefix(),
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        f"{CONTAINER_LABEL}={label}",
    ]
    if privileged:
        command.append("--privileged")
    command.extend([
        "--network",
        "host",
        "--entrypoint",
        "python3",
        COURSE_HOST_IMAGE,
        "-m",
        "lab_resources",
        action,
        "--profile",
        "course",
    ])
    return command


def _run_host_container(
        *, label, suffix, privileged, action, operation, deadline, maximum,
        env):
    name = f"golden-env-{label[:12]}-{suffix}"
    command = _host_container_command(
        name, label, privileged=privileged, action=action
    )
    try:
        return _run_checked(
            command,
            operation=operation,
            deadline=deadline,
            maximum=maximum,
            env=env,
            image=COURSE_HOST_IMAGE,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _cleanup_container(name, env=env)
        raise


def prepare_course_host(profile: dict) -> None:
    """Prepare only the native disposable NYCU-SDNFV GitHub-hosted runner."""
    profile = validate_profile(profile)
    if not _trusted_hosted_runner():
        print(
            "GOLDEN_ENV SKIP operation=course-host-prepare "
            "reason=not-native-nycu-sdnfv-github-hosted-linux",
            flush=True,
        )
        return None
    markers = [str(path) for path in RUNNER_CONTAINER_MARKERS if path.exists()]
    if markers:
        raise EnvironmentError(
            "trusted course host preparation refuses a job container; "
            f"found {', '.join(markers)}"
        )

    label = uuid.uuid4().hex
    env = _clean_docker_environment()
    deadline = time.monotonic() + OVERALL_TIMEOUT_SECONDS
    print(
        "GOLDEN_ENV BEGIN "
        f"image={COURSE_HOST_IMAGE} environment={profile['environment']} "
        f"docker_host={DOCKER_SOCKET} overall_timeout="
        f"{OVERALL_TIMEOUT_SECONDS}s",
        flush=True,
    )

    _run_host_container(
        label=label,
        suffix="prepare",
        privileged=True,
        action="host-prepare",
        operation="host-prepare",
        deadline=deadline,
        maximum=HOST_PREPARE_TIMEOUT_SECONDS,
        env=env,
    )
    _run_host_container(
        label=label,
        suffix="verify",
        privileged=False,
        action="host-verify",
        operation="host-verify",
        deadline=deadline,
        maximum=HOST_VERIFY_TIMEOUT_SECONDS,
        env=env,
    )

    for module in PROFILE_MODULES[profile["environment"]]:
        command = ["sudo", "-n", "modprobe", module]
        _run_checked(
            command,
            operation="module-prepare",
            deadline=deadline,
            maximum=MODULE_TIMEOUT_SECONDS,
            env=env,
            module=module,
        )

    probe_timeout = _remaining_timeout(
        deadline, PROBE_TIMEOUT_SECONDS, ("golden-probe",)
    )
    probe.run_probe(
        _docker_prefix(),
        COURSE_HOST_IMAGE,
        profile["environment"],
        env=env,
        timeout=probe_timeout,
        runner=subprocess.run,
        label=label,
    )
    print(
        "GOLDEN_ENV READY "
        f"image={COURSE_HOST_IMAGE} environment={profile['environment']}",
        flush=True,
    )
    return None
