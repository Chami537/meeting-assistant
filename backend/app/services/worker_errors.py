"""Errors of the Worker protocol and the HTTP status they map to."""

from meeting_protocol import ConflictReason


class WorkerConflict(Exception):
    """HTTP 409 with ConflictResponse(reason): the attempt is no longer allowed to do this."""

    def __init__(self, reason: ConflictReason, detail: str = "", expected_seq: int | None = None):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.expected_seq = expected_seq


class UploadRejected(Exception):
    """HTTP 422: the request is inconsistent (different hash for a known segment, unsealed segment,
    stored object not matching what was declared, event not valid in the current state)."""
