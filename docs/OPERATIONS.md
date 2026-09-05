# First-release operations

## Processes and configuration

Run `python -m kirby.storage migrate` before starting `python -m kirby.api` and
`python -m kirby.worker`. Both processes need `KIRBY_DATABASE_URL`; only Worker
needs `KIRBY_OPENROUTER_KEY_FILE`. Configuration names are in `.env.example`.

API credentials are an array of `{token, tenant, subject, roles}` objects. Generate
random tokens and keep the file outside Git. Restart API after credential changes.
This is configured bearer authentication, not OIDC. Use TLS at the existing gateway
when exposing the service beyond loopback.

The default model is `cohere/north-mini-code:free`. The runtime refuses paid models;
free-model capacity can still cause 429 or failed tasks. No silent paid fallback.
Role files and output schemas are loaded on API startup. Existing contexts keep
stored manifests, including their model. Restart API to activate new role files.

## Deployment

`compose.yaml` supplies a local PostgreSQL service, one-shot migration, API, and one
Worker. Set the two credential file paths and `KIRBY_POSTGRES_PASSWORD`; use a
URL-safe password because it becomes part of the DSN. Set `KIRBY_UID/KIRBY_GID` to
your host IDs so file-based Compose secrets keep their existing permissions.
Create the session directory before starting the containers. Database data and
session files are intentionally persistent. `docker compose down` preserves data;
do not add `-v` unless deletion is intended.

The Helm chart uses an external PostgreSQL secret and one Worker StatefulSet with a
persistent session volume. It does not configure a gateway, OIDC, HPA, or KEDA.
Supply a built image digest and the required Secret. Run Helm lint/template before
applying it to a disposable namespace. No user cluster was deployed by this work.

## Checks and shutdown

- `/healthz`: API process alive. `/readyz`: PostgreSQL schema reachable.
- Worker logs include task IDs and failure categories, never raw prompts or model keys.
- SIGTERM stops new claims and lets the current task finish within its deadline.
  The provided deployment gives it 330 seconds for the default 300-second role limit.
- Expired leases become failed tasks; stale Workers cannot commit results. Failed
  contexts cannot resume. Start a new context after interruption.
- Back up PostgreSQL and the session volume together. Cross-volume/Pod relocation,
  native format upgrades, and point-in-time restoration are not verified procedures.

## Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
KIRBY_TEST_DATABASE_URL=postgresql://... uv run pytest -q -m postgres
KIRBY_RUN_GOOSE=1 uv run pytest -q tests/runtime
uv run python scripts/smoke.py --credentials-file /path/to/api_credentials.json
```

The default suite has no live model calls. Native tests use real Goose with a local
synthetic model server. The final smoke needs running API/Worker and the approved
OpenRouter key; it sends only repository synthetic inputs. Live quality and capacity
are separate from protocol correctness.
