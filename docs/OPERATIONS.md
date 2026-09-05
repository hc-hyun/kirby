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
`KIRBY_ROLES_DIR` defaults to `agents`; each registered package owns its assets.
Run `python -m kirby.roles --root agents` before activation. Docker includes packages
at `/app/agents`, so the current Compose setup needs an image rebuild for content
changes. See [agent packages](AGENT_PACKAGES.md) for registration and shared versions.

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

## Optional MinIO

Start the existing `~/works/my-minio` stack first. KIRBY uses its `dev` bucket and
credentials from that stack's `.env`; it does not create or manage the MinIO service.
For host API/Worker processes, set:

```bash
export KIRBY_MINIO_ENDPOINT=http://127.0.0.1:9000
export KIRBY_MINIO_PUBLIC_ENDPOINT=http://127.0.0.1:9000
export KIRBY_MINIO_CREDENTIALS_FILE="$HOME/works/my-minio/.env"
```

Both API and Worker require these settings. The credentials file supplies
`MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD`; optional `KIRBY_MINIO_BUCKET` and
`KIRBY_MINIO_REGION` override the file/default values (`dev`, `us-east-1`). Leave
`KIRBY_MINIO_ENDPOINT` unset to disable file handling. Secret keys never enter
browser JavaScript, A2A messages or Goose; the browser receives temporary grants.

For Compose, use the optional overlay in every subsequent up/down command:

```bash
export KIRBY_MINIO_CREDENTIALS_FILE="$HOME/works/my-minio/.env"
export KIRBY_MINIO_ENDPOINT=http://minio:9000
export KIRBY_MINIO_PUBLIC_ENDPOINT=http://127.0.0.1:9000
docker compose -f compose.yaml -f compose.minio.yaml up --build -d
```

The overlay joins the existing `my-minio_default` network, mounts the credentials
as a secret, and limits API to 512 MiB and Worker to 1 GiB with no additional swap.
Set `KIRBY_MINIO_NETWORK` if the existing network has another name. The internal
endpoint must be reachable from API/Worker; the public endpoint must be reachable
from the browser because transfers go directly to that origin. MinIO must allow
the client's upload origin when browser CORS rules require it. For remote access,
use the externally reachable HTTPS S3 endpoint rather than container DNS/localhost.

Upload `.txt`, `.log`, `.csv`, `.json` or `.jsonl` through the browser client, up to
64 MiB per file and four per context. Continue the context to reuse its attachments.
Requests may also use the registered A2A file URL returned by
`POST /agents/{role_id}/files/{id}/complete`; reserve first with
`POST /agents/{role_id}/files`, then send the issued POST form directly to MinIO.
Inline/base64 files and arbitrary download URLs are not accepted.

Completed tasks include automatic JSON and CSV analysis reports with 15-minute
MinIO download grants. Authorized Task Get/List/Subscribe requests sign fresh links;
click **Get status** in the browser client after a link expires. The registered input
`/agents/{role_id}/files/{id}/content` URL checks tenant/subject/role before redirecting
to a grant. Issued grants remain usable by their holders until expiry.
Large inputs are sampled/searched through bounded
readers; output files contain the analysis result, not a complete input conversion.

Keep the context volume on disk, including temporary files. A memory-backed
`emptyDir` charges those bytes to Pod memory. Preserve MinIO objects together with
PostgreSQL and native sessions when backing up. No automatic file retention/GC or
cross-Pod session restoration is implemented. MinIO connectivity is checked by
file operations; `/readyz` continues to check the PostgreSQL schema.

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

For interactive checks, run the client and open `http://127.0.0.1:8088`:

```bash
uv run python -m kirby.client --credentials-file .local/credentials.json
```

The client requires only API credentials and binds to loopback. Set `--url` for a
different API address; base path prefixes are supported.
Its UI supports streamed, immediate/polled and blocking sends, Get/List/Subscribe,
Cancel, context continuation, direct file uploads and generated report downloads.
Closing the page stops observation;
use Cancel to cancel the actual task. A working cancel response is still pending.
