# KIRBY status

KIRBY = Knowledge & Instruction Runtime for Building Your-agent.

Updated: 2026-09-05. Implementation: first operational release complete and locally verified.
Scope: input-supplied
analysis, one Worker, persistent native sessions, and configured bearer identities.
The original production-scale backlog is preserved in the input ZIP; deferred
items are listed in [FUTURE.md](docs/FUTURE.md).

## Implemented

- Independent `api/`, `worker/`, `runtime/`, `roles/`, and `storage/` folders. Shared
  records live in `contracts.py`; each component exposes a small public interface.
- Role-specific A2A cards and JSON-RPC endpoints, authenticated catalog,
  Send/Get/List/Stream/Subscribe/Cancel, blocking and immediate responses.
- PostgreSQL admission, message deduplication, one active task per context,
  pinned manifests, leases, cancellation, and atomic result/session-reference commit.
- Separate Worker process with one Goose slot, ownership monitoring and drain.
- Native Goose skill loading, persistent context directories, fresh-process
  session resume, JSON Schema output validation and one bounded correction.
- Read-only native `load_skill` only. A per-turn model proxy enforces request count,
  token parameter and free-model routing; the real key never enters Goose.
- Docker image, Compose services and a minimal Helm chart for API + one Worker/PVC.
- Replaced superseded spike implementations and lengthy planning docs; README is
  Korean, current operational documentation is English. Original ZIP is unchanged.

Historical IDs covered within this release: B00–B06, B08 snapshot serving, B09 basic
cancel/lease handling, B10 configured identity, B11 per-turn budgets, B12 deployment
artifacts. This does not claim all former M2/M3 gates: OIDC, portable checkpoints,
scale-out/fairness, event history and chaos/load testing remain deferred.

## Verification

| Check | Result |
|---|---|
| `uv run ruff check .` | PASS |
| `uv run ruff format --check .` | PASS |
| `KIRBY_RUN_GOOSE=1 KIRBY_TEST_DATABASE_URL=... uv run pytest -q` | PASS: 20 tests, 7.05s |
| Default `uv run pytest -q` | PASS: 13 passed, 7 opt-in skips (3.06s); repeated before the initial commit |
| Latest API projection check: `uv run pytest -q tests/api` | PASS: 4 tests |
| Separate host API + Worker + PostgreSQL + real OpenRouter | PASS: VOC/log initial turns and continuations, 4 completed tasks |
| Native Goose with local synthetic model | PASS: required hints, lazy skill loading, one correction, fresh-process resume, key exclusion |
| Native files from real model runs | PASS: provider key absent from 31 files |
| `docker build -t kirby:local .` | PASS; Python/uv base images and Goose archive pinned by digest |
| `docker compose -p kirby-check up --build -d` | PASS: migration exited successfully, API/DB healthy, Worker started |
| Containerized live task | PASS: completed JSON result using one model request |
| `.tools/helm lint deploy/helm/kirby` / `helm template` | PASS; rendered YAML parses |
| User Kubernetes cluster / external MCP | NOT_RUN: no target configuration provided |

Live evidence: [service smoke](docs/evidence/service-smoke.json),
[Compose smoke](docs/evidence/compose-smoke.json). Older JSON evidence
files are historical and may show failures fixed by this release. The current model
is `cohere/north-mini-code:free`; no paid fallback was used.

## Operational limits

Failed, canceled or lease-expired tasks close their contexts. No automatic retry or
cross-Pod restoration is claimed. Native session files must stay on the same volume
and path. Streams poll durable task snapshots; they do not replay every token.
Only bundled read-only skills are connected. External MCP references in the example
catalog are not active connections. Built-in Goose skill descriptions may also be
visible; they cannot grant additional tools. OS sandbox/egress validation is deferred.

## Next IDs

- **OPS01**: configure deployment secrets, public URL/TLS and the target PostgreSQL;
  apply the verified chart to a disposable namespace before an operational rollout.
- **INT01**: connect approved read-only MCP/OIDC when their endpoints and identity
  contract are supplied. Keep them behind the existing module boundaries.
- Other scale/recovery and corner-case work: [FUTURE.md](docs/FUTURE.md).

Git uses `main` and `origin` at `https://github.com/hc-hyun/kirby.git`.
The initial source snapshot excludes provider keys, local credentials, sessions,
downloaded tools, and virtual environments. User containers,
provider key file, and original planning ZIP were preserved. Test-only containers
and their temporary PostgreSQL volumes were removed; no service is left running.
The local built image is `kirby:local`, digest
`sha256:833ef706f72f6f2ccb8a0601d85dbb56e60071117d5789b3b8670dbbda079e06`.
