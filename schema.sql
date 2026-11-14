-- AI Meeting Assistant · PostgreSQL 16 schema V1.1 (supersedes V1.0, archived in archive/v1.0/schema.sql)
-- Bracketed IDs such as [S1] refer to the issue table in 评审问题与修改方案_V1.0.md.
-- Conventions:
--   * Retention: a purge job deletes COS objects first, then deletes transcripts/minutes rows, clears
--     ciphertext and inbox/outbox text, and keeps the task row plus redacted audit rows. [S6]
--   * Every COS object of a task (audio segments, evidence screenshots) lives under tasks/{task_id}/,
--     so purge and erasure can also delete by prefix.
--   * User erasure is the only hard delete: COS objects -> audio_assets -> meeting_tasks (cascades) -> kf_users
--     (cascades kf_inbox and outbox). RESTRICT FKs enforce that order. consent_records survive as metadata-only
--     evidence until retain_until.
--   * Every *_ciphertext column is encrypted by the application; *_key_version names the key for rotation. [I2]
--   * task_events.data / failure_reason / last_error must never contain links, passwords or transcript text.
--   * No BEGIN/COMMIT inside: run with `psql -v ON_ERROR_STOP=1 -1` or as the Alembic baseline (one transaction).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------- WeCom KF
CREATE TABLE kf_users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  corp_id TEXT NOT NULL,
  external_userid TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_inbound_at TIMESTAMPTZ,              -- [F2] opens the 48h / 5-message reply window
  blocked_at TIMESTAMPTZ,                   -- [P4] abuse control
  deletion_requested_at TIMESTAMPTZ,        -- [S6] erasure request, completed by the purge job
  UNIQUE (corp_id, external_userid)
);

CREATE TABLE kf_inbox (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  open_kfid TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  user_id UUID REFERENCES kf_users(id) ON DELETE CASCADE,
  message_type TEXT NOT NULL,
  origin INTEGER,
  payload_redacted JSONB NOT NULL,          -- [S1] sync_msg item without text, links, passwords or external_userid
  content_ciphertext BYTEA,                 -- [S1] original text, encrypted; purged after 7 days at most
  content_key_version SMALLINT,
  -- PROCESSING rows use next_attempt_at as a lease: a crashed processor's rows become due again.
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PROCESSING','DONE','IGNORED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  content_purged_at TIMESTAMPTZ,
  ingest_seq BIGSERIAL,                     -- (migration 0003) insert order = sync_msg order; per-user apply order
  CHECK ((content_ciphertext IS NULL) = (content_key_version IS NULL)),
  UNIQUE (open_kfid, msg_id)
);
CREATE INDEX idx_kf_inbox_due ON kf_inbox (next_attempt_at) WHERE status IN ('PENDING','PROCESSING');
CREATE INDEX idx_kf_inbox_user ON kf_inbox (user_id, received_at DESC);
CREATE INDEX idx_kf_inbox_purge ON kf_inbox (received_at) WHERE content_ciphertext IS NOT NULL;
CREATE INDEX idx_kf_inbox_user_seq ON kf_inbox (user_id, ingest_seq);

