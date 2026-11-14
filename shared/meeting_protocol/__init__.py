"""Protocol shared by the backend and the Windows Worker: enums, rule sets and API bodies."""

from .enums import (
    AttemptStatus,
    AudioUploadStatus,
    ConflictReason,
    ConsentKind,
    FailureCode,
    HeartbeatCommand,
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

__all__ = [
    "AttemptStatus",
    "AudioUploadStatus",
    "ConflictReason",
    "ConsentKind",
    "FailureCode",
    "HeartbeatCommand",
    "ProcessingJobKind",
    "ProcessingJobStatus",
    "RecordingOutcome",
    "ServerEventType",
    "TaskEffectKind",
    "TaskEffectStatus",
    "TaskKind",
    "TaskStatus",
    "WorkerEventType",
]
