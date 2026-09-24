#!/usr/bin/env python3
"""Independent disposable-container probes for Golden course environments."""

import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid


ENVIRONMENTS = ("toolchain", "controller", "measurement", "vrouter")
PROFILE_MODULES = {
    "toolchain": (),
    "controller": (),
    "measurement": (
        "openvswitch",
        "sch_netem",
        "sch_htb",
        "sch_fq_codel",
        "tcp_bbr",
    ),
    "vrouter": ("openvswitch", "vxlan"),
}
PROBE_LABEL = "edu.nycu.sdnfv.golden.probe"
CLEANUP_TIMEOUT_SECONDS = 15


class ProbeError(RuntimeError):
    """A known-good environment probe did not demonstrate a requirement."""


def probe_source():
    """Return this standalone script for delivery to a remote Docker daemon."""
    return Path(__file__).read_text(encoding="ascii")


def probe_command(docker_prefix, image, environment, name, label):
    if environment not in ENVIRONMENTS:
        raise ValueError(f"unknown Golden environment: {environment}")
    return [
        *docker_prefix,
        "run",
        "--pull=never",
        "--rm",
        "-i",
        "--name",
        name,
        "--label",
        f"{PROBE_LABEL}={label}",
        "--privileged",
        "--network",
        "none",
        "--ulimit",
        "nofile=1048576:1048576",
        image,
        "python3",
        "-u",
        "-",
        environment,
    ]


def cleanup_container(
        docker_prefix, name, *, env=None, runner=subprocess.run,
        timeout=CLEANUP_TIMEOUT_SECONDS):
    return runner(
        [*docker_prefix, "rm", "-f", name],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )


