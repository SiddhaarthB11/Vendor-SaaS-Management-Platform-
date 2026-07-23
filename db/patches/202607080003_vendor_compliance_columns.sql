-- Add vendor compliance columns missing from initial schema
ALTER TABLE slmct.vendors
    ADD COLUMN IF NOT EXISTS soc2_certified boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS iso27001_certified boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS gdpr_compliant boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS data_residency text;
