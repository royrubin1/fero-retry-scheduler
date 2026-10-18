"""Delivers one job over HTTP. The only module that talks to the network."""
from dataclasses import dataclass

import httpx

from app.repository import Job


@dataclass(frozen=True)
class SendResult:
    status_code: int | None  # None: no response at all (timeout, connection refused...)
    error: str | None = None


def send(client: httpx.Client, job: Job) -> SendResult:
    try:
        response = client.post(
            job.url,
            json=job.payload,
            # Delivery is at-least-once (a worker can crash after the downstream answered),
            # so the downstream gets a stable key to drop duplicates.
            headers={"Idempotency-Key": job.id},
        )
    except httpx.RequestError as exc:
        return SendResult(None, f"{type(exc).__name__}: {exc}")
    # Redirects are not followed (httpx default), so a 3xx comes back as a permanent error.
    return SendResult(response.status_code)
