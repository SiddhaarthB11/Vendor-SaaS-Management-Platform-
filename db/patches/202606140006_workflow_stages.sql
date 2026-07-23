-- Extended workflow stages for master approval, finance closure, and reopen.

ALTER TYPE slmct.workflow_status ADD VALUE IF NOT EXISTS 'master_approved';
ALTER TYPE slmct.workflow_status ADD VALUE IF NOT EXISTS 'finance_closed';
ALTER TYPE slmct.workflow_status ADD VALUE IF NOT EXISTS 'reopened';

ALTER TABLE slmct.workflow_requests
  ADD COLUMN IF NOT EXISTS master_approved_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS master_approved_at timestamptz,
  ADD COLUMN IF NOT EXISTS reopened_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS reopened_at timestamptz;
