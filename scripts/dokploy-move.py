#!/usr/bin/env python3
"""Move a single-node Dokploy host over SSH. Python 3.10+, no pip packages.

Run --help and read docs/dokploy-server-migration.md before use.
The destination must be an EMPTY Linux Docker host. No live deployment is
contacted by tests. A warm copy is never considered a restorable backup.
"""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid


STATE_DIR = Path("/var/lib/dokploy-move")
STATE_FILE = STATE_DIR / "state.json"
BASE_PATHS = ("/var/lib/docker", "/var/lib/containerd", "/etc/dokploy")
UNITS = ("docker.service", "docker.socket", "containerd.service")
HOST_MOUNTS = {"/var/run/docker.sock", "/run/docker.sock", "/etc/localtime", "/etc/timezone"}
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=4")
ACTIVE_PHASES = {"activating", "active"}


class Refusal(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def run(args, *, allowed=(0,), timeout=120, input_text=None):
    """Never echo Docker inspect output, credentials, or subprocess stderr."""
    try:
        result = subprocess.run(args, input=input_text, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal(f"{Path(args[0]).name} could not finish; check its installation/connectivity.") from error
    require(result.returncode in allowed,
            f"{Path(args[0]).name} failed (exit {result.returncode}); no success was recorded. "
            "Inspect the service locally; command output is withheld to protect secrets.")
    return result.stdout.strip()


def docker(*args):
    # Ignore DOCKER_HOST, contexts and user aliases: this must be the LOCAL engine.
    return run(["docker", "--host", "unix:///var/run/docker.sock", *args])


def docker_json(*args):
    return json.loads(docker(*args))


def validate_target(value):
    require(re.fullmatch(r"root@(?:[a-zA-Z0-9][a-zA-Z0-9.-]*|\[[0-9a-fA-F:]+\])", value),
            "Use root@HOST (or root@[IPv6]); configure SSH keys/port in /root/.ssh/config.")
    return value


def safe_path(value):
    path = PurePosixPath(value)
    require(str(path) == value and path.is_absolute() and ".." not in path.parts,
            "A data path is not a canonical absolute path.")
    require(not any(c in value for c in ("\n", "\r", "\0", ":")), "Unsupported data path.")
    allowed = ("/srv/", "/opt/", "/home/", "/mnt/", "/media/", "/data/", "/var/lib/")
    require(value == "/etc/dokploy" or value.startswith("/etc/dokploy/")
            or value.startswith(allowed), "A bind mount includes a host system path; migrate it separately.")
    blocked = ("/var/lib/dokploy-move", "/var/lib/systemd", "/var/lib/apt", "/var/lib/dpkg")
    require(not any(value == x or value.startswith(x + "/") or x.startswith(value + "/")
                    for x in blocked), "A data path overlaps host or migration state.")
    return path


def no_symlink_ancestors(value):
    path = Path(str(safe_path(value)))
    require(not any(p.is_symlink() for p in (path, *path.parents)),
            "A data path has a symbolic-link ancestor; use a dedicated ordinary directory.")
    return path


def collapse_paths(values):
    result = []
    for value in sorted(set(values), key=lambda x: (len(PurePosixPath(x).parts), x)):
        safe_path(value)
        if not any(value == p or value.startswith(p + "/") for p in result):
            result.append(value)
    return sorted(result)


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def local_requirements():
    require(sys.platform == "linux" and os.geteuid() == 0, "Run on the old Linux server as root.")
    for binary in ("docker", "containerd", "ctr", "rsync", "ssh", "systemctl", "findmnt", "du", "hostnamectl", "ip"):
        require(shutil.which(binary), f"Install {binary} before migration.")
    require(Path("/run/systemd/system").is_dir(), "A systemd Docker host is required.")
    require(not Path("/.dockerenv").exists(), "Run on the host, not inside a container.")


def check_address(address):
    parsed = ipaddress.ip_address(address)
    require(parsed.version == 4 and not (parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast),
            "Use the destination's static IPv4 address.")
    interfaces = json.loads(run(["ip", "-j", "address", "show"]))
    addresses = {a["local"] for interface in interfaces for a in interface.get("addr_info", [])}
    require(address in addresses, "Swarm address is not assigned to the destination. Use its local static IPv4 address.")


def unit_state(unit):
    return run(["systemctl", "is-active", unit], allowed=(0, 3, 4))


def assert_stopped():
    for unit in UNITS:
        require(unit_state(unit) in {"inactive", "failed", "unknown"}, "Docker/containerd is still active.")
    # Stopping a daemon alone is not proof that a live-restore workload stopped.
    for process in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = process.read_text().strip()
        except FileNotFoundError:
            continue
        require(not name.startswith(("containerd-shim", "dockerd", "containerd", "docker-proxy")),
                "A container runtime process remains; the final copy is refused.")


def stop_engine():
    # Persistent masking also prevents a reboot from starting a partial clone or
    # a second copy of the source cluster. Never automatically unmask on failure.
    run(["systemctl", "mask", *UNITS])
    run(["systemctl", "stop", "docker.socket", "docker.service"], timeout=300)
    run(["systemctl", "stop", "containerd.service"], timeout=180)
    assert_stopped()
    os.sync()


def start_engine():
    run(["systemctl", "unmask", *UNITS])
    run(["systemctl", "start", "containerd.service", "docker.service"], timeout=180)


def mount_checks(paths, *, cold=False):
    mounts = json.loads(run(["findmnt", "--json", "--list", "--output", "TARGET,FSTYPE"]))["filesystems"]
    for value in paths:
        path = no_symlink_ancestors(value)
        require(path.exists(), "A required data path is missing.")
        fs = run(["findmnt", "-n", "-o", "FSTYPE", "-T", value])
        require(fs in {"ext4", "xfs"}, "Only local ext4/xfs data filesystems are supported.")
        for mount in mounts:
            if mount["target"].startswith(value + "/"):
                runtime_overlay = mount["fstype"] == "overlay" and value in BASE_PATHS[:2]
                runtime_shm = mount["fstype"] == "tmpfs" and re.fullmatch(
                    r"/var/lib/docker/containers/[a-f0-9]{64}/mounts/shm", mount["target"])
                require((runtime_overlay or runtime_shm) and not cold,
                        "Nested data mounts need a separate migration; a complete copy cannot be proved.")


def inventory(source):
    local_requirements()
    info = docker_json("info", "--format", "{{json .}}")
    require(info["DockerRootDir"] == "/var/lib/docker", "Custom Docker data-root is not supported.")
    require(info["Driver"] in {"overlay2", "overlayfs"}, "Only Docker overlay2/overlayfs is supported.")
    require(not info.get("LiveRestoreEnabled"), "Disable Docker live-restore before this migration.")
    security = " ".join(info.get("SecurityOptions", []))
    require(not any(x in security for x in ("rootless", "userns", "selinux")),
            "Rootless, user namespace remapping and SELinux hosts need a separate migration.")
    config = run(["containerd", "config", "dump"])
    match = re.search(r'^root\s*=\s*[\'\"]([^\'\"]+)', config, re.M)
    require(match and match[1] == "/var/lib/containerd", "Custom containerd root is not supported.")
    namespaces = run(["ctr", "namespaces", "list", "--quiet"]).splitlines()
    require(set(namespaces) <= {"moby"}, "This containerd also runs non-Docker workloads.")
    ids = docker("ps", "-aq").splitlines()
    containers = docker_json("inspect", *ids) if ids else []
    volume_ids = docker("volume", "ls", "-q").splitlines()
    volumes = docker_json("volume", "inspect", *volume_ids) if volume_ids else []
    for volume in volumes:
        require(volume["Driver"] == "local" and not volume.get("Options"),
                "Network, plugin or bind-backed volumes need a separate migration.")
        require(volume["Mountpoint"].startswith("/var/lib/docker/volumes/"), "Unexpected volume location.")
    require(not docker("plugin", "ls", "-q"), "Docker plugins require a separate migration.")
    services = []
    if source:
        require(Path("/etc/dokploy").is_dir(), "Dokploy configuration was not found.")
        require(info["Swarm"].get("ControlAvailable"), "The source must be a Dokploy Swarm manager.")
        nodes = docker("node", "ls", "-q").splitlines()
        require(len(nodes) == 1, "Multi-node Swarms are refused; this tool moves one complete host.")
        swarm = docker_json("swarm", "inspect")[0]
        require(not swarm["Spec"].get("EncryptionConfig", {}).get("AutoLockManagers"),
                "Swarm autolock is enabled; use a separately rehearsed recovery procedure.")
        service_ids = docker("service", "ls", "-q").splitlines()
        services = docker_json("service", "inspect", *service_ids) if service_ids else []
        require(any(s["Spec"]["Name"] == "dokploy" for s in services), "The Dokploy service was not found.")
        require(all(set(s["Spec"]["Mode"]) <= {"Replicated", "Global"} for s in services),
                "Swarm job modes require a separate migration.")
    else:
        require(not containers and not volumes and not info.get("Images"),
                "Destination has containers, volumes or images. Use a fresh empty Docker server.")
        require(info["Swarm"]["LocalNodeState"] == "inactive", "Destination already belongs to a Swarm.")
        require(not Path("/etc/dokploy").exists(), "Dokploy is already installed on the destination.")
        require(set(docker("network", "ls", "--format", "{{.Name}}").splitlines()) <= {"bridge", "host", "none"},
                "Destination has custom Docker networks; use an empty server.")
    paths = list(BASE_PATHS if source else BASE_PATHS[:2])
    mounts = [m for c in containers for m in c.get("Mounts", [])]
    mounts += [m for s in services for m in s["Spec"]["TaskTemplate"].get("ContainerSpec", {}).get("Mounts", [])]
    for mount in mounts:
        if mount["Type"] == "bind" and mount["Source"] not in HOST_MOUNTS:
            paths.append(mount["Source"])
    for container in containers:
        for bindings in (container.get("HostConfig", {}).get("PortBindings") or {}).values():
            require(all(b.get("HostIp", "") in {"", "0.0.0.0", "::", "127.0.0.1", "::1"}
                        for b in bindings or []), "A container port binds to an old host IP; reconfigure it first.")
        require(not container.get("HostConfig", {}).get("Devices"), "Host device passthrough is not portable.")
        labels = container["Config"].get("Labels") or {}
        require(container["State"]["Running"] or labels.get("com.docker.swarm.service.id")
                or container["HostConfig"].get("RestartPolicy", {}).get("Name") != "always",
                "A stopped standalone container uses restart=always and would start on migration. "
                "Change its policy to unless-stopped first.")
    paths = collapse_paths(paths)
    mount_checks(paths)
    # Compare daemon configuration and service commands; never copy OS identity,
    # SSH host keys, networking, firewalls, or package-manager state.
    compatibility = {"arch": info["Architecture"], "engine": info["ServerVersion"], "driver": info["Driver"],
                     "driver_status": info.get("DriverStatus"), "security": info.get("SecurityOptions"),
                     "os": file_digest("/etc/os-release"), "containerd": run(["containerd", "--version"]),
                     "daemon_config": file_digest("/etc/docker/daemon.json"),
                     "containerd_config": file_digest("/etc/containerd/config.toml")}
    for unit in ("docker.service", "containerd.service"):
        location = run(["systemctl", "show", "--property=FragmentPath", "--value", unit])
        require(location.startswith(("/usr/lib/systemd/", "/lib/systemd/")),
                "Use package-installed Docker/containerd systemd units.")
        compatibility[unit] = hashlib.sha256(run(["systemctl", "cat", unit]).encode()).hexdigest()
    sizes = {p: int(run(["du", "-sx", "--block-size=1", p]).split()[0]) for p in paths}
    standalone = [c["Id"] for c in containers if c["State"]["Running"]
                  and not (c["Config"].get("Labels") or {}).get("com.docker.swarm.service.id")]
    return {"machine": file_digest("/etc/machine-id"), "hostname": run(["hostnamectl", "--static"]),
            "compatibility": compatibility, "paths": paths, "sizes": sizes,
            "standalone": standalone, "services": sorted(s["Spec"]["Name"] for s in services),
            "containers": len(containers), "volumes": len(volumes),
            "types": {p: "directory" if Path(p).is_dir() else "file" for p in paths}}


def compatible(source, target):
    require(source["machine"] != target["machine"], "Source and destination have the same machine identity.")
    differences = [k for k, v in source["compatibility"].items() if target["compatibility"].get(k) != v]
    require(not differences, "Destination must match source Docker/OS configuration: " + ", ".join(differences))


def write_state(state):
    require(not STATE_DIR.is_symlink() and not STATE_FILE.is_symlink(), "Unsafe migration state path.")
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    temporary = STATE_DIR / "state.new"
    require(not temporary.is_symlink(), "Unsafe temporary state path.")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(state, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(STATE_FILE)
    os.sync()


def read_state():
    require(STATE_FILE.is_file() and not STATE_FILE.is_symlink(), "No migration is prepared on this host.")
    state = json.loads(STATE_FILE.read_text())
    require(state.get("format") == 1, "Unsupported migration state format.")
    return state


def source_identity(state):
    require(file_digest("/etc/machine-id") == state["source"]["machine"],
            "Run this migration from its original source host.")


def check_pair(state, payload):
    require(state["id"] == payload["id"] and state["source"]["machine"] == payload["source"]["machine"]
            and state["target_machine"] == payload["target_machine"], "Migration identity mismatch.")
    require(file_digest("/etc/machine-id") == state["target_machine"], "Destination identity changed.")


def reserve_paths(source):
    # Called while the destination is still pristine, BEFORE stopping anything.
    devices = {}
    for value in source["paths"]:
        path = no_symlink_ancestors(value)
        if value not in BASE_PATHS[:2]:
            require(not path.exists(), "A destination bind/config path already exists; nothing was replaced.")
        parent = path if path.exists() else path.parent
        while not parent.exists():
            parent = parent.parent
        require(run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(parent)]) in {"ext4", "xfs"},
                "Destination data must use local ext4/xfs filesystems.")
        device = parent.stat().st_dev
        required, available = devices.get(device, (0, shutil.disk_usage(parent).free))
        devices[device] = (required + source["sizes"][value], available)
    for required, available in devices.values():
        require(available > required * 1.2 + 1024 ** 3, "Destination lacks space (data + 20% + 1 GiB required).")


def check_remaining_space(source, prepared_sizes):
    """Recheck growth/headroom after the warm copy, before source downtime.

    This is a capacity gate, not a guarantee against later writes or the
    temporary space rsync may need for a very large changed file.
    """
    devices = {}
    for value in source["paths"]:
        path = no_symlink_ancestors(value)
        require(path.exists(), "A prepared destination data path is missing.")
        require(path.is_dir() == (source["types"][value] == "directory"),
                "A prepared destination data path changed type.")
        require(run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)]) in {"ext4", "xfs"},
                "Destination data must use local ext4/xfs filesystems.")
        device = path.stat().st_dev
        total, growth, available = devices.get(device, (0, 0, shutil.disk_usage(path).free))
        size = source["sizes"][value]
        devices[device] = (total + size, growth + max(0, size - prepared_sizes[value]), available)
    for total, growth, available in devices.values():
        require(available > growth + total * 0.2 + 1024 ** 3,
                "Destination lacks remaining space for source growth + 20% + 1 GiB. "
                "Source was not stopped; free space or enlarge the destination before cutover.")


