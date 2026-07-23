-- Devtools job history for master admin control panel

CREATE TABLE IF NOT EXISTS slmct.devtools_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('judge', 'autofix', 'watch')),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    params jsonb NOT NULL DEFAULT '{}'::jsonb,
    report jsonb,
    error text,
    actor_user_id uuid,
    actor_email text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_devtools_jobs_created
    ON slmct.devtools_jobs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_devtools_jobs_kind_status
    ON slmct.devtools_jobs (kind, status, created_at DESC);
