-- Constraint checks for schema.sql V1.1. Run against an empty scratch database:
--   createdb scratch && psql -d scratch -v ON_ERROR_STOP=1 -1 -f schema.sql \
--     && psql -d scratch -v ON_ERROR_STOP=1 -At -f tests/schema_checks.sql
-- Every line of output must start with "PASS"; the script aborts on the first unexpected result.
\set QUIET on
CREATE FUNCTION expect_fail(label text, stmt text) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
  BEGIN EXECUTE stmt; EXCEPTION WHEN others THEN RETURN 'PASS reject  ' || label; END;
  RAISE EXCEPTION 'FAIL (accepted) %', label;
END $$;
CREATE FUNCTION expect_ok(label text, stmt text) RETURNS text LANGUAGE plpgsql AS $$
BEGIN EXECUTE stmt; RETURN 'PASS accept  ' || label; END $$;
CREATE FUNCTION expect_true(label text, cond boolean) RETURNS text LANGUAGE plpgsql AS $$
BEGIN IF cond THEN RETURN 'PASS assert  ' || label; END IF; RAISE EXCEPTION 'FAIL (assert) %', label; END $$;

-- fixtures: user u1, workers w1/w2, live tasks f1/f2
INSERT INTO kf_users (id, corp_id, external_userid) VALUES ('00000000-0000-0000-0000-000000000001','c','u1');
INSERT INTO worker_nodes (id, name, token_hash) VALUES
 ('00000000-0000-0000-0000-0000000000a1','w1','h1'), ('00000000-0000-0000-0000-0000000000a2','w2','h2');
INSERT INTO meeting_tasks (id, user_id, idempotency_key, meeting_code) VALUES
 ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','kf:k:1','123'),
 ('00000000-0000-0000-0000-0000000000f2','00000000-0000-0000-0000-000000000001','kf:k:2','456');

-- ---------------------------------------------------------------- meeting_tasks invariants
SELECT expect_fail('S3 idempotency_key NULL',
 $$INSERT INTO meeting_tasks (user_id, idempotency_key) VALUES ('00000000-0000-0000-0000-000000000001', NULL)$$);