-- Lock the row (SELECT ... FOR UPDATE) for the whole sync_msg loop; commit cursor together with inbox rows. [F3]
CREATE TABLE kf_sync_state (
  open_kfid TEXT PRIMARY KEY,
  next_cursor TEXT,
  pending_sync_token_ciphertext BYTEA,      -- callback Token for sync_msg, valid 10 min; cleared after a successful pull
  pending_sync_token_key_version SMALLINT,
  pending_sync_token_expires_at TIMESTAMPTZ,
  last_synced_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((pending_sync_token_ciphertext IS NULL) = (pending_sync_token_key_version IS NULL))
);
CREATE TRIGGER trg_kf_sync_state_updated BEFORE UPDATE ON kf_sync_state
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- Tasks
CREATE TABLE meeting_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES kf_users(id) ON DELETE RESTRICT,
  -- LIVE_MEETING: Worker joins and records. USER_UPLOAD / FORWARDED_MINUTES: fallback MVP, no Worker.
  task_kind TEXT NOT NULL DEFAULT 'LIVE_MEETING' CHECK (task_kind IN ('LIVE_MEETING','USER_UPLOAD','FORWARDED_MINUTES')),
  source_open_kfid TEXT,
  source_msg_id TEXT,
  idempotency_key TEXT NOT NULL,            -- [S3] KF-created tasks use 'kf:' || open_kfid || ':' || msgid
  meeting_provider TEXT NOT NULL DEFAULT 'tencent_meeting',
  meeting_code TEXT,
  join_link_ciphertext BYTEA,
  meeting_password_ciphertext BYTEA,
  secret_key_version SMALLINT,              -- [I2]
  secrets_purged_at TIMESTAMPTZ,            -- [I2] link/password cleared once the meeting has ended
  scheduled_at TIMESTAMPTZ,
  planned_end_at TIMESTAMPTZ,
  -- Recording stops at min(start + max_record_minutes, planned_end_at + 30 min) with outcome LIMIT_REACHED. [P4][A8]
  max_record_minutes INTEGER NOT NULL DEFAULT 180 CHECK (max_record_minutes BETWEEN 1 AND 480),
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  display_name TEXT NOT NULL DEFAULT 'AI会议记录助手' CHECK (display_name LIKE 'AI会议记录助手%'), -- [P2] server-generated
  consent_at TIMESTAMPTZ,                   -- snapshot; evidence lives in consent_records [P2]
  consent_version TEXT,
  host_recording_approval_at TIMESTAMPTZ,   -- approval of the CURRENT attempt; cleared before every rejoin
  status TEXT NOT NULL DEFAULT 'AWAITING_DETAILS' CHECK (status IN (
    'AWAITING_DETAILS','AWAITING_CONSENT','SCHEDULED','DISPATCHING',
    'JOINING','WAITING_ROOM','IN_MEETING','AWAITING_RECORDING_APPROVAL','RECORDING',
    'RETRY_WAIT','UPLOADING','TRANSCRIBING','SUMMARIZING','COMPLETED',
    'CANCELED','FAILED'
  )),                                       -- [A3]
  failure_code TEXT CHECK (failure_code IN (
    'NO_WORKER','WORKER_LOST','NETWORK_ERROR','CLIENT_ERROR',
    'MEETING_NOT_STARTED_TIMEOUT','WAITING_ROOM_TIMEOUT','HOST_REJECTED','MEETING_LOCKED','WRONG_PASSWORD',
    'MEETING_ENDED','REMOVED_BY_HOST',
    'RECORDING_APPROVAL_TIMEOUT','RECORDING_DENIED','CONSENT_REVOKED',
    'INVALID_UPLOAD','EMPTY_AUDIO','UPLOAD_FAILED','ASR_FAILED','LLM_FAILED','INTERNAL_ERROR'
  )),                                       -- [S11]
  failure_reason TEXT,
  recording_outcome TEXT CHECK (recording_outcome IN ('FULL','LIMIT_REACHED','STOPPED_BY_USER','INTERRUPTED')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  next_retry_at TIMESTAMPTZ,
  dispatch_deadline_at TIMESTAMPTZ,         -- set on every entry into DISPATCHING: max(now(), scheduled_at) + 10 min
  upload_deadline_at TIMESTAMPTZ,           -- after it, unfinished segments are FAILED and READY ones are used
  stop_requested_at TIMESTAMPTZ,            -- [A4] "stop and summarize", sent to Worker via heartbeat
  cancel_requested_at TIMESTAMPTZ,          -- [A4] "cancel and discard", sent to Worker via heartbeat
  lock_version INTEGER NOT NULL DEFAULT 0,  -- [A9] UPDATE ... WHERE id = $1 AND status = $2 AND lock_version = $3
  actual_joined_at TIMESTAMPTZ,
  actual_ended_at TIMESTAMPTZ,
  retention_until TIMESTAMPTZ,              -- [S6]
  content_purged_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, idempotency_key),
  CHECK ((source_open_kfid IS NULL) = (source_msg_id IS NULL)),
  CHECK (planned_end_at IS NULL OR scheduled_at IS NULL OR planned_end_at > scheduled_at),
  -- [S4] nothing leaves the intake states without consent; live meetings also need a window and a way to join
  CHECK (status IN ('AWAITING_DETAILS','AWAITING_CONSENT','CANCELED','FAILED') OR (
    consent_at IS NOT NULL AND consent_version IS NOT NULL
    AND (task_kind <> 'LIVE_MEETING' OR (
      scheduled_at IS NOT NULL AND planned_end_at IS NOT NULL
      AND (meeting_code IS NOT NULL OR join_link_ciphertext IS NOT NULL OR secrets_purged_at IS NOT NULL))))),
  CHECK (task_kind = 'LIVE_MEETING' OR status NOT IN (
    'SCHEDULED','DISPATCHING','JOINING','WAITING_ROOM','IN_MEETING','AWAITING_RECORDING_APPROVAL','RECORDING','RETRY_WAIT')),
  CHECK (status <> 'RECORDING' OR host_recording_approval_at IS NOT NULL),
  -- a new attempt must obtain its own approval, so the snapshot is empty in every pre-recording state
  CHECK (status NOT IN ('SCHEDULED','DISPATCHING','JOINING','WAITING_ROOM','IN_MEETING',
                        'AWAITING_RECORDING_APPROVAL','RETRY_WAIT') OR host_recording_approval_at IS NULL),
  CHECK (status <> 'FAILED' OR failure_code IS NOT NULL),
  CHECK (status IN ('FAILED','RETRY_WAIT') OR failure_code IS NULL),  -- RETRY_WAIT keeps the last error
  CHECK (status <> 'RETRY_WAIT' OR next_retry_at IS NOT NULL),
  CHECK (status <> 'DISPATCHING' OR dispatch_deadline_at IS NOT NULL),
  CHECK (status <> 'UPLOADING' OR upload_deadline_at IS NOT NULL),
  CHECK (secret_key_version IS NOT NULL OR (join_link_ciphertext IS NULL AND meeting_password_ciphertext IS NULL))
);
CREATE INDEX idx_meeting_tasks_schedule ON meeting_tasks (scheduled_at) WHERE status = 'SCHEDULED';
CREATE INDEX idx_meeting_tasks_dispatch ON meeting_tasks (dispatch_deadline_at) WHERE status = 'DISPATCHING';
CREATE INDEX idx_meeting_tasks_retry ON meeting_tasks (next_retry_at) WHERE status = 'RETRY_WAIT';
-- [A8] capacity check at confirmation time, over the states that occupy a Worker
CREATE INDEX idx_meeting_tasks_window ON meeting_tasks (scheduled_at, planned_end_at)
  WHERE status IN ('SCHEDULED','DISPATCHING','JOINING','WAITING_ROOM','IN_MEETING',
                   'AWAITING_RECORDING_APPROVAL','RECORDING','RETRY_WAIT');
