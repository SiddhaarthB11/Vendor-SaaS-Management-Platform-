-- Allow reusing organisation codes after an org is archived (is_active = false).

ALTER TABLE slmct.organisations DROP CONSTRAINT IF EXISTS organisations_code_key;
DROP INDEX IF EXISTS slmct.uq_organisations_code_active;
CREATE UNIQUE INDEX uq_organisations_code_active ON slmct.organisations (code) WHERE is_active = true;
