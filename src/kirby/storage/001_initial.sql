CREATE TABLE IF NOT EXISTS contexts (
    id text PRIMARY KEY,
    tenant text NOT NULL,
    subject text NOT NULL,
    role_id text NOT NULL,
    manifest jsonb NOT NULL,
    usable boolean NOT NULL DEFAULT true,
    session_id text,
    session_path text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id text PRIMARY KEY,
    context_id text NOT NULL REFERENCES contexts(id),
    tenant text NOT NULL,
    subject text NOT NULL,
    role_id text NOT NULL,
    message_id text NOT NULL,
    requested_context_id text,
    input_text text NOT NULL,
    state text NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed', 'canceled')),
    result jsonb,
    error text,
    cancel_requested boolean NOT NULL DEFAULT false,
    worker_id text,
    lease_expires_at timestamptz,
    session_id text,
    session_path text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant, subject, role_id, message_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_active_context
    ON tasks(context_id) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS tasks_queue
    ON tasks(created_at, id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS tasks_expired
    ON tasks(lease_expires_at) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS tasks_owner
    ON tasks(tenant, subject, role_id, created_at DESC, id);
