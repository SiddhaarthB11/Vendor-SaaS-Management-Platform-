ALTER TYPE slmct.workflow_status ADD VALUE IF NOT EXISTS 'info_requested';

ALTER TABLE slmct.workflow_requests
  ADD COLUMN IF NOT EXISTS info_request_message text;
