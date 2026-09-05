ALTER TABLE contexts ADD COLUMN IF NOT EXISTS attachments jsonb NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attachments jsonb NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS requested_attachments jsonb NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS output_files jsonb NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS files (
    id text PRIMARY KEY,
    tenant text NOT NULL,
    subject text NOT NULL,
    role_id text NOT NULL,
    filename text NOT NULL,
    media_type text NOT NULL,
    size bigint NOT NULL CHECK (size >= 0 AND size <= 67108864),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
    object_key text,
    etag text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status != 'ready' OR (object_key IS NOT NULL AND etag IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS files_owner ON files(tenant, subject, role_id, id);
