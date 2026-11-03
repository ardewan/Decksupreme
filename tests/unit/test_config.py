import pytest

from decksupreme.config import MissingSettingError, load_settings


@pytest.fixture
def env(monkeypatch):
    for key in [
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
        "MINIO_ENDPOINT", "MINIO_BUCKET", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
        "CLASH_ROYALE_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_BRANCH",
    ]:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults_target_host_ports(env):
    s = load_settings()
    assert s.postgres.host == "localhost"
    assert s.postgres.port == 5432
    assert s.minio.endpoint == "http://localhost:9000"
    assert s.minio.bucket == "decksupreme-raw"
    assert s.github.branch == "main"
    assert s.clash.max_requests_per_second == 30


def test_sqlalchemy_url_escapes_credentials(env):
    env.setenv("POSTGRES_USER", "deck")
    env.setenv("POSTGRES_PASSWORD", "p@ss:w/rd")
    env.setenv("POSTGRES_HOST", "postgres")
    url = load_settings().postgres.sqlalchemy_url
    assert url == "postgresql+psycopg2://deck:p%40ss%3Aw%2Frd@postgres:5432/decksupreme"


def test_secrets_hidden_from_repr(env):
    env.setenv("POSTGRES_PASSWORD", "hunter2")
    env.setenv("MINIO_ROOT_PASSWORD", "minio-secret")
    env.setenv("CLASH_ROYALE_API_KEY", "clash-secret")
    env.setenv("GITHUB_TOKEN", "ghp_secret")
    text = repr(load_settings())
    for secret in ("hunter2", "minio-secret", "clash-secret", "ghp_secret"):
        assert secret not in text


def test_missing_secrets_fail_loudly_only_when_required(env):
    s = load_settings()  # loading never fails
    with pytest.raises(MissingSettingError, match="CLASH_ROYALE_API_KEY"):
        s.clash.require_api_key()
    with pytest.raises(MissingSettingError, match="GITHUB_TOKEN"):
        s.github.require_token()
