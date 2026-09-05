# KIRBY status

KIRBY = Knowledge & Instruction Runtime for Building Your-agent.

Updated: 2026-09-05. Implementation: first operational release complete and locally verified.
Scope: supplied text/file analysis, one Worker, persistent native sessions, and
configured bearer identities, with optional MinIO attachment/report transfers.
The original production-scale backlog is preserved in the input ZIP; deferred
items are listed in [FUTURE.md](docs/FUTURE.md).

## Implemented

- Independent `api/`, `worker/`, `runtime/`, `roles/`, `files/`, and `storage/` folders. Shared
  records live in `contracts.py`; each component exposes a small public interface.
- Role-specific A2A cards and JSON-RPC endpoints, authenticated catalog,
  Send/Get/List/Stream/Subscribe/Cancel, blocking and immediate responses.
- PostgreSQL admission, message deduplication, one active task per context,
  pinned manifests, leases, cancellation, and atomic result/session-reference commit.
- Separate Worker process with one Goose slot, ownership monitoring and drain.
- Native Goose skill loading, persistent context directories, fresh-process
  session resume, JSON Schema output validation and one bounded correction.
- Read-only native `load_skill` plus three bounded internal attachment MCP tools.
  A per-turn model proxy enforces tool allowlists, request count, token parameter
  and free-model routing; the real key never enters Goose.
- Docker image, Compose services and a minimal Helm chart for API + one Worker/PVC.
- **E2E01**: optional local browser client (`python -m kirby.client`) with role/card
  discovery, synthetic inputs, streamed/immediate/blocking sends, task history,
  Get/Subscribe/Cancel, explicit context continuation and JSON report downloads.
  Its separate folder uses the official HTTP SDK; API tokens stay on the client
  server, and agent API requests remain on the configured origin and base path.
- **FILES01**: optional MinIO direct browser uploads, scoped/immutable file references,
  context attachment inheritance, chunked disk downloads, native Goose MCP
  list/read/search, and automatic JSON/CSV analysis report uploads/downloads.
  One-turn MCP credentials rotate when the native session resumes. File bytes are
  excluded from initial model prompts and A2A requests. `file-evidence` supplies
  guidance; API/runtime/storage logic enforces transfer, access and size limits.
  Role profiles are version 1.1.0; existing contexts retain their pinned manifests.
- Replaced superseded spike implementations and lengthy planning docs; README is
  Korean, current operational documentation is English. Original ZIP is unchanged.
- **PKG02**: agent-owned descriptors, skills, schemas and fixtures now live in
  `agents/<id>/`. Explicit registration and shared version resolution replace the
  global example catalog. A package validation CLI and generic client/smoke fixture
  discovery are available. Both original execution manifests are unchanged.

Historical IDs covered within this release: B00–B06, B08 snapshot serving, B09 basic
cancel/lease handling, B10 configured identity, B11 per-turn budgets, B12 deployment
artifacts. This does not claim all former M2/M3 gates: OIDC, portable checkpoints,
scale-out/fairness, event history and chaos/load testing remain deferred.

## Verification

