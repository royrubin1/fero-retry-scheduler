import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.models import JobStatus
from app.repository import JobRepository
from app.retry_policy import Decision

NOW = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
LEASE = timedelta(seconds=60)


@pytest.fixture
def repo(tmp_path):
    # A real SQLite file, not a fake: the claim query is the thing under test.
    repo = JobRepository(str(tmp_path / "jobs.db"))
    repo.init_schema()
    return repo


def test_new_job_is_pending_with_no_attempts(repo):
    job = repo.create("http://example.test/hook", {"a": 1}, now=NOW)

    stored = repo.get(job.id)
    assert stored.status is JobStatus.PENDING
    assert stored.attempts == 0
    assert stored.next_retry_at is None
    assert stored.payload == {"a": 1}


def test_unknown_job_is_none(repo):
    assert repo.get("missing") is None


def test_claim_marks_job_running_and_counts_the_attempt(repo):
    job = repo.create("http://example.test/hook", {}, now=NOW)

    claimed = repo.claim_next(now=NOW, lease=LEASE)

    assert claimed.id == job.id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.attempts == 1


def test_claim_returns_none_when_nothing_is_due(repo):
    assert repo.claim_next(now=NOW, lease=LEASE) is None


def test_claim_skips_jobs_waiting_for_their_retry_time(repo):
    repo.create("http://example.test/hook", {}, now=NOW)
    job = repo.claim_next(now=NOW, lease=LEASE)
    repo.finish(job, Decision(JobStatus.PENDING, NOW + timedelta(seconds=10)), error="503", now=NOW)

    assert repo.claim_next(now=NOW + timedelta(seconds=9), lease=LEASE) is None
    assert repo.claim_next(now=NOW + timedelta(seconds=10), lease=LEASE).id == job.id


def test_running_job_is_not_claimed_again_while_its_lease_holds(repo):
    repo.create("http://example.test/hook", {}, now=NOW)
    repo.claim_next(now=NOW, lease=LEASE)

    assert repo.claim_next(now=NOW + LEASE - timedelta(seconds=1), lease=LEASE) is None


def test_job_of_a_crashed_worker_is_claimed_again_after_the_lease(repo):
    job = repo.create("http://example.test/hook", {}, now=NOW)
    repo.claim_next(now=NOW, lease=LEASE)  # this worker dies and never calls finish

    reclaimed = repo.claim_next(now=NOW + LEASE, lease=LEASE)

    assert reclaimed.id == job.id
    assert reclaimed.attempts == 2


def test_finish_from_an_expired_claim_is_ignored(repo):
    repo.create("http://example.test/hook", {}, now=NOW)
    slow = repo.claim_next(now=NOW, lease=LEASE)
    fresh = repo.claim_next(now=NOW + LEASE, lease=LEASE)

    assert repo.finish(slow, Decision(JobStatus.COMPLETED, None), error=None, now=NOW) is False
    assert repo.finish(fresh, Decision(JobStatus.FAILED, None), error="404", now=NOW) is True
    assert repo.get(fresh.id).status is JobStatus.FAILED


def test_concurrent_workers_never_claim_the_same_job(repo):
    for _ in range(50):
        repo.create("http://example.test/hook", {}, now=NOW)

    claimed: list[str] = []
    start = threading.Barrier(4)

    def worker():
        own_repo = JobRepository(repo.path)  # own connections, like a separate process
        start.wait()
        while (job := own_repo.claim_next(now=NOW, lease=LEASE)) is not None:
            claimed.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 50
    assert len(set(claimed)) == 50