def remote(target, action, payload=None):
    validate_target(target)
    encoded = base64.b64encode(json.dumps(payload or {}).encode()).decode()
    # Send this exact single file over SSH stdin; no remote installation required.
    command = shlex.join(["python3", "-", "_rpc", action, encoded])
    output = run(["ssh", *SSH_OPTIONS, target, command], input_text=Path(__file__).read_text(encoding="utf-8"),
                 timeout=720)
    response = json.loads(output)
    require("error" not in response, response.get("error", "Remote operation refused."))
    return response["result"]


def rsync_args(target, path, kind, *, cold=False, verify=False):
    validate_target(target)
    safe_path(path)
    args = ["rsync", "-aHAXS", "--numeric-ids", "--one-file-system", "--protect-args",
            "--timeout=120", "-e", shlex.join(["ssh", *SSH_OPTIONS])]
    if path == "/var/lib/docker":
        args.append("--exclude=/overlay2/*/merged/***")
    if cold or verify:
        # A warm copy can contain a torn file with unchanged size/mtime.
        args.append("--checksum")
    if verify:
        args.extend(["--dry-run", "--itemize-changes", "--out-format=%i"])
    if kind == "directory":
        args.extend(["--delete-delay", path + "/", target + ":" + path + "/"])
    else:
        require(kind == "file", "Unsupported data entry type.")
        args.extend([path, target + ":" + path])
    return args


