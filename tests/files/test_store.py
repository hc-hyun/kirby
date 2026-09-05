"""Bounded transfer, immutable admission, and restricted signing contracts."""

import base64
import json
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kirby.files import ObjectStore


def object_store():
    return ObjectStore(
        "http://minio.internal:9000",
        "test-access",
        "test-secret-never-forwarded",
        public_endpoint="http://127.0.0.1:9000",
    )


def test_direct_upload_policy_is_exact_and_signing_is_local(monkeypatch, tmp_path):
    secret_file = tmp_path / ".env"
    secret_file.write_text(
        "MINIO_ROOT_USER=test-access\n"
        "MINIO_ROOT_PASSWORD=test-secret-never-forwarded\n"
        "MINIO_BUCKET=dev\nMINIO_REGION=us-east-1\n"
    )
    monkeypatch.setenv("KIRBY_MINIO_ENDPOINT", "http://minio.internal:9000")
    monkeypatch.setenv("KIRBY_MINIO_CREDENTIALS_FILE", str(secret_file))
    monkeypatch.setenv("KIRBY_MINIO_PUBLIC_ENDPOINT", "http://127.0.0.1:9000")
    store = ObjectStore.from_env()
    file_id = str(uuid4())
    grant = store.create_upload(file_id, "demo.log", "text/plain", 50 * 1024 * 1024)
    assert grant["url"] == "http://127.0.0.1:9000/dev"
    assert grant["method"] == "POST"
    assert grant["fields"]["key"] == f"kirby/uploads/{file_id}"
    policy = json.loads(base64.b64decode(grant["fields"]["policy"]))
    assert ["eq", "$key", f"kirby/uploads/{file_id}"] in policy["conditions"]
    assert ["content-length-range", 52428800, 52428800] in policy["conditions"]
    assert "test-secret-never-forwarded" not in json.dumps(grant)
    url = store.download_url(
        {"filename": "결과.csv", "object_key": f"kirby/files/{file_id}"}
    )
    assert url.startswith(f"http://127.0.0.1:9000/dev/kirby/files/{file_id}?")
    assert "test-secret-never-forwarded" not in url
    assert secret_file.read_text().count("test-secret-never-forwarded") == 1
    with pytest.raises(ValueError):
        store.create_upload(file_id, "demo.log", "text/plain", 64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        store.download_url({"filename": "test.log", "object_key": "other/private"})


def test_completion_pins_copy_etag_and_distinct_contents_have_distinct_keys():
    store = object_store()
    source = SimpleNamespace(size=3, etag="original")
    copied = []

    def copy(bucket, key, condition):
        copied.append((key, condition))
        return SimpleNamespace(etag=source.etag)

    store.client = SimpleNamespace(
        stat_object=lambda *args: source,
        copy_object=copy,
    )
    file_id = str(uuid4())
    first = store.complete_upload(file_id, 3)
    assert copied[0][1].match_etag == "original"
    assert first == store.complete_upload(file_id, 3)
    source.etag = "changed"
    second = store.complete_upload(file_id, 3)
    assert first["object_key"] != second["object_key"]
    assert copied[-1][1].match_etag == "changed"
    source.size = 4
    with pytest.raises(ValueError, match="size"):
        store.complete_upload(file_id, 3)


def test_stream_download_cancellation_and_single_part_upload(tmp_path):
    store = object_store()
    size = 6 * 1024 * 1024
    cancel = Event()

    class Response(BytesIO):
        headers = {"Content-Length": str(size), "ETag": '"pinned"'}
        released = False

        def read(self, amount):
            assert amount == 64 * 1024
            value = super().read(amount)
            cancel.set()
            return value

        def release_conn(self):
            self.released = True

    response = Response(b"a" * size)
    requested_headers = []

    def get(bucket, key, request_headers):
        requested_headers.append(request_headers)
        return response

    store.client = SimpleNamespace(get_object=get)
    target = tmp_path / "download.log"
    with pytest.raises(InterruptedError):
        store.download(
            {"object_key": f"kirby/files/{uuid4()}", "size": size, "etag": "pinned"},
            target,
            cancel,
        )
    assert requested_headers == [{"If-Match": '"pinned"'}]
    assert not target.exists()
    assert response.closed and response.released

    cancel.clear()
    with target.open("wb") as stream:
        for _ in range(96):
            stream.write(b"b" * (64 * 1024))

    def put(bucket, key, reader, **options):
        assert options["length"] == size
        assert options["part_size"] == 5 * 1024 * 1024
        assert options["num_parallel_uploads"] == 1
        total = 0
        while chunk := reader.read(size):
            assert len(chunk) <= 5 * 1024 * 1024
            total += len(chunk)
        assert total == size
        return SimpleNamespace(etag="generated")

    store.client = SimpleNamespace(put_object=put)
    result = store.upload_result(str(uuid4()), "report.log", "text/plain", target)
    assert result["size"] == size and result["etag"] == "generated"
    assert result["object_key"] == f"kirby/files/{result['id']}"
