"""A local UI bridge; all agent operations use the public A2A HTTP client."""

import json
from pathlib import Path

import httpx
from a2a.client.errors import A2AClientTimeoutError
from a2a.utils.errors import A2AError
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

STATIC = Path(__file__).with_name("static")


def error_response(exc):
    """Keep credentials and raw upstream response bodies out of the browser."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return {"error": f"KIRBY API returned HTTP {status}."}, 502
    if isinstance(exc, (httpx.TimeoutException, A2AClientTimeoutError, TimeoutError)):
        return {"error": "KIRBY API timed out. The task may still be running."}, 504
    if isinstance(exc, httpx.RequestError):
        return {"error": "Cannot reach KIRBY API. Check its URL and process."}, 502
    if isinstance(exc, A2AError):
        return {"error": f"KIRBY API rejected the request: {type(exc).__name__}."}, 502
    if isinstance(exc, ValueError):
        return {
            "error": "Invalid request. Check the text, role and task identifiers."
        }, 400
    return {"error": "The client could not complete the request."}, 500


class LocalWriteMiddleware:
    """Require a same-origin custom-header request for local credential use."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            headers = dict(scope["headers"])
            origin = headers.get(b"origin")
            expected = scope["scheme"].encode() + b"://" + headers.get(b"host", b"")
            if headers.get(b"x-kirby-client") != b"1" or (
                origin is not None and origin != expected
            ):
                response = JSONResponse(
                    {"error": "Local client request required."}, 403
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def json_body(request, limit=1_700_000):
    # File bytes go directly to object storage; this bridge only accepts JSON.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise ValueError("Request too large")
        raw.extend(chunk)
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("Expected an object")
    return body


async def message_body(request):
    # The service owns role-specific limits; bound the local bridge as well.
    body = await json_body(request)
    text = body.get("text", "")
    context_id = body.get("context_id", "")
    message_id = body.get("message_id", "")
    immediate = body.get("immediate", True)
    attachments = body.get("attachments", [])
    if (
        not isinstance(text, str)
        or not text.strip()
        or not isinstance(context_id, str)
        or not isinstance(message_id, str)
        or not isinstance(immediate, bool)
    ):
        raise ValueError("Invalid message")
    if not isinstance(attachments, list) or len(attachments) > 4:
        raise ValueError("Invalid attachments")
    for part in attachments:
        if (
            not isinstance(part, dict)
            or set(part) != {"url", "filename", "mediaType"}
            or any(not isinstance(value, str) or not value for value in part.values())
            or len(part["url"]) > 2048
            or len(part["filename"]) > 255
            or len(part["mediaType"]) > 128
        ):
            raise ValueError("Invalid file reference")
    return {
        "text": text,
        "context_id": context_id,
        "message_id": message_id,
        "immediate": immediate,
        "attachments": attachments,
    }


async def stream_response(events):
    # Resolve connection/authentication errors before starting the response.
    first = await anext(events, None)

    async def lines():
        try:
            if first is not None:
                yield json.dumps(first, ensure_ascii=False) + "\n"
            async for event in events:
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps(error_response(exc)[0]) + "\n"
        finally:
            await events.aclose()

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def create_app(client, *, samples=None):
    """Create the local bridge. The caller owns the A2AClient lifecycle."""

    async def index(request):
        return FileResponse(STATIC / "index.html")

    async def config(request):
        return JSONResponse({"url": client.base_url, "samples": samples or {}})

    async def catalog(request):
        return JSONResponse(await client.catalog())

    async def card(request):
        return JSONResponse(await client.card(request.path_params["role"]))

    async def files(request):
        role = request.path_params["role"]
        if request.method == "GET":
            return JSONResponse(await client.file_options(role))
        body = await json_body(request, limit=4096)
        if (
            set(body) != {"filename", "media_type", "size"}
            or not isinstance(body["filename"], str)
            or not isinstance(body["media_type"], str)
            or type(body["size"]) is not int
            or body["size"] <= 0
        ):
            raise ValueError("Invalid file metadata")
        return JSONResponse(await client.reserve_file(role, **body))

    async def complete_file(request):
        return JSONResponse(
            await client.complete_file(
                request.path_params["role"], request.path_params["id"]
            )
        )

    async def send_message(request):
        body = await message_body(request)
        role = request.path_params["role"]
        if request.url.path.endswith("/stream"):
            body.pop("immediate")
            return await stream_response(client.stream(role, **body))
        return JSONResponse(await client.send(role, **body))

    async def list_tasks(request):
        return JSONResponse(
            await client.list(
                request.path_params["role"],
                context_id=request.query_params.get("context_id", ""),
                page_token=request.query_params.get("page_token", ""),
            )
        )

    async def task(request):
        return JSONResponse(
            await client.get(request.path_params["role"], request.path_params["id"])
        )

    async def cancel(request):
        return JSONResponse(
            await client.cancel(request.path_params["role"], request.path_params["id"])
        )

    async def subscribe(request):
        return await stream_response(
            client.subscribe(request.path_params["role"], request.path_params["id"])
        )

    async def handle_error(request, exc):
        body, status = error_response(exc)
        return JSONResponse(body, status_code=status)

    prefix = "/api/agents/{role}"
    app = Starlette(
        routes=[
            Route("/", index),
            Mount("/static", StaticFiles(directory=STATIC), name="static"),
            Route("/api/config", config),
            Route("/api/agents", catalog),
            Route(f"{prefix}/card", card),
            Route(f"{prefix}/files", files, methods=["GET", "POST"]),
            Route(f"{prefix}/files/{{id}}/complete", complete_file, methods=["POST"]),
            Route(f"{prefix}/send", send_message, methods=["POST"]),
            Route(f"{prefix}/stream", send_message, methods=["POST"]),
            Route(f"{prefix}/tasks", list_tasks),
            Route(f"{prefix}/tasks/{{id}}", task),
            Route(f"{prefix}/tasks/{{id}}/cancel", cancel, methods=["POST"]),
            Route(f"{prefix}/tasks/{{id}}/subscribe", subscribe),
        ],
        exception_handlers={
            ValueError: handle_error,
            A2AError: handle_error,
            httpx.HTTPError: handle_error,
            TimeoutError: handle_error,
        },
    )
    app.add_middleware(LocalWriteMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    return app