| Check | Result |
|---|---|
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS |
| `KIRBY_RUN_GOOSE=1 KIRBY_TEST_DATABASE_URL=... uv run pytest -q` | PASS: 37 tests, 12.27s; actual Goose/local synthetic provider and isolated PostgreSQL schemas |
| Default `uv run pytest -q` | PASS: 28 passed, 9 opt-in skips (7.50s); no external model calls |
| Package CLI and migration | PASS: complete pre/post manifests and digests match; packaged platform schemas load inside the Docker image |
| Third agent and package boundaries | PASS: explicit registration creates catalog/card/RPC routes without API edits; generic samples, duplicate keys, shared versions, path escape, symlinks and fixture/skill separation checked |
| Live package rollout | PASS: pre-migration log context continued with unchanged manifest/session ID/path; new VOC context uses current package manifest; MinIO downloads succeed |
| Browser package examples | PASS: both role sample buttons load their package fixture exactly; no JavaScript errors |
| Client contracts using official SDK + real API routes | PASS: continuation, task views, streams/cancel, local origin guards, sanitized errors and credential exclusion |
| Browser client + real API/Worker/Goose/OpenRouter | PASS: VOC stream, VOC immediate/polled continuation, log blocking response; 3 completed tasks |
| Chromium UI controls | PASS: queued-task stop/subscribe/cancel, history restoration, JSON download, 390px mobile layout, no JavaScript errors |
| Client JavaScript syntax / wheel assets | PASS: `node --check`; wheel includes client entry point and all static files |
| File API/client/storage contracts | PASS: pending-file rejection, owner/tenant/role scope, immutable references, continuation, cancellation fencing, direct transfer metadata and SDK file artifacts |
| Separate host API + Worker + PostgreSQL + real OpenRouter | PASS: VOC/log initial turns and continuations, 4 completed tasks |
| Native Goose with local synthetic model | PASS: required hints, lazy skill loading, one correction, fresh-process resume, key exclusion |
| Native Goose attachment MCP | PASS: actual search calls and fresh-process continuation to a new authenticated MCP endpoint; JSON/CSV exports |
| 50 MiB newline-free file reader | PASS: bounded read/search, under 1 MiB traced reader allocation |
| 50 MiB generated-file MinIO round trip inside 1 GiB Worker | PASS: SHA-256 match, 2.474s, 10.27 MiB peak Python allocation; container cumulative peak 283.42 MiB, zero OOM events; probe objects cleaned |
| Browser 50 MiB upload + real Goose/free OpenRouter + MinIO | PASS: stream initial turn and polled continuation; actual search/read calls on both turns; JSON/CSV direct downloads match validated output and source/line evidence |
| Live analysis memory with Worker limited to 1 GiB | PASS: cumulative Worker + Goose cgroup peak 192.88 MiB, zero OOM events; sequential synthetic case, not target-Pod/load validation |
| File UI | PASS: desktop and 390px viewport, no overflow/JavaScript errors; one direct MinIO upload, only 81/851/505-byte reservation/initial/follow-up request bodies through client bridge |
| Native files from real model runs | PASS: provider key absent from 31 files |
| `docker build -t kirby:local .` | PASS; Python/uv base images and Goose archive pinned by digest |
| `docker compose -p kirby-check up --build -d` | PASS: migration exited successfully, API/DB healthy, Worker started |
| Containerized live task | PASS: completed JSON result using one model request |
| `.tools/helm lint deploy/helm/kirby` / `helm template` | PASS; rendered YAML parses |
| User Kubernetes cluster / external MCP | NOT_RUN: no target configuration provided |

Live evidence: [agent packages](docs/evidence/agent-packages.json),
[MinIO files](docs/evidence/minio-files.json),
[browser client](docs/evidence/client-browser-smoke.json),
[service smoke](docs/evidence/service-smoke.json),
[Compose smoke](docs/evidence/compose-smoke.json). Older JSON evidence
files are historical and may show failures fixed by this release. The current model
is `cohere/north-mini-code:free`; no paid fallback was used.

The first live file task failed output validation because the model returned schema
metadata. The existing single correction now includes the specific validation error
and requests a data instance; the next initial turn and continuation both passed.
The failure is retained in the evidence and task history. File transmission/search
had already succeeded in that failed task.

## Operational limits

Failed, canceled or lease-expired tasks close their contexts. No automatic retry or
cross-Pod restoration is claimed. Native session files must stay on the same volume
and path. Streams poll durable task snapshots; they do not replay every token.
Bundled read-only skills and internal attachment MCP readers are connected. External
MCP references in the example catalog are not active connections. Built-in Goose
skill descriptions may also be visible; they cannot grant additional tools. OS
sandbox/egress validation is deferred.

