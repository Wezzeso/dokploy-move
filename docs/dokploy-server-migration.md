# Move an entire Dokploy server

[dokploy-move-ui.py](../scripts/dokploy-move-ui.py) opens a local browser console
for moving a **single-node Linux Dokploy server** to a fresh server over SSH.
It runs the existing [migration engine](../scripts/dokploy-move.py) and includes
all local hosted projects. Both tools need Python 3.10+ and no
pip packages.

It copies Dokploy's database and configuration, all local Docker databases and
volumes, uploads, container writable layers, images, build caches, environment
values, registry credentials stored in Docker/Dokploy, Swarm secrets, certificates,
and discovered application bind mounts. Users, password hashes, MFA keys,
sessions, rows, schema, grants and migration history stay together. Application
images are copied at their existing versions; migration does not rebuild or
upgrade the applications, database engine, or Dokploy.

The full-host copy is intended for matching hosts. It is not an installer,
database conversion, or a general cross-platform Docker restore format.
Rehearse on disposable matching VMs before relying on it for production.

## One-click migration

Provision the new machine with the **same Linux distribution/release, CPU
architecture, Docker Engine version, containerd version, storage driver and
daemon/systemd configuration**. Install Docker, Python 3, rsync, and the usual
systemd/util-linux tools. Do **not** install Dokploy or deploy anything on the new
machine. Its Docker Engine must initially be running, empty, and outside a Swarm.

Install Python 3.10+ and OpenSSH on your workstation. Set up root SSH key access
from **workstation to old server** and from **old server to new server**. Verify
both host keys against console/provider fingerprints before using the console.
SSH must work without a password prompt; the console accepts no passwords or
private keys and does not forward your SSH agent. Configure aliases, keys and
non-default ports in the workstation's `~/.ssh/config` for the old server and
the old server's `/root/.ssh/config` for the new server. Both tools enforce strict
host-key checking.

On Windows, double-click [dokploy-move.cmd](../scripts/dokploy-move.cmd). On any
workstation with Python and OpenSSH, run from this checkout:

```bash
python scripts/dokploy-move-ui.py
```

The launcher opens your browser automatically on a random loopback port. Use
`--no-browser` to open the printed address yourself, or `--port 8765` for a fixed
port. The console binds only to loopback and protects requests with a fresh
secret token. Keep its browser URL private; do not expose it through a proxy or
port forward. It uploads the matching migration engine to the old server for
each job; no manual script copy is needed when using the console.

1. Enter the **old source** as `root@OLD_SERVER`, the **new destination** as
   `root@NEW_SERVER`, and the destination's assigned static IPv4 address.
2. Click **Check destination** and review the successful plan, data locations and
   capacity. A successful check on that source for the same destination, static
   IP and engine version is required before **Move all local projects** or
   **Prepare initial copy** can start.
3. Pause deployments, updates and maintenance jobs. Stop any **non-Docker**
   processes that write the copied data, confirm the outage acknowledgement and
   click **Move all local projects**. This starts the entire copy, stop, final
   verification and activation sequence.