def transfer(target, source, *, cold=False, verify=False):
    for index, path in enumerate(source["paths"], 1):
        print(f"{'Verifying' if verify else 'Copying'} data location {index}/{len(source['paths'])}...", flush=True)
        result = run(rsync_args(target, path, source["types"][path], cold=cold, verify=verify),
                     allowed=(0,) if cold or verify else (0, 24), timeout=None)
        require(not verify or not result, "Final byte/metadata comparison differs. Keep both engines stopped and retry cutover.")


def wait_for_engine():
    for _ in range(60):
        try:
            docker("info", "--format", "{{.ServerVersion}}")
            return
        except Refusal:
            time.sleep(2)
    raise Refusal("Destination Docker did not become ready; source must remain stopped.")


def workload_readiness(source):
    """Container health and replica readiness, not end-to-end application acceptance."""
    for _ in range(60):
        lines = docker("service", "ls", "--format", "{{json .}}").splitlines()
        replicas = [json.loads(line)["Replicas"] for line in lines]
        ready = all(re.match(r"^(\d+)/\1(?:\s|$)", value) for value in replicas)
        ids = docker("ps", "-q").splitlines()
        containers = docker_json("inspect", *ids) if ids else []
        running = {c["Id"] for c in containers}
        ready = ready and set(source["standalone"]) <= running
        ready = ready and all(c["State"].get("Health", {}).get("Status", "healthy") == "healthy"
                              for c in containers)
        if ready:
            return
        time.sleep(3)
    raise Refusal("Destination workloads did not become ready within 180 seconds. "
                  "Inspect Docker health/logs on the new host; do not resume the old host.")


