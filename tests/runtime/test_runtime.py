"""Local budget contract and real Goose with a synthetic model server."""

import asyncio
import json
import os
import shutil
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiohttp
import pytest
from aiohttp import web

from kirby.contracts import TaskRecord
from kirby.files.tools import TOOL_NAMES, FileTools
from kirby.roles.registry import build_manifest
from kirby.runtime import GooseRuntime
from kirby.runtime.files import write_reports
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
            {"content": '{"$schema":"synthetic","marker":"OPTIONAL_821"}'},
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
    correction_prompt = json.dumps(requests[2]["messages"][-1])
    assert "Validation error: additionalProperties" in correction_prompt
    assert "$schema" in correction_prompt
    assert "JSON data instance" in correction_prompt
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


def test_large_newline_free_file_has_bounded_reads_search_and_safe_csv(tmp_path):
    path = tmp_path / "large.log"
    with path.open("wb") as output:
        for _ in range(800):
            output.write(b"x" * 65536)
        output.write(b"\nE/Fixture: FILE_ERROR_842\n")
    file_id = str(uuid4())
    ref = {
        "id": file_id,
        "filename": "large.log",
        "media_type": "text/plain",
        "size": path.stat().st_size,
    }
    cancel = asyncio.Event()
    readers = FileTools({file_id: (ref, path)}, cancel)
    tracemalloc.start()
    excerpt = readers.read_file(file_id)
    matches = readers.search_file(file_id, "FILE_ERROR_842")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert excerpt["truncated"] and not excerpt["full_file_read"]
    assert len(json.dumps(excerpt).encode()) < 65536
    assert matches["full_file_scanned"] and matches["matches"][0]["line"] == 2
    assert peak < 1024 * 1024
    with pytest.raises(ValueError, match="unknown_attachment"):
        readers.read_file("../../secret")
    cancel.set()
    with pytest.raises(ValueError, match="execution_stopped"):
        readers.search_file(file_id, "FILE_ERROR_842")
    result = {"observations": [{"description": '=HYPERLINK("bad")'}]}
    reports = write_reports(result, tmp_path / "results")
    assert json.loads(reports[0][0].read_text()) == result
    assert "'=HYPERLINK" in reports[1][0].read_text(encoding="utf-8-sig")


@pytest.mark.goose
@pytest.mark.skipif(os.environ.get("KIRBY_RUN_GOOSE") != "1", reason="opt-in Goose")
async def test_native_attachment_mcp_export_and_fresh_process_resume(
    model_server,
    tmp_path,
    examples,
):
    requests, responses = model_server
    file_id = str(uuid4())
    attachment = tmp_path / "input.log"
    attachment.write_text("I/Fixture: started\nE/Fixture: FILE_MARKER_921\n")
    ref = {
        "id": file_id,
        "filename": "input.log",
        "media_type": "text/plain",
        "size": attachment.stat().st_size,
        "object_key": f"kirby/files/{file_id}",
        "etag": "synthetic",
    }
    downloads = []
    uploads = []

    class Objects:
        def download(self, file_ref, destination, cancel=None):
            downloads.append(file_ref["id"])
            shutil.copyfile(attachment, destination)

        def upload_result(self, task_id, name, media_type, path, cancel=None):
            uploads.append((task_id, name, path.read_bytes()))
            return {
                "id": str(uuid4()),
                "filename": name,
                "media_type": media_type,
                "size": path.stat().st_size,
                "object_key": "synthetic",
                "etag": "synthetic",
            }

    def tool(name, arguments, call_id):
        return {
            "tool_calls": [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": f"kirbyfiles__{name}",
                        "arguments": json.dumps(arguments),
                    },
                }
            ]
        }

    responses.extend(
        [
            tool(
                "search_file",
                {"file_id": file_id, "query": "FILE_MARKER_921"},
                "call_1",
            ),
            {"content": '{"marker":"FILE_MARKER_921"}'},
            tool("read_file", {"file_id": file_id, "start_line": 2}, "call_2"),
            {"content": '{"marker":"continued"}'},
        ]
    )
    key_file = tmp_path / "key"
    key_file.write_text("CANARY_MODEL_KEY")
    sessions = tmp_path / "sessions"
    runtime = GooseRuntime(
        Path(".tools/goose"), sessions, "unused", key_file, object_store=Objects()
    )
    task = replace(
        make_task(examples), input_text="Analyze the attached log.", attachments=[ref]
    )
    result = await runtime.run(task, asyncio.Event())
    assert result.output == {"marker": "FILE_MARKER_921"}
    assert set(result.usage["offered_tools"]) == {"load_skill"} | TOOL_NAMES
    assert result.usage["file_tool_calls"] == 1
    assert "FILE_MARKER_921" not in json.dumps(requests[0]["messages"])
    assert "FILE_MARKER_921" in json.dumps(requests[1]["messages"])
    assert {item["filename"] for item in result.output_files} == {
        "report.json",
        "report.csv",
    }
    continued = replace(
        task,
        id=str(uuid4()),
        input_text="Read the second line again.",
        session_id=result.session_id,
        session_path=result.session_path,
    )
    resumed = await runtime.run(continued, asyncio.Event())
    assert resumed.session_id == result.session_id
    assert resumed.usage["file_tool_calls"] == 1
    assert resumed.output == {"marker": "continued"}
    assert downloads == [file_id, file_id]
    assert len(uploads) == 4
    for path in sessions.rglob("*"):
        if path.is_file():
            assert b"CANARY_MODEL_KEY" not in path.read_bytes()
