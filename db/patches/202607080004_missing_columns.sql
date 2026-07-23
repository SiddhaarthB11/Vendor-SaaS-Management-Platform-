-- Create fx_rates table if missing (definitions match the API's runtime expectations)
CREATE TABLE IF NOT EXISTS slmct.fx_rates (
    currency_code text PRIMARY KEY,
    rate_from_usd numeric(18, 8) NOT NULL,
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

-- Add pre_info_request_status to workflow_requests if missing
ALTER TABLE slmct.workflow_requests
    ADD COLUMN IF NOT EXISTS pre_info_request_status text;
