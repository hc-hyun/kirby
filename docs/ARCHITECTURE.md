# Architecture

KIRBY uses the official A2A SDK externally and Goose over ACP internally.

```text
Client -> API -> PostgreSQL task queue -> Worker -> Goose -> model proxy -> OpenRouter
                   |                        |
             task snapshots           persistent context files
```

| Folder | Responsibility | Public interface |
|---|---|---|
| `api/` | Authentication, role cards, admission, task queries and streams | `create_app(store, roles, credentials)` |
| `storage/` | PostgreSQL contexts/tasks, deduplication, leases and completion | `Store` |
| `worker/` | One execution slot, claim, heartbeat, cancel and commit | `run_worker`, `execute_claimed` |
| `runtime/` | Goose sessions, native skills, output validation and model budgets | `GooseRuntime.run(TaskRecord, cancel) -> RunResult` |
| `roles/` | Local role validation and self-contained versioned manifests | `load_registry`, `build_manifest` |
| `contracts.py` | Records shared across these boundaries | `Principal`, `TaskRecord`, `RunResult` |

Components do not import each other's private functions. The API never starts Goose.
Runtime does not query PostgreSQL. Worker connects their public interfaces.

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
- Only the native read-only `load_skill` tool is enabled. Other native extensions
  and ACP file/terminal/permission callbacks are disabled. Goose's bundled skill
  descriptions may also be visible; these descriptions grant no extra tools.
- The loopback proxy holds the real model key and enforces model request count,
  output token parameter, free-model selection, and the allowed tool list.
- Results must satisfy JSON Schema. One correction is allowed in the same session
  and budget. A single surrounding JSON code fence is accepted; data is not invented.
- Streaming emits current persisted task snapshots and the final artifact. It may
  coalesce intermediate states; it is not a token-history or event-replay service.
- Interrupted/failed/canceled executions close their contexts. There is no automatic
  retry or migration of partially written native sessions. New work uses a new context.

The profile's MCP references are design examples. External MCP is not connected in
this release; current tools only read the bundled skills. See FUTURE.md for expansion.