-- [A9] stuck post-processing; live states are watched through task_attempts.lease_until instead
CREATE INDEX idx_meeting_tasks_postproc ON meeting_tasks (updated_at)
  WHERE status IN ('UPLOADING','TRANSCRIBING','SUMMARIZING');
CREATE INDEX idx_meeting_tasks_user ON meeting_tasks (user_id, created_at DESC);
CREATE INDEX idx_meeting_tasks_retention ON meeting_tasks (retention_until) WHERE content_purged_at IS NULL;
CREATE TRIGGER trg_meeting_tasks_updated BEFORE UPDATE ON meeting_tasks
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- Workers
CREATE TABLE worker_nodes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL UNIQUE,
  platform TEXT NOT NULL DEFAULT 'windows',
  capacity INTEGER NOT NULL DEFAULT 1 CHECK (capacity = 1),  -- [S2] V1: one meeting per Worker
  status TEXT NOT NULL DEFAULT 'OFFLINE' CHECK (status IN ('ONLINE','OFFLINE','DISABLED')), -- busy = has active attempt
  token_hash TEXT NOT NULL,                 -- [I1] per-Worker credential (sha256 of a random token)
  token_rotated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_worker_nodes_updated BEFORE UPDATE ON worker_nodes
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- id doubles as the fencing token: every Worker call carries it. [A2]
-- On LEFT the attempt becomes FINISHED and the Worker is free; sealed segments of FINISHED/EXPIRED attempts may
-- still be uploaded within 2h, up to last_sealed_segment_no.
CREATE TABLE task_attempts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  worker_id UUID REFERENCES worker_nodes(id),
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  lease_until TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  last_event_seq INTEGER NOT NULL DEFAULT 0,
  last_sealed_segment_no INTEGER,           -- late uploads after FINISHED/EXPIRED are capped at this segment
  status TEXT NOT NULL DEFAULT 'CLAIMED' CHECK (status IN ('CLAIMED','RUNNING','FINISHED','FAILED','EXPIRED','CANCELED')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,
  UNIQUE (task_id, attempt_no),
  UNIQUE (id, task_id),
  CHECK (status NOT IN ('CLAIMED','RUNNING') OR (worker_id IS NOT NULL AND lease_until IS NOT NULL AND ended_at IS NULL))
);
CREATE UNIQUE INDEX ux_task_attempts_active_task ON task_attempts (task_id) WHERE status IN ('CLAIMED','RUNNING');
CREATE UNIQUE INDEX ux_task_attempts_active_worker ON task_attempts (worker_id) WHERE status IN ('CLAIMED','RUNNING');
CREATE INDEX idx_task_attempts_lease ON task_attempts (lease_until) WHERE status IN ('CLAIMED','RUNNING');

