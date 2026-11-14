"""The protocol enums must list exactly the values the schema CHECK constraints accept.
(That migrations and schema.sql build the same database is checked in tests/integration.)"""

import re
from pathlib import Path

import pytest
from meeting_protocol import (
    AttemptStatus,
    AudioUploadStatus,
    ConsentKind,
    FailureCode,
    ProcessingJobKind,
    ProcessingJobStatus,
    RecordingOutcome,
    ServerEventType,
    TaskEffectKind,
    TaskEffectStatus,
    TaskKind,
    TaskStatus,
    WorkerEventType,
)

SCHEMA = (Path(__file__).parents[2] / "schema.sql").read_text(encoding="utf-8")


def _values_after(marker: str, until: str) -> set[str]:
    assert SCHEMA.count(marker) == 1, f"marker must be unique: {marker!r}"
    start = SCHEMA.index(marker)
    end = SCHEMA.index(until, start + len(marker))
    return set(re.findall(r"'([A-Z_]+)'", SCHEMA[start:end]))


@pytest.mark.parametrize(
    ("enum", "marker", "until"),
    [
        (TaskKind, "task_kind TEXT NOT NULL", "source_open_kfid"),
        (TaskStatus, "status TEXT NOT NULL DEFAULT 'AWAITING_DETAILS'", "[A3]"),
        (FailureCode, "failure_code TEXT CHECK", "[S11]"),
        (RecordingOutcome, "recording_outcome TEXT CHECK", "attempt_count"),
        (AttemptStatus, "status TEXT NOT NULL DEFAULT 'CLAIMED'", "started_at"),
        (WorkerEventType, "CASE WHEN attempt_id IS NOT NULL", "ELSE event_type"),
        (ServerEventType, "ELSE event_type IN", "UNIQUE (attempt_id, event_seq)"),
        (ConsentKind, "kind TEXT NOT NULL CHECK (kind IN (\n    'REQUESTER_AUTHORITY'", "subject TEXT"),
        (AudioUploadStatus, "upload_status TEXT NOT NULL DEFAULT 'PENDING'", "verified_at"),
        (ProcessingJobKind, "kind TEXT NOT NULL CHECK (kind IN ('AUDIO_NORMALIZE'", "input_version"),
        (ProcessingJobStatus, "CHECK (status IN ('PENDING','SUBMITTED','SUCCEEDED','FAILED'))", "attempts"),
        (TaskEffectKind, "effect TEXT NOT NULL CHECK", "payload"),
        (TaskEffectStatus, "CHECK (status IN ('PENDING','RUNNING','DONE','FAILED'))", "attempts"),
    ],
)
def test_enum_matches_schema_check(enum, marker, until):
    assert {m.value for m in enum} == _values_after(marker, until)
