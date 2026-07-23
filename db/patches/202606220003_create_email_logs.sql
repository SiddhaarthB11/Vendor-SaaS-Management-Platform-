-- Create slmct.email_logs table to store backend email notification logs
CREATE TABLE IF NOT EXISTS slmct.email_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_request_id uuid REFERENCES slmct.workflow_requests(id) ON DELETE SET NULL,
  tool_request_id uuid REFERENCES slmct.tool_requests(id) ON DELETE SET NULL,
  event_type text NOT NULL,
  to_email text NOT NULL,
  cc_email text,
  subject text NOT NULL,
  body text NOT NULL,
  status text NOT NULL, -- SENT, FAILED, PENDING, MOCK_MODE
  provider_message_id text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_email_logs_workflow_request_id ON slmct.email_logs(workflow_request_id);
CREATE INDEX IF NOT EXISTS idx_email_logs_tool_request_id ON slmct.email_logs(tool_request_id);