def activate(state, address):
    check_address(address)
    assert_stopped()
    # Mark BEFORE engine start: even a failed start may have run app workers.
    state["phase"] = "activating"
    write_state(state)
    run(["hostnamectl", "set-hostname", state["source"]["hostname"]])
    start_engine()
    wait_for_engine()
    docker("swarm", "init", "--force-new-cluster", "--advertise-addr", address,
           "--listen-addr", "0.0.0.0:2377")
    for container in state["source"]["standalone"]:
        docker("start", container)
    actual = sorted(docker("service", "ls", "--format", "{{.Name}}").splitlines())
    require(actual == state["source"]["services"], "Swarm service inventory differs; source must remain stopped.")
    workload_readiness(state["source"])
    state["phase"] = "active"
    write_state(state)
    return {"phase": "active", "services": len(actual)}


def rpc(action, payload):
    local_requirements()
    if action == "inventory":
        require(not STATE_FILE.exists(), "Destination already has migration state; resume the existing migration.")
        return inventory(False)
    if action == "preflight":
        require(not STATE_FILE.exists(), "Destination already has migration state; resume the existing migration.")
        target = inventory(False)
        compatible(payload["source"], target)
        reserve_paths(payload["source"])
        return target
    if action == "reserve":
        if STATE_FILE.exists():
            state = read_state()
            check_pair(state, payload)
            require(state["phase"] in {"reserving", "reserved"}, "Destination is already in a later phase.")
            require(state["source"]["paths"] == payload["source"]["paths"]
                    and state["source"]["types"] == payload["source"]["types"], "Reserved paths changed.")
        else:
            target = inventory(False)
            compatible(payload["source"], target)
            require(target["machine"] == payload["target_machine"], "Destination machine changed.")
            reserve_paths(payload["source"])
            state = dict(payload, phase="reserving")
            write_state(state)  # Recovery identity exists even if a service stop fails.
        if state["phase"] == "reserving":
            stop_engine()
        else:
            assert_stopped()
        for value in payload["source"]["paths"]:
            path = no_symlink_ancestors(value)
            if payload["source"]["types"][value] == "directory":
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        mount_checks([p for p in payload["source"]["paths"] if Path(p).is_dir()], cold=True)
        state["phase"] = "reserved"
        write_state(state)
        return {"phase": "reserved"}
    state = read_state()
    check_pair(state, payload)
    if action == "status":
        return {"phase": state["phase"], "docker": unit_state("docker.service")}
    require(state["phase"] not in ACTIVE_PHASES, "Destination activation has begun; recopy/automatic rollback is refused.")
    if action == "capacity":
        assert_stopped()
        require(state["source"]["paths"] == payload["source"]["paths"]
                and state["source"]["types"] == payload["source"]["types"], "Reserved paths changed.")
        check_remaining_space(payload["source"], payload["prepared_sizes"])
        return {"capacity": True}
    if action == "stopped":
        assert_stopped()
        if payload.get("new_ip"):
            check_address(payload["new_ip"])
        for path in state["source"]["paths"]:
            no_symlink_ancestors(path)
        mount_checks([p for p in state["source"]["paths"] if Path(p).is_dir()], cold=True)
        return {"stopped": True}
    if action == "arm":
        assert_stopped()
        state["phase"] = "verified"
        state["source"]["standalone"] = payload["source"]["standalone"]
        state["source"]["services"] = payload["source"]["services"]
        write_state(state)
        return {"phase": "verified"}
    if action == "activate":
        require(state["phase"] == "verified", "Final verification has not finished.")
        return activate(state, payload["new_ip"])
    raise Refusal("Unknown remote operation.")


