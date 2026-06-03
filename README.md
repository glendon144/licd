# licd — Local Inference Controller daemon

`licd` is a local-first Local Inference Controller for Glen's Distributed Cognition architecture.

It creates local inference/context capsules in SQLite, discovers known machines from `/etc/hosts`, probes local services such as `clones-brain`, `dream-server`, and `tortoised`, and performs a first-pass recollection sweep before more expensive inference.

Current role:

- `licd` is **not** an autonomous agent
- `licd` is a **local preprocessing, recollection, cache, and capsule daemon**
- **AI Broker** should remain the executive authority for routing, approvals, and policy

## Current milestone

v0.3.1 keeps the daemon small and standard-library-only while adding a more explicit local diagnostics surface:

- canonical entrypoint: `licd.py`
- `GET /diagnostics`
- first-class local diagnostics for:
  - `clones-brain`
  - `dream-server`
  - `tortoised`
- verified `clones-brain` search route:
  - `POST /search_thoughts`
- loopback/local-first posture preserved

## Run

```bash
python3 licd.py --host 127.0.0.1 --port 8777
```

## Useful endpoints

```text
GET  /health
GET  /diagnostics
GET  /hosts
GET  /service-map
GET  /capsules
GET  /capsule?id=<capsule_id>
GET  /cache/search?q=<query>
POST /capsule/create
POST /recollect
```

## Examples

```bash
curl -s http://127.0.0.1:8777/health | jq
curl -s http://127.0.0.1:8777/diagnostics | jq
curl -s http://127.0.0.1:8777/service-map | jq

curl -s -X POST http://127.0.0.1:8777/recollect \
  -H 'Content-Type: application/json' \
  -d '{"query":"predicting inference cost", "max_hosts":8, "max_results_per_service":3}' | jq
```

## Main configuration knobs

```text
LICD_DB=storage/licd.db
LICD_HOSTS_FILE=/etc/hosts
LICD_HOST_ALLOWLIST=peace,blues,jazz,optiplex310
LICD_TIMEOUT=2.0
LICD_REQUIRE_TOKEN=0
LICD_SHARED_TOKEN=change-me
LICD_BIND_HOST=127.0.0.1
LICD_BIND_PORT=8777
LICD_TORTOISED_PORT=8770
LICD_CLONES_BRAIN_PORT=8765
LICD_DREAM_SERVER_PORT=8181
LICD_ECHO_VAULT_PORT=
```

## Default service ports

```text
tortoised:    8770
clones-brain: 8765
dream-server: 8181
echo_vault:   disabled unless LICD_ECHO_VAULT_PORT is set
```

## Remote deployment helper

There is now a conservative remote deployment helper:

```bash
./scripts/deploy-licd-remote.sh <target-host>
```

Design notes:

- installs `licd` under `/opt/licd`
- creates a dedicated system user `licd`
- creates `/opt/licd/.venv`
- creates `/etc/default/licd`
- installs a systemd service named `licd`
- preserves `/opt/licd/storage/licd.db` across source refreshes
- binds to `127.0.0.1:8777` by default
- smoke-tests `/health` and `/diagnostics`

This is intentionally shell-first. It should be proven from the CLI before any future `licd-console` deployment button tries to wrap it.

## Architectural note

For local-only services like `dream-server` and `clones-brain`, the intended model is:

- each host runs its own local service on loopback
- each host runs its own `licd`
- higher-level consoles query `licd`, rather than reaching directly into each host's private loopback services

That keeps `licd` aligned with the local-first design instead of turning it into a remote command runner.
