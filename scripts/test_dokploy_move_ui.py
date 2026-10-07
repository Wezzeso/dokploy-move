"""Offline console security/job tests. Never connects to SSH or Docker."""

import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("dokploy_console", Path(__file__).with_name("dokploy-move-ui.py"))
console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console)


def request(action="plan", **changes):
    return {"source": "root@old.example", "target": "root@new.example", "new_ip": "192.0.2.20",
            "action": action, **changes}


class SSHBoundary(unittest.TestCase):
    def test_invalid_targets_actions_and_addresses_never_reach_ssh(self):
        invalid = [None, [], request(source=15), request(action=[]), request(source="-oProxyCommand=bad"),
                   request(target="root@host;bad"), request(target="root@old.example"), request(new_ip="127.0.0.1"),
                   request(new_ip="0.0.0.0"), request(new_ip="2001:db8::1"), request(new_ip=[])]
        with patch.object(console.subprocess, "run") as run:
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(console.ENGINE.Refusal):
                    console.remote_request(value)
            run.assert_not_called()

    def test_ssh_uses_stdin_strict_trust_no_forwarding_and_no_shell(self):
        with patch.object(console.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"ok":true,"job":null}')) as run:
            self.assertTrue(console.remote_request(request())["ok"])
        args, kwargs = run.call_args
        self.assertIn("StrictHostKeyChecking=yes", args[0])
        self.assertIn("BatchMode=yes", args[0])
        self.assertIn("ForwardAgent=no", args[0])
        self.assertIn("ClearAllForwardings=yes", args[0])
        self.assertFalse(kwargs.get("shell", False))
        engine = json.loads(kwargs["input"])["engine"]
        self.assertNotIn(engine, " ".join(args[0]))
        self.assertIn("Move a single-node Dokploy host", engine)

    def test_ssh_failures_and_invalid_output_do_not_disclose_credentials(self):
        for result in (subprocess.CompletedProcess([], 255, "PRIVATE_KEY_CONTENT", "SECRET_PASSWORD"),
                       subprocess.CompletedProcess([], 0, "SECRET_PASSWORD", "PRIVATE_KEY_CONTENT")):
            with patch.object(console.subprocess, "run", return_value=result):
                with self.assertRaises(console.ENGINE.Refusal) as error:
                    console.remote_request(request())
                self.assertNotIn("SECRET_PASSWORD", str(error.exception))
                self.assertNotIn("PRIVATE_KEY_CONTENT", str(error.exception))


