from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"      # waiting for its first attempt or for next_retry_at
    RUNNING = "running"      # claimed by a worker, attempt in progress
    COMPLETED = "completed"  # final: downstream returned 2xx
    FAILED = "failed"        # final: non-retryable error (e.g. 400, 404)
    DEAD = "dead"            # final: retryable errors until max attempts ran out
