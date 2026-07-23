-- Email log governance fields and employee portal login.

ALTER TABLE slmct.email_logs
  ADD COLUMN IF NOT EXISTS from_email text,
  ADD COLUMN IF NOT EXISTS workflow_stage text;

UPDATE slmct.roles
SET can_login = true,
    description = 'Employee portal user who can submit and track software requests.'
WHERE code = 'employee';
