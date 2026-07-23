-- Workflow governance: typed workflows, line manager approval, status history.

ALTER TYPE slmct.workflow_status ADD VALUE IF NOT EXISTS 'line_manager_approved';

ALTER TABLE slmct.workflow_requests
  ADD COLUMN IF NOT EXISTS workflow_type text NOT NULL DEFAULT 'generic_procurement',
  ADD COLUMN IF NOT EXISTS line_manager_approved_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS line_manager_approved_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_workflow_requests_workflow_type
  ON slmct.workflow_requests(workflow_type);

CREATE TABLE IF NOT EXISTS slmct.workflow_status_history (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_request_id uuid NOT NULL REFERENCES slmct.workflow_requests(id) ON DELETE CASCADE,
  from_status text,
  to_status text NOT NULL,
  actor_user_id uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  actor_email citext,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflow_status_history_request
  ON slmct.workflow_status_history(workflow_request_id, created_at DESC);

INSERT INTO slmct.roles (code, name, description, can_login, is_system_role)
VALUES
  ('line_manager', 'Line Manager', 'Review and approve employee software requests from their team.', true, true)
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  can_login = EXCLUDED.can_login,
  is_system_role = EXCLUDED.is_system_role;
