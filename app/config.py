"""Settings, with defaults that suit a local demo (short backoff, so a job reaches `dead` in about 30 s)."""
import os
from dataclasses import dataclass, fields

from app.retry_policy import RetryPolicy


@dataclass(frozen=True)
class Settings:
    db_path: str = "jobs.db"
    max_attempts: int = 5
    backoff_base_seconds: float = 2
    backoff_max_seconds: float = 60
    http_timeout_seconds: float = 10
    lease_seconds: float = 60
    poll_interval_seconds: float = 1

    def __post_init__(self):
        # The lease has to outlast an attempt, or a slow but healthy worker loses its job
        # to another worker and the request goes out twice. httpx applies the timeout to
        # each phase (connect, read...), not to the whole request, so leave a wide margin.
        if self.lease_seconds <= self.http_timeout_seconds:
            raise ValueError("LEASE_SECONDS must be greater than HTTP_TIMEOUT_SECONDS")

    @classmethod
    def from_env(cls) -> "Settings":
        """Each field can be overridden by the environment variable of the same name in upper case."""
        return cls(**{
            f.name: f.type(os.environ[f.name.upper()])
            for f in fields(cls)
            if f.name.upper() in os.environ
        })

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(self.max_attempts, self.backoff_base_seconds, self.backoff_max_seconds)
