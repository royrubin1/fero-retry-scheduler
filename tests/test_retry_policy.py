from datetime import datetime, timedelta, timezone

import pytest

from app.models import JobStatus
from app.retry_policy import Decision, Outcome, RetryPolicy, classify

NOW = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
policy = RetryPolicy(max_attempts=3, base_seconds=10, max_seconds=60)


@pytest.mark.parametrize("code", [200, 201, 204])
def test_2xx_is_success(code):
    assert classify(code) is Outcome.SUCCESS


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_errors_are_retried(code):
    assert classify(code) is Outcome.RETRY


def test_no_response_is_retried():
    # None = timeout or connection error, the downstream may be briefly down
    assert classify(None) is Outcome.RETRY


@pytest.mark.parametrize("code", [301, 400, 401, 404, 422, 501])
def test_other_codes_are_permanent(code):
    assert classify(code) is Outcome.PERMANENT


def test_backoff_doubles_after_each_attempt():
    delays = [policy.backoff_delay(attempt) for attempt in (1, 2, 3)]
    assert delays == [timedelta(seconds=10), timedelta(seconds=20), timedelta(seconds=40)]


def test_backoff_is_capped():
    assert policy.backoff_delay(10) == timedelta(seconds=60)


def test_success_completes_the_job():
    assert policy.decide(Outcome.SUCCESS, attempts=1, now=NOW) == Decision(JobStatus.COMPLETED, None)


def test_retry_with_attempts_left_goes_back_to_pending():
    decision = policy.decide(Outcome.RETRY, attempts=2, now=NOW)
    assert decision == Decision(JobStatus.PENDING, NOW + timedelta(seconds=20))


def test_retry_on_last_attempt_is_dead():
    assert policy.decide(Outcome.RETRY, attempts=3, now=NOW) == Decision(JobStatus.DEAD, None)


def test_permanent_error_fails_even_with_attempts_left():
    assert policy.decide(Outcome.PERMANENT, attempts=1, now=NOW) == Decision(JobStatus.FAILED, None)
