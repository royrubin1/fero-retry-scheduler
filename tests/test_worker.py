from datetime import datetime, timedelta, timezone

import pytest

from app.models import JobStatus
from app.repository import JobRepository
from app.retry_policy import RetryPolicy
from app.sender import SendResult
from app.worker import Worker

START = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
LEASE = timedelta(seconds=60)
URL = "http://downstream.test/hook"
policy = RetryPolicy(max_attempts=3, base_seconds=10, max_seconds=60)


class Clock:
    """A clock the test moves by hand, so retries need no sleeping."""

    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeDownstream:
    """Answers each send with the next scripted status code (None = no response)."""

    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, job):
        self.calls.append(job)
        code = self.codes.pop(0)
        return SendResult(code, None if code else "ConnectError: connection refused")


@pytest.fixture
def repo(tmp_path):
    repo = JobRepository(str(tmp_path / "jobs.db"))
    repo.init_schema()
    return repo


@pytest.fixture
def clock():
    return Clock()


def make_worker(repo, clock, downstream):
    return Worker(repo, policy, downstream, LEASE, now=clock)


def test_nothing_due_sends_nothing(repo, clock):
    downstream = FakeDownstream()

    assert make_worker(repo, clock, downstream).process_one() is False
    assert downstream.calls == []


def test_2xx_completes_the_job(repo, clock):
    job = repo.create(URL, {"a": 1}, now=clock())
    downstream = FakeDownstream(200)

    assert make_worker(repo, clock, downstream).process_one() is True

    assert [call.id for call in downstream.calls] == [job.id]
    stored = repo.get(job.id)
    assert (stored.status, stored.attempts, stored.next_retry_at) == (JobStatus.COMPLETED, 1, None)


def test_transient_error_schedules_a_retry_with_backoff(repo, clock):
    job = repo.create(URL, {}, now=clock())

    make_worker(repo, clock, FakeDownstream(503)).process_one()

    stored = repo.get(job.id)
    assert stored.status is JobStatus.PENDING
    assert stored.attempts == 1
    assert stored.next_retry_at == START + timedelta(seconds=10)
    assert stored.last_error == "HTTP 503"


def test_no_response_is_retried(repo, clock):
    job = repo.create(URL, {}, now=clock())

    make_worker(repo, clock, FakeDownstream(None)).process_one()

    stored = repo.get(job.id)
    assert stored.status is JobStatus.PENDING
    assert stored.last_error == "ConnectError: connection refused"


def test_permanent_error_fails_without_retry(repo, clock):
    job = repo.create(URL, {}, now=clock())

    make_worker(repo, clock, FakeDownstream(404)).process_one()

    stored = repo.get(job.id)
    assert (stored.status, stored.next_retry_at, stored.last_error) == (JobStatus.FAILED, None, "HTTP 404")


def test_retry_waits_for_its_backoff_then_succeeds(repo, clock):
    job = repo.create(URL, {}, now=clock())
    worker = make_worker(repo, clock, FakeDownstream(503, 200))
    worker.process_one()

    clock.advance(9)
    assert worker.process_one() is False  # 10 s backoff not over yet
    clock.advance(1)
    assert worker.process_one() is True

    stored = repo.get(job.id)
    assert (stored.status, stored.attempts) == (JobStatus.COMPLETED, 2)


def test_job_is_dead_after_max_attempts(repo, clock):
    job = repo.create(URL, {}, now=clock())
    downstream = FakeDownstream(503, 500, 429)
    worker = make_worker(repo, clock, downstream)

    statuses = []
    for _ in range(3):
        worker.process_one()
        statuses.append(repo.get(job.id).status)
        clock.advance(60)  # past any backoff

    assert statuses == [JobStatus.PENDING, JobStatus.PENDING, JobStatus.DEAD]
    assert len(downstream.calls) == 3
    assert worker.process_one() is False  # dead is final


def test_job_of_a_crashed_worker_is_sent_again(repo, clock):
    # At-least-once: the crashed worker may already have reached the downstream,
    # which is why every request carries the job id as Idempotency-Key.
    job = repo.create(URL, {}, now=clock())
    repo.claim_next(clock(), LEASE)  # this worker dies without saving a result
    clock.advance(LEASE.total_seconds())

    make_worker(repo, clock, FakeDownstream(200)).process_one()

    stored = repo.get(job.id)
    assert (stored.status, stored.attempts) == (JobStatus.COMPLETED, 2)


def test_job_lost_on_its_last_attempt_is_dead_without_sending_again(repo, clock):
    job = repo.create(URL, {}, now=clock())
    worker = make_worker(repo, clock, FakeDownstream(503, 503))
    for _ in range(2):
        worker.process_one()
        clock.advance(60)
    repo.claim_next(clock(), LEASE)  # attempt 3: this worker dies without saving a result
    clock.advance(LEASE.total_seconds())

    downstream = FakeDownstream()
    assert make_worker(repo, clock, downstream).process_one() is True

    assert downstream.calls == []
    assert repo.get(job.id).status is JobStatus.DEAD
