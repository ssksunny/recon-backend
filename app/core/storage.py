"""
File storage abstraction for original documents.

Both implementations expose the same two methods (`save`, `load`), so
nothing above this layer needs to change if you switch backends. The
selected backend is decided once by `get_storage()`: S3 when AWS credentials
are configured, local disk otherwise (the default for local development,
and enough to run the whole ingestion pipeline end-to-end without any cloud
account).

Document.storage_key is the value `save()` returns — treat it as an opaque
identifier the storage backend understands, not a literal path.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.core.config import settings


class DocumentStorage(Protocol):
    def save(self, company_id: uuid.UUID, document_id: uuid.UUID, filename: str, data: bytes) -> str: ...

    def load(self, storage_key: str) -> bytes: ...


class LocalFileStorage:
    """Stores files on local disk under settings.local_storage_dir, namespaced by company."""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, company_id: uuid.UUID, document_id: uuid.UUID, filename: str, data: bytes) -> str:
        safe_filename = filename.replace("/", "_").replace("\\", "_") or "unnamed"
        key = f"{company_id}/{document_id}_{safe_filename}"
        path = self.base_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def load(self, storage_key: str) -> bytes:
        return (self.base_dir / storage_key).read_bytes()


class S3Storage:
    """Stores files in S3 (or an S3-compatible endpoint, e.g. MinIO in dev)."""

    def __init__(self, bucket_name: str, region: str, endpoint_url: str | None = None):
        import boto3  # imported lazily so local-disk-only setups don't need boto3 configured

        self.bucket_name = bucket_name
        self._client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def save(self, company_id: uuid.UUID, document_id: uuid.UUID, filename: str, data: bytes) -> str:
        safe_filename = filename.replace("/", "_").replace("\\", "_") or "unnamed"
        key = f"{company_id}/{document_id}_{safe_filename}"
        self._client.put_object(Bucket=self.bucket_name, Key=key, Body=data)
        return key

    def load(self, storage_key: str) -> bytes:
        obj = self._client.get_object(Bucket=self.bucket_name, Key=storage_key)
        return obj["Body"].read()


@lru_cache
def get_storage() -> DocumentStorage:
    if settings.storage_backend == "s3":
        return S3Storage(settings.s3_bucket_name, settings.s3_region, settings.s3_endpoint_url)
    return LocalFileStorage(settings.local_storage_dir)


# Module-level singleton for straightforward imports: `from app.core.storage import storage`
storage = get_storage()
