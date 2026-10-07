#!/usr/bin/env python3
"""Local button-based launcher for dokploy-move.py; Python 3.10+, no packages.

Run on your computer, not in a Docker container. See docs/dokploy-server-migration.md.
Only the SSH-authenticated old server executes the migration. Its systemd job
continues when this console closes or its connection drops.
"""

import argparse
import importlib.util
import ipaddress
import json
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("dokploy_move", HERE / "dokploy-move.py")
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)
ACTIONS = {"plan", "prepare", "move", "cutover", "status", "resume-source"}

# Executed with python3 -c over SSH. Input is JSON on stdin, never shell text.
# This helper lives outside Docker and all copied directories.
REMOTE_HELPER = r'''
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path("/var/lib/dokploy-move/console")

class ConsoleError(RuntimeError):
    pass

def require(condition, message):
    if not condition:
        raise ConsoleError(message)

def safe(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Unsafe console state path.")
    return path

def execute(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ConsoleError("Server command could not finish. Reconnect before trying another action.")
    require(result.returncode == 0,
            "Server command failed. Reconnect and inspect the migration job on the old server.")
    return result.stdout

def load(path):
    safe(path)
    return json.loads(path.read_text(encoding="utf-8"))

def save(path, value):
    safe(path)
    temporary = safe(path.with_suffix(".new"))
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    os.sync()

def snapshot(record, reconcile=False):
    job_id = str(uuid.UUID(record["id"]))
    unit = "dokploy-move-" + job_id + ".service"
    output = execute(["systemctl", "show", unit, "--no-pager",
                      "--property=LoadState,ActiveState,SubState,ExecMainStatus,Result"])
    props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    active = props.get("ActiveState") in {"active", "activating", "deactivating", "reloading"}
    finished = props.get("SubState") == "exited" and props.get("ActiveState") == "active"
    failed = props.get("ActiveState") == "failed"
    if finished or failed:
        result = "success" if finished and props.get("ExecMainStatus") == "0" else "failed"
        active = False
    elif props.get("LoadState") != "loaded":
        result, active = "unknown", False
    else:
        result = "running" if active else "unknown"
    logfile = safe(ROOT / job_id / "output.log")
    log = ""
    if logfile.is_file():
        with logfile.open("rb") as stream:
            stream.seek(max(0, logfile.stat().st_size - 65536))
            log = stream.read(65536).decode("utf-8", errors="replace")
    phase = None
    statefile = safe(ROOT.parent / "state.json")
    if statefile.is_file():
        state = load(statefile)
        if state.get("target") == record["target"]:
            phase = state.get("phase")
    source_phase = phase
    destination_phase = None
    if reconcile and not active and phase == "verified":
        # The engine may have activated the destination but lost its SSH reply.
        # Ask its guarded status command; never infer rollback safety from the
        # old host's lagging marker or an unreachable destination.
        runner = safe(ROOT / job_id / "engine.py")
        try:
            status = subprocess.run([sys.executable, str(runner), "status", record["target"]],
                                    capture_output=True, text=True, timeout=25, check=False)
            match = re.search(r"destination phase: (activating|active|verified|reserved|reserving),", status.stdout)
            if status.returncode == 0 and match:
                destination_phase = match[1]
                if destination_phase in {"activating", "active"}:
                    phase = destination_phase
            else:
                log += "\nDestination status could not be confirmed. Do not start the old server; inspect both hosts."
        except (OSError, subprocess.TimeoutExpired):
            log += "\nDestination status could not be confirmed. Do not start the old server; inspect both hosts."
    return {"id": job_id, "unit": unit, "action": record["action"],
            "target": record["target"], "new_ip": record["new_ip"],
            "active": active, "result": result, "log": log, "phase": phase,
            "source_phase": source_phase, "destination_phase": destination_phase}

def dispatch(request):
    require(sys.platform == "linux" and os.geteuid() == 0,
            "Connect to the old Linux server as root.")
    require(Path("/run/systemd/system").is_dir(), "The old server must use systemd.")
    require(not Path("/.dockerenv").exists(), "Connect to the host, not a Docker container.")
    action = request.get("action")
    require(action in {"status", "plan", "prepare", "move", "cutover", "resume-source"}, "Unknown action.")
    safe(ROOT).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT.parent, 0o700)
    os.chmod(ROOT, 0o700)
    with safe(ROOT / "lock").open("w") as lock:
        os.chmod(ROOT / "lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        latest = safe(ROOT / "latest.json")
        previous = load(latest) if latest.is_file() else None
        current = snapshot(previous, reconcile=action == "status") if previous else None
        if action == "status":
            return {"ok": True, "job": current}
        require(not current or (not current["active"] and current["result"] != "unknown"),
                "A job is running or its outcome is unknown. Reconnect; do not launch a duplicate.")
        target = request.get("target", "")
        require(isinstance(target, str) and re.fullmatch(r"root@(?:[a-zA-Z0-9][a-zA-Z0-9.-]*|\[[0-9a-fA-F:]+\])", target),
                "Use root@HOST for the destination.")
        address = request.get("new_ip", "")
        parsed = ipaddress.ip_address(address)
        require(parsed.version == 4 and not (parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast),
                "Use the destination's static IPv4 address.")
        engine = request.get("engine", "")
        require(isinstance(engine, str) and 1000 < len(engine) < 200000, "Migration engine is missing.")
        digest = hashlib.sha256(engine.encode()).hexdigest()
        checked = previous.get("checked") if previous else None
        match = {"target": target, "new_ip": address, "engine": digest}
        if current and current["action"] == "plan" and current["result"] == "success":
            checked = previous["configuration"]
        if action in {"move", "prepare"}:
            require(checked == match, "Run Check servers successfully for these settings before migrating.")
        job_id = str(uuid.uuid4())
        folder = safe(ROOT / job_id)
        folder.mkdir(mode=0o700)
        runner = folder / "engine.py"
        with runner.open("x", encoding="utf-8", newline="\n") as stream:
            os.chmod(runner, 0o600)
            stream.write(engine)
        logfile = folder / "output.log"
        with logfile.open("x"):
            os.chmod(logfile, 0o600)
        record = {"id": job_id, "action": action, "target": target, "new_ip": address,
                  "configuration": match, "checked": checked, "created": time.time()}
        # Persist identity before launching: a lost SSH response cannot permit a duplicate.
        save(latest, record)
        command = [sys.executable, "-u", str(runner), action, target]
        if action in {"move", "cutover"}:
            command += ["--new-ip", address]
        execute(["systemd-run", "--quiet", "--unit=dokploy-move-" + job_id,
                 "--property=Type=exec", "--property=UMask=0077", "--property=RemainAfterExit=yes",
                 "--property=TimeoutStartSec=infinity", "--property=Restart=no",
                 "--property=StandardOutput=append:" + str(logfile),
                 "--property=StandardError=append:" + str(logfile), *command])
        return {"ok": True, "job": snapshot(record)}

if __name__ == "__main__":
    os.umask(0o077)
    try:
        print(json.dumps(dispatch(json.load(sys.stdin))))
    except Exception as error:
        # Never forward system exception text, command stderr, or engine internals.
        message = str(error) if isinstance(error, ConsoleError) else "Server state could not be read. Inspect the old server before retrying."
        print(json.dumps({"ok": False, "error": message}))
'''


