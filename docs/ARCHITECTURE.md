# Architecture

KIRBY uses the official A2A SDK externally and Goose over ACP internally.

```text
Client -> API -> PostgreSQL task queue -> Worker -> Goose -> model proxy -> OpenRouter
                   |                        |
             task snapshots           persistent context files
Client <-------------- MinIO <-------------> Worker
       direct transfers       bounded disk I/O
```

| Folder | Responsibility | Public interface |
|---|---|---|
| `client/` | Local browser UI and official SDK calls to a running API | `A2AClient`, `python -m kirby.client` |
| `api/` | Authentication, role cards, tasks and file authorization | `create_app(store, roles, credentials, object_store=...)` |
| `storage/` | PostgreSQL contexts/tasks/file references, leases and completion | `Store` |
| `worker/` | One execution slot, claim, heartbeat, cancel and commit | `run_worker`, `execute_claimed` |
| `runtime/` | Goose sessions, native skills, output validation and model budgets | `GooseRuntime.run(TaskRecord, cancel) -> RunResult` |
| `roles/` | Local role validation and self-contained versioned manifests | `load_registry`, `build_manifest` |
| `files/` | MinIO grants/transfers and bounded attachment tools | `ObjectStore`, `FileTools` |
| `contracts.py` | Records shared across these boundaries | `Principal`, `TaskRecord`, `RunResult` |

Components do not import each other's private functions. The API never starts Goose.
Runtime does not query PostgreSQL. Worker connects their public interfaces.
Agent-owned descriptors, skills, schemas and fixtures live in `agents/<id>/`.
The roles loader resolves package-local assets and explicit shared versions from
`agents/_shared`, then emits the same self-contained execution manifest. Only
`agents/registry.yaml` entries become API roles; source paths stay out of manifests.
The optional browser client uses HTTP only. Its loopback server holds the API token
and bridges SDK stream events as NDJSON; it has no database or model credentials.

## First operational release

- Role IDs map to `/agents/{role_id}/rpc` and a matching Agent Card URL.
- Configured bearer credentials bind tenant, subject and allowed roles. Credentials
  are loaded from a mounted file; request JSON cannot change identity.
- PostgreSQL atomically admits tasks, deduplicates message IDs, and limits each
  context to one active task. API restart does not lose admitted work or results.
- Contexts pin the full role/skill/schema/model manifest. Later role edits affect
  new contexts only. One role per context; a role switch starts a new context.
- Each task starts a fresh Goose process. Completed contexts resume their native
  Goose session from the same persistent context directory. One Worker is the
  deployment default; its session volume must survive restarts.
- Native `load_skill` and, for attachments, the three internal `kirbyfiles` MCP
  readers are allowed. Other native extensions and ACP file/terminal/permission
  callbacks are disabled. Bundled skill descriptions grant no extra tools.
- The loopback proxy holds the real model key and enforces model request count,
  output token parameter, free-model selection, and the allowed tool list.
- Results must satisfy JSON Schema. One correction is allowed in the same session
  and budget. A single surrounding JSON code fence is accepted; data is not invented.
- Streaming emits current persisted task snapshots and the final artifact. It may
  coalesce intermediate states; it is not a token-history or event-replay service.
- Interrupted/failed/canceled executions close their contexts. There is no automatic
  retry or migration of partially written native sessions. New work uses a new context.

## Optional file flow

1. API reserves a file for the authenticated tenant, subject and role, then issues
   a 15-minute MinIO POST grant. The browser sends bytes directly to MinIO.
2. Completion checks the object size and seals a private immutable copy. A2A sends
   its registered URL reference; arbitrary external URLs and inline file bytes are
   rejected. PostgreSQL retains metadata and ETag, never the file body.
3. Worker downloads each pinned attachment to its context directory in 64 KiB
   chunks. Goose receives file IDs and metadata, then calls the official MCP
   readers over an authenticated, per-turn loopback connection. Fresh processes
   reconnect these readers when resuming a context.
4. After schema validation, runtime writes `report.json` and `report.csv` to disk
   and uploads them using bounded parts with concurrency one. Worker publishes their references
   together with successful task completion; stale ownership cannot publish them.
5. Authorized task snapshots include fresh 15-minute MinIO result download URLs.
   Registered input URLs check authorization before redirecting to MinIO. The API
   and browser-client server do not buffer file bodies.

Inputs are `.txt`, `.log`, `.csv`, `.json` or `.jsonl`, at most 64 MiB each and four
per context, including prior turns. Readers treat them as UTF-8 text, replacing
invalid bytes. A read returns at most 200 lines/8 KiB of text; literal search scans
at most 64 MiB and returns at most 50 short matches. Both expose coverage and
truncation. These are bounded evidence tools, not a claim of full-file analysis.
CSV exports flatten result `items` or `observations` and escape spreadsheet formulas;
they do not transform every row of a large input file.

Skills describe analysis steps, evidence and limitations. Runtime logic supplies
authorized transfers, tool limits, cancellation and deterministic exports; adding
skill instructions alone cannot provide those capabilities. MinIO/model credentials
stay outside Goose. Profile MCP references remain examples; external organization
MCP endpoints are deferred separately from these internal attachment readers.