-- [P2] one row per consent fact; revocation is recorded, never overwritten.
-- Metadata only (no message text), so it outlives the task as evidence until retain_until.
CREATE TABLE consent_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID REFERENCES meeting_tasks(id) ON DELETE SET NULL,
  attempt_id UUID REFERENCES task_attempts(id) ON DELETE SET NULL,  -- in-meeting facts are per attempt
  user_ref_hash TEXT NOT NULL,              -- sha256 of corp_id || ':' || external_userid
  kind TEXT NOT NULL CHECK (kind IN (
    'REQUESTER_AUTHORITY','RECORDING_NOTICE_SENT','HOST_RECORDING_APPROVAL','PARTICIPANT_OBJECTION')),
  subject TEXT NOT NULL CHECK (subject IN ('requester','host','participant','worker')),
  channel TEXT NOT NULL CHECK (channel IN ('wecom_kf','meeting_chat','meeting_ui','api','web_upload')), -- web_upload: 0004
  evidence_ref TEXT,                        -- kf msg_id, task_events.id or screenshot object key
  consent_version TEXT NOT NULL,
  text_sha256 TEXT NOT NULL,                -- hash of the exact wording shown
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at TIMESTAMPTZ,
  revoke_reason TEXT,
  retain_until TIMESTAMPTZ NOT NULL         -- period set by legal review
);
CREATE INDEX idx_consent_records_task ON consent_records (task_id, kind);
CREATE INDEX idx_consent_records_retention ON consent_records (retain_until);

-- [A5] Worker events are idempotent per (attempt_id, event_seq); out-of-order seq is rejected
CREATE TABLE task_events (
  id BIGSERIAL PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  attempt_id UUID REFERENCES task_attempts(id) ON DELETE CASCADE,
  event_seq INTEGER,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  data JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((attempt_id IS NULL) = (event_seq IS NULL)),
  CHECK (CASE WHEN attempt_id IS NOT NULL THEN event_type IN (   -- Worker events
           'JOIN_REQUESTED','IN_WAITING_ROOM','JOINED','RECORDING_NOTICE_SENT','RECORDING_NOTICE_FAILED',
           'RECORDING_APPROVED','RECORDING_DENIED','RECORDING_STARTED','SEGMENT_SEALED','RECORDING_STOPPED',
           'REMOVED_FROM_MEETING','MEETING_ENDED','LEFT','FAILED')
         ELSE event_type IN (                                      -- server events
           'STATE_CHANGED','CONSENT_RECORDED','FILE_RECEIVED','STOP_REQUESTED','CANCEL_REQUESTED',
           'LEASE_EXPIRED','SECRETS_PURGED','CONTENT_PURGED') END),
  UNIQUE (attempt_id, event_seq)
);
CREATE INDEX idx_task_events_task ON task_events (task_id, id);

