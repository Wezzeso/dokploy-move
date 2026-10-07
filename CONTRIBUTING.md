# Contributing

Keep changes focused and describe the behavior they change. Read the
[migration guide](docs/dokploy-server-migration.md) before changing transfer,
cutover, or recovery behavior.

## Run the offline checks

Use Python 3.10 or newer and Node.js 24. The tests use the Python standard
library and Node.js built-ins; no package installation is needed.

```sh
python scripts/test_dokploy_move.py
python scripts/test_dokploy_move_ui.py
node --test scripts/dokploy-move-ui.test.mjs
```

These tests do not connect to SSH or mutate Docker. On Linux, install `rsync`
to exercise the real filesystem round trip in temporary directories. That test
checks checksums, permissions, hard links, sparse files, symbolic links, and
extended attributes. It is skipped on Windows.

CI runs the same suites on Linux and Windows with Python 3.10 and 3.14.

## Rehearse migration changes

Changes to the transfer engine also need a rehearsal on disposable Linux VMs:

1. Create a representative single-node Dokploy source and a fresh compatible
   destination. Use fixture data and an independent backup.
2. Check the destination, perform the initial copy, and complete cutover.
3. Verify application reads and writes, database contents, uploaded files,
   volumes, certificates, and the source remaining stopped.
4. Exercise interrupted copying and recovery without starting two writers.

Report the commands you ran, their results, and any paths you could not verify.
Passing offline tests does not establish that a live migration has succeeded.
Do not run a production migration as part of a pull request check.

## Pull requests

- Preserve strict SSH host verification, input validation, state locking,
  checksum verification, and the guards against simultaneous writers.
- Keep English, Russian, and Kazakh interface dictionaries in sync.
- Update the guide when prerequisites, supported layouts, or recovery steps
  change.
- Use synthetic examples. Remove credentials, private host details, and data
  from screenshots, logs, fixtures, and issue reports.
- Pin GitHub Actions to full commit SHAs and keep workflow permissions narrow.

See [SECURITY.md](SECURITY.md) for vulnerability reporting.
