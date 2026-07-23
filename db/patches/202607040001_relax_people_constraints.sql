-- Relax people table constraints so bulk uploads accept any data format
-- Duplicate checking (UNIQUE on work_email, employee_number) is preserved

-- Drop email format check — allow any string or NULL in work_email
ALTER TABLE slmct.people
  DROP CONSTRAINT IF EXISTS people_email_required_for_active_login;

-- Allow full_name to be NULL (was NOT NULL)
ALTER TABLE slmct.people
  ALTER COLUMN full_name DROP NOT NULL;
