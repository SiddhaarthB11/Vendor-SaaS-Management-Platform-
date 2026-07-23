-- Relax contracts table constraints so bulk uploads accept any data format
-- Duplicate checking is preserved

ALTER TABLE slmct.contracts DROP CONSTRAINT IF EXISTS contracts_value_non_negative;
ALTER TABLE slmct.contracts DROP CONSTRAINT IF EXISTS contracts_currency_code_format;
ALTER TABLE slmct.contracts ALTER COLUMN title DROP NOT NULL;
ALTER TABLE slmct.contracts ALTER COLUMN contract_type DROP NOT NULL;
