-- AI Meeting Assistant V1 · PostgreSQL 16 schema (initial design)
-- Note: secrets, meeting passwords and sensitive URLs should be encrypted by the application.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE kf_users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  corp_id TEXT NOT NULL,
  external_userid TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (corp_id, external_userid)
);

CREATE TABLE kf_inbox (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  open_kfid TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  user_id UUID REFERENCES kf_users(id),
  message_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  UNIQUE (open_kfid, msg_id)
);

CREATE TABLE kf_sync_state (
  open_kfid TEXT PRIMARY KEY,
  next_cursor TEXT,
  last_synced_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE meeting_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES kf_users(id),
  source_msg_id TEXT,
  idempotency_key TEXT,
  meeting_provider TEXT NOT NULL DEFAULT 'tencent_meeting',
  meeting_code TEXT,
  join_link_ciphertext BYTEA,
  meeting_password_ciphertext BYTEA,
  scheduled_at TIMESTAMPTZ,
  planned_end_at TIMESTAMPTZ,
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  display_name TEXT NOT NULL DEFAULT 'AI会议记录助手',
  consent_at TIMESTAMPTZ,
  consent_version TEXT,
  host_recording_approval_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'AWAITING_DETAILS' CHECK (status IN (
    'AWAITING_DETAILS','AWAITING_CONSENT','SCHEDULED','DISPATCHING',
    'JOINING','WAITING_ROOM','IN_MEETING','RECORDING',
    'UPLOADING','TRANSCRIBING','SUMMARIZING','COMPLETED',
    'CANCELED','FAILED'
  )),
  failure_code TEXT,
  failure_reason TEXT,
  actual_joined_at TIMESTAMPTZ,
  actual_ended_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, idempotency_key)
);
CREATE INDEX idx_meeting_tasks_schedule ON meeting_tasks (scheduled_at) WHERE status = 'SCHEDULED';
CREATE INDEX idx_meeting_tasks_user ON meeting_tasks (user_id, created_at DESC);

CREATE TABLE worker_nodes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  platform TEXT NOT NULL DEFAULT 'windows',
  capacity INTEGER NOT NULL DEFAULT 1 CHECK (capacity > 0),
  status TEXT NOT NULL DEFAULT 'OFFLINE' CHECK (status IN ('ONLINE','BUSY','OFFLINE','DISABLED')),
  last_heartbeat_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE task_attempts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  worker_id UUID REFERENCES worker_nodes(id),
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  lease_until TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'CLAIMED' CHECK (status IN ('CLAIMED','RUNNING','FINISHED','FAILED','EXPIRED')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,
  UNIQUE (task_id, attempt_no)
);
CREATE INDEX idx_task_attempts_lease ON task_attempts (lease_until) WHERE status IN ('CLAIMED','RUNNING');

CREATE TABLE audio_assets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  storage_provider TEXT NOT NULL DEFAULT 'cos',
  object_key TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  format TEXT NOT NULL,
  duration_seconds INTEGER,
  bytes BIGINT NOT NULL CHECK (bytes > 0),
  upload_status TEXT NOT NULL DEFAULT 'READY',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_id, file_sha256)
);

CREATE TABLE transcripts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN ('worker_asr','authorized_openapi','authorized_mcp')),
  body JSONB NOT NULL,
  language TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_id, version)
);

CREATE TABLE meeting_minutes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  transcript_id UUID REFERENCES transcripts(id),
  summary TEXT NOT NULL,
  decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
  action_items JSONB NOT NULL DEFAULT '[]'::jsonb,
  model_name TEXT,
  prompt_version TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_id)
);

CREATE TABLE task_events (
  id BIGSERIAL PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  data JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE callback_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider TEXT NOT NULL CHECK (provider IN ('wecom_kf','tencent_meeting')),
  external_event_id TEXT NOT NULL,
  event_type TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  UNIQUE (provider, external_event_id)
);

CREATE TABLE outgoing_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID REFERENCES meeting_tasks(id) ON DELETE SET NULL,
  user_id UUID NOT NULL REFERENCES kf_users(id),
  open_kfid TEXT NOT NULL,
  purpose TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','DEFERRED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ,
  sent_at TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_outgoing_pending ON outgoing_messages (next_attempt_at) WHERE status = 'PENDING';