SELECT expect_fail('P2 display_name without AI prefix',
 $$UPDATE meeting_tasks SET display_name='张三' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('S4 SCHEDULED without consent',
 $$UPDATE meeting_tasks SET status='SCHEDULED', scheduled_at=now(), planned_end_at=now()+interval '1h' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('S4 planned_end before scheduled',
 $$UPDATE meeting_tasks SET scheduled_at=now(), planned_end_at=now()-interval '1h' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('I2 ciphertext without key version',
 $$UPDATE meeting_tasks SET meeting_password_ciphertext='\x01' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_ok('S4 schedule with consent and window',
 $$UPDATE meeting_tasks SET status='SCHEDULED', consent_at=now(), consent_version='v1', scheduled_at=now(), planned_end_at=now()+interval '1h'
   WHERE id IN ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000f2')$$);
SELECT expect_fail('FAILED without failure_code',
 $$UPDATE meeting_tasks SET status='FAILED' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('unknown failure_code',
 $$UPDATE meeting_tasks SET status='FAILED', failure_code='OOPS' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('failure_code outside FAILED/RETRY_WAIT',
 $$UPDATE meeting_tasks SET failure_code='NETWORK_ERROR' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('RECORDING without host approval',
 $$UPDATE meeting_tasks SET status='RECORDING' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('DISPATCHING without dispatch_deadline_at',
 $$UPDATE meeting_tasks SET status='DISPATCHING' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_ok('outcome LIMIT_REACHED accepted',
 $$UPDATE meeting_tasks SET recording_outcome='LIMIT_REACHED' WHERE id='00000000-0000-0000-0000-0000000000f2'$$);
SELECT expect_ok('A9 updated_at trigger fires',
 $q$DO $d$ DECLARE a timestamptz; BEGIN
   UPDATE meeting_tasks SET updated_at='2000-01-01' WHERE id='00000000-0000-0000-0000-0000000000f1';
   SELECT updated_at INTO a FROM meeting_tasks WHERE id='00000000-0000-0000-0000-0000000000f1';
   IF a < '2020-01-01' THEN RAISE EXCEPTION 'trigger did not fire'; END IF; END $d$ $q$);

-- ---------------------------------------------------------------- fallback MVP task kinds
SELECT expect_ok('MVP upload task skips meeting fields',
 $$INSERT INTO meeting_tasks (id, user_id, idempotency_key, task_kind, status, consent_at, consent_version, upload_deadline_at)
   VALUES ('00000000-0000-0000-0000-0000000000f3','00000000-0000-0000-0000-000000000001','kf:k:3','USER_UPLOAD','UPLOADING',now(),'u1',now()+interval '1h')$$);
SELECT expect_fail('MVP upload task cannot enter a live state',
 $$UPDATE meeting_tasks SET status='SCHEDULED' WHERE id='00000000-0000-0000-0000-0000000000f3'$$);
SELECT expect_ok('MVP uploaded segment without attempt',
 $$INSERT INTO audio_assets (task_id, source, segment_no, object_key, file_sha256, format, bytes, retention_until)
   VALUES ('00000000-0000-0000-0000-0000000000f3','user_upload',0,'tasks/f3/audio/0','h','m4a',10,now()+interval '30 days')$$);
SELECT expect_fail('MVP duplicate uploaded segment_no',
 $$INSERT INTO audio_assets (task_id, source, segment_no, object_key, file_sha256, format, bytes, retention_until)
   VALUES ('00000000-0000-0000-0000-0000000000f3','user_upload',0,'tasks/f3/audio/0b','h','m4a',10,now()+interval '30 days')$$);
SELECT expect_fail('worker segment without attempt',
 $$INSERT INTO audio_assets (task_id, source, segment_no, object_key, file_sha256, format, bytes, retention_until)
   VALUES ('00000000-0000-0000-0000-0000000000f3','worker',1,'tasks/f3/audio/1','h','wav',10,now()+interval '30 days')$$);
SELECT expect_ok('MVP upload_asr transcript',
 $$INSERT INTO transcripts (task_id, source, body) VALUES ('00000000-0000-0000-0000-0000000000f3','upload_asr','[]')$$);

-- ---------------------------------------------------------------- lease, fencing, approval per attempt
UPDATE meeting_tasks SET status='DISPATCHING', dispatch_deadline_at=now()+interval '10 min'
 WHERE id IN ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000f2');
SELECT expect_ok('A2 claim attempt 1',
 $$INSERT INTO task_attempts (id, task_id, worker_id, attempt_no, lease_until) VALUES
   ('00000000-0000-0000-0000-0000000000b1','00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000a1',1,now()+interval '1m')$$);
SELECT expect_fail('A2 second active attempt for the same task',
 $$INSERT INTO task_attempts (task_id, worker_id, attempt_no, lease_until) VALUES
   ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000a2',2,now()+interval '1m')$$);
SELECT expect_fail('S2 same worker holding two active attempts',
 $$INSERT INTO task_attempts (task_id, worker_id, attempt_no, lease_until) VALUES
   ('00000000-0000-0000-0000-0000000000f2','00000000-0000-0000-0000-0000000000a1',1,now()+interval '1m')$$);
SELECT expect_fail('active attempt without lease',
 $$INSERT INTO task_attempts (task_id, worker_id, attempt_no) VALUES
   ('00000000-0000-0000-0000-0000000000f2','00000000-0000-0000-0000-0000000000a2',1)$$);
SELECT expect_fail('S2 worker capacity 2',
 $$INSERT INTO worker_nodes (name, token_hash, capacity) VALUES ('w3','h3',2)$$);
SELECT expect_fail('approval snapshot in a pre-recording state',
 $$UPDATE meeting_tasks SET status='AWAITING_RECORDING_APPROVAL', host_recording_approval_at=now() WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_ok('approval then RECORDING',
 $$UPDATE meeting_tasks SET status='RECORDING', host_recording_approval_at=now() WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('rejoin without clearing previous approval',
 $$UPDATE meeting_tasks SET status='RETRY_WAIT', next_retry_at=now(), failure_code='WORKER_LOST' WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_ok('rejoin clears approval, keeps last error',
 $$UPDATE meeting_tasks SET status='RETRY_WAIT', next_retry_at=now(), failure_code='WORKER_LOST', host_recording_approval_at=NULL
   WHERE id='00000000-0000-0000-0000-0000000000f1'$$);
SELECT expect_fail('leaving RETRY_WAIT keeps a stale failure_code',
 $$UPDATE meeting_tasks SET status='DISPATCHING', dispatch_deadline_at=now() WHERE id='00000000-0000-0000-0000-0000000000f1'$$);

-- ---------------------------------------------------------------- events
SELECT expect_ok('A5 worker event seq 1',
 $$INSERT INTO task_events (task_id, attempt_id, event_seq, event_type, actor) VALUES
   ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000b1',1,'JOINED','worker')$$);
SELECT expect_fail('A5 duplicate event seq',
 $$INSERT INTO task_events (task_id, attempt_id, event_seq, event_type, actor) VALUES
   ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000b1',1,'JOINED','worker')$$);
SELECT expect_fail('A5 unknown worker event type',
 $$INSERT INTO task_events (task_id, attempt_id, event_seq, event_type, actor) VALUES
   ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000b1',2,'JOINDE','worker')$$);
SELECT expect_fail('A5 worker event type as server event',
 $$INSERT INTO task_events (task_id, event_type, actor) VALUES ('00000000-0000-0000-0000-0000000000f1','JOINED','server')$$);

-- ---------------------------------------------------------------- audio segments
SELECT expect_fail('A6 segment attempt belongs to another task',
 $$INSERT INTO audio_assets (task_id, attempt_id, segment_no, object_key, file_sha256, format, bytes, recorded_started_at, recorded_ended_at, retention_until)
   VALUES ('00000000-0000-0000-0000-0000000000f2','00000000-0000-0000-0000-0000000000b1',0,'k-x','h','wav',10,now(),now()+interval '1m',now())$$);
SELECT expect_fail('A6 worker segment without recording times',
 $$INSERT INTO audio_assets (task_id, attempt_id, segment_no, object_key, file_sha256, format, bytes, retention_until)
   VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000b1',0,'k-y','h','wav',10,now())$$);
SELECT expect_fail('S6 segment without retention_until',
 $$INSERT INTO audio_assets (task_id, attempt_id, segment_no, object_key, file_sha256, format, bytes, recorded_started_at, recorded_ended_at)
   VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000b1',0,'k-z','h','wav',10,now(),now()+interval '1m')$$);
SELECT expect_ok('A6 segment 0 pending',
 $$INSERT INTO audio_assets (task_id, attempt_id, segment_no, object_key, file_sha256, format, bytes, recorded_started_at, recorded_ended_at, retention_until)
   VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000b1',0,'tasks/f1/audio/0','same','wav',10,now(),now()+interval '1m',now()+interval '30 days')$$);
SELECT expect_ok('S14 identical-hash silent segment',
 $$INSERT INTO audio_assets (task_id, attempt_id, segment_no, object_key, file_sha256, format, bytes, recorded_started_at, recorded_ended_at, retention_until)
   VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000b1',1,'tasks/f1/audio/1','same','wav',10,now()+interval '1m',now()+interval '2m',now()+interval '30 days')$$);
SELECT expect_fail('A6 READY without server verification',
 $$UPDATE audio_assets SET upload_status='READY' WHERE object_key='tasks/f1/audio/0'$$);
SELECT expect_fail('S6 deleting a task that still has audio rows',
 $$DELETE FROM meeting_tasks WHERE id='00000000-0000-0000-0000-0000000000f1'$$);

-- ---------------------------------------------------------------- transcripts, minutes, jobs
INSERT INTO transcripts (id, task_id, source, body) VALUES ('00000000-0000-0000-0000-0000000000c1','00000000-0000-0000-0000-0000000000f1','worker_asr','[]');
SELECT expect_fail('S8 minutes pointing at another task transcript',
 $$INSERT INTO meeting_minutes (task_id, transcript_id, summary) VALUES ('00000000-0000-0000-0000-0000000000f2','00000000-0000-0000-0000-0000000000c1','x')$$);
SELECT expect_ok('S8 minutes v1',
 $$INSERT INTO meeting_minutes (task_id, transcript_id, summary) VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000c1','x')$$);
SELECT expect_fail('S8 two current minutes',
 $$INSERT INTO meeting_minutes (task_id, transcript_id, summary, version) VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000c1','y',2)$$);
SELECT expect_ok('S8 minutes v2 after demoting v1',
 $q$DO $d$ BEGIN UPDATE meeting_minutes SET is_current=false WHERE task_id='00000000-0000-0000-0000-0000000000f1';
   INSERT INTO meeting_minutes (task_id, transcript_id, summary, version) VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-0000000000c1','y',2); END $d$ $q$);
SELECT expect_fail('A7 SUBMITTED without external id and submitted_at',
 $$INSERT INTO processing_jobs (task_id, kind, provider, status) VALUES ('00000000-0000-0000-0000-0000000000f1','ASR','tencent_asr','SUBMITTED')$$);

-- ---------------------------------------------------------------- inbox, outbox, callbacks, result page
SELECT expect_fail('S1 inbox ciphertext without key version',
 $$INSERT INTO kf_inbox (open_kfid, msg_id, message_type, payload_redacted, content_ciphertext) VALUES ('kf','m0','text','{}','\x01')$$);
SELECT expect_ok('S7 outbox system notice',
 $$INSERT INTO outgoing_messages (task_id, user_id, open_kfid, purpose, dedup_key, content)
   VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','kf','ACK','f1:ACK','ok')$$);
SELECT expect_fail('S7 duplicate system notice',
 $$INSERT INTO outgoing_messages (task_id, user_id, open_kfid, purpose, dedup_key, content)
   VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','kf','ACK','f1:ACK','ok')$$);
SELECT expect_ok('A10 second result link for a new user request',
 $$INSERT INTO outgoing_messages (task_id, user_id, open_kfid, purpose, dedup_key, content) VALUES
   ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','kf','RESULT_LINK','f1:RESULT_LINK:m1',''),
   ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','kf','RESULT_LINK','f1:RESULT_LINK:m2','')$$);
SELECT expect_true('S7 outbox next_attempt_at defaulted',
 NOT EXISTS (SELECT 1 FROM outgoing_messages WHERE next_attempt_at IS NULL));
SELECT expect_fail('S13 callback payload without key version',
 $$INSERT INTO callback_events (provider, external_event_id, payload_ciphertext) VALUES ('tencent_meeting','t1','\x01')$$);
SELECT expect_fail('S13 wecom_kf callbacks are not stored here',
 $$INSERT INTO callback_events (provider, external_event_id) VALUES ('wecom_kf','t2')$$);
SELECT expect_fail('A10 session before the token is used',
 $$INSERT INTO result_access_tokens (task_id, user_id, token_hash, expires_at, session_hash, session_expires_at)
   VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','th',now()+interval '1h','sh',now())$$);
INSERT INTO result_access_tokens (task_id, user_id, token_hash, expires_at)
 VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','tok',now()+interval '24h');
CREATE FUNCTION redeem(tok text, sess text) RETURNS bigint LANGUAGE sql AS $$
  WITH r AS (UPDATE result_access_tokens SET used_at=now(), session_hash=sess, session_expires_at=now()+interval '7 days'
             WHERE token_hash=tok AND used_at IS NULL AND expires_at>now() AND revoked_at IS NULL RETURNING 1)
  SELECT count(*) FROM r $$;
SELECT expect_true('A10 first redeem succeeds', redeem('tok','s1') = 1);
SELECT expect_true('A10 second redeem finds nothing', redeem('tok','s2') = 0);

-- ---------------------------------------------------------------- user erasure
INSERT INTO kf_inbox (open_kfid, msg_id, message_type, payload_redacted, content_ciphertext, content_key_version, user_id)
 VALUES ('kf','m9','text','{}','\x01',1,'00000000-0000-0000-0000-000000000001');
INSERT INTO consent_records (task_id, user_ref_hash, kind, subject, channel, consent_version, text_sha256, retain_until)
 VALUES ('00000000-0000-0000-0000-0000000000f1','uh','REQUESTER_AUTHORITY','requester','wecom_kf','v1','th',now()+interval '3 years');
SELECT expect_fail('S6 deleting the user before the tasks',
 $$DELETE FROM kf_users WHERE id='00000000-0000-0000-0000-000000000001'$$);
SELECT expect_ok('S6 erasure order: audio -> tasks -> user',
 $q$DO $d$ BEGIN
   DELETE FROM audio_assets WHERE task_id IN (SELECT id FROM meeting_tasks WHERE user_id='00000000-0000-0000-0000-000000000001');
   DELETE FROM meeting_tasks WHERE user_id='00000000-0000-0000-0000-000000000001';
   DELETE FROM kf_users WHERE id='00000000-0000-0000-0000-000000000001'; END $d$ $q$);
SELECT expect_true('S6 erasure leaves no task, attempt, outbox, token or transcript rows',
 (SELECT count(*) FROM meeting_tasks) + (SELECT count(*) FROM task_attempts) + (SELECT count(*) FROM outgoing_messages)
 + (SELECT count(*) FROM result_access_tokens) + (SELECT count(*) FROM transcripts) = 0);
SELECT expect_true('S6 erasure removes the user''s inbox rows and ciphertext',
 NOT EXISTS (SELECT 1 FROM kf_inbox WHERE msg_id='m9'));
SELECT expect_true('P2 consent evidence survives erasure as metadata',
 (SELECT count(*) FROM consent_records WHERE task_id IS NULL AND user_ref_hash='uh') = 1);
