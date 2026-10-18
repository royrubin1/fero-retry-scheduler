"""API tests: the real FastAPI app on a temporary SQLite database, called through
TestClient, which runs the app in-process (no server, no network)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.models import JobStatus
from app.repository import JobRepository
from app.retry_policy import RetryPolicy
from app.sender import SendResult
from app.worker import Worker

# `.test` is a reserved domain (RFC 2606) that never resolves, so this can't reach a real
# server by accident. Nothing here sends to it anyway: the downstream is always faked.
URL = "http://downstream.test/hook"


@pytest.fixture
def repo(tmp_path):
    # A new database file for every test, so tests can't see each other's jobs.
    repo = JobRepository(str(tmp_path / "jobs.db"))
    repo.init_schema()
    return repo


@pytest.fixture
def client(repo):
    return TestClient(create_app(repo))


def test_post_stores_a_pending_job(client, repo):
    response = client.post("/jobs/", json={"url": URL, "payload": {"order": 42, "items": [1, 2]}})

    # The response has exactly the fields the spec asks for...
    assert response.status_code == 202
    body = response.json()
    assert body == {"id": body["id"], "status": "pending"}
    assert response.headers["Location"] == f"/jobs/{body['id']}/"
    # ...and the job really is in the database, payload unchanged.
    stored = repo.get(body["id"])
    assert (stored.url, stored.payload, stored.status) == (URL, {"order": 42, "items": [1, 2]}, JobStatus.PENDING)


def test_get_returns_the_state_of_a_new_job(client):
    job_id = client.post("/jobs/", json={"url": URL, "payload": {}}).json()["id"]

    response = client.get(f"/jobs/{job_id}/")

    # No worker has run: no attempts yet and no retry scheduled.
    assert response.status_code == 200
    assert response.json() == {"id": job_id, "status": "pending", "attempts": 0,
                               "next_retry_at": None, "last_error": None}


def test_get_shows_the_scheduled_retry(client, repo):
    job_id = client.post("/jobs/", json={"url": URL, "payload": {}}).json()["id"]
    # One attempt by a worker whose downstream always answers 503.
    worker = Worker(repo, RetryPolicy(max_attempts=3, base_seconds=10, max_seconds=60),
                    send=lambda job: SendResult(503), lease=timedelta(seconds=60))
    before = datetime.now(timezone.utc)
    worker.process_one()

    body = client.get(f"/jobs/{job_id}/").json()

    assert (body["status"], body["attempts"], body["last_error"]) == ("pending", 1, "HTTP 503")
    next_retry_at = datetime.fromisoformat(body["next_retry_at"])
    assert next_retry_at.tzinfo is not None  # clients get UTC, not a naive local time
    # The worker used the real clock, so check a window: about 10 s (the first backoff) after the attempt.
    assert before + timedelta(seconds=10) <= next_retry_at <= datetime.now(timezone.utc) + timedelta(seconds=10)


def test_job_posted_through_the_api_is_delivered_by_the_worker(client, repo):
    # API and worker together: the job goes in through POST, the worker sends it, GET reports the result.
    sent = []

    def downstream(job):  # records what was sent and answers 200
        sent.append(job)
        return SendResult(200)

    worker = Worker(repo, RetryPolicy(max_attempts=3, base_seconds=10, max_seconds=60),
                    send=downstream, lease=timedelta(seconds=60))
    job_id = client.post("/jobs/", json={"url": URL, "payload": {"order": 42}}).json()["id"]

    worker.process_one()

    assert [(job.id, job.url, job.payload) for job in sent] == [(job_id, URL, {"order": 42})]
    body = client.get(f"/jobs/{job_id}/").json()
    assert (body["status"], body["attempts"], body["next_retry_at"]) == ("completed", 1, None)


def test_unknown_job_is_404(client):
    response = client.get("/jobs/does-not-exist/")

    assert response.status_code == 404
    assert response.json() == {"detail": "job not found"}


@pytest.mark.parametrize("body", [
    {"url": "ftp://downstream.test/hook", "payload": {}},  # only http and https are allowed
    {"url": "file:///etc/passwd", "payload": {}},          # would point at a local file
    {"url": "not a url", "payload": {}},
    {"payload": {}},                                       # url missing
    {"url": URL},                                          # payload missing
])
def test_invalid_request_is_422_and_stores_nothing(client, repo, body):
    response = client.post("/jobs/", json=body)

    assert response.status_code == 422
    # The database is empty: there is no job for a worker to claim.
    assert repo.claim_next(datetime.now(timezone.utc), timedelta(seconds=60)) is None
