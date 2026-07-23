-- Prevent duplicate budget records for the same department + fiscal year per org.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'budgets_org_dept_fy_unique'
  ) THEN
    ALTER TABLE slmct.budgets
      ADD CONSTRAINT budgets_org_dept_fy_unique
      UNIQUE (organisation_id, department, fiscal_year);
  END IF;
END $$;
