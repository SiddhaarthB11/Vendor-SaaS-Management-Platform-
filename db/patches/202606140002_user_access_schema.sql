-- User, role, and audit access model for SLMCT.
-- Roles:
--   master_admin: full platform access
--   finance: payments, costs, budgets
--   it_admin: subscriptions, vendors, licences
--   auditor: audit monitoring
-- Employees are tracked as people records and can receive licence/subscription
-- assignments without being granted application login access.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA IF NOT EXISTS slmct;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.user_status AS ENUM ('active', 'inactive', 'suspended');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'person_status' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.person_status AS ENUM ('active', 'inactive', 'left_org');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'audit_action' AND typnamespace = 'slmct'::regnamespace) THEN
    CREATE TYPE slmct.audit_action AS ENUM ('create', 'read', 'update', 'delete', 'login', 'logout', 'export', 'approve', 'reject');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS slmct.organisations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_id uuid REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT organisations_code_format CHECK (code ~ '^[a-z0-9][a-z0-9_-]*$')
);

CREATE TABLE IF NOT EXISTS slmct.roles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  description text NOT NULL,
  can_login boolean NOT NULL DEFAULT true,
  is_system_role boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT roles_code_format CHECK (code ~ '^[a-z][a-z0-9_]*$')
);

CREATE TABLE IF NOT EXISTS slmct.permissions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,
  name text NOT NULL,
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT permissions_code_format CHECK (code ~ '^[a-z][a-z0-9_:.*]*$')
);

ALTER TABLE slmct.permissions
  DROP CONSTRAINT IF EXISTS permissions_code_format;

ALTER TABLE slmct.permissions
  ADD CONSTRAINT permissions_code_format CHECK (code ~ '^[a-z][a-z0-9_:.*]*$');

