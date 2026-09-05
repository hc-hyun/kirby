# KIRBY development

KIRBY = Knowledge & Instruction Runtime for Building Your-agent.

- Read README.md and STATUS.md before changing the project. Preserve user changes.
- Keep the first operational release simple. Avoid broad frameworks, speculative recovery,
  and tests that merely repeat implementation. Put deferred cases in docs/FUTURE.md.
- Major components own their folders: api, worker, runtime, roles, storage. Shared records
  live in contracts.py. Use exported interfaces instead of another module's internals.
- Reuse Goose and the pinned official A2A/ACP SDKs. Do not invent protocol APIs or build
  another inference loop. Role YAML is KIRBY configuration, not Goose configuration.
- API admits/reads/cancels tasks; Worker executes them. PostgreSQL is the task ledger.
- Check authenticated tenant/subject/role on every resource access. Never trust identity
  supplied in message JSON. Isolate context files and never log credentials.
- Keep one task active per context and one Goose process active per worker. Preserve
  manifests for existing contexts. Stop execution when ownership is lost.
- Do not claim untested capabilities or recovery. Keep real tests opt-in, use synthetic
  inputs with the approved free OpenRouter model, and never choose a paid fallback.
- No deployment to user infrastructure or outbound messages without task authorization.
- Keep documentation short, in English except README.md. Record outcomes and the next
  task in STATUS.md; distinguish PASS, FAIL, NOT_RUN, and deferred work.

Required checks after meaningful changes:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```
