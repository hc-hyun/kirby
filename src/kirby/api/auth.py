"""Opaque bearer identity and a request limit before SDK JSON parsing."""

from a2a.server.context import ServerCallContext
from a2a.server.routes.common import ServerCallContextBuilder
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class IdentityContext(ServerCallContextBuilder):
    def build(self, request):
        return ServerCallContext(
            state={
                "principal": request.scope["kirby_principal"],
                "headers": {"a2a-version": request.headers.get("a2a-version", "")},
            }
        )


class AccessMiddleware:
    def __init__(self, app, credentials, max_request_bytes):
        self.app = app
        self.credentials = credentials
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in {"/healthz", "/readyz"}:
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        principal = self.credentials.get(token)
        if principal is None:
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        scope["kirby_principal"] = principal

        if scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_request_bytes:
                await JSONResponse({"error": "request too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body)}
            return await receive()

        await self.app(scope, bounded_receive, send)
