-- Lease / reaper SQL from the design doc §5, run against schema.sql V1.1 on an empty scratch database.
-- Every line of output must start with "PASS".
\set QUIET on
CREATE FUNCTION expect_true(label text, cond boolean) RETURNS text LANGUAGE plpgsql AS $$
BEGIN IF cond THEN RETURN 'PASS assert  ' || label; END IF; RAISE EXCEPTION 'FAIL (assert) %', label; END $$;

-- Claim for worker $1: returns the worker's active attempt if it already has one (idempotent retry),
-- otherwise claims the oldest DISPATCHING task. Callers catching unique_violation (23505) retry once.
CREATE FUNCTION lease(worker UUID) RETURNS TABLE (attempt_id UUID, attempt_no INTEGER, task_id UUID)
LANGUAGE sql AS $$
  WITH existing AS (
    SELECT a.id, a.attempt_no, a.task_id FROM task_attempts a
    WHERE a.worker_id = worker AND a.status IN ('CLAIMED','RUNNING')
  ), t AS (
    SELECT m.id FROM meeting_tasks m
    WHERE NOT EXISTS (SELECT 1 FROM existing)
      AND m.status = 'DISPATCHING' AND m.attempt_count < m.max_attempts AND m.cancel_requested_at IS NULL
    ORDER BY m.scheduled_at LIMIT 1 FOR UPDATE SKIP LOCKED
  ), a AS (
    INSERT INTO task_attempts (task_id, worker_id, attempt_no, lease_until)
    SELECT t.id, worker,
           (SELECT coalesce(max(x.attempt_no), 0) + 1 FROM task_attempts x WHERE x.task_id = t.id),
           now() + interval '60 seconds'
    FROM t
    RETURNING id, attempt_no, task_id
  ), claimed AS (
    UPDATE meeting_tasks m
    SET status = 'JOINING', attempt_count = attempt_count + 1, dispatch_deadline_at = NULL,
        lock_version = lock_version + 1
    FROM a WHERE m.id = a.task_id
    RETURNING a.id, a.attempt_no, a.task_id
  )
  SELECT * FROM existing UNION ALL SELECT * FROM claimed
$$;

INSERT INTO kf_users (id, corp_id, external_userid) VALUES ('00000000-0000-0000-0000-000000000001','c','u1');
INSERT INTO worker_nodes (id, name, token_hash) VALUES
 ('00000000-0000-0000-0000-0000000000a1','w1','h1'), ('00000000-0000-0000-0000-0000000000a2','w2','h2');
INSERT INTO meeting_tasks (id, user_id, idempotency_key, meeting_code, status, consent_at, consent_version,
                           scheduled_at, planned_end_at, dispatch_deadline_at)
VALUES ('00000000-0000-0000-0000-0000000000f1','00000000-0000-0000-0000-000000000001','kf:k:1','123','DISPATCHING',
        now(),'v1',now(),now()+interval '1h',now()+interval '10 min');

CREATE TEMP TABLE first_lease AS SELECT * FROM lease('00000000-0000-0000-0000-0000000000a1');
SELECT expect_true('w1 claims attempt 1', (SELECT attempt_no FROM first_lease) = 1);
SELECT expect_true('task moved to JOINING',
 (SELECT status FROM meeting_tasks WHERE id='00000000-0000-0000-0000-0000000000f1') = 'JOINING');
SELECT expect_true('w1 retry after a lost response gets the same attempt',
 (SELECT attempt_id FROM lease('00000000-0000-0000-0000-0000000000a1')) = (SELECT attempt_id FROM first_lease));
SELECT expect_true('w2 finds nothing to claim',
 NOT EXISTS (SELECT 1 FROM lease('00000000-0000-0000-0000-0000000000a2')));

-- reaper: expire the lease, task goes back through RETRY_WAIT to DISPATCHING
UPDATE task_attempts SET lease_until = now() - interval '1 second';
UPDATE task_attempts SET status = 'EXPIRED', ended_at = now()
 WHERE status IN ('CLAIMED','RUNNING') AND lease_until < now();
UPDATE meeting_tasks SET status = 'RETRY_WAIT', next_retry_at = now(), failure_code = 'WORKER_LOST';
UPDATE meeting_tasks SET status = 'DISPATCHING', failure_code = NULL, next_retry_at = NULL,
                         dispatch_deadline_at = now() + interval '5 min';
SELECT expect_true('w2 claims attempt 2 after expiry',
 (SELECT attempt_no FROM lease('00000000-0000-0000-0000-0000000000a2')) = 2);

-- attempts exhausted: nothing is claimable
UPDATE task_attempts SET status = 'EXPIRED', ended_at = now() WHERE status IN ('CLAIMED','RUNNING');
UPDATE meeting_tasks SET status = 'DISPATCHING', dispatch_deadline_at = now(), attempt_count = max_attempts;
SELECT expect_true('no claim once attempt_count reaches max_attempts',
 NOT EXISTS (SELECT 1 FROM lease('00000000-0000-0000-0000-0000000000a1')));

-- after LEFT the attempt is FINISHED: the Worker is free for the next meeting
INSERT INTO meeting_tasks (id, user_id, idempotency_key, meeting_code, status, consent_at, consent_version,
                           scheduled_at, planned_end_at, dispatch_deadline_at)
VALUES ('00000000-0000-0000-0000-0000000000f2','00000000-0000-0000-0000-000000000001','kf:k:2','456','DISPATCHING',
        now(),'v1',now(),now()+interval '1h',now()+interval '10 min');
UPDATE meeting_tasks SET status = 'UPLOADING', dispatch_deadline_at = NULL, upload_deadline_at = now() + interval '2 hours',
                         attempt_count = 1
 WHERE id = '00000000-0000-0000-0000-0000000000f1';
UPDATE task_attempts SET status = 'RUNNING', ended_at = NULL, lease_until = now() + interval '60 seconds' WHERE attempt_no = 2;
SELECT expect_true('a Worker with a RUNNING attempt gets that attempt back, not the next meeting',
 (SELECT task_id FROM lease('00000000-0000-0000-0000-0000000000a2')) = '00000000-0000-0000-0000-0000000000f1');
UPDATE task_attempts SET status = 'FINISHED', ended_at = now() WHERE attempt_no = 2;
SELECT expect_true('a Worker whose attempt is FINISHED claims the next meeting',
 (SELECT task_id FROM lease('00000000-0000-0000-0000-0000000000a2')) = '00000000-0000-0000-0000-0000000000f2');
