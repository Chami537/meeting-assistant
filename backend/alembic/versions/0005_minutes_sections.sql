-- Minutes v2 (prompt minutes-v2, skills/meeting-minutes): key points, facts and figures, plans and expectations,
-- and the name fixes the model made against the meeting title. Minutes written before stay as they are.
ALTER TABLE meeting_minutes
  ADD COLUMN key_points JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN key_facts JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN outlook JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN name_fixes JSONB NOT NULL DEFAULT '[]'::jsonb;
