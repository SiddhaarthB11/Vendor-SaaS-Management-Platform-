-- Phase 2b: AI Operator plans storage

CREATE TABLE IF NOT EXISTS slmct.operator_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id uuid,
    organisation_id uuid NOT NULL REFERENCES slmct.organisations(id) ON DELETE CASCADE,
    instruction text NOT NULL,
    plan jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'awaiting_confirmation', 'executing', 'completed', 'failed', 'cancelled')),
    step_results jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_operator_plans_org_created
    ON slmct.operator_plans (organisation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_operator_plans_actor
    ON slmct.operator_plans (actor_user_id, created_at DESC);
