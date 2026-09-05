"""Browser client contracts against the official A2A HTTP routes, without a model."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from kirby.api import create_app as create_api
from kirby.client.a2a import A2AClient
from kirby.client.app import create_app
from kirby.contracts import NotFound, Principal, TaskRecord
from kirby.roles.registry import load_registry

ROLE = "voc-analyst"
TOKEN = "synthetic-secret-that-must-stay-on-the-client-server"
BASE_URL = "http://127.0.0.1:8088"
WRITE_HEADERS = {"X-KIRBY-Client": "1", "Origin": BASE_URL}


class ControlledStore:
    """Keep model execution outside the HTTP adapter contract."""

    def __init__(self, state="completed"):
        self.state = state
        self.tasks = {}
        self.submitted = asyncio.Event()
        self.read = asyncio.Event()

    async def submit(
        self, principal, role_id, message_id, text, manifest, context_id=None
    ):
        now = datetime.now(UTC)
        task = TaskRecord(
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
        self.tasks[task.id] = task
        self.submitted.set()
        return task

    async def get(self, principal, role_id, task_id):
        task = self.tasks.get(task_id)
        if task is None or (task.tenant, task.subject, task.role_id) != (
            principal.tenant,
            principal.subject,
            role_id,
        ):
            raise NotFound
        self.read.set()
        return task

    async def list(self, principal, role_id, context_id=None, limit=50, offset=0):
        tasks = [
            task
            for task in self.tasks.values()
            if (task.tenant, task.subject, task.role_id)
            == (principal.tenant, principal.subject, role_id)
            and (context_id is None or task.context_id == context_id)
        ]
        return tasks[offset : offset + limit]

    async def cancel(self, principal, role_id, task_id):
        task = await self.get(principal, role_id, task_id)
        self.tasks[task.id] = replace(
            task, state="canceled", updated_at=datetime.now(UTC)
        )
        return self.tasks[task.id]

    async def complete_submitted(self):
        await self.submitted.wait()
        await asyncio.sleep(0.02)
        task = next(reversed(self.tasks.values()))
        self.tasks[task.id] = replace(
            task,
            state="completed",
            result={"synthetic": True},
            updated_at=datetime.now(UTC),
        )


@asynccontextmanager
async def browser(examples, store):
    api = create_api(
        store,
        load_registry(examples, "synthetic-model"),
        {TOKEN: Principal("test", "alice", frozenset({ROLE}))},
        # The adapter must use its configured URL even if the card advertises
        # another host; the bearer credential belongs only to the configured API.
        public_url="https://advertised.example/runtime",
        poll_interval=0.005,
    )

    async def confined_api(scope, receive, send):
        assert scope["server"][0] == "kirby.test"
        await api(scope, receive, send)

    async with A2AClient(
        "http://kirby.test", TOKEN, transport=httpx.ASGITransport(app=confined_api)
    ) as upstream:
        app = create_app(upstream, samples={ROLE: "Synthetic example"})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as http:
            yield http


async def test_initial_turn_continuation_and_durable_task_views(examples):
    store = ControlledStore()
    async with browser(examples, store) as http:
        catalog = (await http.get("/api/agents")).json()
        assert [agent["id"] for agent in catalog["agents"]] == [ROLE]
        card = await http.get(f"/api/agents/{ROLE}/card")
        assert card.status_code == 200
        assert card.json()["capabilities"]["streaming"] is True

        response = await http.post(
            f"/api/agents/{ROLE}/send",
            headers=WRITE_HEADERS,
            json={"text": "Synthetic first turn", "message_id": "first-message"},
        )
        assert response.status_code == 200, response.text
        first = response.json()["task"]
        assert first["status"]["state"] == "TASK_STATE_COMPLETED"
        assert json.loads(first["artifacts"][0]["parts"][0]["text"]) == {
            "synthetic": True
        }
        response = await http.post(
            f"/api/agents/{ROLE}/send",
            headers=WRITE_HEADERS,
            json={"text": "Synthetic follow-up", "context_id": first["contextId"]},
        )
        assert response.status_code == 200, response.text
        second = response.json()["task"]
        assert second["contextId"] == first["contextId"]
        assert second["id"] != first["id"]
        assert store.tasks[first["id"]].message_id == "first-message"
        assert store.tasks[second["id"]].message_id

        restored = await http.get(f"/api/agents/{ROLE}/tasks/{first['id']}")
        assert restored.status_code == 200
        assert restored.json()["artifacts"] == first["artifacts"]
        page = await http.get(
            f"/api/agents/{ROLE}/tasks", params={"context_id": first["contextId"]}
        )
        assert page.status_code == 200
        assert {task["id"] for task in page.json()["tasks"]} == {
            first["id"],
            second["id"],
        }


async def test_stream_completion_and_cancel_during_subscription(examples):
    store = ControlledStore("queued")
    async with browser(examples, store) as http:
        worker = asyncio.create_task(store.complete_submitted())
        response = await http.post(
            f"/api/agents/{ROLE}/stream",
            headers=WRITE_HEADERS,
            json={"text": "Synthetic streamed task"},
        )
        await worker
        assert response.status_code == 200, response.text
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
        assert any("artifactUpdate" in event for event in events)
        assert events[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"

        response = await http.post(
            f"/api/agents/{ROLE}/send",
            headers=WRITE_HEADERS,
            json={"text": "Synthetic cancellable task", "immediate": True},
        )
        task = response.json()["task"]
        store.read.clear()
        subscription = asyncio.create_task(
            http.get(f"/api/agents/{ROLE}/tasks/{task['id']}/subscribe")
        )
        await asyncio.wait_for(store.read.wait(), timeout=2)
        canceled = await http.post(
            f"/api/agents/{ROLE}/tasks/{task['id']}/cancel", headers=WRITE_HEADERS
        )
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["status"]["state"] == "TASK_STATE_CANCELED"
        response = await asyncio.wait_for(subscription, timeout=2)
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[0]["task"]["id"] == task["id"]
        assert events[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_CANCELED"


async def test_local_client_keeps_credentials_private_and_rejects_foreign_writes(
    examples,
):
    store = ControlledStore()
    async with browser(examples, store) as http:
        for path in ["/", "/static/app.js", "/static/style.css", "/api/config"]:
            response = await http.get(path)
            assert response.status_code == 200
            assert TOKEN not in response.text
        config = (await http.get("/api/config")).json()
        assert config["url"] == "http://kirby.test"
        assert config["samples"][ROLE] == "Synthetic example"

        for headers in [
            {},
            {"X-KIRBY-Client": "1", "Origin": "https://foreign.example"},
        ]:
            response = await http.post(
                f"/api/agents/{ROLE}/send",
                headers=headers,
                json={"text": "Must not be admitted"},
            )
            assert response.status_code == 403
        assert not store.tasks
        response = await http.get("/api/config", headers={"Host": "foreign.example"})
        assert response.status_code == 400


async def test_upstream_errors_are_sanitized_before_and_after_stream_start():
    async def upstream(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["a2a-version"] == "1.0"
        if request.url.path.endswith("agent-card.json"):
            return httpx.Response(
                200,
                json={"name": "Synthetic agent", "capabilities": {"streaming": True}},
            )
        if request.method == "GET":
            return httpx.Response(401, text=TOKEN)
        payload = json.loads(request.content)
        if payload["method"] == "SendMessage":
            return httpx.Response(401, text=TOKEN)
        error = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "error": {"code": -32602, "message": TOKEN},
        }
        chunks = []
        if payload["params"]["message"]["parts"][0]["text"] == "Late failure":
            chunks.append(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "task": {
                            "id": "synthetic-task",
                            "contextId": "synthetic-context",
                            "status": {"state": "TASK_STATE_SUBMITTED"},
                        }
                    },
                }
            )
        chunks.append(error)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks),
        )

    async with A2AClient(
        "http://kirby.test", TOKEN, transport=httpx.MockTransport(upstream)
    ) as client:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(client)), base_url=BASE_URL
        ) as http:
            denied = await http.get("/api/agents")
            assert denied.status_code == 502
            assert TOKEN not in denied.text and denied.json()["error"]
            for action in ["send", "stream"]:
                response = await http.post(
                    f"/api/agents/{ROLE}/{action}",
                    headers=WRITE_HEADERS,
                    json={"text": "Upfront failure"},
                )
                assert response.status_code == 502, response.text
                assert TOKEN not in response.text and response.json()["error"]
            response = await http.post(
                f"/api/agents/{ROLE}/stream",
                headers=WRITE_HEADERS,
                json={"text": "Late failure"},
            )
            assert response.status_code == 200
            assert TOKEN not in response.text
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[0]["task"]["id"] == "synthetic-task"
            assert events[-1]["error"]


async def test_file_metadata_bridge_and_official_a2a_url_parts():
    file_id = str(uuid4())
    part = {
        "url": f"http://kirby.test/agents/{ROLE}/files/{file_id}/content",
        "filename": "synthetic.log",
        "mediaType": "text/plain",
    }
    download = {
        "url": "http://minio.test/dev/report.json?signature=synthetic",
        "filename": "report.json",
        "mediaType": "application/json",
        "metadata": {"size": 120},
    }
    calls = []

    async def upstream(request):
        assert request.url.host == "kirby.test"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        # The 50 MiB file is represented only by metadata and a URL.
        assert len(request.content) < 2048
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("agent-card.json"):
            return httpx.Response(
                200,
                json={"name": "Synthetic agent", "capabilities": {"streaming": True}},
            )
        if request.url.path.endswith("/files"):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "enabled": True,
                        "max_file_bytes": 64 * 1024 * 1024,
                        "max_attachments": 4,
                        "allowed_extensions": [".txt", ".log"],
                    },
                )
            assert json.loads(request.content) == {
                "filename": "synthetic.log",
                "media_type": "text/plain",
                "size": 50 * 1024 * 1024,
            }
            return httpx.Response(
                200,
                json={
                    "id": file_id,
                    "upload": {
                        "url": "http://minio.test/dev",
                        "fields": {"key": "synthetic.log", "policy": "synthetic"},
                        "method": "POST",
                    },
                },
            )
        if request.url.path.endswith("/complete"):
            assert not request.content
            return httpx.Response(200, json={"id": file_id, "part": part})
        payload = json.loads(request.content)
        assert payload["params"]["message"]["parts"] == [
            {"text": "Analyze the attached synthetic log"},
            part,
        ]
        result = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "task": {
                    "id": "synthetic-task",
                    "contextId": "synthetic-context",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"artifactId": "report", "parts": [download]}],
                }
            },
        }
        if payload["method"] == "SendStreamingMessage":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=f"data: {json.dumps(result)}\n\n",
            )
        assert payload["method"] == "SendMessage"
        return httpx.Response(200, json=result)

    async with A2AClient(
        "http://kirby.test", TOKEN, transport=httpx.MockTransport(upstream)
    ) as client:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(client)), base_url=BASE_URL
        ) as http:
            path = f"/api/agents/{ROLE}/files"
            options = await http.get(path)
            assert options.json()["enabled"] is True
            reserved = await http.post(
                path,
                headers=WRITE_HEADERS,
                json={
                    "filename": "synthetic.log",
                    "media_type": "text/plain",
                    "size": 50 * 1024 * 1024,
                },
            )
            assert reserved.status_code == 200, reserved.text
            assert TOKEN not in reserved.text
            assert reserved.json()["upload"]["url"] == "http://minio.test/dev"
            completed = await http.post(
                f"{path}/{file_id}/complete", headers=WRITE_HEADERS
            )
            assert completed.status_code == 200, completed.text
            assert completed.json()["part"] == part
            for action in ["send", "stream"]:
                response = await http.post(
                    f"/api/agents/{ROLE}/{action}",
                    headers=WRITE_HEADERS,
                    json={
                        "text": "Analyze the attached synthetic log",
                        "attachments": [part],
                    },
                )
                assert response.status_code == 200, response.text
                task = json.loads(response.text)["task"]
                assert task["artifacts"][0]["parts"][0] == download
                assert TOKEN not in response.text

            previous_calls = len(calls)
            for attachments in [[{"raw": "base64-not-allowed"}], [part] * 5]:
                response = await http.post(
                    f"/api/agents/{ROLE}/send",
                    headers=WRITE_HEADERS,
                    json={"text": "Invalid attachment", "attachments": attachments},
                )
                assert response.status_code == 400
            denied = await http.post(path, json={"filename": "untrusted.log"})
            assert denied.status_code == 403
            oversized = await http.post(
                path, headers=WRITE_HEADERS, content=b"x" * 4097
            )
            assert oversized.status_code == 400
            assert len(calls) == previous_calls
