"""Environment-driven settings shared by DAGs, scripts and tests.

The same code runs on the host (talking to published ports on localhost) and
inside the Airflow containers (where docker-compose overrides POSTGRES_HOST,
POSTGRES_PORT and MINIO_ENDPOINT to the in-network service addresses).

Secrets are optional at load time so the stack can boot before keys exist;
call the `require_*` helpers at the point a secret is actually needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote_plus


class MissingSettingError(RuntimeError):
    """Raised when a secret needed by the current task is not configured."""


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _require(value: str, name: str) -> str:
    if not value:
        raise MissingSettingError(f"{name} is not set — add it to .env and restart the stack.")
    return value


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    user: str
    db: str
    password: str = field(repr=False)

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg2://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.db}"
        )


@dataclass(frozen=True)
class MinioSettings:
    endpoint: str
    bucket: str
    region: str
    access_key: str
    secret_key: str = field(repr=False)


@dataclass(frozen=True)
class ClashSettings:
    base_url: str
    max_requests_per_second: float
    api_key: str = field(repr=False)

    def require_api_key(self) -> str:
        return _require(self.api_key, "CLASH_ROYALE_API_KEY")


@dataclass(frozen=True)
class GitHubSettings:
    repo: str
    branch: str
    token: str = field(repr=False)

    def require_token(self) -> str:
        return _require(self.token, "GITHUB_TOKEN")

    def require_repo(self) -> str:
        return _require(self.repo, "GITHUB_REPO")


DEFAULT_WEIGHTS = {"wr": 0.35, "ur": 0.25, "ape": 0.15, "dpe": 0.15, "ss": 0.10}


def parse_weights(raw: str) -> dict[str, float]:
    """'wr=0.4,ur=0.2,...' -> dict. Must name exactly the five features and sum to 1."""
    if not raw:
        return dict(DEFAULT_WEIGHTS)
    weights: dict[str, float] = {}
    for part in raw.split(","):
        key, _, value = part.partition("=")
        weights[key.strip().lower()] = float(value)
    if set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError(f"SCORING_WEIGHTS must set exactly {sorted(DEFAULT_WEIGHTS)}, got {raw!r}")
    if any(w < 0 for w in weights.values()) or abs(sum(weights.values()) - 1) > 1e-6:
        raise ValueError(f"SCORING_WEIGHTS must be non-negative and sum to 1.0, got {raw!r}")
    return weights


@dataclass(frozen=True)
class ScoringSettings:
    weights: dict[str, float]
    min_matches: int  # cards with fewer deck appearances in the window are not ranked


@dataclass(frozen=True)
class Settings:
    postgres: PostgresSettings
    minio: MinioSettings
    clash: ClashSettings
    github: GitHubSettings
    scoring: ScoringSettings


def load_settings() -> Settings:
    """Build settings from the current environment (uncached; handy in tests)."""
    return Settings(
        postgres=PostgresSettings(
            host=_get("POSTGRES_HOST", "localhost"),
            port=int(_get("POSTGRES_PORT", "5432")),
            user=_get("POSTGRES_USER", "decksupreme"),
            db=_get("POSTGRES_DB", "decksupreme"),
            password=_get("POSTGRES_PASSWORD"),
        ),
        minio=MinioSettings(
            endpoint=_get("MINIO_ENDPOINT", "http://localhost:9000"),
            bucket=_get("MINIO_BUCKET", "decksupreme-raw"),
            region=_get("MINIO_REGION", "us-east-1"),
            access_key=_get("MINIO_ROOT_USER"),
            secret_key=_get("MINIO_ROOT_PASSWORD"),
        ),
        clash=ClashSettings(
            base_url=_get("CLASH_ROYALE_API_BASE_URL", "https://api.clashroyale.com/v1"),
            max_requests_per_second=float(_get("CLASH_ROYALE_MAX_RPS", "30")),
            api_key=_get("CLASH_ROYALE_API_KEY"),
        ),
        github=GitHubSettings(
            repo=_get("GITHUB_REPO"),
            branch=_get("GITHUB_BRANCH", "main"),
            token=_get("GITHUB_TOKEN"),
        ),
        scoring=ScoringSettings(
            weights=parse_weights(_get("SCORING_WEIGHTS")),
            min_matches=int(_get("SCORING_MIN_MATCHES", "1")),
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings."""
    return load_settings()


def __getattr__(name: str):
    # `from decksupreme.config import settings` resolves lazily, so importing
    # this module never freezes the environment before tests can patch it.
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
