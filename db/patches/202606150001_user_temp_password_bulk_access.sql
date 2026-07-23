-- Temporary password expiry and HR admin role for app access.

ALTER TABLE slmct.app_users
  ADD COLUMN IF NOT EXISTS temp_password_expires_at timestamptz;

INSERT INTO slmct.roles (code, name, description, can_login, is_system_role)
VALUES
  ('hr_admin', 'HR Admin', 'Manage employee records and assist with controlled bulk onboarding.', true, true)
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  can_login = EXCLUDED.can_login,
  is_system_role = EXCLUDED.is_system_role;

WITH hr_permissions(permission_code) AS (
  VALUES
    ('employees:manage'),
    ('licences:assign'),
    ('audit_logs:read')
)
INSERT INTO slmct.role_permissions (role_id, permission_id)
SELECT roles.id, permissions.id
FROM hr_permissions
JOIN slmct.roles roles ON roles.code = 'hr_admin'
JOIN slmct.permissions permissions ON permissions.code = hr_permissions.permission_code
ON CONFLICT DO NOTHING;
