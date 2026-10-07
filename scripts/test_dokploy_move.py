"""Offline migration safety tests. No SSH connection or Docker mutation is made.

On Linux, also exercises real rsync checksums, hard links, sparse files and xattrs
inside temporary directories. Run: python3 scripts/test_dokploy_move.py
"""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("dokploy_move", Path(__file__).with_name("dokploy-move.py"))
move = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(move)


def fixture():
    return {"format": 1, "id": "ba2ebce8-0512-48d3-a293-cc69e94b47cd", "target": "root@new.example",
            "target_machine": "new-machine", "phase": "prepared", "source": {
                "machine": "old-machine", "hostname": "original-host", "compatibility": {"engine": "fixture"},
                "paths": ["/var/lib/docker"], "types": {"/var/lib/docker": "directory"},
                "sizes": {"/var/lib/docker": 100}, "standalone": ["web-id"],
                "services": ["dokploy", "dokploy-postgres"]}}


class PathSafety(unittest.TestCase):
    def test_target_injection_and_non_root_are_refused(self):
        for target in ("-oProxyCommand=bad", "root@host;bad", "root@host path", "user@host", "root@host:22"):
            with self.subTest(target=target), self.assertRaises(move.Refusal):
                move.validate_target(target)

    def test_safe_target_and_data_paths(self):
        for target in ("root@new.example", "root@192.0.2.10", "root@[2001:db8::1]"):
            self.assertEqual(move.validate_target(target), target)
        for value in ("/", "/var/lib", "/etc", "/etc/shadow", "/root", "/srv/../etc", "/srv/a\nB",
                      "/srv/a:b", "/var/lib/dokploy-move/data", "/var/lib/systemd"):
            with self.subTest(path=value), self.assertRaises(move.Refusal):
                move.safe_path(value)
        self.assertEqual(str(move.safe_path("/srv/app with spaces")), "/srv/app with spaces")

    def test_nested_paths_collapse_without_prefix_confusion(self):
        self.assertEqual(move.collapse_paths(["/etc/dokploy", "/etc/dokploy/files", "/srv/a", "/srv/ab"]),
                         ["/etc/dokploy", "/srv/a", "/srv/ab"])

    def test_checksum_and_metadata_are_required_in_final_and_verification(self):
        for verify in (False, True):
            args = move.rsync_args("root@host", "/srv/data", "directory", cold=True, verify=verify)
            self.assertIn("--checksum", args)
            self.assertIn("-aHAXS", args)
            self.assertIn("--numeric-ids", args)
            self.assertIn("--delete-delay", args)
            self.assertIn("StrictHostKeyChecking=yes", " ".join(args))
            self.assertIn("ForwardAgent=no", " ".join(args))
            self.assertIn("ClearAllForwardings=yes", " ".join(args))
            self.assertEqual("--dry-run" in args, verify)
            self.assertNotIn("--inplace", args)

    def test_remote_rpc_disables_agent_and_port_forwarding(self):
        with patch.object(move, "run", return_value='{"result": {"phase": "reserved"}}') as run:
            self.assertEqual(move.remote("root@host", "status"), {"phase": "reserved"})
        args = run.call_args.args[0]
        self.assertIn("ForwardAgent=no", args)
        self.assertIn("ClearAllForwardings=yes", args)
        self.assertIn("StrictHostKeyChecking=yes", args)

    def test_file_transfer_cannot_delete_sibling_files(self):
        args = move.rsync_args("root@host", "/srv/app/settings", "file", cold=True)
        self.assertFalse(any(x.startswith("--delete") for x in args))

    def test_runtime_overlay_exclusion_only_applies_to_docker(self):
        self.assertIn("--exclude=/overlay2/*/merged/***", move.rsync_args("root@host", "/var/lib/docker", "directory"))
        self.assertFalse(any(x.startswith("--exclude") for x in move.rsync_args("root@host", "/srv/data", "directory")))

    def test_configuration_and_machine_identity_must_match(self):
        old = fixture()["source"]
        new = dict(old, machine="new-machine")
        move.compatible(old, new)
        for invalid in (old, dict(new, compatibility={"engine": "different"})):
            with self.assertRaises(move.Refusal):
                move.compatible(old, invalid)

    def test_ip_must_be_assigned_on_destination(self):
        with patch.object(move, "run", return_value=json.dumps([{"addr_info": [{"local": "192.0.2.10"}]}])):
            move.check_address("192.0.2.10")
            for value in ("192.0.2.11", "127.0.0.1", "0.0.0.0", "224.0.0.1"):
                with self.assertRaises(move.Refusal):
                    move.check_address(value)


