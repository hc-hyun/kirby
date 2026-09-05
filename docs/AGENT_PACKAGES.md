# Agent packages

PKG02 is implemented. Each agent owns its descriptor, private skills, result schema
and synthetic fixtures under `agents/<id>/`. API, Worker, Goose integration and tool
implementations remain shared in `src/kirby/`.

```text
agents/
  registry.yaml
  log-analyst/
    agent.yaml
    README.md
    skills/log-analyst-role/SKILL.md
    skills/log-triage/SKILL.md
    schemas/result.schema.json
    fixtures/input.synthetic.json
    fixtures/result.synthetic.json
  voc-analyst/
    agent.yaml
    README.md
    skills/...
    schemas/result.schema.json
    fixtures/...
  _shared/
    releases.yaml
    skills/file-evidence/SKILL.md
```

## Authoring and activation

`agent.yaml` retains the role's identity, version, skill bindings, execution limits
and output schema ID. Its `assets` section resolves the actual source files:

```yaml
assets:
  skills:
    log-analyst-role:
      path: skills/log-analyst-role
      version: 1.0.0
    log-triage:
      path: skills/log-triage
      version: 1.0.0
    file-evidence:
      shared: file-evidence
      version: 1.0.0
  output_schema: schemas/result.schema.json
  fixtures:
    input: fixtures/input.synthetic.json
    result: fixtures/result.synthetic.json
```

Private paths are relative to the agent folder. Shared references resolve through
`_shared/releases.yaml`, which maps skill ID and version to a path relative to
`_shared`. Multiple shared versions can coexist using different source directories;
each final skill folder must have the skill ID as its basename. For example:

```yaml
file-evidence:
  1.0.0:
    path: skills/file-evidence
```

Keep published source versions immutable by review policy. To change shared
instructions, add a new version/path and update only the agent bindings that should
adopt it. The loader checks exact dependency versions; it does not implement a
remote release registry or enforce immutability against historical source edits.
Resolved bytes are embedded and hashed in each execution manifest.

`registry.yaml` explicitly enables agents:

```yaml
schema_version: 1
enabled:
  log-analyst: log-analyst/agent.yaml
  voc-analyst: voc-analyst/agent.yaml
```

The registry key, folder name and descriptor ID must agree. Unregistered folders do
not become endpoints. Duplicate YAML keys, missing versions, cross-agent paths,
path escape and symlinks are rejected. Fixtures must be inside the package, outside
skill folders; skill files are recursively read as UTF-8 and included in the bundle
size limit. Generate large fixtures locally rather than committing them.

To add an agent, copy a package, update its identity/instructions/result contract
and fixtures, and register it. Grant its role ID in the API credential file.
Validate before deploying:

```bash
uv run python -m kirby.roles --root agents
```

The command checks enabled packages, input size and example results against their
schemas, then prints execution digests. It does not call a model. API startup loads
registered packages and generates `/agents/<id>/rpc` and the Agent Card. Restart API
to activate source or credential changes. There is no hot reload.

## Runtime boundary

The loader removes authoring-only `assets` metadata and emits the existing execution
manifest: normalized `.agents/skills/<id>/...`, `.goosehints`, profile and result
schema. Source paths, fixtures and README files do not enter the model instructions.
A content change requires a release update; a pure relocation can preserve the
execution digest. Existing contexts retain their stored manifest and native session.
Keep their role registered and preserve the absolute session root and volume.

Package instructions do not enable executable tools. Runtime policy still controls
file access, allowed model/tool calls and credentials. Platform validation schema
and known reference names live in `src/kirby/roles/`; `model_profile`, `worker_pool`
and external MCP references do not automatically select pools or connect services.
Custom output shapes may also need a CSV export mapping: current CSV generation
uses `items`/`observations` or a summary fallback.

Generated task reports, user uploads and sessions belong in PostgreSQL, MinIO and
the session volume, not in agent source folders.

## Deployment and clients

`KIRBY_ROLES_DIR` defaults to `agents`. Docker packages the root at `/app/agents`,
including shared sources; role validation schemas are included in the Python wheel.
Current Compose deployment therefore uses an image rebuild for content changes.
A future exported manifest loader/separate content release remains PKG03.

The browser client and smoke script discover registered input fixtures through the
same loader, with no fixed VOC/log role list:

```bash
uv run python -m kirby.client --agents-dir agents --credentials-file .local/credentials.json
uv run python scripts/smoke.py --agents-dir agents --credentials-file .local/credentials.json
```

The smoke script calls only roles allowed by the test credential and fails when none
match. `--examples-dir` remains a client option alias for the new package root;
the obsolete `examples/catalogs.example.yaml` format is no longer loaded.

## Migration evidence

Both original execution manifests were compared in full before and after the
migration and matched. Regression checks retain their pre-migration digests.
A temporary third package verifies explicit registration, generic fixture lookup
and generated API routes. Native Goose/PostgreSQL contracts also remain covered.
Current service and continuation outcomes are recorded in STATUS.md and
[evidence/agent-packages.json](evidence/agent-packages.json).

The earlier [layout probe](evidence/agent-layout-review.json) is historical. PKG03
manifest export/loading, separate content mounts, publishers and automated rollout
remain deferred. No Goose source modification or session-volume migration is needed
for the package layout.
