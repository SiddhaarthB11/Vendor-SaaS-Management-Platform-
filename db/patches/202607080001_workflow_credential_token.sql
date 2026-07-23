-- Add credential change token columns to workflow_requests (missing from initial schema)
ALTER TABLE slmct.workflow_requests
    ADD COLUMN IF NOT EXISTS credential_change_token text,
    ADD COLUMN IF NOT EXISTS credential_change_token_expires_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_wf_credential_token
    ON slmct.workflow_requests (credential_change_token)
    WHERE credential_change_token IS NOT NULL;