def prepare(target):
    if STATE_FILE.exists():
        previous = read_state()
        # A successfully moved destination can itself become the source next time.
        if previous["phase"] == "active" and previous["target_machine"] == file_digest("/etc/machine-id"):
            archive = STATE_DIR / ("completed-" + str(uuid.UUID(previous["id"])) + ".json")
            require(not archive.exists(), "Completed migration record already exists.")
            STATE_FILE.rename(archive)
    if STATE_FILE.exists():
        state = read_state()
        source_identity(state)
        require(state["target"] == target, "A different destination is already reserved.")
        require(state["phase"] in {"reserved", "prepared"}, "Use cutover/status for this migration phase.")
        fresh = inventory(True)
        require(fresh["paths"] == state["source"]["paths"] and fresh["types"] == state["source"]["types"],
                "Mount layout changed after preparation; review before continuing.")
        require(fresh["compatibility"] == state["source"]["compatibility"], "Runtime configuration changed.")
        state["source"] = fresh
        remote(target, "reserve" if state["phase"] == "reserved" else "stopped", state)
        write_state(state)
    else:
        source = inventory(True)
        destination = remote(target, "preflight", {"source": source})
        compatible(source, destination)
        state = {"format": 1, "id": str(uuid.uuid4()), "target": target,
                 "target_machine": destination["machine"], "source": source, "phase": "reserved"}
        # Store local recovery identity before making the remote mutation.
        write_state(state)
        remote(target, "reserve", state)
    transfer(target, state["source"])
    state["phase"] = "prepared"
    write_state(state)
    print("Initial copy complete. Old server is still serving; destination Docker stays stopped.")
    return state