Files: UTF-8 `.txt/.log/.csv/.json/.jsonl`, at most 64 MiB each and four per context.
Read/search return bounded evidence with coverage; scanning bytes does not imply
semantic analysis of the whole input. Exports contain validated analysis results,
not full-file bulk conversion. PDF/Office/archive parsing and retention/GC are
deferred. Issued MinIO grants remain usable for 15 minutes; authorized task reads
refresh output links. The session volume must use disk, not memory-backed storage.

## Next IDs

- **PKG03**: add exported manifest loading and separate read-only content releases
  when independent agent deployment is needed; deferred.
- **FILES02**: verify file transfers and memory usage on the target Kubernetes
  deployment with its disk-backed PVC, network and MinIO endpoint; **NOT_RUN** here.
- **FILES03**: define file/session retention and implement scoped staging/orphan GC
  before accumulating production data; deferred pending retention requirements.
- **E2E02**: run the same browser scenarios against the target environment after
  OPS01 supplies its API URL and test identity. Local browser checks are complete.
- **OPS01**: configure deployment secrets, public URL/TLS and the target PostgreSQL;
  apply the verified chart to a disposable namespace before an operational rollout.
- **INT01**: connect approved read-only MCP/OIDC when their endpoints and identity
  contract are supplied. Keep them behind the existing module boundaries.
- Other scale/recovery and corner-case work: [FUTURE.md](docs/FUTURE.md).

## Agent artifacts

**PKG01/PKG02 complete.** Agent-local assets and explicitly versioned shared skills
compile into the existing manifest contract. The global catalog was removed;
`examples/README.md` points to the new location. Runtime code, credentials and
generated task files retain separate ownership. Existing user changes were carried
into the new folders. See [package authoring](docs/AGENT_PACKAGES.md).

Current evidence: [migration and live checks](docs/evidence/agent-packages.json).
The earlier [layout probe](docs/evidence/agent-layout-review.json) is historical.
Independent exported content deployment, hot reload and automated release publishing
are **NOT_RUN / deferred** under PKG03. Current Compose includes agents in the image.

Git uses `main` and `origin` at `https://github.com/hc-hyun/kirby.git`.
The initial source snapshot excludes provider keys, local credentials, sessions,
downloaded tools, and virtual environments. User containers,
provider key file, and original planning ZIP were preserved. Historical test-only
containers and their temporary PostgreSQL volumes were removed.
The current local server image is `kirby:local`, digest
`sha256:1fdab2d658d466f1017967da4c56e258734c0438ad152ec5d937adaec3adb045`.
The source includes the client, MinIO and agent-package changes. Existing user changes,
MinIO configuration/data, credentials, database volume and sessions were preserved.

## Local services running

Started at the user's request on 2026-09-05 and left running:

- Browser client: `http://127.0.0.1:8088`, background PID `564408`;
  PID file `.local/client.pid`, log `.local/client.log`.
- API: `http://127.0.0.1:8000`; PostgreSQL and Worker use Compose project `kirby-local`.
- PASS: UI/assets, authenticated catalog and VOC card return HTTP 200; API readiness
  succeeds, API/DB are healthy, Worker is running, migration exited successfully.
- Private startup configuration: `.local/services.env`; persistent sessions:
  `.local/local-sessions/`; PostgreSQL volume: `kirby-local_postgres`.
- MinIO: existing S3 API `http://127.0.0.1:9000`, console `http://127.0.0.1:9001`,
  bucket `dev`; `.local/services.env` references its external `.env` as a secret.
- KIRBY uses `compose.yaml` plus `compose.minio.yaml`; API limit 512 MiB,
  Worker limit 1 GiB, session storage on disk.
- Stop the client with `kill 564408`; stop the stack while preserving its data with
  `docker compose -f compose.yaml -f compose.minio.yaml --env-file .local/services.env -p kirby-local down`.

The existing provider key and API credential files were reused without modification.
MinIO's existing service and other user containers were left running.
Next work: PKG03, followed by FILES02/FILES03 and E2E02/OPS01 as their prerequisites
become available.