def validate_request(value):
    ENGINE.require(isinstance(value, dict), "Expected an action object.")
    ENGINE.require(isinstance(value.get("source"), str), "Use root@HOST for the old server.")
    source = ENGINE.validate_target(value.get("source", ""))
    action = value.get("action")
    ENGINE.require(isinstance(action, str) and action in ACTIONS, "Unknown migration action.")
    if action == "status":
        return source, {"action": "status"}
    ENGINE.require(isinstance(value.get("target"), str), "Use root@HOST for the destination.")
    target = ENGINE.validate_target(value.get("target", ""))
    ENGINE.require(source.lower() != target.lower(), "Choose different old and new servers.")
    address = value.get("new_ip", "")
    ENGINE.require(isinstance(address, str), "Enter the destination's static IPv4 address.")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as error:
        raise ENGINE.Refusal("Enter the destination's static IPv4 address.") from error
    ENGINE.require(parsed.version == 4 and not (parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast),
                   "Enter the destination's static IPv4 address.")
    return source, {"action": action, "target": target, "new_ip": str(parsed),
                    "engine": (HERE / "dokploy-move.py").read_text(encoding="utf-8")}


def remote_request(value):
    source, payload = validate_request(value)
    command = ["ssh", "-T", "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", *ENGINE.SSH_OPTIONS, source,
               shlex.join(["python3", "-c", REMOTE_HELPER])]
    try:
        result = subprocess.run(command, input=json.dumps(payload), text=True,
                                capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ENGINE.Refusal("SSH did not finish. Reconnect to check whether the job started. "
                             "Check SSH keys and verified known_hosts on your computer.") from error
    ENGINE.require(result.returncode == 0,
                   "SSH connection failed. Check root SSH access, verified host keys and Python 3 on the old server; "
                   "reconnect before retrying. SSH output is withheld to protect credentials.")
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise ENGINE.Refusal("The old server returned an invalid response. Reconnect before retrying.") from error
    ENGINE.require(isinstance(response, dict) and isinstance(response.get("ok"), bool),
                   "Invalid server response. Reconnect before retrying.")
    return response


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, ConsoleHandler)
        self.token = secrets.token_urlsafe(32)
        self.origin = "http://127.0.0.1:" + str(self.server_port)
        self.action_lock = threading.Lock()


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # URLs and headers can include the access token or operator input.
        pass

    def reply(self, code, payload, content_type="application/json; charset=utf-8"):
        body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, *, api=False):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.reply(403, {"ok": False, "error": "Invalid local host."})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self.reply(403, {"ok": False, "error": "Cross-origin access refused."})
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Migration-Token", "").encode(), self.server.token.encode()):
            self.reply(403, {"ok": False, "error": "Open the console using the launcher's browser link."})
            return False
        return True

    def do_GET(self):
        parsed = urlsplit(self.path)
        if not self.authorized(api=parsed.path.startswith("/api/")):
            return
        if parsed.path == "/":
            self.reply(200, (HERE / "dokploy-move-ui.html").read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/api/status":
            source = parse_qs(parsed.query).get("source", [""])[0]
            self.perform({"source": source, "action": "status"})
        else:
            self.reply(404, {"ok": False, "error": "Not found."})

    def do_POST(self):
        if not self.authorized(api=True):
            return
        if self.path != "/api/action":
            self.reply(404, {"ok": False, "error": "Not found."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError
            value = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            self.reply(400, {"ok": False, "error": "Send a JSON action of at most 4096 bytes."})
            return
        self.perform(value)

    def perform(self, value):
        if not self.server.action_lock.acquire(blocking=False):
            self.reply(409, {"ok": False, "error": "A server request is in progress. Wait, then reconnect."})
            return
        try:
            self.reply(200, remote_request(value))
        except ENGINE.Refusal as error:
            self.reply(400, {"ok": False, "error": str(error)})
        except Exception:
            self.reply(500, {"ok": False, "error": "Console request failed. Reconnect before trying another action."})
        finally:
            self.server.action_lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="localhost port; default chooses an available port")
    parser.add_argument("--no-browser", action="store_true", help="print the local link without opening a browser")
    args = parser.parse_args()
    if not shutil.which("ssh"):
        parser.error("Install the OpenSSH client on this computer first.")
    if not 0 <= args.port <= 65535:
        parser.error("Choose a port between 0 and 65535.")
    with ConsoleServer(("127.0.0.1", args.port)) as server:
        url = server.origin + "/#token=" + server.token
        print("Dokploy migration console: " + url, flush=True)
        print("Keep this launcher open to view progress. Remote jobs continue if it closes.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