def cutover(target, address):
    ipaddress.ip_address(address)
    state = read_state()
    source_identity(state)
    require(state["target"] == target, "Prepared destination does not match.")
    require(state["phase"] in {"prepared", "stopping", "source-stopped", "verified"},
            "Prepare first; an activated migration cannot be recopied.")
    state["new_ip"] = address
    remote(target, "stopped", state)
    if state["phase"] == "prepared":
        prepared_sizes = state["source"]["sizes"]
        fresh = inventory(True)
        require(fresh["paths"] == state["source"]["paths"] and fresh["types"] == state["source"]["types"],
                "Mount layout changed; migration refused before stopping the source.")
        require(fresh["compatibility"] == state["source"]["compatibility"], "Runtime configuration changed.")
        state["source"] = fresh
        remote(target, "capacity", dict(state, prepared_sizes=prepared_sizes))
        state["phase"] = "stopping"
        write_state(state)
    if state["phase"] == "stopping":
        print("Stopping all Docker workloads on the old server. Maintenance starts now.", flush=True)
        stop_engine()
        state["phase"] = "source-stopped"
        write_state(state)
    assert_stopped()
    mount_checks(state["source"]["paths"], cold=True)
    transfer(target, state["source"], cold=True)
    transfer(target, state["source"], verify=True)
    assert_stopped()
    remote(target, "arm", state)
    state["phase"] = "verified"
    write_state(state)
    print("Final copy verified. Starting the destination; the old server remains disabled.", flush=True)
    state["new_ip"] = address
    remote(target, "activate", state)
    state["phase"] = "active"
    write_state(state)
    print("Destination replicas and container health checked. Check login/uploads, then update "
          "Dokploy's Server IP and DNS. Do not start the old server.")


