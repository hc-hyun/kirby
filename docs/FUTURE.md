# Deferred work

The first release prioritizes ordinary requests and clear module ownership. These
items are deliberately outside its release claim, not silently marked complete.

| Area | Follow-up |
|---|---|
| External identity/data | OIDC/JWKS, organization claims, approved read-only MCP endpoints and per-user credentials |
| File processing | PDF/Office/archive parsers, full-file bulk transformations, arbitrary report formats, resumable browser uploads and file removal/replacement within a context |
| Strong execution isolation | OS/container sandbox per execution, network policy validation, built-in skill allowlist |
| Portable recovery | Immutable checkpoints/object storage, cross-Pod restore, graceful retry after interruption |
| Higher throughput | Multiple Worker placement, model quotas/fairness, KEDA, load-based sizing |
| Rich history | Durable token/tool event log, replay cursors, additional A2A List filters |
| Role delivery | Release publisher, archive ingestion, signed bundles, registry and rollback UI |
| Operations | File/staging/orphan retention and GC, backup restore drills, dashboards/alerts, measured SLOs and load/chaos testing |
| Extended actions | Write approvals, arbitrary code sandbox, role handoff and outbound A2A |

Do not turn these into mandatory work for simple first-release changes. Add a feature
when a real requirement justifies it, with focused tests and a bounded interface.

Optional MinIO attachment/result transfers and internal bounded MCP readers are
implemented. They do not provide portable native checkpoints, full-file LLM analysis
or external MCP authorization. Current JSON/CSV exports contain validated analysis
results; broader file processing needs explicit runtime/parser work as well as skills.

The original detailed B00–B15/S01–S08 planning package remains in
`goose-a2a-runtime-plan_2026-09-05.zip`. STATUS.md describes the implemented release,
not completion of every historical production-scale gate.
