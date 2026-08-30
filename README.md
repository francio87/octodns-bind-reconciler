# octodns-bind-reconciler

Containerized Git reconciler for octoDNS and BIND9.

It keeps a persistent checkout of a public or private Git repository, validates its octoDNS configuration, calculates a dry-run plan, applies the plan to BIND9 through RFC 2136, and verifies with a second dry-run. A commit is marked as applied only after every command succeeds.

## Configuration model

The reconciler has **no configuration file**. Every runtime setting is an environment variable and can be declared directly in Compose or loaded from `.env`.

The fetched repository still contains the octoDNS configuration and zone files: those files are the DNS source of truth, not reconciler settings.

## Flow

```text
Git repository
    -> git fetch + fast-forward
    -> octodns-validate
    -> octodns-sync (dry-run)
    -> octodns-sync --doit
    -> octodns-sync (verification dry-run)
    -> record applied commit
```

The checkout and last successfully applied commit are persisted in `/data`. Failed fetches, validation, planning, application, or verification do not advance the state marker.

## Required BIND9 setup

BIND9 must expose the managed zone to the container and permit both AXFR and RFC 2136 updates with the same TSIG identity. Keep BIND9 authoritative-only when it serves a private zone.

Generic zone example:

```bind
include "/etc/bind/octodns.key";

zone "internal.example.org" {
    type primary;
    file "/var/lib/bind/db.internal.example.org";
    allow-transfer { key octodns-internal.; };
    update-policy {
        grant octodns-internal. zonesub ANY;
    };
};
```

Restrict BIND9 query access and port 53 exposure to the intended networks. Do not expose RFC 2136 publicly.

## Source repository example

`config-internal.yaml`:

```yaml
providers:
  records:
    class: octodns.provider.yaml.YamlProvider
    directory: ./zones-internal
    default_ttl: 300
    enforce_order: false

  bind:
    class: octodns_bind.Rfc2136Provider
    host: env/BIND_HOST
    port: 53
    timeout: 15
    key_name: env/OCTODNS_TSIG_NAME
    key_secret: env/OCTODNS_TSIG_SECRET
    key_algorithm: hmac-sha256

zones:
  internal.example.org.:
    sources:
      - records
    targets:
      - bind
```

`zones-internal/internal.example.org.yaml`:

```yaml
app:
  type: A
  value: 192.0.2.10
```

## Run with Compose

```bash
cp .env.example .env
chmod 600 .env
# Edit .env without committing it.
docker compose config
docker compose up -d --build
```

No port, Docker socket, or extra application configuration mount is required. The container needs outbound Git access and network access to BIND9 port 53/TCP and 53/UDP as required by the provider.

All variables may instead be written as literal values under `services.reconciler.environment` in `compose.yaml`.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---:|---|---|
| `REPOSITORY_URL` | yes | — | HTTPS URL of public or private source repository |
| `REPOSITORY_BRANCH` | no | `main` | Branch to follow |
| `OCTODNS_CONFIG_FILE` | no | `config-internal.yaml` | Path inside checkout |
| `POLL_INTERVAL_SECONDS` | no | `300` | Delay after successful cycle |
| `RETRY_INTERVAL_SECONDS` | no | `60` | Delay after failed cycle |
| `COMMAND_TIMEOUT_SECONDS` | no | `120` | Timeout for each Git or octoDNS command |
| `RUN_ONCE` | no | `false` | Run one cycle, then exit |
| `RESTART_POLICY` | Compose only | `unless-stopped` | Set to `"no"` when `RUN_ONCE=true` |
| `LOG_LEVEL` | no | `INFO` | Python log level |
| `GIT_USERNAME` | private repo only | `x-access-token` | HTTPS Git username |
| `GIT_TOKEN` | private repo only | empty | Read-only repository token |
| `BIND_HOST` | source config dependent | `bind9` | Used by `env/BIND_HOST` in octoDNS config |
| `OCTODNS_TSIG_NAME` | source config dependent | empty | TSIG key name |
| `OCTODNS_TSIG_SECRET` | source config dependent | empty | TSIG secret |

`WORKTREE` and `STATE_FILE` are also supported for advanced layouts; defaults are `/data/repository` and `/data/last-applied-commit`.

For private GitHub repositories, use a fine-grained token limited to the configuration repository with read-only `Contents` permission. Never commit `.env` or TSIG material.

## Safety behavior

- persistent clone; no repeated full clone;
- exact `origin` URL check;
- clean-worktree check;
- `git merge --ff-only`; no reset or force operation;
- process lock in the persistent data directory;
- per-command timeout;
- validation and reconciliation on every poll, including unchanged commits, to repair DNS drift;
- state marker written atomically after successful zero-change verification;
- non-root, read-only container with all Linux capabilities dropped in the example Compose;
- credentials passed through environment and Git askpass, never embedded in repository URLs.

## Tests

```bash
python3 -m unittest discover -s tests -v
docker build -t octodns-bind-reconciler:test .
```
