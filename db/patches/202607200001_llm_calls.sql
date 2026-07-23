-- LLM call logging for cost, routing, and eval visibility.

CREATE TABLE IF NOT EXISTS slmct.llm_calls (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task text NOT NULL,
  model text NOT NULL,
  prompt_variant text,
  input_tokens integer,
  output_tokens integer,
  total_tokens integer,
  latency_ms integer NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_task_created
  ON slmct.llm_calls (task, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_calls_model_created
  ON slmct.llm_calls (model, created_at DESC);
