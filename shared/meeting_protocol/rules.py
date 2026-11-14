"""Classification sets derived from the design doc §5.3."""

from .enums import FailureCode, TaskStatus

RETRYABLE_FAILURE_CODES = frozenset(
    {FailureCode.WORKER_LOST, FailureCode.NETWORK_ERROR, FailureCode.CLIENT_ERROR}
)

# Failures after which audio already captured must be deleted rather than summarised.
DISCARD_AUDIO_FAILURE_CODES = frozenset({FailureCode.CONSENT_REVOKED, FailureCode.REMOVED_BY_HOST})

TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.FAILED})

INTAKE_STATUSES = frozenset({TaskStatus.AWAITING_DETAILS, TaskStatus.AWAITING_CONSENT})

# Statuses in which a Worker attempt may be active (between lease and leaving the meeting).
IN_ATTEMPT_STATUSES = frozenset(
    {
        TaskStatus.JOINING,
        TaskStatus.WAITING_ROOM,
        TaskStatus.IN_MEETING,
        TaskStatus.AWAITING_RECORDING_APPROVAL,
        TaskStatus.RECORDING,
    }
)

# In-meeting statuses before recording starts; host approval must be empty here (schema CHECK).
PRE_RECORDING_STATUSES = IN_ATTEMPT_STATUSES - {TaskStatus.RECORDING}

# Statuses only a LIVE_MEETING task may enter (schema CHECK).
LIVE_ONLY_STATUSES = frozenset(
    {TaskStatus.SCHEDULED, TaskStatus.DISPATCHING, TaskStatus.RETRY_WAIT} | IN_ATTEMPT_STATUSES
)

POST_PROCESSING_STATUSES = frozenset({TaskStatus.UPLOADING, TaskStatus.TRANSCRIBING, TaskStatus.SUMMARIZING})