-- ---------------------------------------------------------------- Audio and post-processing
CREATE TABLE audio_assets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE RESTRICT,  -- [S6] delete COS object first
  source TEXT NOT NULL DEFAULT 'worker' CHECK (source IN ('worker','user_upload')),
  attempt_id UUID,
  segment_no INTEGER NOT NULL CHECK (segment_no >= 0),
  storage_provider TEXT NOT NULL DEFAULT 'cos',
  object_key TEXT NOT NULL UNIQUE,          -- tasks/{task_id}/audio/...
  cos_upload_id TEXT,                       -- [A6] multipart upload resume
  file_sha256 TEXT NOT NULL,                -- declared in upload-url, checked against COS in complete
  format TEXT NOT NULL,
  duration_ms INTEGER CHECK (duration_ms > 0),
  bytes BIGINT NOT NULL CHECK (bytes > 0),
  recorded_started_at TIMESTAMPTZ,          -- Worker clock corrected by the offset returned at lease time
  recorded_ended_at TIMESTAMPTZ,
  rms_dbfs NUMERIC(5,1),                    -- empty-audio detection
  upload_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (upload_status IN ('PENDING','UPLOADING','READY','FAILED','DELETED')),
  verified_at TIMESTAMPTZ,                  -- server checked object size and hash
  retention_until TIMESTAMPTZ NOT NULL,
  deleted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (attempt_id, task_id) REFERENCES task_attempts(id, task_id) ON DELETE RESTRICT,
  UNIQUE (attempt_id, segment_no),
  CHECK ((source = 'worker') = (attempt_id IS NOT NULL)),
  CHECK (source <> 'worker' OR (recorded_started_at IS NOT NULL AND recorded_ended_at > recorded_started_at)),
  CHECK (upload_status <> 'READY' OR verified_at IS NOT NULL),
  CHECK (upload_status <> 'DELETED' OR deleted_at IS NOT NULL)
);
CREATE UNIQUE INDEX ux_audio_assets_upload_segment ON audio_assets (task_id, segment_no) WHERE attempt_id IS NULL;
CREATE INDEX idx_audio_assets_task ON audio_assets (task_id, recorded_started_at);
CREATE INDEX idx_audio_assets_retention ON audio_assets (retention_until) WHERE deleted_at IS NULL;
CREATE TRIGGER trg_audio_assets_updated BEFORE UPDATE ON audio_assets
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- [A7] persisted before calling the provider so a restart polls instead of resubmitting.
-- A PENDING row with attempts > 0 means "submit may have happened": alert instead of blind retry.
CREATE TABLE processing_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('AUDIO_NORMALIZE','ASR','SUMMARY')),
  input_version INTEGER NOT NULL DEFAULT 1,
  provider TEXT NOT NULL,
  -- Tencent Cloud ASR TaskId: valid 24h and may repeat across days, so never unique on its own;
  -- results are kept by the provider for 24h, so polling must finish before submitted_at + 24h.
  external_job_id TEXT,
  submitted_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SUBMITTED','SUCCEEDED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_poll_at TIMESTAMPTZ,
  last_error TEXT,
  billed_audio_seconds INTEGER,             -- [P4] cost tracking
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_cents INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_id, kind, input_version),
  CHECK (status <> 'SUBMITTED' OR (external_job_id IS NOT NULL AND submitted_at IS NOT NULL))
);
CREATE INDEX idx_processing_jobs_due ON processing_jobs (next_poll_at) WHERE status IN ('PENDING','SUBMITTED');
CREATE TRIGGER trg_processing_jobs_updated BEFORE UPDATE ON processing_jobs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE transcripts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN (
    'worker_asr','upload_asr','forwarded_text','authorized_openapi','authorized_mcp')),
  processing_job_id UUID REFERENCES processing_jobs(id) ON DELETE SET NULL,
  body JSONB NOT NULL,
  language TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_id, version),
  UNIQUE (id, task_id)
);

