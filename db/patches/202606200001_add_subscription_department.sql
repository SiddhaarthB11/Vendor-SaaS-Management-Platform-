-- Add department column to subscriptions table for department-level tracking
ALTER TABLE slmct.subscriptions ADD COLUMN IF NOT EXISTS department text;