class PersistentJobs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # fcntl is used on the Linux server only; tests also run on Windows.
        shim = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *_: None)
        self.ns = {"__name__": "console_test"}
        with patch.dict(sys.modules, {"fcntl": shim}):
            exec(compile(console.REMOTE_HELPER, "remote_console", "exec"), self.ns)
        self.ns["ROOT"] = Path(self.temp.name) / "migration" / "console"
        self.units = {}
        self.launches = []
        self.ns["execute"] = self.execute
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(os, "geteuid", return_value=0, create=True))
        self.stack.enter_context(patch.object(os, "sync", create=True))
        self.stack.enter_context(patch.object(sys, "platform", "linux"))
        original_is_dir = Path.is_dir
        self.stack.enter_context(patch.object(Path, "is_dir", lambda p: True if str(p).replace("\\", "/") == "/run/systemd/system" else original_is_dir(p)))
        original_exists = Path.exists
        self.stack.enter_context(patch.object(Path, "exists", lambda p: False if str(p).replace("\\", "/") == "/.dockerenv" else original_exists(p)))

    def execute(self, args):
        if args[0] == "systemd-run":
            self.launches.append(args)
            unit = next(x.split("=", 1)[1] for x in args if x.startswith("--unit=")) + ".service"
            self.units[unit] = "LoadState=loaded\nActiveState=active\nSubState=running\nExecMainStatus=0\n"
            return ""
        self.assertEqual(args[:2], ["systemctl", "show"])
        return self.units.get(args[2], "LoadState=not-found\nActiveState=inactive\nSubState=dead\n")

    def dispatch(self, action="plan", **changes):
        _, payload = console.validate_request(request(action, **changes))
        return self.ns["dispatch"](payload)

    def finish(self, success=True):
        latest = json.loads((self.ns["ROOT"] / "latest.json").read_text())
        unit = "dokploy-move-" + latest["id"] + ".service"
        self.units[unit] = ("LoadState=loaded\nActiveState=active\nSubState=exited\nExecMainStatus=0\n" if success else
                            "LoadState=loaded\nActiveState=failed\nSubState=failed\nExecMainStatus=1\n")

    def test_successful_plan_required_and_bound_to_exact_configuration(self):
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("move")
        self.assertEqual(self.launches, [])
        self.dispatch("plan")
        self.finish()
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("move", new_ip="192.0.2.21")
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("prepare", target="root@different.example")
        self.assertEqual(len(self.launches), 1)
        self.assertTrue(self.dispatch("move")["job"]["active"])
        args = self.launches[-1]
        self.assertIn("--property=RemainAfterExit=yes", args)
        self.assertIn("--property=Restart=no", args)
        self.assertEqual(args[-4:], ["move", "root@new.example", "--new-ip", "192.0.2.20"])

    def test_running_or_uncertain_jobs_cannot_be_duplicated(self):
        result = self.dispatch("plan")
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("plan")
        self.units.clear()  # Reboot/lost transient unit is not proof of success.
        result = self.dispatch("status")
        self.assertEqual(result["job"]["result"], "unknown")
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("move")
        self.assertEqual(len(self.launches), 1)

    def test_failed_preflight_never_authorizes_move(self):
        self.dispatch("plan")
        self.finish(False)
        self.assertEqual(self.dispatch("status")["job"]["result"], "failed")
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("move")

    def test_engine_change_invalidates_successful_check(self):
        self.dispatch("plan")
        self.finish()
        _, payload = console.validate_request(request("move"))
        payload["engine"] += "\n# different engine\n"
        with self.assertRaises(self.ns["ConsoleError"]):
            self.ns["dispatch"](payload)

    def test_reconnect_retains_phase_logs_and_plan_for_prepare_retry(self):
        self.dispatch("plan")
        self.finish()
        started = self.dispatch("prepare")["job"]
        logfile = self.ns["ROOT"] / started["id"] / "output.log"
        logfile.write_text("Initial copy complete.", encoding="utf-8")
        (self.ns["ROOT"].parent / "state.json").write_text(json.dumps({"target": "root@new.example", "phase": "prepared"}))
        self.finish()
        status = self.dispatch("status")["job"]
        self.assertEqual(status["phase"], "prepared")
        self.assertEqual(status["log"], "Initial copy complete.")
        self.assertEqual(status["new_ip"], "192.0.2.20")
        self.assertTrue(self.dispatch("prepare")["job"]["active"])

    def test_lost_launch_response_preserves_identity_and_blocks_blind_retry(self):
        execute = self.ns["execute"]
        def uncertain(args):
            if args[0] == "systemd-run":
                raise self.ns["ConsoleError"]("Response lost")
            return execute(args)
        self.ns["execute"] = uncertain
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("plan")
        self.assertTrue((self.ns["ROOT"] / "latest.json").is_file())
        self.assertEqual(self.dispatch("status")["job"]["result"], "unknown")
        with self.assertRaises(self.ns["ConsoleError"]):
            self.dispatch("plan")

    def test_reconnect_reconciles_destination_activation_after_lost_ssh_reply(self):
        self.dispatch("plan")
        self.finish()
        self.dispatch("move")
        self.finish(False)
        (self.ns["ROOT"].parent / "state.json").write_text(json.dumps({"target": "root@new.example", "phase": "verified"}))
        status = "Source phase: verified, Docker: inactive; destination phase: active, Docker: active.\n"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, status)):
            job = self.dispatch("status")["job"]
        self.assertEqual(job["source_phase"], "verified")
        self.assertEqual(job["destination_phase"], "active")
        self.assertEqual(job["phase"], "active")
        self.assertEqual(job["result"], "failed")  # Never turn failed orchestration into success.

    def test_unreachable_destination_never_proves_safe_recovery_or_leaks_stderr(self):
        self.dispatch("plan")
        self.finish()
        self.dispatch("move")
        self.finish(False)
        (self.ns["ROOT"].parent / "state.json").write_text(json.dumps({"target": "root@new.example", "phase": "verified"}))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "SECRET_PASSWORD")):
            job = self.dispatch("status")["job"]
        self.assertIsNone(job["destination_phase"])
        self.assertEqual(job["phase"], "verified")
        self.assertIn("Do not start the old server", job["log"])
        self.assertNotIn("SECRET_PASSWORD", json.dumps(job))


class LocalHTTPBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = console.ConsoleServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def http(self, method="POST", path="/api/action", value=None, **headers):
        body = json.dumps(request() if value is None else value)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        defaults = {"Content-Type": "application/json", "X-Migration-Token": self.server.token}
        connection.request(method, path, body if method == "POST" else None, {**defaults, **headers})
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        connection.close()
        return result

    def test_api_refuses_cross_origin_missing_token_and_rebinding_before_ssh(self):
        with patch.object(console, "remote_request") as remote:
            for headers in ({"Origin": "https://evil.example"}, {"X-Migration-Token": ""},
                            {"Host": "evil.example"}, {"Origin": "null"}):
                with self.subTest(headers=headers):
                    self.assertEqual(self.http(**headers)[0], 403)
                    self.assertEqual(self.http("GET", "/api/status?source=root%40old.example", **headers)[0], 403)
            remote.assert_not_called()

    def test_authorized_action_and_status_use_no_cache_security_headers(self):
        with patch.object(console, "remote_request", return_value={"ok": True, "job": None}) as remote:
            status, headers, body = self.http(Origin=self.server.origin)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.http("GET", "/api/status?source=root%40old.example")
            self.assertEqual(remote.call_args.args[0], {"source": "root@old.example", "action": "status"})

    def test_concurrent_actions_and_oversized_or_non_json_requests_are_rejected(self):
        with patch.object(console, "remote_request") as remote:
            self.server.action_lock.acquire()
            try:
                self.assertEqual(self.http()[0], 409)
            finally:
                self.server.action_lock.release()
            self.assertEqual(self.http(**{"Content-Type": "text/plain"})[0], 400)
            self.assertEqual(self.http(value={"data": "x" * 5000})[0], 400)
            remote.assert_not_called()

    def test_unexpected_errors_are_redacted(self):
        with patch.object(console, "remote_request", side_effect=OSError("SECRET_PASSWORD")):
            code, _, body = self.http()
            self.assertEqual(code, 500)
            self.assertNotIn(b"SECRET_PASSWORD", body)


if __name__ == "__main__":
    unittest.main()