4. Follow the job log. After completion, finish the DNS, Dokploy IP and application
   checks in [After the move](#after-the-move).

The address must actually be assigned to a destination network interface.
Behind provider NAT, use the private local address for Swarm; public DNS can use
the public address separately. **Move all local projects stops every Docker workload
on the source during cutover.** It copies the supported Docker/Dokploy state
listed below; it does not copy the whole operating system or external services.

Jobs run through `systemd-run` on the old server and continue after the browser,
launcher or SSH connection closes. To reconnect, reopen the launcher, enter the
same source and click **Reconnect / refresh status** to retrieve its latest job
and log.
Reconnect reads status; it never retries an operation automatically. Do not
manually start Docker on either machine during copying or recovery.

Under **Scheduled copy and recovery**, use **Prepare initial copy** ahead of
maintenance and **Run final switch** during the scheduled outage. **Resume source
safely** follows the guarded recovery rules below; it is not an automatic
rollback. The console retains root-only job records in
`/var/lib/dokploy-move/console` on the old server, separately from the engine's
migration state. A job failure requires inspection and an explicit recovery
action.

A server reboot interrupts its job and discards transient systemd unit status.
An unknown job result blocks new console actions to prevent duplicate execution.
Verify on the old server that no migration job is running, then use the direct
command-line status and recovery procedure below. Keep the migration records;
do not remove them to bypass this gate.

## Direct command-line use

For terminal operation, copy `scripts/dokploy-move.py` to
`/root/dokploy-move.py` on the **old server** and use the same SSH setup and host
prerequisites above.

Run from the old server, replacing `NEW_SERVER` and `NEW_IPV4`:

```bash
sudo python3 /root/dokploy-move.py plan root@NEW_SERVER
sudo python3 /root/dokploy-move.py move root@NEW_SERVER --new-ip NEW_IPV4
```

`plan` checks compatibility, empty destination, mount coverage and free space.
It prints sizes and data locations without revealing environment variables or
database credentials. `NEW_IPV4` must be a static IPv4 address actually assigned
to the destination network interface. Behind provider NAT, use the private local
address for Swarm; public DNS can use the public address separately.

`move` first copies data while the old server runs. It then **stops every Docker
workload on the old server**, makes a checksum-based final copy, verifies it,
starts the new server and checks service replicas/container health. It persists
migration state and leaves the old Docker/containerd services **masked**, including
across a reboot, to prevent two copies of the same cluster from running.

For a shorter scheduled outage, do the initial copy ahead of time:

```bash
# Before maintenance; old applications remain available.
sudo python3 /root/dokploy-move.py prepare root@NEW_SERVER

# During maintenance; stops the old server and starts the verified new copy.
sudo python3 /root/dokploy-move.py cutover root@NEW_SERVER --new-ip NEW_IPV4
```

Run these from a persistent terminal session so an operator disconnect does not
interrupt the orchestration. Pause deployments, updates and maintenance jobs
during the move. Stop any **non-Docker** processes that write the copied data.
Do not manually start Docker on either machine during copying or recovery.

## What moves and what remains external

| Data | Handling |
|---|---|
| `/etc/dokploy` | Full copy, including Compose checkouts, configuration, keys and proxy certificates |
| `/var/lib/docker` | Full cold copy of images, all local volumes, container state, networks and Swarm state |
| `/var/lib/containerd` | Full cold copy, including the containerd image store when used |
| Other local application bind mounts | Discovered from all existing containers and Swarm services, checked and copied at the same paths |
| Remote databases, S3 buckets and remote deployment servers | Connections/settings are retained; their data stays at the existing external service |
| DNS, cloud firewalls, provider IPs, OS cron jobs, host packages and external integrations | Configure separately; these are not Docker/Dokploy data |
| OS identity and SSH host keys | Destination keeps its own; the hostname is set to the source hostname for placement compatibility |

Bind mounts must exist and be covered by an existing container or Swarm service.
Undeployed Compose files can reference other host paths that have never been
mounted; inspect the plan and copy such data separately. Cross-directory hard
links and symlinks to data outside the copied locations need a separate review.
Persistent mounts inside a copied directory are refused rather than silently
skipped. Running Docker overlay and shared-memory mounts are allowed for the
initial copy only; the final copy requires them to have stopped.

The tool refuses occupied destinations, multiple Swarm nodes, custom data roots,
Swarm autolock, live restore, rootless/user-namespace/SELinux engines, Docker
plugins, network/plugin/bind-backed volumes, non-ext4/xfs data filesystems, host
device passthrough and host-specific port bindings. System-wide bind mounts such
as `/` or `/etc` are also refused. These cases need a migration tailored to their
storage/runtime. Separate remote Dokploy servers are not members of this clone;
their configuration survives but they keep running on their existing machines.

Keep the same public application hostnames. New IP addresses require DNS updates;
changing application domains can additionally require app environment changes,
OAuth callbacks, and rebuilding frontend images with new public build values.

## After the move

1. On the new server, open Dokploy using its existing hostname routed to the new
   IP (a local hosts-file override is useful before public DNS changes).
2. In **Web Server → Server → Update IP**, set the new server IP. Review references
   to the old IP, including Traefik.me domains, remote-server access and callbacks.
3. Check representative existing logins/MFA, database records, uploads/downloads
   and each application's health. Container health does not prove these flows.
4. Point the relevant DNS A/AAAA records at the new host and verify HTTPS and
   ingress. Configure the destination firewall before opening traffic. Retain
   external-provider allowlists, SMTP access, OAuth, bot and storage connectivity.
5. Keep the old server stopped and retain an independent backup through your
   recovery window. Once the new server starts, workers may have sent messages
   or changed data even before DNS changes. Do not simply restart the old copy.

On a subsequent move, reopen the console with the current server as the source,
or copy the engine there for direct terminal use. A successfully completed
destination archives its prior migration record when preparing another
destination. No change of application/database passwords is required.

## Timing

**Ten minutes is a target, not a guarantee.** A one-command full migration includes
the initial transfer. Even transferring 100 GB over an ideal 1 Gbit/s link takes
more than 13 minutes before filesystem overhead and verification. Run `prepare`
in advance to move the bulk of bytes outside maintenance. Use a rehearsal to
measure final downtime on your disks and network.

The final pass checks every file's content: size/timestamp comparisons alone
cannot validate an initial copy of a running database. A second read-only pass
verifies content and metadata before starting the destination. Large databases,
slow disks, many small files and DNS caches can all exceed ten minutes. The tool
does not kill a valid migration at a ten-minute deadline. Interrupted rsync
copies can resume; final verification still must finish.

Before stopping the source, cutover rechecks destination capacity for source
growth since preparation, plus 20% of current data and 1 GiB per filesystem.
This is a best-effort check: later writes and temporary copies of large changed
files can still exhaust space. Keep additional headroom for your workload.

## Failure and recovery

Run on the old server:

```bash
sudo python3 /root/dokploy-move.py status root@NEW_SERVER
```

If the source still records `verified` after losing the destination's activation
reply, **Reconnect / refresh status** checks the destination's guarded status.
It can show the destination as `activating` or `active` while retaining the
source phase and failed job result. A running destination does not turn that
failed job into a successful one; inspect the destination and finish application
checks there. An unreachable destination never proves that source recovery is
safe.

- Initial copy failed: run `prepare` again with the same destination. The source
  remains live; destination runtime stays masked.
- Final copy or verification failed: leave both runtimes stopped and repeat
  `cutover` after resolving disk/network errors. It rechecks content before start.
- To abort **before destination activation**, run the command below. It requires
  proof from the destination that activation has never begun and the runtime is
  stopped. It refuses if the destination is unreachable. The next cutover must
  take a fresh final copy because source writes have resumed.

```bash
sudo python3 /root/dokploy-move.py resume-source root@NEW_SERVER
```

- Destination phase `activating` or `active`: do not resume/re-copy the old host.
  Inspect the destination's Docker service/container logs and repair it there.
  The activation marker is written *before* starting any worker, so startup or
  SSH failures cannot accidentally authorize stale-source recovery. Failed
  readiness also leaves this marker in place.
- If remote reservation fails before its state exists, fix the reported
  prerequisite and rerun `prepare`. Local recovery identity is retained.

State is in `/var/lib/dokploy-move/state.json`, mode `0600`, on each host. Do not
delete or edit it to bypass refusal checks. Transfers use encrypted SSH and retain
numeric owners, permissions, ACLs, extended attributes, within-directory hard
links and sparse files. No database password or environment dump is printed.
Subprocess error output is withheld because Docker configuration may contain
secrets; diagnose locally without posting credentials in logs or tickets.

## Verification and basis

Run:

```bash
python scripts/test_dokploy_move.py
python scripts/test_dokploy_move_ui.py
node --test scripts/dokploy-move-ui.test.mjs
```

The engine suite checks refusal conditions,
copy/verification/start ordering, interrupted transfer recovery, secret-safe
errors and stale-source protection using mocked engines. On Linux with rsync it
also performs a real temporary-directory round trip, including changed bytes
with unchanged size/time, hard links, symlinks, modes, xattrs and sparse files.
The Python console suite uses mocked SSH/systemd operations and local HTTP
requests to check input validation, the preflight gate, job reconnection and
browser access protection. The JavaScript suite exercises the shipped browser
controller with a minimal DOM and mocked HTTP responses: acknowledgement and
phase gates, changed settings, uncertain requests, recovery controls, locale
coverage and literal rendering of untrusted output. These tests do not contact
production or establish acceptance of a real Docker/Swarm restore. The
source/target VM rehearsal and application checks remain required.

Dokploy's [built-in backup](https://docs.dokploy.com/docs/core/backups) covers its
own PostgreSQL database and `/etc/dokploy`; application volumes need separate
coverage. Docker documents stopping the engine before copying Swarm state and
restoring with [`--force-new-cluster`](https://docs.docker.com/engine/swarm/admin_guide/).
This tool extends that procedure with full local engine/application storage;
the restriction to matching, single-node hosts is deliberate. PostgreSQL's
[filesystem backup guidance](https://www.postgresql.org/docs/current/backup-file.html)
requires a stopped server or an appropriately consistent filesystem snapshot.

This tool relocates an existing server without converting application schemas
or changing database engines. Plan those changes as separate operations.