-- [S8] versioned like transcripts; the composite FK keeps transcript and minutes on the same task.
-- Each element of decisions / action_items carries evidence_segments, validated by the output JSON Schema.
CREATE TABLE meeting_minutes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  transcript_id UUID NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  is_current BOOLEAN NOT NULL DEFAULT true,
  summary TEXT NOT NULL,
  decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
  action_items JSONB NOT NULL DEFAULT '[]'::jsonb,
  risks JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- (migration 0005) minutes-v2: key points, facts and figures, plans and expectations, and the name fixes
  -- made against the meeting title (written spelling + what the transcript said)
  key_points JSONB NOT NULL DEFAULT '[]'::jsonb,
  key_facts JSONB NOT NULL DEFAULT '[]'::jsonb,
  outlook JSONB NOT NULL DEFAULT '[]'::jsonb,
  name_fixes JSONB NOT NULL DEFAULT '[]'::jsonb,
  -- (migration 0006) minutes-v3: the meeting's type (minutes.MEETING_TYPES) and date, shown with the minutes
  -- for filing; meeting_date_source: 预约时间, 文件名, 会上提到 or 上传日期. None of them before v3.
  meeting_type TEXT,
  meeting_date DATE,
  meeting_date_source TEXT,
  model_provider TEXT,                      -- [P3] which provider / region processed the transcript
  model_name TEXT,
  model_region TEXT,
  prompt_version TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (transcript_id, task_id) REFERENCES transcripts(id, task_id) ON DELETE CASCADE,
  UNIQUE (task_id, version)
);
CREATE UNIQUE INDEX ux_meeting_minutes_current ON meeting_minutes (task_id) WHERE is_current;

-- (migration 0004) Material a user hands in for the fallback path (design doc §4 路径 D): a recording sent through WeChat KF,
-- a recording staged in COS (CLI demo, web upload), or forwarded minutes / transcript text. The payload is
-- encrypted like message content (AAD task_inputs.payload:<task_id>) and cleared once consumed.
CREATE TABLE task_inputs (
  task_id UUID PRIMARY KEY REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('WECOM_MEDIA','STAGED_OBJECT','FORWARDED_TEXT')),
  payload_ciphertext BYTEA,                 -- media_id / COS key / text
  payload_key_version SMALLINT,
  source_msg_type TEXT,                     -- kf msgtype it came from (file, video, voice, text), if any
  file_name TEXT,                           -- display only, never used as a path
  expires_at TIMESTAMPTZ,                   -- WeChat media_id validity
  consumed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((payload_ciphertext IS NULL) = (payload_key_version IS NULL)),
  CHECK (consumed_at IS NOT NULL OR payload_ciphertext IS NOT NULL)
);

-- Transactional outbox for side effects that leave the database (migration 0002). Written in the same
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

-- ---------------------------------------------------------------- Callbacks and delivery
CREATE TABLE callback_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- WeCom KF callbacks are only "go pull" notifications; their dedup is kf_inbox (open_kfid, msg_id).
  provider TEXT NOT NULL CHECK (provider IN ('tencent_meeting')),
  external_event_id TEXT NOT NULL,          -- Tencent Meeting trace_id
  event_type TEXT,
  payload_ciphertext BYTEA,                 -- decrypted body, re-encrypted; read by the async processor
  payload_key_version SMALLINT,
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','DONE','IGNORED','FAILED')), -- [S13]
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  CHECK ((payload_ciphertext IS NULL) = (payload_key_version IS NULL)),
  UNIQUE (provider, external_event_id)
);
CREATE INDEX idx_callback_events_pending ON callback_events (received_at) WHERE status = 'PENDING';

