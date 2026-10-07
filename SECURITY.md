# Security

Dokploy Move uses root SSH access and transfers data that may include database
contents, Docker secrets, environment values, and private certificates. Treat
the migration hosts, SSH keys, local console, and migration records as
privileged resources.

## Using the tool

- Read the [migration guide](docs/dokploy-server-migration.md), keep an
  independent backup, and rehearse with disposable VMs before moving live data.
- Verify SSH host fingerprints through a trusted channel. Do not disable host
  key checks to bypass a failed connection.
- Keep the local console bound to loopback and protect its access token. Do
  not expose it through a public proxy or share its token-bearing launch URL.
- Keep only one writable copy active. An unreachable destination is not proof
  that it is stopped; inspect its state before any source recovery.
- Review logs and screenshots before sharing them. Never upload SSH private
  keys, passwords, tokens, migration state, or customer data to an issue.

## Reporting a vulnerability

Do not publish exploit details or sensitive data in a public issue. If the
repository's **Security** tab offers **Report a vulnerability**, use that private
reporting form. If it is unavailable, open an issue asking the maintainer to
arrange a private reporting channel, without including vulnerability details.

Include a minimal reproduction using synthetic data, the affected commit,
expected behavior, and actual behavior once a private channel is established.
Never test a suspected issue against someone else's server.

Security fixes target the current `main` branch. No support or response-time
guarantee is made for older commits.
