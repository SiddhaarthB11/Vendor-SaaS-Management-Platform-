-- Add it_confirmed value to workflow_status enum.
-- This value is used when IT confirms a completed offboarding workflow step.
ALTER TYPE slmct.workflow_status ADD VALUE IF NOT EXISTS 'it_confirmed';
