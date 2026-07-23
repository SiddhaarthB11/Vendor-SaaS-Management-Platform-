-- Create table for Contracts
-- (This patch previously also created slmct.discovered_apps for a SaaS
-- Discovery feature that was removed — see 202607140001_drop_discovered_apps.)

CREATE TABLE IF NOT EXISTS slmct.contracts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  vendor_id uuid REFERENCES slmct.vendors(id) ON DELETE SET NULL,
  subscription_id uuid REFERENCES slmct.subscriptions(id) ON DELETE SET NULL,
  title text NOT NULL,
  contract_number text,
  contract_type text NOT NULL,
  start_date date,
  end_date date,
  value numeric(14,2) NOT NULL DEFAULT 0,
  currency_code char(3) NOT NULL DEFAULT 'AED',
  auto_renew boolean NOT NULL DEFAULT false,
  notice_period_days integer NOT NULL DEFAULT 0,
  owner text,
  status text NOT NULL DEFAULT 'draft',
  document_name text,
  document_data text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT contracts_value_non_negative CHECK (value >= 0),
  CONSTRAINT contracts_currency_code_format CHECK (currency_code ~ '^[A-Z]{3}$')
);

CREATE INDEX IF NOT EXISTS idx_contracts_org ON slmct.contracts(organisation_id);
CREATE INDEX IF NOT EXISTS idx_contracts_vendor ON slmct.contracts(vendor_id);
CREATE INDEX IF NOT EXISTS idx_contracts_subscription ON slmct.contracts(subscription_id);
