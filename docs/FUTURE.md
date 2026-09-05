# Deferred work

The first release prioritizes ordinary requests and clear module ownership. These
items are deliberately outside its release claim, not silently marked complete.

| Area | Follow-up |
|---|---|
| External identity/data | OIDC/JWKS, organization claims, approved read-only MCP endpoints and per-user credentials |
| Strong execution isolation | OS/container sandbox per execution, network policy validation, built-in skill allowlist |
| Portable recovery | Immutable checkpoints/object storage, cross-Pod restore, graceful retry after interruption |
| Higher throughput | Multiple Worker placement, model quotas/fairness, KEDA, load-based sizing |
| Rich history | Durable token/tool event log, replay cursors, additional A2A List filters |
| Role delivery | Release publisher, archive ingestion, signed bundles, registry and rollback UI |
| Operations | Retention/GC, backup restore drills, dashboards/alerts, measured SLOs and load/chaos testing |
| Extended actions | Write approvals, arbitrary code sandbox, role handoff and outbound A2A |

Do not turn these into mandatory work for simple first-release changes. Add a feature
when a real requirement justifies it, with focused tests and a bounded interface.

The original detailed B00–B15/S01–S08 planning package remains in
`goose-a2a-runtime-plan_2026-09-05.zip`. STATUS.md describes the implemented release,
not completion of every historical production-scale gate.
