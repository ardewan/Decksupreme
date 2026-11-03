"""Manual smoke-test DAG: proves Airflow can reach the warehouse, the raw bucket,
and the decksupreme package. Run via `make verify` or trigger it in the UI."""

from __future__ import annotations

from datetime import datetime

from airflow.decorators import dag, task


@dag(
    dag_id="infra_healthcheck",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["infra"],
    default_args={"retries": 0},
)
def infra_healthcheck():
    @task
    def check_warehouse() -> str:
        from airflow.providers.postgres.hooks.postgres import PostgresHook

        hook = PostgresHook(postgres_conn_id="decksupreme_warehouse")
        db, version = hook.get_first("SELECT current_database(), current_setting('server_version')")
        if not str(version).startswith("16"):
            raise RuntimeError(f"Expected PostgreSQL 16, got {version}")
        return f"{db} on PostgreSQL {version}"

    @task
    def check_raw_bucket() -> str:
        from airflow.providers.amazon.aws.hooks.s3 import S3Hook

        from decksupreme.config import get_settings

        bucket = get_settings().minio.bucket
        hook = S3Hook(aws_conn_id="minio_s3")
        if not hook.check_for_bucket(bucket):
            raise RuntimeError(f"Bucket {bucket!r} does not exist — did minio-init run?")

        key = "_healthcheck/ping.txt"
        hook.load_string("ok", key=key, bucket_name=bucket, replace=True)
        if hook.read_key(key, bucket_name=bucket) != "ok":
            raise RuntimeError("Round-trip read from MinIO returned unexpected content")
        hook.delete_objects(bucket=bucket, keys=[key])
        return f"s3://{bucket} read/write OK"

    @task
    def check_app_package() -> str:
        import decksupreme
        from decksupreme.config import get_settings

        settings = get_settings()
        return f"decksupreme {decksupreme.__version__}; warehouse host={settings.postgres.host}"

    [check_warehouse(), check_raw_bucket(), check_app_package()]


infra_healthcheck()