class TransferSafety(unittest.TestCase):
    def test_only_warm_copy_accepts_vanished_files(self):
        with patch.object(move, "run", return_value="") as run:
            move.transfer("root@host", fixture()["source"])
            self.assertEqual(run.call_args.kwargs["allowed"], (0, 24))
            move.transfer("root@host", fixture()["source"], cold=True)
            self.assertEqual(run.call_args.kwargs["allowed"], (0,))

    def test_any_verification_difference_refuses_promotion(self):
        with patch.object(move, "run", return_value=">f.s......."), self.assertRaises(move.Refusal):
            move.transfer("root@host", fixture()["source"], verify=True)

    def test_subprocess_errors_never_expose_credentials(self):
        result = subprocess.CompletedProcess([], 2, "PRIVATE_SENTINEL", "PRIVATE_SENTINEL")
        with patch.object(move.subprocess, "run", return_value=result), self.assertRaises(move.Refusal) as caught:
            move.run(["docker", "inspect", "fixture"])
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))

    def test_state_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with patch.object(move, "STATE_DIR", root), patch.object(move, "STATE_FILE", root / "state.json"), \
                    patch.object(move.os, "sync", create=True):
                move.write_state(fixture())
                self.assertEqual(move.read_state(), fixture())
                self.assertFalse((root / "state.new").exists())
                if os.name != "nt":
                    self.assertEqual((root / "state.json").stat().st_mode & 0o777, 0o600)
                    self.assertEqual(root.stat().st_mode & 0o777, 0o700)


