-- Minutes v3 (prompt minutes-v3): the meeting's type (one of minutes.MEETING_TYPES) and date, shown with the
-- minutes so that people can file them. meeting_date_source says where the date came from (预约时间, 文件名,
-- 会上提到 or 上传日期). Minutes written before have none of them and show as before.
ALTER TABLE meeting_minutes
  ADD COLUMN meeting_type TEXT,
  ADD COLUMN meeting_date DATE,
  ADD COLUMN meeting_date_source TEXT;
