"""Small HTTP client for KIRBY using the pinned official A2A transport."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
from a2a import types as t
from a2a.client.transports.jsonrpc import JsonRpcTransport
from google.protobuf.json_format import MessageToDict, ParseDict


class A2AClient:
    """Own one HTTP connection pool; callers must use the async context manager."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 330,
    ):
        url = httpx.URL(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Base URL must be HTTP(S) without credentials, query or fragment"
            )
        if not token.strip():
            raise ValueError("A bearer token is required")
        self._http = httpx.AsyncClient(
            base_url=str(url).rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        )
        self._transports: dict[str, JsonRpcTransport] = {}

    @property
    def base_url(self) -> str:
        return str(self._http.base_url).rstrip("/")

    async def __aenter__(self):
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self._http.__aexit__(*args)

    @staticmethod
    def _role_path(role_id: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", role_id):
            raise ValueError("Invalid role ID")
        return f"agents/{role_id}"

    async def catalog(self) -> dict:
        response = await self._http.get("agents")
        response.raise_for_status()
        return response.json()

    async def card(self, role_id: str) -> dict:
        response = await self._http.get(
            f"{self._role_path(role_id)}/.well-known/agent-card.json"
        )
        response.raise_for_status()
        return MessageToDict(ParseDict(response.json(), t.AgentCard()))

    def _file_path(self, role_id: str, file_id: str = "") -> str:
        path = f"{self._role_path(role_id)}/files"
        if file_id:
            if not re.fullmatch(r"[a-zA-Z0-9-]{1,128}", file_id):
                raise ValueError("Invalid file ID")
            path += f"/{file_id}"
        return path

    async def file_options(self, role_id: str) -> dict:
        response = await self._http.get(self._file_path(role_id))
        response.raise_for_status()
        return response.json()

    async def reserve_file(
        self, role_id: str, *, filename: str, media_type: str, size: int
    ) -> dict:
        response = await self._http.post(
            self._file_path(role_id),
            json={"filename": filename, "media_type": media_type, "size": size},
        )
        response.raise_for_status()
        return response.json()

    async def complete_file(self, role_id: str, file_id: str) -> dict:
        response = await self._http.post(
            f"{self._file_path(role_id, file_id)}/complete"
        )
        response.raise_for_status()
        return response.json()

    async def _transport(self, role_id: str) -> JsonRpcTransport:
        path = self._role_path(role_id)
        if role_id not in self._transports:
            card = ParseDict(await self.card(role_id), t.AgentCard())
            # Keep credentials on the configured API even if its card advertises
            # a different public address, such as one behind a reverse proxy.
            url = str(self._http.base_url.join(f"{path}/rpc"))
            self._transports[role_id] = JsonRpcTransport(self._http, card, url)
        return self._transports[role_id]

    @staticmethod
    def _message(
        text: str,
        context_id: str,
        message_id: str,
        *,
        immediate: bool = False,
        attachments: list[dict] | None = None,
    ) -> t.SendMessageRequest:
        return t.SendMessageRequest(
            message=t.Message(
                message_id=message_id or str(uuid4()),
                context_id=context_id,
                role=t.Role.ROLE_USER,
                parts=[t.Part(text=text)]
                + [ParseDict(part, t.Part()) for part in attachments or []],
            ),
            configuration=t.SendMessageConfiguration(return_immediately=immediate),
        )

    async def send(
        self,
        role_id: str,
        text: str,
        context_id: str = "",
        message_id: str = "",
        immediate: bool = True,
        attachments: list[dict] | None = None,
    ) -> dict:
        client = await self._transport(role_id)
        response = await client.send_message(
            self._message(
                text,
                context_id,
                message_id,
                immediate=immediate,
                attachments=attachments,
            )
        )
        return MessageToDict(response)

    async def get(self, role_id: str, task_id: str) -> dict:
        client = await self._transport(role_id)
        return MessageToDict(
            await client.get_task(t.GetTaskRequest(id=task_id, history_length=1))
        )

    async def list(
        self, role_id: str, context_id: str = "", page_token: str = ""
    ) -> dict:
        client = await self._transport(role_id)
        return MessageToDict(
            await client.list_tasks(
                t.ListTasksRequest(
                    context_id=context_id,
                    page_token=page_token,
                    page_size=20,
                    include_artifacts=True,
                )
            )
        )

    async def cancel(self, role_id: str, task_id: str) -> dict:
        client = await self._transport(role_id)
        return MessageToDict(await client.cancel_task(t.CancelTaskRequest(id=task_id)))

    async def stream(
        self,
        role_id: str,
        text: str,
        context_id: str = "",
        message_id: str = "",
        attachments: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        client = await self._transport(role_id)
        async for event in client.send_message_streaming(
            self._message(text, context_id, message_id, attachments=attachments)
        ):
            yield MessageToDict(event)

    async def subscribe(self, role_id: str, task_id: str) -> AsyncIterator[dict]:
        client = await self._transport(role_id)
        async for event in client.subscribe(t.SubscribeToTaskRequest(id=task_id)):
            yield MessageToDict(event)
