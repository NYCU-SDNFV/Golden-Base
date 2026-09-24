import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools" / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from golden import contract, environment, pretest, probe


TRUSTED_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY_OWNER": "NYCU-SDNFV",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "RUNNER_OS": "Linux",
    "PATH": "/usr/bin",
}


def profile(environment_name="toolchain"):
    values = {
        "toolchain": ("Toolchain", 0, "lab0-toolchain", 8),
        "controller": ("Controller", 1, "lab1-controller", 16),
        "measurement": ("Measure", 2, "lab2-measure", 12),
        "vrouter": ("VRouter", 3, "lab3-vrouter", 20),
    }
    name, lab, assignment, checks = values[environment_name]
    return {
        "schema": 1,
        "name": name,
        "lab": lab,
        "assignment": assignment,
        "checks": checks,
        "environment": environment_name,
    }


class RecordingRunner:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))
        if self.handler is not None:
            result = self.handler(command, kwargs)
            if result is not None:
                return result
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class HostedPreparationTests(unittest.TestCase):
    def test_cleanup_has_its_own_bound_after_operation_deadline(self):
        runner = RecordingRunner()
        with mock.patch.object(environment.subprocess, "run", runner):
            environment._cleanup_container("golden-env-owned", env={})
        self.assertEqual(1, len(runner.calls))
        command, options = runner.calls[0]
        self.assertEqual(["rm", "-f", "golden-env-owned"], command[-3:])
        self.assertEqual(probe.CLEANUP_TIMEOUT_SECONDS, options["timeout"])

    def test_environment_error_is_a_configuration_value_error(self):
        self.assertTrue(issubclass(environment.EnvironmentError, ValueError))
        self.assertIs(
            environment.GoldenEnvironmentError, environment.EnvironmentError
        )

    def run_trusted(self, environment_name="toolchain", runner=None,
                    extra_environment=None):
        runner = runner or RecordingRunner()
        values = dict(TRUSTED_ENV)
        if extra_environment:
            values.update(extra_environment)
        with mock.patch.dict(environment.os.environ, values, clear=True), \
                mock.patch.object(environment.sys, "platform", "linux"), \
                mock.patch.object(
                    environment, "RUNNER_CONTAINER_MARKERS", ()), \
                mock.patch.object(environment.subprocess, "run", runner), \
                contextlib.redirect_stdout(io.StringIO()):
            environment.prepare_course_host(profile(environment_name))
        return runner

    def test_no_preparation_outside_every_trusted_gate(self):
        variants = (
            {"GITHUB_ACTIONS": "false"},
            {"GITHUB_REPOSITORY_OWNER": "someone-else"},
            {"RUNNER_ENVIRONMENT": "self-hosted"},
            {"RUNNER_OS": "Windows"},
        )
        for change in variants:
            values = dict(TRUSTED_ENV)
            values.update(change)
            runner = RecordingRunner()
            with self.subTest(change=change), \
                    mock.patch.dict(environment.os.environ, values, clear=True), \
                    mock.patch.object(environment.sys, "platform", "linux"), \
                    mock.patch.object(environment.subprocess, "run", runner), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(
                    environment.prepare_course_host(profile())
                )
                self.assertEqual([], runner.calls)

        runner = RecordingRunner()
        with mock.patch.dict(
                environment.os.environ,
                {"DOCKER_HOST": "tcp://remote.example:2375"}, clear=True), \
                mock.patch.object(environment.sys, "platform", "linux"), \
                mock.patch.object(environment.subprocess, "run", runner), \
                contextlib.redirect_stdout(io.StringIO()):
            environment.prepare_course_host(profile())
        self.assertEqual([], runner.calls)

        with mock.patch.dict(environment.os.environ, {}, clear=True), \
                mock.patch.object(environment.subprocess, "run", runner), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(ValueError, "invalid Golden"):
            environment.prepare_course_host({"invalid": True})
        self.assertEqual([], runner.calls)

        runner = RecordingRunner()
        with mock.patch.dict(
                environment.os.environ, TRUSTED_ENV, clear=True), \
                mock.patch.object(environment.sys, "platform", "win32"), \
                mock.patch.object(environment.subprocess, "run", runner), \
                contextlib.redirect_stdout(io.StringIO()):
            environment.prepare_course_host(profile())
        self.assertEqual([], runner.calls)

    def test_native_gate_rejects_a_github_job_container(self):
        marker = mock.Mock()
        marker.exists.return_value = True
        marker.__str__ = mock.Mock(return_value="/.dockerenv")
        runner = RecordingRunner()
        with mock.patch.dict(
                environment.os.environ, TRUSTED_ENV, clear=True), \
                mock.patch.object(environment.sys, "platform", "linux"), \
                mock.patch.object(
                    environment, "RUNNER_CONTAINER_MARKERS", (marker,)), \
                mock.patch.object(environment.subprocess, "run", runner):
            with self.assertRaisesRegex(
                    environment.EnvironmentError, "job container"):
                environment.prepare_course_host(profile())
        self.assertEqual([], runner.calls)

    def test_order_socket_digest_environment_and_profile_modules(self):
        runner = self.run_trusted(
            "measurement",
            extra_environment={
                "DOCKER_HOST": "tcp://attacker:2375",
                "DOCKER_CONTEXT": "remote",
                "DOCKER_TLS_VERIFY": "1",
                "DOCKER_CERT_PATH": "/tmp/certs",
            },
        )
        operations = []
        modules = []
        for command, kwargs in runner.calls:
            if "host-prepare" in command:
                operations.append("prepare")
            elif "host-verify" in command:
                operations.append("verify")
            elif command[:3] == ["sudo", "-n", "modprobe"]:
                operations.append("module")
                modules.append(command[-1])
            elif kwargs.get("input"):
                operations.append("probe")
            if command[0] == "docker":
                self.assertEqual(
                    f"--host={environment.DOCKER_SOCKET}", command[1]
                )
                if "run" in command:
                    self.assertIn(contract.COURSE_HOST_IMAGE, command)
            for name in environment.DOCKER_ENVIRONMENT_OVERRIDES:
                self.assertNotIn(name, kwargs["env"])
        self.assertEqual(
            ["prepare", "verify", "module", "module", "module", "module",
             "module", "probe"],
            operations,
        )
        self.assertEqual(
            list(environment.PROFILE_MODULES["measurement"]), modules
        )

    def test_userspace_profiles_do_not_load_kernel_modules(self):
        for environment_name in ("toolchain", "controller"):
            runner = self.run_trusted(environment_name)
            with self.subTest(environment=environment_name):
                self.assertFalse(any(
                    call[0][:3] == ["sudo", "-n", "modprobe"]
                    for call in runner.calls
                ))
                probe_calls = [
                    call for call in runner.calls if call[1].get("input")
                ]
                self.assertEqual(1, len(probe_calls))
                self.assertEqual(
                    environment_name, probe_calls[0][0][-1]
                )

    def test_vrouter_prepares_only_ovs_and_vxlan(self):
        runner = self.run_trusted("vrouter")
        modules = [
            command[-1] for command, _ in runner.calls
            if command[:3] == ["sudo", "-n", "modprobe"]
        ]
        self.assertEqual(["openvswitch", "vxlan"], modules)
        self.assertFalse(any("||" in command for command, _ in runner.calls))

    def test_prepare_verify_and_probe_failures_propagate(self):
        for failing_operation in ("host-prepare", "host-verify", "probe"):
            def handler(command, kwargs, target=failing_operation):
                is_target = (
                    target in command
                    or (target == "probe" and kwargs.get("input") is not None)
                )
                if is_target:
                    raise subprocess.CalledProcessError(9, command)
                return None

            runner = RecordingRunner(handler)
            with self.subTest(operation=failing_operation), \
                    self.assertRaises(subprocess.CalledProcessError):
                self.run_trusted("toolchain", runner=runner)
            if failing_operation == "probe":
                self.assertTrue(any(
                    command[2:4] == ["rm", "-f"]
                    for command, _ in runner.calls
                ))

    def test_timeout_propagates_and_attempts_scoped_cleanup(self):
        def handler(command, kwargs):
            if "host-prepare" in command:
                raise subprocess.TimeoutExpired(command, 1)
            return None

        runner = RecordingRunner(handler)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_trusted("toolchain", runner=runner)
        cleanup = [
            command for command, _ in runner.calls
            if command[2:4] == ["rm", "-f"]
        ]
        self.assertEqual(1, len(cleanup))
        self.assertIn("golden-env-", cleanup[0][-1])


