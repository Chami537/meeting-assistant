-- Per-user message order and outbox msgid generations (review round 3).
-- kf_inbox.ingest_seq: assigned at insert, in sync_msg order; process_inbox applies a user's messages by it.
ALTER TABLE kf_inbox ADD COLUMN ingest_seq BIGSERIAL;
CREATE INDEX idx_kf_inbox_user_seq ON kf_inbox (user_id, ingest_seq);
-- outgoing_messages.send_generation: part of the WeCom msgid. A message released from DEFERRED is a new
-- delivery and needs a new msgid (WeCom rejects a reused one); retries after a timeout keep the same one.
ALTER TABLE outgoing_messages ADD COLUMN send_generation SMALLINT NOT NULL DEFAULT 0;
