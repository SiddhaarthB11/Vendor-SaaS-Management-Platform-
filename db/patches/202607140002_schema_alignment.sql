-- Align column definitions on installs whose fx_rates / vendor_catalogue /
-- vendors tables were created with earlier definitions (either by an older
-- version of a patch or by API runtime code). All statements are idempotent.

-- fx_rates: currency_code char(3) -> text, rate precision 18,6 -> 18,8
ALTER TABLE slmct.fx_rates
    ALTER COLUMN currency_code TYPE text,
    ALTER COLUMN rate_from_usd TYPE numeric(18, 8);

-- vendor_catalogue: enforce price/currency defaults and NOT NULL
UPDATE slmct.vendor_catalogue SET price = 0 WHERE price IS NULL;
UPDATE slmct.vendor_catalogue SET currency_code = 'AED' WHERE currency_code IS NULL;
ALTER TABLE slmct.vendor_catalogue
    ALTER COLUMN price SET DEFAULT 0,
    ALTER COLUMN price SET NOT NULL,
    ALTER COLUMN currency_code TYPE text,
    ALTER COLUMN currency_code SET DEFAULT 'AED',
    ALTER COLUMN currency_code SET NOT NULL;

-- vendors compliance flags: relax NOT NULL to match application expectations
ALTER TABLE slmct.vendors
    ALTER COLUMN soc2_certified DROP NOT NULL,
    ALTER COLUMN iso27001_certified DROP NOT NULL,
    ALTER COLUMN gdpr_compliant DROP NOT NULL;
