"""Background worker: claims due jobs, sends them and stores where each one goes next.

Run one or more copies with `python -m app.worker`. They share the database and
never work on the same job at the same time, because every job is taken with the
atomic claim in the repository. The poll loop is the scheduler: a job waiting for
a retry is just a `pending` row whose next_retry_at hasn't passed yet.
"""
import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Callable

import httpx

from app.config import Settings
from app.models import JobStatus
from app.repository import Job, JobRepository
from app.retry_policy import Decision, Outcome, RetryPolicy, classify
from app.sender import SendResult, send

log = logging.getLogger("worker")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Worker:
    repo: JobRepository
    policy: RetryPolicy
    send: Callable[[Job], SendResult]
    lease: timedelta
    now: Callable[[], datetime] = utcnow

    def process_one(self) -> bool:
        """Claim one due job, send it and store the result. False when nothing was due."""
        job = self.repo.claim_next(self.now(), self.lease)
        if job is None:
            return False

        if job.attempts > self.policy.max_attempts:
            # Only happens when a worker died during the last attempt and its lease ran out:
            # the claim counted one attempt too many. We can't know whether that attempt
            # reached the downstream, so park the job in `dead` instead of sending it again.
            self._finish(job, Decision(JobStatus.DEAD, None), "no attempts left, the last one never reported back")
            return True

        result = self.send(job)
        outcome = classify(result.status_code)
        error = None if outcome is Outcome.SUCCESS else result.error or f"HTTP {result.status_code}"
        self._finish(job, self.policy.decide(outcome, job.attempts, self.now()), error)
        return True

    def _finish(self, job: Job, decision: Decision, error: str | None) -> None:
        if not self.repo.finish(job, decision, error, self.now()):
            log.warning("job=%s attempt=%d result not saved: the lease ran out and another worker owns the job",
                        job.id, job.attempts)
            return
        retry = f" retry_at={decision.next_retry_at.isoformat()}" if decision.next_retry_at else ""
        log.info("job=%s attempt=%d result=%s status=%s%s", job.id, job.attempts, error or "ok", decision.status, retry)

    def run(self, poll_interval: float, stop: threading.Event) -> None:
        """Process jobs until `stop` is set. Sleeps only when nothing is due."""
        while not stop.is_set():
            try:
                busy = self.process_one()
            except Exception:
                # A bug or a database hiccup must not kill the worker. The job it held keeps
                # its lease and is claimed again once the lease runs out.
                log.exception("unexpected error while processing a job")
                busy = False
            if not busy:
                stop.wait(poll_interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # our own line per attempt says enough
    settings = Settings.from_env()
    repo = JobRepository(settings.db_path)
    repo.init_schema()

    # Finish the current attempt on Ctrl+C / docker stop instead of dying halfway through it.
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    with httpx.Client(timeout=settings.http_timeout_seconds) as client:
        worker = Worker(repo, settings.retry_policy(), partial(send, client),
                        lease=timedelta(seconds=settings.lease_seconds))
        log.info("worker started db=%s", settings.db_path)
        worker.run(settings.poll_interval_seconds, stop)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