class ProbeContractTests(unittest.TestCase):
    def test_missing_module_metadata_does_not_reject_unreported_builtins(self):
        output = io.StringIO()
        with mock.patch.object(probe.Path, "is_file", return_value=True), \
                mock.patch.object(probe.Path, "read_text", return_value=""), \
                mock.patch.object(probe.Path, "is_dir", return_value=False), \
                contextlib.redirect_stdout(output):
            unreported = probe._report_kernel_module_state("measurement")
        self.assertEqual(probe.PROFILE_MODULES["measurement"], unreported)
        self.assertIn("active capability checks must determine support", output.getvalue())
        self.assertNotIn("PASS", output.getvalue())

    def test_actual_qdisc_capability_is_required_even_if_metadata_is_missing(self):
        host = mock.Mock()
        host.pid = 123
        host.defaultIntf.return_value.name = "h1-eth0"
        show_count = 0

        def run(*arguments, **kwargs):
            nonlocal show_count
            if "net.ipv4.tcp_available_congestion_control" in arguments:
                return "reno cubic bbr"
            if "net.ipv4.tcp_congestion_control" in arguments:
                return "bbr"
            if "qdisc" in arguments and "show" in arguments:
                show_count += 1
                return "qdisc netem 10: root" if show_count == 1 else "qdisc htb 1: root"
            if "class" in arguments and "show" in arguments:
                return "class htb 1:10"
            return ""

        with mock.patch.object(probe, "_run", side_effect=run), \
                self.assertRaisesRegex(probe.ProbeError, "HTB/fq_codel"):
            probe._check_measurement_features(host)

    def test_probe_is_network_isolated_and_has_no_host_mount(self):
        command = probe.probe_command(
            ("docker",), contract.COURSE_HOST_IMAGE,
            "measurement", "probe-name", "label",
        )
        self.assertIn("--privileged", command)
        self.assertIn("--pull=never", command)
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertNotIn("--mount", command)
        self.assertNotIn("-v", command)
        self.assertEqual("measurement", command[-1])

    def test_probe_timeout_attempts_named_cleanup(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((list(command), kwargs))
            if command[1:3] == ["rm", "-f"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            raise subprocess.TimeoutExpired(command, 1)

        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(subprocess.TimeoutExpired):
            probe.run_probe(
                ("docker",), contract.COURSE_HOST_IMAGE, "toolchain",
                runner=runner, timeout=1, label="abc",
            )
        self.assertEqual(["docker", "rm", "-f"], calls[-1][0][:3])
        self.assertEqual("golden-probe-abc", calls[-1][0][-1])

    def test_process_limit_check_uses_normalized_range(self):
        class Limits:
            RLIMIT_NOFILE = 7
            RLIM_INFINITY = -1

            def __init__(self, value):
                self.value = value

            def getrlimit(self, limit):
                self.assert_limit = limit
                return self.value

        with contextlib.redirect_stdout(io.StringIO()):
            probe._check_process_limits(Limits((65536, 65536)))
        with self.assertRaisesRegex(probe.ProbeError, "normalized"):
            probe._check_process_limits(Limits((1048576, 1048576)))

    def test_limit_is_checked_after_mininet_construction(self):
        source = probe.probe_source()
        self.assertLess(
            source.index("net = Mininet("),
            source.index("_check_process_limits()", source.index("net = Mininet(")),
        )

    def test_profile_specific_real_capabilities_are_in_probe(self):
        source = probe.probe_source()
        for expected in (
                "netdev@", "system@", "iperf3", "tcp_received_bytes",
                "ofproto_v1_3", "matplotlib", "tcp_bbr", "netem", "htb",
                "fq_codel", "/usr/lib/frr/zebra", "/usr/lib/frr/bgpd",
                "net.ipv4.ip_forward=1", "net.ipv6.conf.all.forwarding=1",
                "vxlan id 42"):
            with self.subTest(expected=expected):
                self.assertIn(expected, source)
        self.assertNotIn("modprobe", source)


class PretestFixture:
    def __init__(self, case, environment_name="toolchain"):
        self.case = case
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(
            json.dumps(profile(environment_name)), encoding="ascii"
        )
        (self.root / "Dockerfile").write_text(
            f"FROM {contract.COURSE_HOST_IMAGE}\nWORKDIR /workspace\n",
            encoding="ascii",
        )
        self.stream = io.StringIO()
        self.commands = []

    def close(self):
        self.temporary.cleanup()

    def runner(self, command, **kwargs):
        command = list(command)
        self.commands.append((command, kwargs))
        if command[:2] == ["git", "--version"]:
            stdout = "git version 2.45\n"
        elif command[:2] == ["make", "--version"]:
            stdout = "GNU Make 4.4\n"
        elif command[:2] == ["bash", "--version"]:
            stdout = "GNU bash, version 5.2\n"
        elif command[:2] == ["docker", "--version"]:
            stdout = "Docker version 27.0\n"
        elif command[:3] == ["docker", "compose", "version"]:
            stdout = "Docker Compose version v2.29\n"
        elif command[:3] == ["docker", "context", "show"]:
            stdout = "default\n"
        elif command[:3] == ["docker", "context", "inspect"]:
            stdout = json.dumps("unix:///var/run/docker.sock") + "\n"
        elif command[:2] == ["docker", "info"]:
            stdout = json.dumps({
                "Name": "course-vm",
                "OSType": "linux",
                "Architecture": "x86_64",
                "ServerVersion": "27.0",
            })
        elif command[:3] == ["docker", "image", "inspect"]:
            stdout = json.dumps({
                "Id": "sha256:local",
                "Os": "linux",
                "Architecture": "amd64",
                "RepoDigests": [contract.COURSE_HOST_IMAGE],
            })
        elif "host-verify" in command:
            stdout = "Course host prerequisites verified.\n"
        elif kwargs.get("input"):
            stdout = (
                'GOLDEN_PROBE_RESULT {"status":"PASS",'
                '"tcp_received_bytes":1024}\n'
            )
        elif command[:3] == ["docker", "rm", "-f"]:
            stdout = ""
        elif command[:2] == ["docker", "run"]:
            stdout = "/usr/local/share/lab-resources/lab_resources.py\n"
        else:
            self.case.fail(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def run(self, **kwargs):
        values = dict(
            profile_path=self.profile_path,
            project_root=self.root,
            runner=self.runner,
            which=lambda name: f"/usr/bin/{name}",
            environ={},
            platform_name="linux",
            stream=self.stream,
        )
        values.update(kwargs)
        with mock.patch.object(
                pretest.platform, "platform", return_value="Linux-test"):
            return pretest.run_pretest(**values)


class PretestTests(unittest.TestCase):
    def test_failure_detail_keeps_stderr_after_noisy_success_output(self):
        error = subprocess.CalledProcessError(
            1, ["docker"], output="\n".join(f"noise {number}" for number in range(50)),
            stderr="GOLDEN_PROBE_FAIL: fq_codel is unavailable",
        )
        self.assertIn("GOLDEN_PROBE_FAIL: fq_codel is unavailable", pretest._error_text(error))

    def fixture(self, environment_name="toolchain"):
        value = PretestFixture(self, environment_name)
        self.addCleanup(value.close)
        return value

    def test_complete_read_only_pretest_passes_without_mutation(self):
        fixture = self.fixture("measurement")
        self.assertEqual(0, fixture.run())
        commands = [command for command, _ in fixture.commands]
        flattened = [" ".join(command) for command in commands]
        for forbidden in (
                "host-prepare", " modprobe ", "docker pull", "apt-get",
                "apk add", "dnf install", "sysctl -w", "mn -c", "prune"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(any(
                    forbidden in command for command in flattened
                ))
        probe_calls = [
            (command, kwargs) for command, kwargs in fixture.commands
            if kwargs.get("input")
        ]
        self.assertEqual(1, len(probe_calls))
        self.assertNotIn("--mount", probe_calls[0][0])
        self.assertEqual("measurement", probe_calls[0][0][-1])
        self.assertIn("[PASS] Independent environment probe",
                      fixture.stream.getvalue())

    def test_direct_script_import_mode_uses_adjacent_modules(self):
        location = (
            ROOT / "tools" / "runtime" / "golden" / "pretest.py"
        )
        spec = importlib.util.spec_from_file_location(
            "golden_pretest_direct_test", location
        )
        module = importlib.util.module_from_spec(spec)
        adjacent = str(location.parent)
        with mock.patch.object(
                sys, "path", [adjacent, *sys.path]):
            spec.loader.exec_module(module)
        self.assertEqual(module.COURSE_HOST_IMAGE, contract.COURSE_HOST_IMAGE)
        self.assertTrue(callable(module.main))

    def test_explicit_profile_path_discovers_its_project_root(self):
        fixture = self.fixture()
        self.assertEqual(0, fixture.run(project_root=None))
        self.assertIn(
            "[PASS] Protected Dockerfile image", fixture.stream.getvalue()
        )

    def test_missing_project_dockerfile_cannot_select_a_healthy_parent(self):
        fixture = self.fixture()
        child = fixture.root / "assignment"
        profile_path = child / ".github/golden/profile.json"
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(json.dumps(profile()), encoding="ascii")
        self.assertEqual(child, pretest._find_project_root(
            profile_path.with_name("pretest.py"), profile_path))
        self.assertEqual(1, fixture.run(profile_path=profile_path, project_root=None))
        self.assertIn("[FAIL] Protected Dockerfile image", fixture.stream.getvalue())
        self.assertFalse(any(
            command[:2] == ["docker", "run"] for command, _ in fixture.commands))

    def test_a_second_flagged_docker_stage_cannot_hide_the_actual_base(self):
        fixture = self.fixture()
        (fixture.root / "Dockerfile").write_text(
            f"FROM {contract.COURSE_HOST_IMAGE}\n"
            "FROM --platform=linux/amd64 alpine:3.20\n", encoding="ascii")
        self.assertEqual(1, fixture.run())
        self.assertIn("[FAIL] Protected Dockerfile image", fixture.stream.getvalue())
        self.assertFalse(any(
            command[:2] == ["docker", "run"] for command, _ in fixture.commands))

    def test_explicit_docker_context_takes_precedence_over_host_override(self):
        fixture = self.fixture()
        self.assertEqual(0, fixture.run(environ={
            "DOCKER_CONTEXT": "default", "DOCKER_HOST": "tcp://not-selected:2375",
        }))
        self.assertIn("unix:///var/run/docker.sock", fixture.stream.getvalue())
        self.assertNotIn("not-selected", fixture.stream.getvalue())
        self.assertTrue(any(
            command[:3] == ["docker", "context", "inspect"]
            for command, _ in fixture.commands))

    def test_supported_arm64_can_pass_measured_checks_without_full_lab_claim(self):
        fixture = self.fixture()
        original = fixture.runner

        def runner(command, **kwargs):
            result = original(command, **kwargs)
            if list(command)[:2] == ["docker", "info"]:
                value = json.loads(result.stdout)
                value["Architecture"] = "aarch64"
                result.stdout = json.dumps(value)
            elif list(command)[:3] == ["docker", "image", "inspect"]:
                value = json.loads(result.stdout)
                value["Architecture"] = "arm64"
                result.stdout = json.dumps(value)
            return result

        fixture.runner = runner
        self.assertEqual(0, fixture.run())
        self.assertIn("[WARN] Docker engine selection", fixture.stream.getvalue())
        self.assertIn("[PASS] Independent environment probe", fixture.stream.getvalue())
        self.assertIn("full course validation is linux/amd64", fixture.stream.getvalue())

    def test_remote_context_is_diagnosed_without_local_path_or_socket_pinning(self):
        fixture = self.fixture()
        original = fixture.runner

        def runner(command, **kwargs):
            if list(command)[:3] == ["docker", "context", "show"]:
                fixture.commands.append((list(command), kwargs))
                return subprocess.CompletedProcess(
                    command, 0, "course-remote\n", ""
                )
            if list(command)[:3] == ["docker", "context", "inspect"]:
                fixture.commands.append((list(command), kwargs))
                return subprocess.CompletedProcess(
                    command, 0, json.dumps("ssh://student@course-vm") + "\n", ""
                )
            return original(command, **kwargs)

        fixture.runner = runner
        self.assertEqual(0, fixture.run())
        output = fixture.stream.getvalue()
        self.assertIn("course-remote", output)
        self.assertIn("ssh://student@course-vm", output)
        docker_runs = [
            command for command, _ in fixture.commands
            if command[:2] == ["docker", "run"]
        ]
        self.assertTrue(docker_runs)
        self.assertFalse(any(
            any(part.startswith("--host=") for part in command)
            for command in docker_runs
        ))
        self.assertFalse(any("--mount" in command for command in docker_runs))

    def test_missing_image_fails_and_skips_all_dependent_containers(self):
        fixture = self.fixture()
        original = fixture.runner

        def runner(command, **kwargs):
            if list(command)[:3] == ["docker", "image", "inspect"]:
                fixture.commands.append((list(command), kwargs))
                raise subprocess.CalledProcessError(
                    1, command, stderr="No such image"
                )
            return original(command, **kwargs)

        fixture.runner = runner
        self.assertEqual(1, fixture.run())
        output = fixture.stream.getvalue()
        self.assertIn("[FAIL] Immutable course image", output)
        self.assertIn("[SKIP] Image resource API", output)
        self.assertFalse(any(
            command[:2] == ["docker", "run"]
            for command, _ in fixture.commands
        ))

    def test_wrong_dockerfile_never_probes_a_different_healthy_image(self):
        fixture = self.fixture()
        (fixture.root / "Dockerfile").write_text(
            "FROM ghcr.io/nycu-sdnfv/lab-base:latest\n", encoding="ascii"
        )
        self.assertEqual(1, fixture.run())
        self.assertFalse(any(
            command[:3] == ["docker", "image", "inspect"]
            for command, _ in fixture.commands
        ))
        self.assertIn(
            "[FAIL] Protected Dockerfile image", fixture.stream.getvalue()
        )

    def test_host_verify_failure_skips_probe_and_prints_explicit_fix(self):
        fixture = self.fixture("vrouter")
        original = fixture.runner

        def runner(command, **kwargs):
            if "host-verify" in command:
                fixture.commands.append((list(command), kwargs))
                raise subprocess.CalledProcessError(
                    1, command, stderr="net.core.wmem_max is too small"
                )
            return original(command, **kwargs)

        fixture.runner = runner
        self.assertEqual(1, fixture.run())
        output = fixture.stream.getvalue()
        self.assertIn("host-prepare --profile course", output)
        self.assertIn("not the PVE hypervisor", output)
        self.assertIn("[SKIP] Independent environment probe", output)
        self.assertFalse(any(
            kwargs.get("input") for _, kwargs in fixture.commands
        ))

    def test_probe_failure_prints_profile_module_commands(self):
        fixture = self.fixture("measurement")
        original = fixture.runner

        def runner(command, **kwargs):
            if kwargs.get("input"):
                fixture.commands.append((list(command), kwargs))
                raise subprocess.CalledProcessError(
                    1, command, stderr="kernel datapath unavailable"
                )
            return original(command, **kwargs)

        fixture.runner = runner
        self.assertEqual(1, fixture.run())
        output = fixture.stream.getvalue()
        for module in environment.PROFILE_MODULES["measurement"]:
            self.assertIn(f"sudo modprobe {module}", output)

    def test_container_timeout_attempts_only_named_cleanup(self):
        fixture = self.fixture()
        original = fixture.runner
        timed_out = {"done": False}

        def runner(command, **kwargs):
            if (not timed_out["done"] and command[:2] == ["docker", "run"]):
                fixture.commands.append((list(command), kwargs))
                timed_out["done"] = True
                raise subprocess.TimeoutExpired(command, 1)
            return original(command, **kwargs)

        fixture.runner = runner
        self.assertEqual(1, fixture.run())
        cleanup = [
            command for command, _ in fixture.commands
            if command[:3] == ["docker", "rm", "-f"]
        ]
        self.assertEqual(1, len(cleanup))
        self.assertTrue(cleanup[0][-1].startswith("golden-pretest-"))
        self.assertFalse(any("prune" in command for command in cleanup))

    def test_native_windows_is_warned_and_never_certified(self):
        fixture = self.fixture()
        self.assertEqual(1, fixture.run(platform_name="win32"))
        output = fixture.stream.getvalue()
        self.assertIn("[WARN] Client platform", output)
        self.assertIn("WSL2", output)
        self.assertNotIn("[PASS] Client platform", output)

    def test_missing_docker_client_fails_and_skips_daemon_checks(self):
        fixture = self.fixture()
        self.assertEqual(1, fixture.run(
            which=lambda name: None if name == "docker" else f"/usr/bin/{name}"
        ))
        self.assertFalse(any(
            command and command[0] == "docker"
            for command, _ in fixture.commands
        ))
        self.assertIn("[SKIP] Docker engine selection",
                      fixture.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
