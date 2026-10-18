"""Retry decisions as pure functions: no network, no database, no clock.

Every input (status code, attempt number, current time) is passed in,
so the whole policy can be tested without sleeping or mocking.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from app.models import JobStatus

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class Outcome(Enum):
    SUCCESS = "success"
    RETRY = "retry"          # transient, worth trying again later
    PERMANENT = "permanent"  # retrying won't change the answer


def classify(status_code: int | None) -> Outcome:
    """status_code is None when there was no response at all (timeout, connection error)."""
    if status_code is None or status_code in RETRYABLE_STATUS_CODES:
        return Outcome.RETRY
    if 200 <= status_code < 300:
        return Outcome.SUCCESS
    return Outcome.PERMANENT


@dataclass(frozen=True)
class Decision:
    status: JobStatus
    next_retry_at: datetime | None


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int    # counts every attempt, including the first one
    base_seconds: float
    max_seconds: float

    def backoff_delay(self, attempt: int) -> timedelta:
        """Wait after failed attempt number `attempt` (1-based): base, 2*base, 4*base... up to max."""
        return timedelta(seconds=min(self.base_seconds * 2 ** (attempt - 1), self.max_seconds))

    def decide(self, outcome: Outcome, attempts: int, now: datetime) -> Decision:
        """Where the job goes after an attempt. `attempts` already includes the one that just ran."""
        if outcome is Outcome.SUCCESS:
            return Decision(JobStatus.COMPLETED, None)
        if outcome is Outcome.PERMANENT:
            return Decision(JobStatus.FAILED, None)
        if attempts >= self.max_attempts:
            return Decision(JobStatus.DEAD, None)
        return Decision(JobStatus.PENDING, now + self.backoff_delay(attempts))
