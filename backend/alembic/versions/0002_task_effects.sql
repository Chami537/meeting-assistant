-- Transactional outbox for side effects that leave the database. Written in the same
-- transaction as the status change, executed afterwards by a dispatcher; handlers must be idempotent.
CREATE TABLE task_effects (
  id BIGSERIAL PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  effect TEXT NOT NULL CHECK (effect IN ('DISCARD_AUDIO','NOTIFY_USER')),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"to": ..., "reason": ..., "trigger": ...}; never secrets
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RUNNING','DONE','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- RUNNING rows use it as a lease, as in kf_inbox
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  done_at TIMESTAMPTZ,
  CHECK (status <> 'DONE' OR done_at IS NOT NULL)
);
CREATE INDEX idx_task_effects_due ON task_effects (next_attempt_at) WHERE status IN ('PENDING','RUNNING');
CREATE INDEX idx_task_effects_task ON task_effects (task_id);