class LifecycleSafety(unittest.TestCase):
    def setUp(self):
        self.state = fixture()
        self.events = []
        self.patches = [
            patch.object(move, "read_state", side_effect=lambda: copy.deepcopy(self.state)),
            patch.object(move, "write_state", side_effect=self.save),
            patch.object(move, "inventory", side_effect=lambda source: copy.deepcopy(self.state["source"])),
            patch.object(move, "remote", side_effect=self.remote),
            patch.object(move, "stop_engine", side_effect=lambda: self.events.append("stop")),
            patch.object(move, "assert_stopped", side_effect=lambda: self.events.append("stopped")),
            patch.object(move, "mount_checks"),
            patch.object(move, "file_digest", return_value="old-machine"),
            patch.object(move, "transfer", side_effect=self.transfer),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.events.append("save:" + state["phase"])

    def remote(self, target, action, state):
        self.events.append("remote:" + action)
        return {"phase": "active" if action == "activate" else "reserved"}

    def transfer(self, target, source, **kwargs):
        self.events.append("verify" if kwargs.get("verify") else "copy")

    def test_cutover_stops_then_copies_then_verifies_then_starts(self):
        move.cutover("root@new.example", "192.0.2.10")
        self.assertLess(self.events.index("stop"), self.events.index("copy"))
        self.assertLess(self.events.index("copy"), self.events.index("verify"))
        self.assertLess(self.events.index("verify"), self.events.index("remote:activate"))
        self.assertEqual(self.state["phase"], "active")

    def test_failure_never_starts_either_copy(self):
        for fail_phase in ("copy", "verify"):
            with self.subTest(phase=fail_phase):
                self.state = fixture()
                self.events = []

                def failed_transfer(target, source, **kwargs):
                    phase = "verify" if kwargs.get("verify") else "copy"
                    if phase == fail_phase:
                        raise move.Refusal("fixture failure")

                with patch.object(move, "transfer", side_effect=failed_transfer), self.assertRaises(move.Refusal):
                    move.cutover("root@new.example", "192.0.2.10")
                self.assertNotIn("remote:activate", self.events)
                self.assertEqual(self.state["phase"], "source-stopped")

    def test_changed_mounts_refused_before_source_shutdown(self):
        changed = copy.deepcopy(self.state["source"])
        changed["paths"].append("/srv/new-data")
        with patch.object(move, "inventory", return_value=changed), self.assertRaises(move.Refusal):
            move.cutover("root@new.example", "192.0.2.10")
        self.assertNotIn("stop", self.events)

    def test_remaining_capacity_is_checked_with_fresh_sizes_before_shutdown(self):
        changed = copy.deepcopy(self.state["source"])
        changed["sizes"]["/var/lib/docker"] = 300
        with patch.object(move, "inventory", return_value=changed), \
                patch.object(move, "remote", wraps=self.remote) as remote:
            move.cutover("root@new.example", "192.0.2.10")
        capacity = next(call for call in remote.call_args_list if call.args[1] == "capacity")
        self.assertEqual(capacity.args[2]["source"]["sizes"]["/var/lib/docker"], 300)
        self.assertEqual(capacity.args[2]["prepared_sizes"]["/var/lib/docker"], 100)
        self.assertLess(self.events.index("remote:capacity"), self.events.index("stop"))

    def test_insufficient_remaining_capacity_keeps_source_live_and_prepared(self):
        def refused_capacity(target, action, state):
            if action == "capacity":
                raise move.Refusal("insufficient remaining capacity")
            return self.remote(target, action, state)

        with patch.object(move, "remote", side_effect=refused_capacity), self.assertRaises(move.Refusal):
            move.cutover("root@new.example", "192.0.2.10")
        self.assertNotIn("stop", self.events)
        self.assertNotIn("copy", self.events)
        self.assertEqual(self.state["phase"], "prepared")

    def test_interrupted_cold_copy_can_resume(self):
        self.state["phase"] = "source-stopped"
        move.cutover("root@new.example", "192.0.2.10")
        self.assertNotIn("stop", self.events)
        self.assertIn("verify", self.events)

    def test_activated_migration_cannot_be_recopied(self):
        self.state["phase"] = "active"
        with self.assertRaises(move.Refusal):
            move.cutover("root@new.example", "192.0.2.10")
        self.assertNotIn("copy", self.events)

    def test_wrong_target_never_stops_source(self):
        with self.assertRaises(move.Refusal):
            move.cutover("root@another.example", "192.0.2.10")
        self.assertNotIn("stop", self.events)

    def test_unreachable_or_activated_target_prevents_source_recovery(self):
        self.state["phase"] = "source-stopped"
        with patch.object(move, "remote", side_effect=move.Refusal("unreachable or active")), \
                patch.object(move, "start_engine") as start, self.assertRaises(move.Refusal):
            move.resume_source("root@new.example")
        start.assert_not_called()

    def test_source_recovery_invalidates_old_snapshot(self):
        self.state["phase"] = "source-stopped"
        with patch.object(move, "start_engine"), patch.object(move, "wait_for_engine"), patch.object(move, "docker"):
            move.resume_source("root@new.example")
        self.assertEqual(self.state["phase"], "prepared")


class DestinationSafety(unittest.TestCase):
    def test_occupied_destination_refuses_before_runtime_mutation(self):
        state = fixture()
        with patch.object(move, "local_requirements"), patch.object(move.Path, "exists", return_value=False), \
                patch.object(move, "inventory", side_effect=move.Refusal("occupied")), \
                patch.object(move, "stop_engine") as stop, patch.object(move, "write_state") as write, \
                self.assertRaises(move.Refusal):
            move.rpc("reserve", state)
        stop.assert_not_called()
        write.assert_not_called()

    def test_failed_reservation_can_resume_without_replacing_other_hosts(self):
        state = fixture()
        state["phase"] = "reserving"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(move, "local_requirements"), patch.object(move.Path, "exists", return_value=True), \
                    patch.object(move, "read_state", return_value=state), patch.object(move, "file_digest", return_value="new-machine"), \
                    patch.object(move, "stop_engine") as stop, patch.object(move, "write_state") as write, \
                    patch.object(move, "no_symlink_ancestors", return_value=Path(directory)), patch.object(move, "mount_checks"):
                self.assertEqual(move.rpc("reserve", fixture())["phase"], "reserved")
                stop.assert_called_once()
                self.assertEqual(write.call_args.args[0]["phase"], "reserved")

    def test_running_shim_blocks_cold_copy_even_when_systemd_is_inactive(self):
        class Process:
            def read_text(self):
                return "containerd-shim"

        with patch.object(move, "unit_state", return_value="inactive"), \
                patch.object(move.Path, "glob", return_value=[Process()]), self.assertRaises(move.Refusal):
            move.assert_stopped()

    def test_standard_docker_shm_allowed_only_during_warm_copy(self):
        mounted = {"filesystems": [{"target": "/var/lib/docker/containers/" + "a" * 64 + "/mounts/shm", "fstype": "tmpfs"}]}
        for cold in (False, True):
            with patch.object(move, "no_symlink_ancestors", return_value=Path(__file__)), \
                    patch.object(move, "run", side_effect=[json.dumps(mounted), "ext4"]):
                if cold:
                    with self.assertRaises(move.Refusal):
                        move.mount_checks(["/var/lib/docker"], cold=True)
                else:
                    move.mount_checks(["/var/lib/docker"])

    def test_activation_is_recorded_before_any_worker_can_run(self):
        events = []
        with patch.object(move, "check_address"), patch.object(move, "assert_stopped"), \
                patch.object(move, "write_state", side_effect=lambda s: events.append(s["phase"])), \
                patch.object(move, "run"), patch.object(move, "start_engine", side_effect=move.Refusal("failed")), \
                self.assertRaises(move.Refusal):
            move.activate(fixture(), "192.0.2.10")
        self.assertEqual(events, ["activating"])

    def test_remote_recopy_and_rollback_refuse_after_activation(self):
        state = fixture()
        state["phase"] = "activating"
        with patch.object(move, "local_requirements"), patch.object(move, "read_state", return_value=state), \
                patch.object(move, "file_digest", return_value="new-machine"):
            for action in ("capacity", "stopped", "arm", "activate"):
                with self.subTest(action=action), self.assertRaises(move.Refusal):
                    move.rpc(action, fixture())

    def test_target_identity_change_is_refused(self):
        with patch.object(move, "file_digest", return_value="wrong-machine"), self.assertRaises(move.Refusal):
            move.check_pair(fixture(), fixture())

    def test_nested_mount_would_not_silently_skip_data(self):
        with patch.object(move, "no_symlink_ancestors", return_value=Path(__file__)), \
                patch.object(move, "run", side_effect=[json.dumps({"filesystems": [
                    {"target": "/srv/data/nested", "fstype": "ext4"}]}), "ext4"]), \
                self.assertRaises(move.Refusal):
            move.mount_checks(["/srv/data"])

    def test_insufficient_disk_is_refused_before_stop(self):
        with patch.object(move, "no_symlink_ancestors", return_value=Path(__file__).parent), \
                patch.object(move, "run", return_value="ext4"), \
                patch.object(move.shutil, "disk_usage", return_value=type("Usage", (), {"free": 10})()), \
                self.assertRaises(move.Refusal):
            move.reserve_paths(fixture()["source"])

    def test_remaining_capacity_accounts_for_copied_data_and_source_growth(self):
        source = fixture()["source"]
        with patch.object(move, "no_symlink_ancestors", return_value=Path(__file__).parent), \
                patch.object(move, "run", return_value="ext4"), \
                patch.object(move.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1024 ** 3 + 30})()):
            move.check_remaining_space(source, {"/var/lib/docker": 100})
            source["sizes"]["/var/lib/docker"] = 200
            with self.assertRaises(move.Refusal):
                move.check_remaining_space(source, {"/var/lib/docker": 100})

    def test_remaining_capacity_groups_headroom_by_filesystem(self):
        source = fixture()["source"]
        source["paths"] = ["/srv/a", "/srv/b"]
        source["types"] = {path: "directory" for path in source["paths"]}
        source["sizes"] = {path: 100 for path in source["paths"]}

        class DataPath:
            def __init__(self, device):
                self.device = device

            def exists(self):
                return True

            def is_dir(self):
                return True

            def stat(self):
                return type("Stat", (), {"st_dev": self.device})()

            def __str__(self):
                return "/srv/data"

        with patch.object(move, "run", return_value="ext4"), \
                patch.object(move.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1024 ** 3 + 30})()):
            with patch.object(move, "no_symlink_ancestors", return_value=DataPath(1)), self.assertRaises(move.Refusal):
                move.check_remaining_space(source, source["sizes"])
            with patch.object(move, "no_symlink_ancestors", side_effect=[DataPath(1), DataPath(2)]):
                move.check_remaining_space(source, source["sizes"])

    def test_remaining_capacity_refuses_missing_or_changed_target_data(self):
        source = fixture()["source"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (root / "missing", root / "file"):
                if path.name == "file":
                    path.write_text("not a prepared directory")
                with self.subTest(path=path.name), patch.object(move, "no_symlink_ancestors", return_value=path), \
                        self.assertRaises(move.Refusal):
                    move.check_remaining_space(source, source["sizes"])

    def test_capacity_rpc_refuses_changed_reserved_paths(self):
        state = fixture()
        changed = copy.deepcopy(state)
        changed["source"]["paths"] = ["/srv/other-data"]
        with patch.object(move, "local_requirements"), patch.object(move, "read_state", return_value=state), \
                patch.object(move, "file_digest", return_value="new-machine"), patch.object(move, "assert_stopped"), \
                patch.object(move, "check_remaining_space") as check, self.assertRaises(move.Refusal):
            move.rpc("capacity", changed)
        check.assert_not_called()

    def test_existing_bind_directory_is_never_overwritten(self):
        source = fixture()["source"]
        source["paths"] = ["/srv/customer-data"]
        with patch.object(move, "no_symlink_ancestors", return_value=Path(__file__).parent), self.assertRaises(move.Refusal):
            move.reserve_paths(source)

    def test_second_move_archives_previous_destination_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = fixture()
            prior["phase"] = "active"
            path = root / "state.json"
            path.write_text(json.dumps(prior))
            with patch.object(move, "STATE_DIR", root), patch.object(move, "STATE_FILE", path), \
                    patch.object(move, "file_digest", return_value="new-machine"), \
                    patch.object(move, "inventory", side_effect=move.Refusal("stop after archival")), \
                    self.assertRaises(move.Refusal):
                move.prepare("root@third.example")
            self.assertFalse(path.exists())
            archived = root / ("completed-" + prior["id"] + ".json")
            self.assertEqual(json.loads(archived.read_text()), prior)


class InventorySafety(unittest.TestCase):
    def setUp(self):
        self.info = {"DockerRootDir": "/var/lib/docker", "Driver": "overlay2", "Architecture": "x86_64",
                     "ServerVersion": "fixture", "Swarm": {"ControlAvailable": True, "LocalNodeState": "active"}}
        self.volume = {"Driver": "local", "Options": None, "Mountpoint": "/var/lib/docker/volumes/data/_data"}
        self.services = [{"Spec": {"Name": "dokploy", "Mode": {"Replicated": {"Replicas": 1}},
                                    "TaskTemplate": {"ContainerSpec": {}}}}]
        self.nodes = "node1"
        self.container = {"Id": "container", "State": {"Running": True}, "Config": {"Labels": {}},
                          "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}, "Mounts": []}

    def docker(self, *args):
        return {("ps", "-aq"): "container", ("volume", "ls", "-q"): "data", ("plugin", "ls", "-q"): "",
                ("node", "ls", "-q"): self.nodes, ("service", "ls", "-q"): "dokploy"}[args]

    def docker_json(self, *args):
        return {"info": self.info, "inspect": [self.container], "volume": [self.volume],
                "swarm": [{"Spec": {}}], "service": self.services}[args[0]]

    def run_command(self, args):
        if args[:3] == ["containerd", "config", "dump"]:
            return 'root = "/var/lib/containerd"'
        if args[0] == "ctr":
            return "moby"
        if args[0] == "du":
            return "100 path"
        if args[:2] == ["systemctl", "show"]:
            return "/usr/lib/systemd/system/fixture.service"
        return "fixture"

    def inspect(self, source=True):
        with patch.object(move, "local_requirements"), patch.object(move, "docker", side_effect=self.docker), \
                patch.object(move, "docker_json", side_effect=self.docker_json), patch.object(move, "run", side_effect=self.run_command), \
                patch.object(move.Path, "is_dir", return_value=True), patch.object(move, "mount_checks"), \
                patch.object(move, "file_digest", return_value="fixture"):
            return move.inventory(source)

    def test_standard_host_collects_all_data_locations_without_env_values(self):
        self.container["Mounts"] = [{"Type": "bind", "Source": "/srv/custom data"}]
        self.container["Config"]["Env"] = ["SECRET=PRIVATE_SENTINEL"]
        result = self.inspect()
        self.assertIn("/srv/custom data", result["paths"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_remote_volume_and_multiple_swarm_nodes_refused(self):
        self.volume["Options"] = {"type": "nfs"}
        with self.assertRaises(move.Refusal):
            self.inspect()
        self.volume["Options"] = None
        self.nodes = "node1\nnode2"
        with self.assertRaises(move.Refusal):
            self.inspect()

    def test_live_restore_and_nonempty_target_refused(self):
        self.info["LiveRestoreEnabled"] = True
        with self.assertRaises(move.Refusal):
            self.inspect()
        self.info["LiveRestoreEnabled"] = False
        with self.assertRaises(move.Refusal):
            self.inspect(source=False)

    def test_host_specific_port_and_unwanted_restarts_refused(self):
        self.container["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostIp": "192.0.2.5"}]}
        with self.assertRaises(move.Refusal):
            self.inspect()
        self.container["HostConfig"]["PortBindings"] = {}
        self.container["State"]["Running"] = False
        self.container["HostConfig"]["RestartPolicy"]["Name"] = "always"
        with self.assertRaises(move.Refusal):
            self.inspect()


@unittest.skipUnless(os.name == "posix" and shutil.which("rsync"), "real rsync test requires Linux")
class RealRsyncRoundTrip(unittest.TestCase):
    def test_warm_then_cold_copy_preserves_data_and_detects_torn_warm_file(self):
        with tempfile.TemporaryDirectory(prefix="dokploy-copy-test-") as root:
            source, target = Path(root) / "source with spaces", Path(root) / "target with spaces"
            source.mkdir()
            target.mkdir()
            data = source / "rows"
            data.write_bytes(b"old-row")
            os.chmod(data, 0o640)
            os.setxattr(data, "user.test", b"fixture")
            os.link(data, source / "rows-hardlink")
            (source / "relative-link").symlink_to("rows")
            with (source / "sparse").open("wb") as stream:
                stream.seek(2 * 1024 * 1024)
                stream.write(b"end")

            def sync(**kwargs):
                args = move.rsync_args("root@fixture", "/srv/test", "directory", **kwargs)
                shell = args.index("-e")
                del args[shell:shell + 2]
                args[-2:] = [str(source) + "/", str(target) + "/"]
                return subprocess.run(args, text=True, capture_output=True, check=True).stdout

            sync()
            before = data.stat()
            data.write_bytes(b"new-row")
            os.utime(data, ns=(before.st_atime_ns, before.st_mtime_ns))
            (target / "stale-record").write_text("delete from destination only")
            self.assertTrue(sync(verify=True))
            sync(cold=True)
            self.assertEqual(sync(verify=True), "")
            self.assertEqual((target / "rows").read_bytes(), b"new-row")
            self.assertEqual((target / "rows").stat().st_ino, (target / "rows-hardlink").stat().st_ino)
            self.assertEqual((target / "rows").stat().st_mode & 0o777, 0o640)
            self.assertEqual(os.getxattr(target / "rows", "user.test"), b"fixture")
            self.assertEqual(os.readlink(target / "relative-link"), "rows")
            self.assertFalse((target / "stale-record").exists())
            self.assertEqual((target / "sparse").read_bytes(), (source / "sparse").read_bytes())
            self.assertLess((target / "sparse").stat().st_blocks * 512, (target / "sparse").stat().st_size)


if __name__ == "__main__":
    unittest.main()