CREATE TABLE outgoing_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID REFERENCES meeting_tasks(id) ON DELETE CASCADE,         -- [S6] was SET NULL, leaving content behind
  user_id UUID NOT NULL REFERENCES kf_users(id) ON DELETE CASCADE,
  open_kfid TEXT NOT NULL,
  purpose TEXT NOT NULL,
  -- [S7] system notices: task_id:purpose; replies to a user request: task_id:purpose:<inbound msgid>
  dedup_key TEXT NOT NULL UNIQUE,
  -- Short notices only. RESULT_LINK rows store no token: the link is minted at send time and never persisted.
  content TEXT NOT NULL,
  -- SENDING rows use next_attempt_at as a lease, same as kf_inbox
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENDING','SENT','DEFERRED','FAILED','CANCELED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- [F1] msgid passed to kf/send_msg: id hex (27 chars) || 'g' || send_generation (4 digits), 32 chars,
  -- unique per KF account; written before the call so msg_send_fail (fail_msgid) always matches
  wecom_msgid TEXT UNIQUE,
  sent_at TIMESTAMPTZ,
  last_error TEXT,
  content_purged_at TIMESTAMPTZ,            -- purge sets content = ''
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- (migration 0003) msgid generation: bumped when a DEFERRED message is released, since WeCom rejects a
  -- reused msgid; a retry after a timeout keeps it, and "msgid 重复" (95033) then means already accepted.
  send_generation SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX idx_outgoing_due ON outgoing_messages (next_attempt_at) WHERE status IN ('PENDING','SENDING');
CREATE INDEX idx_outgoing_deferred ON outgoing_messages (user_id, created_at) WHERE status = 'DEFERRED';
CREATE TRIGGER trg_outgoing_messages_updated BEFORE UPDATE ON outgoing_messages
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- [F5] G4 only: STS tickets arrive via webhook (callback_events) and are refreshed before expires_at
CREATE TABLE tmeeting_credentials (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind TEXT NOT NULL CHECK (kind IN ('STS_TOKEN','OAUTH_ACCESS','OAUTH_REFRESH')),
  app_id TEXT NOT NULL,
  token_ciphertext BYTEA NOT NULL,
  key_version SMALLINT NOT NULL,
  req_id TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tmeeting_credentials_expiry ON tmeeting_credentials (kind, expires_at);

-- ---------------------------------------------------------------- Result page [A10]
-- GET shows a landing page only; the user's POST redeems the token atomically:
--   UPDATE ... SET used_at = now(), session_hash = $s, session_expires_at = $e
--   WHERE token_hash = $h AND used_at IS NULL AND expires_at > now() AND revoked_at IS NULL RETURNING id
CREATE TABLE result_access_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES kf_users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ,
  session_hash TEXT UNIQUE,
  session_expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- (migration 0004) UPLOAD links: redeemed by the upload itself, never open a session
  purpose TEXT NOT NULL DEFAULT 'RESULT' CHECK (purpose IN ('RESULT','UPLOAD')),
  CHECK ((session_hash IS NULL) = (session_expires_at IS NULL)),
  CHECK (session_hash IS NULL OR used_at IS NOT NULL),
  CONSTRAINT result_access_tokens_upload_no_session CHECK (purpose = 'RESULT' OR session_hash IS NULL)
);
CREATE INDEX idx_result_access_tokens_task ON result_access_tokens (task_id);

CREATE TABLE result_access_logs (
  id BIGSERIAL PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES meeting_tasks(id) ON DELETE CASCADE,
  token_id UUID REFERENCES result_access_tokens(id) ON DELETE SET NULL,
  action TEXT NOT NULL CHECK (action IN ('VIEW','DOWNLOAD','DELETE_REQUEST','DENIED')),
  ip_hash TEXT,
  user_agent TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_result_access_logs_task ON result_access_logs (task_id, created_at DESC);
