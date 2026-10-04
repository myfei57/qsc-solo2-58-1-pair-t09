"""Failures raised by the job step ledger."""

from __future__ import annotations


class JobError(RuntimeError):
    """Base class for job ledger failures."""


class JobKindMismatch(JobError):
    """Raised when a job id is reused under a different job kind."""


class JobRequestMismatch(JobError):
    """Raised when a job id is resumed with a different request payload."""


class StepAlreadySettled(JobError):
    """Raised when a step that already settled is recorded again."""


class JobNotFinished(JobError):
    """Raised when a replay is requested for a job that has not completed."""


class JobNotFound(LookupError):
    """Raised when a job id has no ledger record."""
