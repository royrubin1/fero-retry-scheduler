import pytest

from app.config import Settings


def test_environment_overrides_defaults(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/data/jobs.db")
    monkeypatch.setenv("MAX_ATTEMPTS", "7")

    settings = Settings.from_env()

    assert settings.db_path == "/data/jobs.db"
    assert settings.max_attempts == 7
    assert settings.lease_seconds == Settings.lease_seconds


def test_lease_must_outlast_the_http_timeout():
    with pytest.raises(ValueError):
        Settings(http_timeout_seconds=30, lease_seconds=30)
