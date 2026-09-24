# Golden environment bootstrap and pretest

This directory is the shared environment runtime distributed to every SDNFV
Golden repository. The protected `profile.json` selects one environment:

| Profile | Independent probe |
| --- | --- |
| `toolchain` | Mininet, userspace OVS datapath, ping, and TCP |
| `controller` | Toolchain probe plus os-ken OpenFlow 1.3 imports |
| `measurement` | Kernel OVS, BBR, netem, HTB, fq_codel, and matplotlib |
| `vrouter` | Kernel OVS, FRR zebra/bgpd, os-ken, IPv4/IPv6 forwarding, and VXLAN |

The probe is independent of the student's Compose file, topology, answers, and
`make up`. It uses the exact immutable course image declared by the protected
Dockerfile:

```text
ghcr.io/nycu-sdnfv/lab-base@sha256:c9b6ee4a5271038225a7ca41f541fd005e3766abf4bd70f9f924a61e24984a19
```

## Student pretest

Run:

```sh
make pretest
```

The pretest is non-scoring and does not change host configuration. It checks the client tools, Docker
Compose, selected Docker context and actual server, protected Dockerfile,
locally available immutable image, the image resource API, host resources, and
a short-lived known-good probe. It does not:

- install packages or pull/replace an image;
- run `host-prepare`, change host sysctls, or lower resource limits;
- explicitly load or unload kernel modules;
- mount the assignment or any host path into the probe;
- run a homework-dependent `make up`; or
- prune Docker or run `mn -c` on the host.

The active network probe is not a purely passive host audit. It creates a
temporary privileged container, and Linux may autoload networking modules when
OVS, TUN, veth or socket diagnostics are exercised. Those modules are shared by
the Docker engine's kernel and are not unloaded afterward. Run this command on
an authorized dedicated lab VM, **not a shared production Docker host**.

`PASS` means the named check ran successfully. `WARN` is not certification.
`FAIL` identifies a missing requirement. `SKIP` means a prerequisite prevented
that check from running; it is never treated as a pass. The command exits
nonzero for any failure or incomplete required verification.

Full validation is for a native Linux amd64 Docker Engine, including a Linux VM
hosted by PVE. Native Windows and Docker Desktop/macOS are not certified.
The multiarch image also supports Linux arm64. Its pretest reports a warning
about the full-Lab validation scope, but can pass when every required
environment check actually succeeds; that is not a claim that all arm64 Labs
have been validated.
Windows users should run the repository and Docker CLI from WSL2 connected to a
Linux Docker Engine, or use the course Linux VM.

Remote Docker contexts are diagnosed on the selected daemon. The probe script
is passed through standard input, so it never assumes that a client path exists
on a remote server.

## Explicit administrator preparation

Run remediation on the **Linux VM running the Docker Engine**. Do not run it on
the PVE hypervisor and do not run it inside a lab container.

Set the exact image:

```sh
IMAGE='ghcr.io/nycu-sdnfv/lab-base@sha256:c9b6ee4a5271038225a7ca41f541fd005e3766abf4bd70f9f924a61e24984a19'
docker pull "$IMAGE"
```

Raise only course settings that are currently too small, then verify them:

```sh
docker run --rm --privileged --network host --entrypoint python3 "$IMAGE" \
  -m lab_resources host-prepare --profile course
docker run --rm --network host --entrypoint python3 "$IMAGE" \
  -m lab_resources host-verify --profile course
```

The pretest runs only the second, read-only command. It never runs the first
command automatically.

For Measurement on an administered Linux Docker host:

```sh
sudo modprobe openvswitch
sudo modprobe sch_netem
sudo modprobe sch_htb
sudo modprobe sch_fq_codel
sudo modprobe tcp_bbr
```

For VRouter:

```sh
sudo modprobe openvswitch
sudo modprobe vxlan
```

Toolchain and Controller use the OVS userspace datapath and do not require
kernel OVS or BBR. `modprobe` also succeeds for features built into a kernel;
errors are not ignored. Never unload modules merely to run this diagnostic.
Built-in features may appear in neither `/proc/modules` nor `/sys/module`.
The pretest reports that metadata for diagnosis; actual datapath, congestion
control, qdisc, and VXLAN operations determine whether the capability works.

See the
[course image host preparation guide](https://github.com/NYCU-SDNFV/lab-images/blob/main/README.md#1-prepare-the-docker-host)
for persistent administrator configuration.

## Trusted hosted bootstrap

Canonical grading calls `prepare_course_host(profile)`. It is intentionally a
no-op unless all of these are true:

- `GITHUB_ACTIONS=true`;
- repository owner is `NYCU-SDNFV` (case-insensitive);
- `RUNNER_ENVIRONMENT=github-hosted`;
- `RUNNER_OS=Linux`; and
- Python is running on native Linux, not in a Docker/Podman job container.

On that disposable runner only, the bootstrap pins Docker to
`unix:///var/run/docker.sock`, removes Docker redirection variables, runs the
image's checked `host-prepare` and `host-verify`, loads only the profile's
required host modules with checked `sudo -n modprobe`, and runs the same
independent probe. Any preparation, verification, module, probe, or timeout
failure is an infrastructure error raised before scoring.
Explicit bootstrap configuration rejection uses `EnvironmentError`, a
`ValueError` subclass; subprocess and timeout failures retain their standard
exception types.