def run_probe(
        docker_prefix, image, environment, *, env=None, timeout=120,
        runner=subprocess.run, capture_output=False, label=None):
    """Run the independent probe and remove its named container after failures."""
    label = label or uuid.uuid4().hex
    name = f"golden-probe-{label[:16]}"
    command = probe_command(docker_prefix, image, environment, name, label)
    print(
        f"GOLDEN_PROBE START image={image} environment={environment} "
        f"container={name} command={shlex.join(command)}",
        flush=True,
    )
    try:
        result = runner(
            command,
            input=probe_source(),
            text=True,
            check=True,
            timeout=timeout,
            env=env,
            capture_output=capture_output,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        try:
            cleanup = cleanup_container(
                docker_prefix, name, env=env, runner=runner,
                timeout=min(CLEANUP_TIMEOUT_SECONDS, timeout),
            )
            cleanup_output = (
                (cleanup.stdout or "") + (cleanup.stderr or "")
            )
            if (cleanup.returncode != 0
                    and "No such container" not in cleanup_output):
                print(
                    "GOLDEN_PROBE CLEANUP_ERROR "
                    f"container={name} rc={cleanup.returncode}: "
                    f"{cleanup_output.strip()}",
                    file=sys.stderr,
                    flush=True,
                )
        except (OSError, subprocess.TimeoutExpired) as cleanup_error:
            print(
                f"GOLDEN_PROBE CLEANUP_ERROR container={name}: {cleanup_error}",
                file=sys.stderr,
                flush=True,
            )
        raise
    print(
        f"GOLDEN_PROBE PASS image={image} environment={environment} "
        f"container={name}",
        flush=True,
    )
    return result


def _run(*command, check=True, timeout=30):
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    print(result.stdout, end="", flush=True)
    if check:
        result.check_returncode()
    return result.stdout.strip()


def _check_process_limits(limits=None):
    if limits is None:
        import resource as limits
    soft, hard = limits.getrlimit(limits.RLIMIT_NOFILE)
    if soft == limits.RLIM_INFINITY or not 16384 <= soft <= 65536:
        raise ProbeError(
            f"Mininet-normalized RLIMIT_NOFILE soft limit is {soft}; "
            "required range is 16384..65536"
        )
    if hard != limits.RLIM_INFINITY and hard < 16384:
        raise ProbeError(
            f"RLIMIT_NOFILE hard limit is {hard}; required minimum is 16384"
        )
    print(
        "GOLDEN_PROBE_LIMITS",
        json.dumps({"nofile_soft": soft, "nofile_hard": hard}, sort_keys=True),
        flush=True,
    )


def _check_openflow_imports():
    from os_ken.ofproto import ofproto_v1_3, ofproto_v1_3_parser

    if (ofproto_v1_3.OFP_VERSION != 4
            or not callable(ofproto_v1_3_parser.OFPHello)):
        raise ProbeError("os-ken OpenFlow 1.3 imports are unusable")
    print("GOLDEN_PROBE_OPENFLOW version=1.3", flush=True)


def _check_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot

    figure, axes = pyplot.subplots()
    try:
        axes.plot([0, 1], [0, 1])
        output = io.BytesIO()
        figure.savefig(output, format="png")
        if not output.getvalue().startswith(b"\x89PNG\r\n\x1a\n"):
            raise ProbeError("matplotlib did not render a PNG")
    finally:
        pyplot.close(figure)
    print("GOLDEN_PROBE_MATPLOTLIB renderer=Agg", flush=True)


def _report_kernel_module_state(environment):
    loaded = set()
    modules_file = Path("/proc/modules")
    if modules_file.is_file():
        loaded = {
            line.split()[0]
            for line in modules_file.read_text(encoding="ascii").splitlines()
            if line.split()
        }
    unreported = []
    for module in PROFILE_MODULES[environment]:
        if module in loaded:
            state = "loaded"
        elif Path("/sys/module", module).is_dir():
            state = "builtin-or-present"
        else:
            state = "unreported"
            unreported.append(module)
        print(
            f"GOLDEN_PROBE_MODULE module={module} state={state}",
            flush=True,
        )
    if unreported:
        print(
            "GOLDEN_PROBE_MODULE_INFO built-in features may have no module "
            "metadata; the active capability checks must determine support: "
            + ", ".join(unreported),
            flush=True,
        )
    return tuple(unreported)


def _check_datapath(environment):
    configured = _run(
        "ovs-vsctl", "--timeout=10", "get", "Bridge", "s1", "datapath_type"
    ).strip('"[]')
    details = _run("ovs-appctl", "dpif/show")
    if environment in ("toolchain", "controller"):
        if (configured != "netdev" or "netdev@" not in details
                or "s1" not in details):
            raise ProbeError(
                "OVS did not create the required userspace netdev datapath"
            )
        actual = "netdev"
    else:
        if configured not in ("", "system"):
            raise ProbeError(
                f"OVS bridge s1 selected {configured!r}, not the kernel datapath"
            )
        if "system@" not in details or "s1" not in details:
            fallback = _run("ovs-dpctl", "show", check=False)
            if "lookups:" not in fallback or "s1" not in fallback:
                raise ProbeError(
                    "OVS did not create the required kernel system datapath"
                )
        actual = "system"
    print(
        f"GOLDEN_PROBE_DATAPATH configured={configured or 'system'} "
        f"actual={actual}",
        flush=True,
    )


def _disable_userspace_offloads(hosts):
    for host in hosts:
        _run(
            "mnexec",
            "-a",
            str(host.pid),
            "ethtool",
            "-K",
            host.defaultIntf().name,
            "tx",
            "off",
            "tso",
            "off",
            "gso",
            "off",
            "gro",
            "off",
        )


def _check_tcp(client, server_host):
    server = server_host.popen(
        ["iperf3", "--server", "--one-off"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            if server.poll() is not None:
                output, _ = server.communicate()
                raise ProbeError(f"iperf3 server exited before listening: {output}")
            listeners = _run(
                "mnexec",
                "-a",
                str(server_host.pid),
                "ss",
                "-H",
                "-ltn",
                "sport",
                "=",
                ":5201",
            )
            if listeners:
                break
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("iperf3 server listen", 10)
            time.sleep(0.1)
        result = json.loads(_run(
            "mnexec",
            "-a",
            str(client.pid),
            "iperf3",
            "--client",
            server_host.IP(),
            "--time",
            "1",
            "--connect-timeout",
            "3000",
            "--json",
        ))
        if "error" in result:
            raise ProbeError(f"iperf3 client failed: {result['error']}")
        received = result["end"]["sum_received"]["bytes"]
        if type(received) is not int or received <= 0:
            raise ProbeError(f"iperf3 received invalid byte count: {received!r}")
        server_output, _ = server.communicate(timeout=10)
        print(server_output, end="", flush=True)
        if server.returncode != 0:
            raise ProbeError(f"iperf3 server returned {server.returncode}")
        return received
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()


def _check_measurement_features(host):
    pid = str(host.pid)
    interface = host.defaultIntf().name
    prefix = ("mnexec", "-a", pid)
    available = _run(
        *prefix, "sysctl", "-n", "net.ipv4.tcp_available_congestion_control"
    ).split()
    if "bbr" not in available:
        raise ProbeError(
            "BBR is not available in net.ipv4.tcp_available_congestion_control"
        )
    _run(
        *prefix, "sysctl", "-qw", "net.ipv4.tcp_congestion_control=bbr"
    )
    selected = _run(
        *prefix, "sysctl", "-n", "net.ipv4.tcp_congestion_control"
    )
    if selected != "bbr":
        raise ProbeError(f"BBR selection read back as {selected!r}")

    _run(*prefix, "tc", "qdisc", "del", "dev", interface, "root", check=False)
    try:
        _run(
            *prefix, "tc", "qdisc", "add", "dev", interface,
            "root", "handle", "10:", "netem", "delay", "1ms",
        )
        if "netem" not in _run(
                *prefix, "tc", "qdisc", "show", "dev", interface):
            raise ProbeError("netem qdisc was not created")
        _run(*prefix, "tc", "qdisc", "del", "dev", interface, "root")
        _run(
            *prefix, "tc", "qdisc", "add", "dev", interface,
            "root", "handle", "1:", "htb", "default", "10",
        )
        _run(
            *prefix, "tc", "class", "add", "dev", interface,
            "parent", "1:", "classid", "1:10", "htb", "rate", "10mbit",
        )
        _run(
            *prefix, "tc", "qdisc", "add", "dev", interface,
            "parent", "1:10", "handle", "20:", "fq_codel",
        )
        qdiscs = _run(
            *prefix, "tc", "qdisc", "show", "dev", interface
        )
        classes = _run(
            *prefix, "tc", "class", "show", "dev", interface
        )
        if "fq_codel" not in qdiscs or "htb" not in classes:
            raise ProbeError("HTB/fq_codel qdisc stack was not created")
    finally:
        _run(
            *prefix, "tc", "qdisc", "del", "dev", interface,
            "root", check=False,
        )
    print(
        "GOLDEN_PROBE_MEASUREMENT bbr=available "
        "qdiscs=netem,htb,fq_codel",
        flush=True,
    )


def _router_namespace_command(namespace, *command, check=True):
    return _run(
        "ip", "netns", "exec", namespace, *command, check=check
    )


def _configure_router_namespace(namespace):
    _run("ip", "-n", namespace, "link", "set", "lo", "up")
    _router_namespace_command(
        namespace, "sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=0"
    )
    _router_namespace_command(
        namespace, "sysctl", "-qw", "net.ipv6.conf.default.disable_ipv6=0"
    )
    _router_namespace_command(
        namespace, "sysctl", "-qw", "net.ipv6.conf.all.accept_dad=0"
    )
    _router_namespace_command(
        namespace, "sysctl", "-qw", "net.ipv6.conf.default.accept_dad=0"
    )


def _check_vrouter_features():
    _run("/usr/lib/frr/zebra", "--version")
    _run("/usr/lib/frr/bgpd", "--version")
    namespaces = ("golden-left", "golden-router", "golden-right")
    for namespace in reversed(namespaces):
        _run("ip", "netns", "del", namespace, check=False)
    try:
        for namespace in namespaces:
            _run("ip", "netns", "add", namespace)
            _configure_router_namespace(namespace)

        _run("ip", "link", "add", "golden-l0", "type", "veth",
             "peer", "name", "golden-r0")
        _run("ip", "link", "add", "golden-r1", "type", "veth",
             "peer", "name", "golden-x0")
        _run("ip", "link", "set", "golden-l0", "netns", "golden-left")
        _run("ip", "link", "set", "golden-r0", "netns", "golden-router")
        _run("ip", "link", "set", "golden-r1", "netns", "golden-router")
        _run("ip", "link", "set", "golden-x0", "netns", "golden-right")

        interfaces = (
            ("golden-left", "golden-l0", "10.253.1.2/24",
             "fd00:253:1::2/64"),
            ("golden-router", "golden-r0", "10.253.1.1/24",
             "fd00:253:1::1/64"),
            ("golden-router", "golden-r1", "10.253.2.1/24",
             "fd00:253:2::1/64"),
            ("golden-right", "golden-x0", "10.253.2.2/24",
             "fd00:253:2::2/64"),
        )
        for namespace, interface, ipv4, ipv6 in interfaces:
            _run("ip", "-n", namespace, "addr", "add", ipv4,
                 "dev", interface)
            _run("ip", "-n", namespace, "-6", "addr", "add", ipv6,
                 "dev", interface, "nodad")
            _run("ip", "-n", namespace, "link", "set", interface, "up")

        _router_namespace_command(
            "golden-router", "sysctl", "-qw", "net.ipv4.ip_forward=1"
        )
        _router_namespace_command(
            "golden-router", "sysctl", "-qw",
            "net.ipv6.conf.all.forwarding=1",
        )
        _run("ip", "-n", "golden-left", "route", "add",
             "10.253.2.0/24", "via", "10.253.1.1")
        _run("ip", "-n", "golden-right", "route", "add",
             "10.253.1.0/24", "via", "10.253.2.1")
        _run("ip", "-n", "golden-left", "-6", "route", "add",
             "fd00:253:2::/64", "via", "fd00:253:1::1")
        _run("ip", "-n", "golden-right", "-6", "route", "add",
             "fd00:253:1::/64", "via", "fd00:253:2::1")
        _router_namespace_command(
            "golden-left", "ping", "-c", "2", "-W", "2", "10.253.2.2"
        )
        _router_namespace_command(
            "golden-left", "ping", "-6", "-c", "2", "-W", "2",
            "fd00:253:2::2",
        )

        _run(
            "ip", "-n", "golden-left", "link", "add", "vxlan42",
            "type", "vxlan", "id", "42", "local", "10.253.1.2",
            "remote", "10.253.1.1", "dstport", "4789",
            "dev", "golden-l0", "nolearning",
        )
        vxlan = _run(
            "ip", "-d", "-n", "golden-left", "link", "show", "vxlan42"
        )
        if "vxlan id 42" not in vxlan:
            raise ProbeError("VXLAN device did not report VNI 42")
        print(
            "GOLDEN_PROBE_VROUTER frr=zebra,bgpd forwarding=ipv4,ipv6 "
            "vxlan=vni42",
            flush=True,
        )
    finally:
        for namespace in reversed(namespaces):
            _run("ip", "netns", "del", namespace, check=False)


def _inside_main(environment):
    if environment not in ENVIRONMENTS:
        raise ProbeError(f"unknown Golden environment: {environment}")

    from functools import partial
    from mininet.net import Mininet
    from mininet.node import OVSSwitch
    from mininet.topo import SingleSwitchTopo

    for command in (
        ("ovs-vsctl", "--version"),
        ("ovs-vswitchd", "--version"),
        ("iperf3", "--version"),
        ("tc", "-V"),
        ("ethtool", "--version"),
    ):
        _run(*command)
    _run("ovs-vsctl", "--timeout=10", "show")
    _report_kernel_module_state(environment)

    if environment in ("controller", "vrouter"):
        _check_openflow_imports()
    if environment == "measurement":
        _check_matplotlib()
    if environment == "vrouter":
        _check_vrouter_features()

    datapath = (
        "user" if environment in ("toolchain", "controller") else "kernel"
    )
    switch = partial(
        OVSSwitch, datapath=datapath, failMode="standalone",
        protocols="OpenFlow13",
    )
    net = Mininet(
        topo=SingleSwitchTopo(k=2),
        switch=switch,
        controller=None,
        autoSetMacs=True,
        build=False,
    )
    received = 0
    loss = 100
    try:
        # Mininet construction runs its patched fixLimits before this check.
        _check_process_limits()
        net.build()
        net.start()
        _check_datapath(environment)
        if datapath == "user":
            _disable_userspace_offloads(net.hosts)
        loss = net.pingAll(timeout="3")
        if loss != 0:
            raise ProbeError(f"known-good Mininet topology lost {loss}% of pings")
        received = _check_tcp(net.hosts[0], net.hosts[1])
        if environment == "measurement":
            _check_measurement_features(net.hosts[0])
    finally:
        net.stop()

    print(
        "GOLDEN_PROBE_RESULT",
        json.dumps({
            "environment": environment,
            "ping_loss_percent": loss,
            "status": "PASS",
            "tcp_received_bytes": received,
        }, sort_keys=True),
        flush=True,
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ENVIRONMENTS:
        print(
            "usage: probe.py {toolchain|controller|measurement|vrouter}",
            file=sys.stderr,
        )
        return 2
    try:
        _inside_main(argv[0])
    except (
            OSError, ValueError, KeyError, ProbeError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"GOLDEN_PROBE_FAIL: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
