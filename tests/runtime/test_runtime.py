"""Local budget contract and real Goose with a synthetic model server."""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web

from kirby.contracts import TaskRecord
from kirby.roles.registry import build_manifest
from kirby.runtime import GooseRuntime
from kirby.runtime.goose import parse_output
from kirby.runtime.proxy import ModelProxy


def make_task(examples):
    manifest = build_manifest(examples, "log-analyst", "cohere/north-mini-code:free")
    manifest["output_schema"] = {
        "type": "object",
        "properties": {"marker": {"type": "string"}},
        "required": ["marker"],
        "additionalProperties": False,
    }
    manifest["profile"]["execution"]["max_runtime_seconds"] = 30
    manifest["files"][".goosehints"] = "Required instruction marker: REQUIRED_712."
    manifest["files"][".agents/skills/log-triage/SKILL.md"] = (
        "---\nname: log-triage\ndescription: Synthetic optional marker\n---\n"
        "Optional body marker: OPTIONAL_821."
    )
    now = datetime.now(UTC)
    return TaskRecord(
        str(uuid4()),
        str(uuid4()),
        "log-analyst",
        "tenant",
        "user",
        str(uuid4()),
        "Read optional skill log-triage.",
        "running",
        manifest,
        now,
        now,
    )


@pytest.fixture
async def model_server(monkeypatch):
    requests = []
    responses = []

    async def complete(request):
        body = await request.json()
        requests.append(body)
        assert request.headers["Authorization"] == "Bearer CANARY_MODEL_KEY"
        chunk = responses.pop(0) if responses else {"content": '{"marker":"ok"}'}
        response = {
            "id": "synthetic",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "delta": chunk, "finish_reason": None}],
        }
        final = {
            **response,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls" if "tool_calls" in chunk else "stop",
                }
            ],
        }
        return web.Response(
            text="data: "
            + json.dumps(response)
            + "\n\n"
            + "data: "
            + json.dumps(final)
            + "\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/api/v1/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/api/v1/chat/completions"
    monkeypatch.setattr("kirby.runtime.proxy.OPENROUTER_URL", url)
    try:
        yield requests, responses
    finally:
        await runner.cleanup()


async def test_proxy_forces_model_budget_credentials_and_tool_policy(model_server):
    requests, _ = model_server
    cancel = asyncio.Event()
    proxy = ModelProxy("CANARY_MODEL_KEY", "pinned:free", 1, 128, cancel)
    async with proxy.serve() as host, aiohttp.ClientSession() as client:
        url = host + "/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {proxy.token}"}
        body = {
            "model": "expensive",
            "max_tokens": 9999,
            "messages": [],
            "models": ["expensive"],
            "provider": {"allow_fallbacks": True},
        }
        async with client.post(url, headers=headers, json=body) as response:
            assert response.status == 200
            await response.read()
        async with client.post(url, headers=headers, json=body) as response:
            assert response.status == 429
        cancel.set()
        async with client.post(url, headers=headers, json=body) as response:
            assert response.status == 409
    assert len(requests) == 1
    assert requests[0]["model"] == "pinned:free"
    assert requests[0]["max_tokens"] == 128
    assert "models" not in requests[0]
    assert requests[0]["provider"] == {
        "allow_fallbacks": False,
        "max_price": {"prompt": 0, "completion": 0},
    }
    denied = ModelProxy("CANARY_MODEL_KEY", "pinned:free", 1, 128, asyncio.Event())
    async with denied.serve() as host, aiohttp.ClientSession() as client:
        async with client.post(
            host + "/api/v1/chat/completions",
            json={**body, "tools": [{"function": {"name": "shell"}}]},
            headers={"Authorization": f"Bearer {denied.token}"},
        ) as response:
            assert response.status == 403
    assert len(requests) == 1


def test_json_fence_and_schema():
    assert parse_output('```json\n{"ok":true}\n```', {"type": "object"}) == {"ok": True}
    with pytest.raises(ValueError):
        parse_output('Commentary {"ok":true}', {"type": "object"})


@pytest.mark.goose
@pytest.mark.skipif(os.environ.get("KIRBY_RUN_GOOSE") != "1", reason="opt-in Goose")
async def test_native_skills_correction_resume_and_secret_boundary(
    model_server,
    tmp_path,
    examples,
):
    requests, responses = model_server
    responses.extend(
        [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "load_skill",
                            "arguments": '{"name":"log-triage"}',
                        },
                    }
                ]
            },
            {"content": "invalid JSON"},
            {"content": '{"marker":"OPTIONAL_821"}'},
            {"content": '{"marker":"continued"}'},
        ]
    )
    key_file = tmp_path / "key"
    key_file.write_text("CANARY_MODEL_KEY")
    sessions = tmp_path / "sessions"
    runtime = GooseRuntime(Path(".tools/goose"), sessions, "unused", key_file)
    task = make_task(examples)
    result = await runtime.run(task, asyncio.Event())
    assert result.output == {"marker": "OPTIONAL_821"}
    assert result.usage["offered_tools"] == ["load_skill"]
    assert result.usage["json_corrections"] == 1
    assert "REQUIRED_712" in json.dumps(requests[0])
    assert "OPTIONAL_821" not in json.dumps(requests[0])
    assert "OPTIONAL_821" in json.dumps(requests[1]), requests[1]["messages"][-2:]
    assert result.usage["tool_calls"] == 1
    continued = replace(
        task,
        id=str(uuid4()),
        input_text="Continue the prior session.",
        session_id=result.session_id,
        session_path=result.session_path,
    )
    resumed = await runtime.run(continued, asyncio.Event())
    assert resumed.session_id == result.session_id
    assert resumed.output == {"marker": "continued"}
    assert "OPTIONAL_821" in json.dumps(requests[-1])
    for path in sessions.rglob("*"):
        if path.is_file():
            assert b"CANARY_MODEL_KEY" not in path.read_bytes()
