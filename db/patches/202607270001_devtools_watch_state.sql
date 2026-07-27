-- Persisted Self-Heal Watch state, so it survives uvicorn --reload restarts.
--
-- Watch previously lived only as in-process Python globals. Any file change
-- (including the exact edits Autofix itself makes while healing) triggers a
-- worker restart under --reload, which silently reset watch to disabled with
-- no notification — the feature could turn itself off the moment it did its
-- job. This table lets the API resume watch automatically on startup if it
-- was left enabled, instead of requiring a human to notice and re-click it.

CREATE TABLE IF NOT EXISTS slmct.devtools_watch_state (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled boolean NOT NULL DEFAULT false,
    interval_sec integer NOT NULL DEFAULT 300,
    params jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO slmct.devtools_watch_state (id, enabled, interval_sec, params)
VALUES (1, false, 300, '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;
