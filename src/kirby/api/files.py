"""Authorize file references; file bodies travel directly to object storage."""

import asyncio
import contextlib
from functools import partial
from pathlib import PurePath
from uuid import UUID, uuid4

from a2a import types as t
from google.protobuf.json_format import MessageToDict
from minio.error import MinioException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route
from urllib3.exceptions import HTTPError

from kirby.contracts import Conflict, NotFound

FILE_TYPES = {
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
}
MAX_ATTACHMENTS = 4


def file_part(record, public_url, role_id):
    return t.Part(
        url=f"{public_url.rstrip('/')}/agents/{role_id}/files/{record['id']}/content",
        filename=record["filename"],
        media_type=record["media_type"],
    )


def file_reference(part, public_url, role_id):
    prefix = f"{public_url.rstrip('/')}/agents/{role_id}/files/"
    if not part.url.startswith(prefix) or not part.url.endswith("/content"):
        raise ValueError("Only registered file references are accepted")
    value = part.url[len(prefix) : -len("/content")]
    if str(UUID(value)) != value:
        raise ValueError("Invalid file ID")
    return value


def validate_file(body, max_size):
    if not isinstance(body, dict):
        raise ValueError("Expected file metadata")
    name = body.get("filename", "")
    size = body.get("size")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.encode()) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or any(char in name for char in '/\\:"')
    ):
        raise ValueError("Invalid filename")
    media_type = FILE_TYPES.get(PurePath(name).suffix.lower())
    if media_type is None or type(size) is not int or not 0 < size <= max_size:
        raise ValueError("Unsupported file type or size")
    return name, media_type, size


def file_routes(store, object_store, public_url, role_id):
    async def handle(request, action):
        principal = request.scope["kirby_principal"]
        if role_id not in principal.roles:
            return JSONResponse({"error": "File not found"}, 404)
        if action == "options":
            return JSONResponse(
                {
                    "enabled": object_store is not None,
                    "max_file_bytes": object_store.max_file_bytes
                    if object_store
                    else 0,
                    "allowed_extensions": list(FILE_TYPES),
                    "max_attachments": MAX_ATTACHMENTS,
                }
            )
        if object_store is None:
            return JSONResponse({"error": "File storage is not configured"}, 503)
        try:
            if action == "reserve":
                name, media_type, size = validate_file(
                    await request.json(), object_store.max_file_bytes
                )
                file_id = str(uuid4())
                record = await store.create_file(
                    principal, role_id, file_id, name, media_type, size
                )
                upload = await asyncio.to_thread(
                    object_store.create_upload, file_id, name, media_type, size
                )
                return JSONResponse(
                    {
                        "id": record["id"],
                        "filename": name,
                        "media_type": media_type,
                        "size": size,
                        "upload": upload,
                    }
                )
            file_id = str(UUID(request.path_params["file_id"]))
            record = await store.get_file(principal, role_id, file_id)
            if action == "complete" and record["status"] != "ready":
                sealed = await asyncio.to_thread(
                    object_store.complete_upload, file_id, record["size"]
                )
                record = await store.complete_file(
                    principal, role_id, file_id, sealed["object_key"], sealed["etag"]
                )
                with contextlib.suppress(MinioException, HTTPError, OSError):
                    await asyncio.to_thread(object_store.discard_upload, file_id)
            if record["status"] != "ready":
                raise Conflict("Upload is not complete")
            if action == "content":
                return RedirectResponse(
                    object_store.download_url(record),
                    status_code=307,
                    headers={"Cache-Control": "private, no-store"},
                )
            return JSONResponse(
                {
                    "id": record["id"],
                    "filename": record["filename"],
                    "media_type": record["media_type"],
                    "size": record["size"],
                    "part": MessageToDict(file_part(record, public_url, role_id)),
                }
            )
        except NotFound:
            return JSONResponse({"error": "File not found"}, 404)
        except Conflict:
            return JSONResponse({"error": "Upload is incomplete or conflicts"}, 409)
        except ValueError:
            return JSONResponse({"error": "Invalid file metadata, type or size"}, 400)
        except (MinioException, HTTPError, OSError):
            return JSONResponse({"error": "Object storage is unavailable"}, 503)

    prefix = f"/agents/{role_id}/files"
    return [
        Route(prefix, partial(handle, action="options"), methods=["GET"]),
        Route(prefix, partial(handle, action="reserve"), methods=["POST"]),
        Route(
            f"{prefix}/{{file_id}}/complete",
            partial(handle, action="complete"),
            methods=["POST"],
        ),
        Route(f"{prefix}/{{file_id}}/content", partial(handle, action="content")),
    ]
