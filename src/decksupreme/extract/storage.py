"""Raw-layer writer: puts the weekly battle payload in MinIO via the S3 API.

Key layout uses the ISO year and week, `year=YYYY/week=WW/raw_battles.json`,
which matches rankings_YYYY_WW.json. Running again in the same week overwrites
that week's object, so reruns are safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import boto3
from botocore.config import Config

from decksupreme.config import MinioSettings, get_settings

RAW_BATTLES_FILENAME = "raw_battles.json"


def iso_year_week(when: datetime) -> tuple[int, int]:
    year, week, _ = when.isocalendar()
    return year, week


def raw_battles_key(when: datetime) -> str:
    year, week = iso_year_week(when)
    return f"year={year:04d}/week={week:02d}/{RAW_BATTLES_FILENAME}"


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    size_bytes: int
    record_count: int

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class RawStorage:
    def __init__(self, settings: MinioSettings | None = None, client: Any | None = None):
        self.settings = settings or get_settings().minio
        self.bucket = self.settings.bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=self.settings.endpoint,
            aws_access_key_id=self.settings.access_key,
            aws_secret_access_key=self.settings.secret_key,
            region_name=self.settings.region,
            config=Config(s3={"addressing_style": "path"}, retries={"mode": "standard"}),
        )

    def put_raw_battles(
        self, payload: dict[str, Any], *, record_count: int, extracted_at: datetime
    ) -> StoredObject:
        if extracted_at.tzinfo is None:
            extracted_at = extracted_at.replace(tzinfo=UTC)
        ts = extracted_at.astimezone(UTC).isoformat(timespec="seconds")
        key = raw_battles_key(extracted_at)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        meta = {"record-count": str(record_count), "extracted-at": ts}
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata=meta,
            Tagging=urlencode({"record_count": record_count, "extracted_at": ts}),
        )
        return StoredObject(self.bucket, key, len(body), record_count)

    def get_json(self, key: str) -> dict[str, Any]:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return json.loads(obj["Body"].read())
