DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_type
    WHERE typname = 'activation_method' AND typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'slmct')
  ) THEN
    CREATE TYPE slmct.activation_method AS ENUM (
      'company_account',
      'invitation_email',
      'license_key',
      'vendor_provisioned'
    );
  END IF;
END $$;

ALTER TABLE slmct.workflow_requests
  ADD COLUMN IF NOT EXISTS activation_method slmct.activation_method,
  ADD COLUMN IF NOT EXISTS activation_status text,
  ADD COLUMN IF NOT EXISTS assigned_employee_name text,
  ADD COLUMN IF NOT EXISTS assigned_employee_email text,
  ADD COLUMN IF NOT EXISTS activation_details jsonb NOT NULL DEFAULT '{}'::jsonb;