CREATE TABLE IF NOT EXISTS slmct.role_permissions (
  role_id uuid NOT NULL REFERENCES slmct.roles(id) ON DELETE CASCADE,
  permission_id uuid NOT NULL REFERENCES slmct.permissions(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS slmct.people (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organisation_id uuid REFERENCES slmct.organisations(id) ON DELETE RESTRICT,
  employee_number text UNIQUE,
  full_name text NOT NULL,
  work_email citext UNIQUE,
  department text,
  job_title text,
  status slmct.person_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT people_email_required_for_active_login CHECK (work_email IS NULL OR work_email ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+[.][A-Z]{2,}$')
);

ALTER TABLE slmct.people
  DROP CONSTRAINT IF EXISTS people_email_required_for_active_login;

CREATE TABLE IF NOT EXISTS slmct.app_users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id uuid NOT NULL UNIQUE REFERENCES slmct.people(id) ON DELETE RESTRICT,
  email citext NOT NULL UNIQUE,
  password_hash text,
  status slmct.user_status NOT NULL DEFAULT 'active',
  mfa_enabled boolean NOT NULL DEFAULT false,
  last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT app_users_email_format CHECK (email ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+[.][A-Z]{2,}$')
);

ALTER TABLE slmct.app_users
  DROP CONSTRAINT IF EXISTS app_users_email_format;

ALTER TABLE slmct.app_users
  ADD CONSTRAINT app_users_email_format CHECK (email ~* '^[A-Z0-9._%+-]+@[A-Z0-9.-]+[.][A-Z]{2,}$');

CREATE TABLE IF NOT EXISTS slmct.user_roles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES slmct.app_users(id) ON DELETE CASCADE,
  role_id uuid NOT NULL REFERENCES slmct.roles(id) ON DELETE RESTRICT,
  organisation_id uuid REFERENCES slmct.organisations(id) ON DELETE CASCADE,
  assigned_by uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  assigned_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  CONSTRAINT user_roles_revocation_order CHECK (revoked_at IS NULL OR revoked_at >= assigned_at)
);

CREATE TABLE IF NOT EXISTS slmct.audit_logs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_user_id uuid REFERENCES slmct.app_users(id) ON DELETE SET NULL,
  actor_email citext,
  action slmct.audit_action NOT NULL,
  entity_type text NOT NULL,
  entity_id uuid,
  organisation_id uuid REFERENCES slmct.organisations(id) ON DELETE SET NULL,
  ip_address inet,
  user_agent text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT audit_logs_entity_type_format CHECK (entity_type ~ '^[a-z][a-z0-9_]*$')
);

CREATE INDEX IF NOT EXISTS idx_organisations_parent_id ON slmct.organisations(parent_id);
CREATE INDEX IF NOT EXISTS idx_people_organisation_id ON slmct.people(organisation_id);
CREATE INDEX IF NOT EXISTS idx_people_status ON slmct.people(status);
CREATE INDEX IF NOT EXISTS idx_app_users_status ON slmct.app_users(status);
CREATE INDEX IF NOT EXISTS idx_user_roles_user_id ON slmct.user_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_user_roles_role_id ON slmct.user_roles(role_id);
CREATE INDEX IF NOT EXISTS idx_user_roles_org_id ON slmct.user_roles(organisation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_roles_active_global
  ON slmct.user_roles(user_id, role_id)
  WHERE organisation_id IS NULL AND revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_roles_active_org
  ON slmct.user_roles(user_id, role_id, organisation_id)
  WHERE organisation_id IS NOT NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_user_id ON slmct.audit_logs(actor_user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON slmct.audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON slmct.audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_org_id ON slmct.audit_logs(organisation_id);

INSERT INTO slmct.organisations (code, name)
SELECT 'derisk360_group', 'Derisk360 Group'
WHERE NOT EXISTS (
  SELECT 1 FROM slmct.organisations WHERE code = 'derisk360_group' AND is_active = true
);

UPDATE slmct.organisations
SET name = 'Derisk360 Group', is_active = true, updated_at = now()
WHERE code = 'derisk360_group' AND is_active = true;

INSERT INTO slmct.roles (code, name, description, can_login, is_system_role)
VALUES
  ('master_admin', 'Master Admin', 'Full platform access across all organisations and modules.', true, true),
  ('finance', 'Finance', 'Manage payments, software costs, budgets, and finance reporting.', true, true),
  ('it_admin', 'IT Admin', 'Manage subscriptions, vendors, licences, and employee assignments.', true, true),
  ('employee', 'Employee', 'Non-login employee record used for licence and subscription assignment.', false, true),
  ('auditor', 'Auditor', 'Monitor audit logs and review access and activity history.', true, true)
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  can_login = EXCLUDED.can_login,
  is_system_role = EXCLUDED.is_system_role;

INSERT INTO slmct.permissions (code, name, description)
VALUES
  ('system:*', 'System administration', 'Full administrative control.'),
  ('users:manage', 'Manage users', 'Create, update, suspend, and assign application users.'),
  ('roles:manage', 'Manage roles', 'Assign and revoke user roles.'),
  ('organisations:manage', 'Manage organisations', 'Maintain parent and subsidiary organisation records.'),
  ('finance:payments:manage', 'Manage payments', 'Create and manage subscription payment records.'),
  ('finance:costs:manage', 'Manage costs', 'Create and manage actual software costs.'),
  ('finance:budgets:manage', 'Manage budgets', 'Create and manage allocated budgets.'),
  ('finance:reports:read', 'Read finance reports', 'View finance reporting and analytics.'),
  ('subscriptions:manage', 'Manage subscriptions', 'Create and manage software subscription records.'),
  ('vendors:manage', 'Manage vendors', 'Create and manage vendor records.'),
  ('licences:manage', 'Manage licences', 'Create and manage software licence records.'),
  ('licences:assign', 'Assign licences', 'Assign licences and subscriptions to employees.'),
  ('employees:manage', 'Manage employees', 'Create and manage employee records without granting login access.'),
  ('audit_logs:read', 'Read audit logs', 'View audit log entries.'),
  ('audit_reviews:manage', 'Manage audit reviews', 'Create and manage audit review actions.')
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description;

WITH role_permission_map(role_code, permission_code) AS (
  VALUES
    ('master_admin', 'system:*'),
    ('master_admin', 'users:manage'),
    ('master_admin', 'roles:manage'),
    ('master_admin', 'organisations:manage'),
    ('master_admin', 'finance:payments:manage'),
    ('master_admin', 'finance:costs:manage'),
    ('master_admin', 'finance:budgets:manage'),
    ('master_admin', 'finance:reports:read'),
    ('master_admin', 'subscriptions:manage'),
    ('master_admin', 'vendors:manage'),
    ('master_admin', 'licences:manage'),
    ('master_admin', 'licences:assign'),
    ('master_admin', 'employees:manage'),
    ('master_admin', 'audit_logs:read'),
    ('master_admin', 'audit_reviews:manage'),
    ('finance', 'finance:payments:manage'),
    ('finance', 'finance:costs:manage'),
    ('finance', 'finance:budgets:manage'),
    ('finance', 'finance:reports:read'),
    ('it_admin', 'subscriptions:manage'),
    ('it_admin', 'vendors:manage'),
    ('it_admin', 'licences:manage'),
    ('it_admin', 'licences:assign'),
    ('it_admin', 'employees:manage'),
    ('auditor', 'audit_logs:read'),
    ('auditor', 'audit_reviews:manage')
)
INSERT INTO slmct.role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM role_permission_map rpm
JOIN slmct.roles r ON r.code = rpm.role_code
JOIN slmct.permissions p ON p.code = rpm.permission_code
ON CONFLICT DO NOTHING;

CREATE OR REPLACE VIEW slmct.active_user_permissions AS
SELECT
  au.id AS user_id,
  au.email,
  ur.organisation_id,
  r.code AS role_code,
  p.code AS permission_code
FROM slmct.app_users au
JOIN slmct.user_roles ur ON ur.user_id = au.id
JOIN slmct.roles r ON r.id = ur.role_id
JOIN slmct.role_permissions rp ON rp.role_id = r.id
JOIN slmct.permissions p ON p.id = rp.permission_id
WHERE au.status = 'active'
  AND ur.revoked_at IS NULL
  AND r.can_login = true;
