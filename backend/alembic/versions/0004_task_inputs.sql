-- Material a user hands in for the fallback path (design doc §4 路径 D): a recording sent through WeChat KF,
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

-- Upload links reuse the one-time link table: purpose UPLOAD, redeemed by the upload itself, never a session.
ALTER TABLE result_access_tokens
  ADD COLUMN purpose TEXT NOT NULL DEFAULT 'RESULT' CHECK (purpose IN ('RESULT','UPLOAD')),
  ADD CONSTRAINT result_access_tokens_upload_no_session CHECK (purpose = 'RESULT' OR session_hash IS NULL);

-- Consent given on the upload page (the user ticks the declaration before the file is sent).
ALTER TABLE consent_records
  DROP CONSTRAINT consent_records_channel_check,
  ADD CONSTRAINT consent_records_channel_check
    CHECK (channel IN ('wecom_kf','meeting_chat','meeting_ui','api','web_upload'));
