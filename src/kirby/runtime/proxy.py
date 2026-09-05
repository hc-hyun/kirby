"""A per-turn OpenRouter budget boundary. Credentials never enter Goose."""

import asyncio
import secrets
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class ModelProxy:
    def __init__(
        self, key, model, max_requests, max_tokens, cancel, allowed_tools=None
    ):
        self._key = key
        self.model = model
        self.max_requests = max_requests
        self.max_tokens = max_tokens
        self.cancel = cancel
        self.token = secrets.token_urlsafe(32)
        self.requests = 0
        self.tool_names: set[str] = set()
        self.allowed_tools = frozenset(allowed_tools or {"load_skill"})
        self.limit_reached = asyncio.Event()

    async def complete(self, request):
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            raise web.HTTPUnauthorized()
        body = await request.json()
        # Reserve the request after reading its body, with no intervening await.
        if self.cancel.is_set():
            raise web.HTTPConflict(reason="execution_stopped")
        if self.requests >= self.max_requests:
            self.limit_reached.set()
            raise web.HTTPTooManyRequests(reason="model_request_limit")
        offered = {item["function"]["name"] for item in body.get("tools", [])}
        # Also verify the native tool surface: ACP callbacks alone are insufficient.
        if offered - self.allowed_tools:
            self.limit_reached.set()
            raise web.HTTPForbidden(reason="unapproved_native_tool")
        self.tool_names.update(offered)
        body.update(model=self.model, max_tokens=self.max_tokens)
        body.pop("models", None)
        body.pop("max_completion_tokens", None)
        body["provider"] = {
            "allow_fallbacks": False,
            **(
                {"max_price": {"prompt": 0, "completion": 0}}
                if self.model.endswith(":free")
                else {}
            ),
        }
        self.requests += 1
        try:
            async with self.http.post(
                OPENROUTER_URL,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
            ) as upstream:
                if upstream.status >= 400:
                    # Upstream error bodies are not part of the public/log contract.
                    return web.json_response(
                        {"error": {"message": "model_provider_error"}},
                        status=upstream.status,
                    )
                response = web.StreamResponse(
                    headers={
                        "Content-Type": upstream.headers.get(
                            "Content-Type", "application/json"
                        )
                    }
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    if self.cancel.is_set():
                        break
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, TimeoutError):
            return web.json_response(
                {"error": {"message": "model_provider_unavailable"}}, status=502
            )

    @asynccontextmanager
    async def serve(self):
        app = web.Application(client_max_size=4 * 1048576)
        app.router.add_post("/api/v1/chat/completions", self.complete)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300), trust_env=False
        ) as self.http:
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            try:
                await site.start()
                port = runner.addresses[0][1]
                yield f"http://127.0.0.1:{port}"
            finally:
                await runner.cleanup()
