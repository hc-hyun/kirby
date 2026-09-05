"""Small MinIO adapter: private objects and short-lived direct transfer grants."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4, uuid5

import urllib3
from dotenv import dotenv_values
from minio import Minio
from minio.commonconfig import CopySource
from minio.datatypes import PostPolicy

MAX_FILE_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
PART_BYTES = 5 * 1024 * 1024
GRANT_LIFETIME = timedelta(minutes=15)


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Expected a canonical file or task UUID")
    return value


def _key(value: str, *, staging: bool = False) -> str:
    parts = value.split("/")
    allowed = {"files", "uploads"} if staging else {"files"}
    if len(parts) != 3 or parts[0] != "kirby" or parts[1] not in allowed:
        raise ValueError("Object is outside the KIRBY file prefix")
    _uuid(parts[2])
    return value


def _filename(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or len(value.encode("utf-8")) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in '/\\:"')
    ):
        raise ValueError("Invalid filename")
    return value


def _media_type(value: str) -> str:
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid media type")
    return value


def _endpoint(value: str) -> tuple[str, bool]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MinIO endpoint must be an HTTP(S) origin")
    return parsed.netloc, parsed.scheme == "https"


def _check_cancel(cancel: Any) -> None:
    if cancel is not None and cancel.is_set():
        raise InterruptedError("File operation canceled")


class _UploadReader:
    def __init__(self, stream: Any, cancel: Any):
        self.stream = stream
        self.cancel = cancel

    def read(self, size: int) -> bytes:
        _check_cancel(self.cancel)
        if size < 0:
            raise ValueError("Unbounded file reads are disabled")
        return self.stream.read(min(size, PART_BYTES))


class ObjectStore:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        bucket: str = "dev",
        public_endpoint: str | None = None,
        region: str = "us-east-1",
        max_file_bytes: int = MAX_FILE_BYTES,
    ):
        if not 0 < max_file_bytes <= MAX_FILE_BYTES:
            raise ValueError("Maximum file size must be at most 64 MiB")
        if not access_key or not secret_key or not region:
            raise ValueError("MinIO credentials and region are required")
        host, secure = _endpoint(endpoint)
        public_host, public_secure = _endpoint(public_endpoint or endpoint)
        self.bucket = bucket
        self.max_file_bytes = max_file_bytes
        self.public_endpoint = f"{'https' if public_secure else 'http'}://{public_host}"
        self.client = Minio(
            host,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region,
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=5, read=15), retries=False
            ),
        )
        # Explicit region means signing never probes a bucket or performs network I/O.
        self.signer = Minio(
            public_host,
            access_key=access_key,
            secret_key=secret_key,
            secure=public_secure,
            region=region,
        )

    @classmethod
    def from_env(cls) -> "ObjectStore | None":
        endpoint = os.environ.get("KIRBY_MINIO_ENDPOINT")
        if not endpoint:
            return None
        credentials_file = os.environ.get("KIRBY_MINIO_CREDENTIALS_FILE")
        if not credentials_file:
            raise ValueError("KIRBY_MINIO_CREDENTIALS_FILE is required")
        values = dotenv_values(Path(credentials_file).expanduser(), interpolate=False)
        return cls(
            endpoint,
            values.get("MINIO_ROOT_USER") or "",
            values.get("MINIO_ROOT_PASSWORD") or "",
            bucket=os.environ.get("KIRBY_MINIO_BUCKET")
            or values.get("MINIO_BUCKET")
            or "dev",
            region=os.environ.get("KIRBY_MINIO_REGION")
            or values.get("MINIO_REGION")
            or "us-east-1",
            public_endpoint=os.environ.get("KIRBY_MINIO_PUBLIC_ENDPOINT"),
        )

    def _size(self, size: int) -> int:
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= self.max_file_bytes
        ):
            raise ValueError("File exceeds the configured size limit")
        return size

    def create_upload(
        self, file_id: str, filename: str, media_type: str, size: int
    ) -> dict[str, Any]:
        _filename(filename)
        _media_type(media_type)
        self._size(size)
        key = f"kirby/uploads/{_uuid(file_id)}"
        policy = PostPolicy(self.bucket, datetime.now(UTC) + GRANT_LIFETIME)
        policy.add_equals_condition("key", key)
        policy.add_equals_condition("Content-Type", media_type)
        policy.add_content_length_range_condition(size, size)
        fields = self.signer.presigned_post_policy(policy)
        fields.update({"key": key, "Content-Type": media_type})
        return {
            "url": f"{self.public_endpoint}/{self.bucket}",
            "fields": fields,
            "method": "POST",
        }

    def complete_upload(self, file_id: str, expected_size: int) -> dict[str, Any]:
        self._size(expected_size)
        staging_key = f"kirby/uploads/{_uuid(file_id)}"
        source = self.client.stat_object(self.bucket, staging_key)
        if source.size != expected_size:
            raise ValueError("Uploaded size differs from the admitted size")
        # A new upload to staging cannot overwrite an already admitted immutable file.
        final_key = f"kirby/files/{uuid5(UUID(file_id), source.etag)}"
        result = self.client.copy_object(
            self.bucket,
            final_key,
            CopySource(self.bucket, staging_key, match_etag=source.etag),
        )
        return {"object_key": final_key, "size": expected_size, "etag": result.etag}

    def discard_upload(self, file_id: str) -> None:
        """Call only after the durable ready row has committed."""
        self.client.remove_object(self.bucket, f"kirby/uploads/{_uuid(file_id)}")

    def download(self, ref: dict[str, Any], path: Path, cancel: Any = None) -> None:
        key = _key(ref["object_key"])
        size = self._size(ref["size"])
        etag = ref["etag"]
        if not isinstance(etag, str) or not etag:
            raise ValueError("A pinned ETag is required")
        _check_cancel(cancel)
        response = self.client.get_object(
            self.bucket, key, request_headers={"If-Match": f'"{etag}"'}
        )
        destination = Path(path)
        opened = False
        try:
            if (
                int(response.headers.get("Content-Length", "-1")) != size
                or response.headers.get("ETag", "").strip('"') != etag
            ):
                raise ValueError("Stored file no longer matches the pinned reference")
            count = 0
            with destination.open("wb") as stream:
                opened = True
                while True:
                    _check_cancel(cancel)
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > size:
                        raise ValueError("Downloaded file exceeds its admitted size")
                    stream.write(chunk)
            if count != size:
                raise ValueError("Downloaded file was truncated")
        except BaseException:
            if opened:
                destination.unlink(missing_ok=True)
            raise
        finally:
            response.close()
            response.release_conn()

    def upload_result(
        self,
        task_id: str,
        name: str,
        media_type: str,
        path: Path,
        cancel: Any = None,
    ) -> dict[str, Any]:
        _filename(name)
        _media_type(media_type)
        source = Path(path)
        size = self._size(source.stat().st_size)
        _check_cancel(cancel)
        file_id = str(uuid5(UUID(_uuid(task_id)), f"{name}:{uuid4()}"))
        key = f"kirby/files/{file_id}"
        with source.open("rb") as stream:
            result = self.client.put_object(
                self.bucket,
                key,
                _UploadReader(stream, cancel),
                length=size,
                content_type=media_type,
                part_size=PART_BYTES,
                num_parallel_uploads=1,
            )
        _check_cancel(cancel)
        return {
            "id": file_id,
            "filename": name,
            "media_type": media_type,
            "size": size,
            "object_key": key,
            "etag": result.etag,
        }

    def download_url(self, ref: dict[str, Any]) -> str:
        name = _filename(ref["filename"])
        return self.signer.presigned_get_object(
            self.bucket,
            _key(ref["object_key"]),
            expires=GRANT_LIFETIME,
            response_headers={
                "response-content-disposition": (
                    f"attachment; filename*=UTF-8''{quote(name, safe='')}"
                ),
                "response-content-type": "application/octet-stream",
            },
        )

    def delete(self, ref: dict[str, Any]) -> None:
        self.client.remove_object(self.bucket, _key(ref["object_key"], staging=True))
