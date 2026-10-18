import json

import httpx
import pytest

from app.models import JobStatus
from app.repository import Job
from app.sender import SendResult, send

JOB = Job(id="job-1", url="http://downstream.test/hook", payload={"order": 42},
          status=JobStatus.RUNNING, attempts=1, next_retry_at=None, last_error=None)


def client_answering(handler):
    # MockTransport calls the handler instead of opening a socket.
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_posts_the_payload_with_the_job_id_as_idempotency_key():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(202)

    assert send(client_answering(handler), JOB) == SendResult(202)

    [request] = requests
    assert request.method == "POST"
    assert str(request.url) == JOB.url
    assert json.loads(request.content) == {"order": 42}
    assert request.headers["Idempotency-Key"] == "job-1"


def test_redirect_is_returned_not_followed():
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://elsewhere.test/"})

    assert send(client_answering(handler), JOB) == SendResult(302)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_error_is_no_response(error):
    def handler(request):
        raise error("boom", request=request)

    assert send(client_answering(handler), JOB) == SendResult(None, f"{error.__name__}: boom")
