-- Create vendor_catalogue table (missing from initial schema)
CREATE TABLE IF NOT EXISTS slmct.vendor_catalogue (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_id           uuid NOT NULL REFERENCES slmct.vendors(id) ON DELETE CASCADE,
    name                text NOT NULL,
    price               numeric(14, 2) NOT NULL DEFAULT 0,
    currency_code       text NOT NULL DEFAULT 'AED',
    scrape_url          text,
    password_change_url text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Add last_scraped_at to vendor_catalogue if missing
ALTER TABLE slmct.vendor_catalogue
    ADD COLUMN IF NOT EXISTS last_scraped_at timestamptz;

-- Add vendor_catalogue_id to subscriptions if missing
ALTER TABLE slmct.subscriptions
    ADD COLUMN IF NOT EXISTS vendor_catalogue_id uuid REFERENCES slmct.vendor_catalogue(id) ON DELETE SET NULL;
