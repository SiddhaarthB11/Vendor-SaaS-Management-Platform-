-- Core SLMCT operational modules: vendors, subscriptions, licences, budgets, and payments.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'record_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.record_status AS ENUM ('active', 'inactive', 'archived');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'subscription_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.subscription_status AS ENUM ('active', 'trial', 'pending_renewal', 'cancelled', 'expired');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'licence_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.licence_status AS ENUM ('available', 'assigned', 'suspended', 'revoked', 'expired');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.payment_status AS ENUM ('planned', 'pending', 'paid', 'failed', 'cancelled');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'budget_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.budget_status AS ENUM ('draft', 'approved', 'locked', 'closed');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS slmct.vendors (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  name text NOT NULL,
  legal_name text,
  website_url text,
  contact_name text,
  contact_email citext,
  status slmct.record_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organisation_id, name)
);

CREATE TABLE IF NOT EXISTS slmct.subscriptions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  vendor_id uuid REFERENCES slmct.vendors(id) ON DELETE SET NULL,
  name text NOT NULL,
  category text,
  owner_person_id uuid REFERENCES slmct.people(id) ON DELETE SET NULL,
  start_date date,
  renewal_date date,
  billing_cycle text NOT NULL DEFAULT 'annual',
  amount numeric(14,2) NOT NULL DEFAULT 0,
  currency_code char(3) NOT NULL DEFAULT 'AED',
  status slmct.subscription_status NOT NULL DEFAULT 'active',
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT subscriptions_amount_non_negative CHECK (amount >= 0),
  CONSTRAINT subscriptions_currency_code_format CHECK (currency_code ~ '^[A-Z]{3}$')
);

CREATE TABLE IF NOT EXISTS slmct.budgets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  fiscal_year integer NOT NULL,
  department text NOT NULL,
  allocated_amount numeric(14,2) NOT NULL,
  currency_code char(3) NOT NULL DEFAULT 'AED',
  status slmct.budget_status NOT NULL DEFAULT 'approved',
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organisation_id, fiscal_year, department),
  CONSTRAINT budgets_amount_non_negative CHECK (allocated_amount >= 0),
  CONSTRAINT budgets_currency_code_format CHECK (currency_code ~ '^[A-Z]{3}$')
);

CREATE TABLE IF NOT EXISTS slmct.payments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  subscription_id uuid REFERENCES slmct.subscriptions(id) ON DELETE SET NULL,
  vendor_id uuid REFERENCES slmct.vendors(id) ON DELETE SET NULL,
  budget_id uuid REFERENCES slmct.budgets(id) ON DELETE SET NULL,
  payment_date date,
  due_date date,
  amount numeric(14,2) NOT NULL,
  currency_code char(3) NOT NULL DEFAULT 'AED',
  status slmct.payment_status NOT NULL DEFAULT 'planned',
  reference text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT payments_amount_non_negative CHECK (amount >= 0),
  CONSTRAINT payments_currency_code_format CHECK (currency_code ~ '^[A-Z]{3}$')
);

CREATE TABLE IF NOT EXISTS slmct.licences (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  subscription_id uuid REFERENCES slmct.subscriptions(id) ON DELETE SET NULL,
  assigned_to_person_id uuid REFERENCES slmct.people(id) ON DELETE SET NULL,
  licence_name text NOT NULL,
  assigned_at date,
  expires_at date,
  status slmct.licence_status NOT NULL DEFAULT 'available',
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vendors_org ON slmct.vendors(organisation_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_org ON slmct.subscriptions(organisation_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_vendor ON slmct.subscriptions(vendor_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_renewal ON slmct.subscriptions(renewal_date);
CREATE INDEX IF NOT EXISTS idx_budgets_org_year ON slmct.budgets(organisation_id, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_payments_org_date ON slmct.payments(organisation_id, payment_date);
CREATE INDEX IF NOT EXISTS idx_payments_subscription ON slmct.payments(subscription_id);
CREATE INDEX IF NOT EXISTS idx_licences_org ON slmct.licences(organisation_id);
CREATE INDEX IF NOT EXISTS idx_licences_subscription ON slmct.licences(subscription_id);
CREATE INDEX IF NOT EXISTS idx_licences_assignee ON slmct.licences(assigned_to_person_id);
