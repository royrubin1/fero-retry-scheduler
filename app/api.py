"""HTTP API: accepts jobs and reports their state. It only reads and writes the
database; the delivery happens in the worker process.

Run with `python -m app.api`, then open http://127.0.0.1:8000/docs.
"""
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Response, status
from pydantic import AnyHttpUrl, BaseModel

from app.config import Settings
from app.models import JobStatus
from app.repository import Job, JobRepository


class JobRequest(BaseModel):
    url: AnyHttpUrl  # http and https only: file://, ftp:// and junk get a 422
    payload: Any     # any JSON value, sent as the request body


class JobAccepted(BaseModel):
    id: str
    status: JobStatus


class JobState(BaseModel):
    id: str
    status: JobStatus
    attempts: int
    next_retry_at: datetime | None
    last_error: str | None  # not in the spec, but it tells you why a job is failed or dead

    @classmethod
    def of(cls, job: Job) -> "JobState":
        return cls(id=job.id, status=job.status, attempts=job.attempts,
                   next_retry_at=job.next_retry_at, last_error=job.last_error)


def create_app(repo: JobRepository) -> FastAPI:
    app = FastAPI(title="Retry Scheduler")

    # 202, not 201: the job is accepted but not delivered yet. Location points to where its state can be followed.
    @app.post("/jobs/", status_code=status.HTTP_202_ACCEPTED)
    def create_job(request: JobRequest, response: Response) -> JobAccepted:
        job = repo.create(str(request.url), request.payload, now=datetime.now(timezone.utc))
        response.headers["Location"] = f"/jobs/{job.id}/"
        return JobAccepted(id=job.id, status=job.status)

    @app.get("/jobs/{job_id}/", responses={404: {"description": "No job with this id"}})
    def get_job(job_id: str) -> JobState:
        job = repo.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
        return JobState.of(job)

    return app


def main() -> None:
    settings = Settings.from_env()
    repo = JobRepository(settings.db_path)
    repo.init_schema()
    uvicorn.run(create_app(repo), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
