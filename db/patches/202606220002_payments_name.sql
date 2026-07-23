-- Add a human-readable payment name/description for the Payments page.

ALTER TABLE slmct.payments
  ADD COLUMN IF NOT EXISTS name text;

UPDATE slmct.payments p
SET name = s.name
FROM slmct.subscriptions s
WHERE p.subscription_id = s.id
  AND (p.name IS NULL OR btrim(p.name) = '');

UPDATE slmct.payments p
SET name = v.name || ' payment'
FROM slmct.vendors v
WHERE p.vendor_id = v.id
  AND (p.name IS NULL OR btrim(p.name) = '');

UPDATE slmct.payments
SET name = reference
WHERE name IS NULL OR btrim(name) = '';