def resume_source(target):
    state = read_state()
    source_identity(state)
    require(state["target"] == target, "Prepared destination does not match.")
    require(state["phase"] in {"stopping", "source-stopped", "verified"}, "Source recovery is not applicable.")
    # An unreachable destination is NOT proof that it never started.
    remote(target, "stopped", state)
    start_engine()
    wait_for_engine()
    for container in state["source"]["standalone"]:
        docker("start", container)
    state["phase"] = "prepared"  # The warm copy is now stale; next cutover rechecks every byte.
    write_state(state)
    print("Source resumed. Next cutover must take another complete final copy.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "prepare", "cutover", "move", "status", "resume-source"):
        command = sub.add_parser(name)
        command.add_argument("target", help="root@NEW_SERVER; host key must already be verified")
        if name in {"cutover", "move"}:
            command.add_argument("--new-ip", required=True, help="new server's static IPv4 address for Swarm")
    args = parser.parse_args()
    target = validate_target(args.target)
    if hasattr(args, "new_ip"):
        require(ipaddress.ip_address(args.new_ip).version == 4, "Use the destination's static IPv4 address.")
    local_requirements()
    # Serialize operator calls; the lock remains outside all copied directories.
    import fcntl
    require(not STATE_DIR.is_symlink(), "Unsafe state directory.")
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    lockpath = STATE_DIR / "lock"
    require(not lockpath.is_symlink(), "Unsafe lock path.")
    with lockpath.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "plan":
            source = inventory(True)
            destination = remote(target, "preflight", {"source": source})
            compatible(source, destination)
            print(f"Compatible empty target. {source['containers']} containers, {source['volumes']} volumes; "
                  f"{sum(source['sizes'].values()) / 1024 ** 3:.1f} GiB allocated local data.")
            print("Data locations: " + ", ".join(source["paths"]))
            print("No bandwidth/time guarantee. Prepare first to copy most bytes before maintenance.")
        elif args.command in {"prepare", "move"}:
            prepare(target)
            if args.command == "move":
                cutover(target, args.new_ip)
        elif args.command == "cutover":
            cutover(target, args.new_ip)
        elif args.command == "resume-source":
            resume_source(target)
        else:
            state = read_state()
            require(state["target"] == target, "Destination does not match.")
            status = remote(target, "status", state)
            print(f"Source phase: {state['phase']}, Docker: {unit_state('docker.service')}; "
                  f"destination phase: {status['phase']}, Docker: {status['docker']}.")


if __name__ == "__main__":
    os.umask(0o077)
    started = time.monotonic()
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "_rpc":
            try:
                local_requirements()
                import fcntl
                require(not STATE_DIR.is_symlink(), "Unsafe state directory.")
                STATE_DIR.mkdir(mode=0o700, exist_ok=True)
                require(not (STATE_DIR / "lock").is_symlink(), "Unsafe lock path.")
                with (STATE_DIR / "lock").open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    print(json.dumps({"result": rpc(sys.argv[2], json.loads(base64.b64decode(sys.argv[3])))}))
            except (Refusal, OSError, ValueError, KeyError) as error:
                # Our errors are deliberately secret-free. System exceptions may
                # contain file data, so do not serialize their original text.
                print(json.dumps({"error": str(error) if isinstance(error, Refusal) else "Remote state/configuration error."}))
        else:
            main()
            print(f"Elapsed: {(time.monotonic() - started) / 60:.1f} minutes.")
    except (Refusal, OSError, ValueError, KeyError, KeyboardInterrupt) as error:
        print(str(error) if isinstance(error, Refusal) else "Operation interrupted or invalid state/configuration.", file=sys.stderr)
        print("Migration not reported successful. Run status. Engines may remain masked for recovery; "
              "never start both copies. See docs/dokploy-server-migration.md.", file=sys.stderr)
        sys.exit(1)
