"""Official A2A client contracts against independently controlled task storage."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from a2a import types as t
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.utils.errors import (
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf.json_format import ParseDict

from kirby.api import create_app
from kirby.contracts import NotFound, Principal, TaskRecord
from kirby.roles.registry import load_registry

ROLE = "voc-analyst"
ALICE = Principal("test-tenant", "alice", frozenset({ROLE, "log-analyst"}))
CREDENTIALS = {
    "alice-token": ALICE,
    "bob-token": Principal("test-tenant", "bob", ALICE.roles),
    "tenant-token": Principal("other-tenant", "alice", ALICE.roles),
    "denied-token": Principal("test-tenant", "alice", frozenset()),
}


class ControlledStore:
    """Only HTTP contracts use this fake; no runtime executes inside the API."""

    def __init__(self, state="completed"):
        self.tasks = {}
        self.state = state
        self.submitted = asyncio.Event()
        self.canceled = []
        self.available = True

    async def health(self):
        return self.available

    async def submit(
        self, principal, role_id, message_id, text, manifest, context_id=None
    ):
        now = datetime.now(UTC)
        record = TaskRecord(
            str(uuid4()),
            context_id or str(uuid4()),
            role_id,
            principal.tenant,
            principal.subject,
            message_id,
            text,
            self.state,
            manifest,
            now,
            now,
            result={"synthetic": True} if self.state == "completed" else None,
        )
        self.tasks[record.id] = record
        self.submitted.set()
        return record

    async def get(self, principal, role_id, task_id):
        record = self.tasks.get(task_id)
        if record is None or (record.tenant, record.subject, record.role_id) != (
            principal.tenant,
            principal.subject,
            role_id,
        ):
            raise NotFound
        return record

    async def list(self, principal, role_id, context_id=None, limit=50, offset=0):
        records = [
            r
            for r in self.tasks.values()
            if (r.tenant, r.subject, r.role_id)
            == (principal.tenant, principal.subject, role_id)
            and (context_id is None or r.context_id == context_id)
        ]
        return records[offset : offset + limit]

    async def cancel(self, principal, role_id, task_id):
        record = await self.get(principal, role_id, task_id)
        self.canceled.append(task_id)
        self.tasks[task_id] = replace(record, state="canceled", cancel_requested=True)
        return self.tasks[task_id]

    async def finish(self):
        await self.submitted.wait()
        await asyncio.sleep(0.02)
        task_id = next(reversed(self.tasks))
        self.tasks[task_id] = replace(
            self.tasks[task_id], state="running", updated_at=datetime.now(UTC)
        )
        await asyncio.sleep(0.02)
        self.tasks[task_id] = replace(
            self.tasks[task_id],
            state="completed",
            result={"synthetic": True},
            updated_at=datetime.now(UTC),
        )


def request(*, immediate=False, context_id="", tenant="", text="Synthetic input"):
    return t.SendMessageRequest(
        message=t.Message(
            message_id=str(uuid4()),
            role=t.Role.ROLE_USER,
            parts=[t.Part(text=text)],
            context_id=context_id,
        ),
        tenant=tenant,
        configuration=t.SendMessageConfiguration(return_immediately=immediate),
    )


@asynccontextmanager
async def connection(examples, store, role=ROLE):
    app = create_app(
        store,
        load_registry(examples, "synthetic-model"),
        CREDENTIALS,
        public_url="https://kirby.example/runtime",
        poll_interval=0.005,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer alice-token", "A2A-Version": "1.0"},
    ) as http:
        response = await http.get(f"/agents/{role}/.well-known/agent-card.json")
        assert response.status_code == 200
        card = ParseDict(response.json(), t.AgentCard())
        yield JsonRpcTransport(http, card, f"http://test/agents/{role}/rpc"), http, card


async def test_roles_identity_catalog_and_request_limits(examples):
    store = ControlledStore()
    async with connection(examples, store) as (client, http, card):
        assert card.supported_interfaces[0].url == (
            "https://kirby.example/runtime/agents/voc-analyst/rpc"
        )
        assert (
            card.security_schemes["bearer"].http_auth_security_scheme.scheme == "bearer"
        )
        assert len((await http.get("/agents")).json()["agents"]) == 2
        assert (await http.get("/readyz")).status_code == 200
        store.available = False
        assert (await http.get("/readyz")).status_code == 503
        with pytest.raises(InvalidParamsError):
            await client.send_message(request(text="x" * 262145))
        oversized = await http.post(f"/agents/{ROLE}/rpc", content=b"x" * 1_700_000)
        assert oversized.status_code == 413
        with pytest.raises(TaskNotFoundError):
            await client.send_message(request(tenant="other-tenant"))
        http.headers["Authorization"] = "Bearer denied-token"
        assert (await http.get("/agents")).json() == {"agents": []}
        assert (
            await http.get(f"/agents/{ROLE}/.well-known/agent-card.json")
        ).status_code == 403
        http.headers["Authorization"] = "alice-token"
        assert (await http.get("/agents")).status_code == 401
        assert (await http.get("/healthz")).status_code == 200


async def test_blocking_continuation_pagination_and_cross_owner_queries(examples):
    store = ControlledStore()
    async with connection(examples, store) as (client, http, card):
        first = (await client.send_message(request())).task
        assert first.status.state == t.TaskState.TASK_STATE_COMPLETED
        assert first.artifacts[0].parts[0].media_type == "application/json"
        second = (await client.send_message(request(context_id=first.context_id))).task
        assert first.context_id == second.context_id
        page = await client.list_tasks(t.ListTasksRequest(page_size=1))
        assert len(page.tasks) == 1 and not page.tasks[0].artifacts
        next_page = await client.list_tasks(
            t.ListTasksRequest(
                page_size=1,
                page_token=page.next_page_token,
                include_artifacts=True,
            )
        )
        assert next_page.tasks[0].id == second.id and next_page.tasks[0].artifacts
        for token in ["bob-token", "tenant-token"]:
            http.headers["Authorization"] = f"Bearer {token}"
            with pytest.raises(TaskNotFoundError):
                await client.get_task(t.GetTaskRequest(id=first.id))
            with pytest.raises(TaskNotFoundError):
                await client.cancel_task(t.CancelTaskRequest(id=first.id))
            with pytest.raises(TaskNotFoundError):
                _ = [
                    event
                    async for event in client.subscribe(
                        t.SubscribeToTaskRequest(id=first.id)
                    )
                ]
            assert not (await client.list_tasks(t.ListTasksRequest())).tasks
    async with connection(examples, store, "log-analyst") as (client, http, card):
        with pytest.raises(TaskNotFoundError):
            await client.get_task(t.GetTaskRequest(id=first.id))
        assert (await client.send_message(request())).task.id
    # A new app instance reads durable state without recreating an execution.
    async with connection(examples, store) as (client, http, card):
        restored = await client.get_task(
            t.GetTaskRequest(id=first.id, history_length=1)
        )
        assert restored.history[0].parts[0].text == "Synthetic input"
        with pytest.raises(TaskNotCancelableError):
            await client.cancel_task(t.CancelTaskRequest(id=first.id))


async def test_stream_and_resubscription_from_durable_snapshot(examples):
    store = ControlledStore("queued")
    async with connection(examples, store) as (client, http, card):
        worker = asyncio.create_task(store.finish())
        events = [event async for event in client.send_message_streaming(request())]
        await worker
        assert events[0].task.status.state == t.TaskState.TASK_STATE_SUBMITTED
        assert any(event.HasField("artifact_update") for event in events)
        assert events[-1].status_update.status.state == t.TaskState.TASK_STATE_COMPLETED
        store.submitted.clear()
        task = (await client.send_message(request(immediate=True))).task
        assert task.status.state == t.TaskState.TASK_STATE_SUBMITTED
        worker = asyncio.create_task(store.finish())
        events = [
            event
            async for event in client.subscribe(t.SubscribeToTaskRequest(id=task.id))
        ]
        await worker
        assert events[0].task.id == task.id
        assert events[-1].status_update.status.state == t.TaskState.TASK_STATE_COMPLETED
        with pytest.raises(UnsupportedOperationError):
            _ = [
                event
                async for event in client.subscribe(
                    t.SubscribeToTaskRequest(id=task.id)
                )
            ]


async def test_nonblocking_cancel_and_blocking_disconnect_do_not_execute(examples):
    store = ControlledStore("queued")
    async with connection(examples, store) as (client, http, card):
        call = asyncio.create_task(client.send_message(request()))
        await store.submitted.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        task_id = next(iter(store.tasks))
        assert store.tasks[task_id].state == "queued" and not store.canceled
        canceled = await client.cancel_task(t.CancelTaskRequest(id=task_id))
        assert canceled.status.state == t.TaskState.TASK_STATE_CANCELED
        assert store.canceled == [task_id]


class FileControlledStore(ControlledStore):
    def __init__(self):
        super().__init__()
        self.files = {}

    async def create_file(
        self, principal, role_id, file_id, filename, media_type, size
    ):
        self.files[file_id] = {
            "id": file_id,
            "filename": filename,
            "media_type": media_type,
            "size": size,
            "status": "pending",
            "tenant": principal.tenant,
            "subject": principal.subject,
            "role_id": role_id,
        }
        return self.files[file_id]

    async def get_file(self, principal, role_id, file_id):
        record = self.files.get(file_id)
        if record is None or (
            record["tenant"],
            record["subject"],
            record["role_id"],
        ) != (principal.tenant, principal.subject, role_id):
            raise NotFound
        return record

    async def complete_file(self, principal, role_id, file_id, object_key, etag):
        record = await self.get_file(principal, role_id, file_id)
        record.update(status="ready", object_key=object_key, etag=etag)
        return record

    async def submit(self, *args, attachments=None, **kwargs):
        task = await super().submit(*args, **kwargs)
        task.attachments = attachments or []
        task.output_files = [
            {
                "filename": "report.csv",
                "media_type": "text/csv",
                "size": 42,
                "object_key": f"kirby/files/{uuid4()}",
                "etag": "synthetic",
            }
        ]
        return task


class FileObjects:
    max_file_bytes = 64 * 1024 * 1024

    def __init__(self):
        self.signed = 0

    def create_upload(self, file_id, filename, media_type, size):
        return {
            "url": "https://objects.example.invalid/dev",
            "fields": {"key": file_id},
        }

    def complete_upload(self, file_id, expected_size):
        return {"object_key": f"kirby/files/{file_id}", "etag": "synthetic"}

    def discard_upload(self, file_id):
        pass

    def download_url(self, ref):
        self.signed += 1
        return (
            f"https://objects.example.invalid/{ref['object_key']}?grant={self.signed}"
        )


async def test_owned_file_references_and_fresh_output_download_grants(examples):
    store, objects = FileControlledStore(), FileObjects()
    app = create_app(
        store,
        load_registry(examples, "synthetic-model"),
        CREDENTIALS,
        public_url="http://test",
        object_store=objects,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer alice-token", "A2A-Version": "1.0"},
    ) as http:
        prefix = f"/agents/{ROLE}/files"
        options = (await http.get(prefix)).json()
        assert options["enabled"] and options["max_file_bytes"] == 64 * 1024 * 1024
        uploaded = await http.post(
            prefix, json={"filename": "input.log", "size": 50 * 1024 * 1024}
        )
        assert uploaded.status_code == 200
        assert len(uploaded.content) < 1024  # Only metadata, never the 50 MiB body.
        file_id = uploaded.json()["id"]
        part = t.Part(
            url=f"http://test{prefix}/{file_id}/content", filename="input.log"
        )
        card = ParseDict(
            (await http.get(f"/agents/{ROLE}/.well-known/agent-card.json")).json(),
            t.AgentCard(),
        )
        client = JsonRpcTransport(http, card, f"http://test/agents/{ROLE}/rpc")
        message = request(immediate=True)
        message.message.parts.append(part)
        with pytest.raises(InvalidParamsError):
            await client.send_message(message)
        ready = await http.post(f"{prefix}/{file_id}/complete")
        assert ready.status_code == 200
        assert ready.json()["part"]["mediaType"] == "text/plain"
        task = (await client.send_message(message)).task
        assert len(task.artifacts) == 2
        assert task.artifacts[1].parts[0].filename == "report.csv"
        fetched = await client.get_task(t.GetTaskRequest(id=task.id))
        assert fetched.artifacts[1].parts[0].url != task.artifacts[1].parts[0].url
        assert store.tasks[task.id].attachments[0]["id"] == file_id
        assert (await http.get(f"{prefix}/{file_id}/content")).status_code == 307
        http.headers["Authorization"] = "Bearer bob-token"
        assert (await http.post(f"{prefix}/{file_id}/complete")).status_code == 404
        assert (await http.get(f"{prefix}/{file_id}/content")).status_code == 404
        with pytest.raises(TaskNotFoundError):
            await client.send_message(message)
        http.headers["Authorization"] = "Bearer alice-token"
        message.message.parts[-1].url = "http://169.254.169.254/metadata"
        with pytest.raises(InvalidParamsError):
            await client.send_message(message)
        message.message.parts[-1].raw = b"inline file bytes"
        with pytest.raises(InvalidParamsError):
            await client.send_message(message)
        for metadata in [
            {"filename": "large.log", "size": 64 * 1024 * 1024 + 1},
            {"filename": "archive.zip", "size": 42},
            {"filename": "../private.log", "size": 42},
        ]:
            assert (await http.post(prefix, json=metadata)).status_code == 400
