-- Add line_manager_email to slmct.people so employees can be assigned to a line manager.
-- Nullable — existing rows are unaffected. HR/IT admin sets this field on the employee record.

ALTER TABLE slmct.people
  ADD COLUMN IF NOT EXISTS line_manager_email citext;
