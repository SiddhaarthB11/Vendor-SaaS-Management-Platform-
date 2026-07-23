-- User provisioning and approval workflow model.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'workflow_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.workflow_status AS ENUM ('draft', 'submitted', 'finance_approved', 'rejected', 'completed', 'cancelled');
  END IF;
END $$;

ALTER TABLE slmct.app_users
  ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS slmct.workflow_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid REFERENCES slmct.organisations(id) ON DELETE SET NULL,
  requested_module text NOT NULL,
  requested_action text NOT NULL DEFAULT 'create',
  payload jsonb NOT NULL,
  status slmct.workflow_status NOT NULL DEFAULT 'submitted',
  requested_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  requested_by_email citext,
  finance_approved_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  finance_approved_at timestamptz,
  completed_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  completed_at timestamptz,
  activated_entity_id uuid,
  rejection_reason text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT workflow_requests_module_format CHECK (requested_module ~ '^[a-z][a-z0-9_-]*$'),
  CONSTRAINT workflow_requests_action_format CHECK (requested_action ~ '^[a-z][a-z0-9_]*$')
);

CREATE INDEX IF NOT EXISTS idx_workflow_requests_status ON slmct.workflow_requests(status);
CREATE INDEX IF NOT EXISTS idx_workflow_requests_module ON slmct.workflow_requests(requested_module);
CREATE INDEX IF NOT EXISTS idx_workflow_requests_org ON slmct.workflow_requests(organisation_id);
CREATE INDEX IF NOT EXISTS idx_workflow_requests_created_at ON slmct.workflow_requests(created_at DESC);
