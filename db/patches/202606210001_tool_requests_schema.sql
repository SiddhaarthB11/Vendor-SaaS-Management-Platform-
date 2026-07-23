DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tool_request_status' AND typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'slmct')) THEN
    CREATE TYPE slmct.tool_request_status AS ENUM (
      'new',
      'under_review',
      'converted_to_workflow',
      'rejected',
      'closed'
    );
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS slmct.tool_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  requester_name text NOT NULL,
  requester_email text NOT NULL,
  department text,
  requested_tool text NOT NULL,
  vendor_name text,
  category text,
  estimated_amount numeric(14, 2),
  currency_code char(3) NOT NULL DEFAULT 'AED',
  business_justification text,
  message_body text,
  email_subject text,
  status slmct.tool_request_status NOT NULL DEFAULT 'new',
  workflow_request_id uuid REFERENCES slmct.workflow_requests(id) ON DELETE SET NULL,
  reviewed_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  reviewed_at timestamptz,
  rejection_reason text,
  info_request_message text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tool_requests_org ON slmct.tool_requests(organisation_id);
CREATE INDEX IF NOT EXISTS idx_tool_requests_status ON slmct.tool_requests(status);
CREATE INDEX IF NOT EXISTS idx_tool_requests_created_at ON slmct.tool_requests(created_at DESC);
