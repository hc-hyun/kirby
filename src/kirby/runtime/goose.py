"""Pinned, isolated Goose ACP sessions with one bounded JSON correction."""

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path
from uuid import UUID

import acp
import jsonschema
import yaml
from acp import schema

from kirby.contracts import RunResult, TaskRecord

from .proxy import ModelProxy

# Verified against Goose 1.49.0 platform_extensions/mod.rs. Only skills is enabled.
PLATFORM_EXTENSIONS = (
    "analyze",
    "todo",
    "apps",
    "chatrecall",
    "extensionmanager",
    "scheduler",
    "summon",
    "summarize",
    "code_execution",
    "developer",
    "orchestrator",
    "tom",
    "skills",
)


class Client:
    def __init__(self, max_output_bytes):
        self.max_output_bytes = max_output_bytes
        self.output_bytes = 0
        self.chunks = []
        self.tool_calls = 0
        self.overflow = asyncio.Event()
        self.collecting = False

    async def session_update(self, session_id, update, **kwargs):
        if not self.collecting:
            return
        if isinstance(update, schema.AgentMessageChunk) and isinstance(
            update.content, schema.TextContentBlock
        ):
            self.output_bytes += len(update.content.text.encode())
            if self.output_bytes > self.max_output_bytes:
                self.overflow.set()
            elif not self.overflow.is_set():
                self.chunks.append(update.content.text)
        elif isinstance(update, schema.ToolCallStart):
            self.tool_calls += 1

    async def request_permission(self, **kwargs):
        return schema.RequestPermissionResponse(
            outcome=schema.DeniedOutcome(outcome="cancelled")
        )

    async def deny(self, **kwargs):
        raise acp.RequestError(-32601, "Client capability disabled")

    read_text_file = deny
    write_text_file = deny
    create_terminal = deny
    terminal_output = deny
    release_terminal = deny
    wait_for_terminal_exit = deny
    kill_terminal = deny
    create_elicitation = deny
    ext_method = deny

    async def ext_notification(self, **kwargs):
        return None


def parse_output(text, output_schema):
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    result = json.loads(text)
    jsonschema.validate(result, output_schema)
    if not isinstance(result, dict):
        raise ValueError("output_must_be_object")
    return result


def prepare_session(task, sessions_dir, model, host, token):
    context_id = str(UUID(task.context_id))
    base = sessions_dir.resolve() / context_id
    if task.session_path is not None and Path(task.session_path).resolve() != base:
        raise ValueError("session_path_mismatch")
    if task.session_id and not base.is_dir():
        raise ValueError("session_unavailable")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    for key, name in {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_DATA_HOME": "data",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "TMPDIR": "tmp",
    }.items():
        directory = base / name
        directory.mkdir(mode=0o700, exist_ok=True)
        env[key] = str(directory)
    work = base / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    # Stop native ancestor instruction discovery at this isolated work directory.
    (work / ".git").mkdir(mode=0o700, exist_ok=True)
    for relative, content in task.manifest["files"].items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("invalid_bundle_path")
        path = work / relative_path
        if not path.resolve().is_relative_to(work):
            raise ValueError("invalid_bundle_path")
        if task.session_id:
            if not path.is_file() or path.read_text() != content:
                raise ValueError("session_manifest_mismatch")
        else:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o400)
    config_dir = base / "config/goose"
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Only the verified read-only native reader bypasses interactive approval.
    (config_dir / "permission.yaml").write_text(
        yaml.safe_dump(
            {
                "user": {
                    "always_allow": ["load_skill"],
                    "ask_before": [],
                    "never_allow": [],
                }
            }
        )
    )
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "GOOSE_MODE": "approve",
                "extensions": {
                    name: {
                        "enabled": name == "skills",
                        "type": "platform",
                        "name": name,
                    }
                    for name in PLATFORM_EXTENSIONS
                },
            }
        )
    )
    env.update(
        {
            "GOOSE_PROVIDER": "openrouter",
            "GOOSE_MODEL": model,
            "OPENROUTER_HOST": host,
            "OPENROUTER_API_KEY": token,
            "GOOSE_DISABLE_KEYRING": "1",
            "GOOSE_TELEMETRY_OFF": "1",
            "GOOSE_DISABLE_SESSION_NAMING": "true",
            "GOOSE_MAX_TOKENS": str(
                task.manifest["profile"]["execution"]["max_output_tokens"]
            ),
        }
    )
    return base, work, env


async def terminate(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), 0.5)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


class GooseRuntime:
    def __init__(
        self,
        binary: Path,
        sessions_dir: Path,
        model: str,
        key_file: Path,
        max_model_requests: int = 20,
    ):
        self.binary = binary.resolve()
        self.sessions_dir = sessions_dir
        self.model = model
        self.key_file = key_file
        self.max_model_requests = max_model_requests

    async def run(self, task: TaskRecord, cancel: asyncio.Event) -> RunResult:
        if cancel.is_set():
            raise asyncio.CancelledError
        manifest = task.manifest
        if not manifest["model"].endswith(":free"):
            raise ValueError("only_free_models_approved")
        if manifest["goose_version"] != "1.49.0":
            raise ValueError("unsupported_goose_version")
        limits = manifest["profile"]["execution"]
        if len(task.input_text.encode()) > limits["max_input_bytes"]:
            raise ValueError("input_limit")
        if Path("/etc/goose/config.yaml").exists():
            raise RuntimeError("system_goose_config_not_isolated")
        key = self.key_file.read_text().strip()
        if not key or "\n" in key:
            raise ValueError("invalid_model_key_file")
        proxy = ModelProxy(
            key,
            manifest["model"],
            min(self.max_model_requests, limits["max_model_requests"]),
            limits["max_output_tokens"],
            cancel,
        )
        client = Client(limits["max_output_bytes"])
        async with (
            asyncio.timeout(limits["max_runtime_seconds"]),
            proxy.serve() as host,
        ):
            base, work, env = prepare_session(
                task, self.sessions_dir, manifest["model"], host, proxy.token
            )
            process = await asyncio.create_subprocess_exec(
                str(self.binary),
                "acp",
                cwd=work,
                env=env,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2 * 1048576,
            )
            conn = acp.connect_to_agent(client, process.stdin, process.stdout)
            try:
                initialized = await conn.initialize(
                    protocol_version=1, client_capabilities=schema.ClientCapabilities()
                )
                if initialized.protocol_version != 1:
                    raise RuntimeError("unsupported_acp_version")
                if (
                    initialized.agent_info is None
                    or initialized.agent_info.version != manifest["goose_version"]
                ):
                    raise RuntimeError("goose_binary_version_mismatch")
                if task.session_id:
                    if not initialized.agent_capabilities.load_session:
                        raise RuntimeError("session_load_unsupported")
                    await conn.load_session(
                        cwd=str(work), session_id=task.session_id, mcp_servers=[]
                    )
                    session_id = task.session_id
                else:
                    session = await conn.new_session(cwd=str(work), mcp_servers=[])
                    session_id = session.session_id
                # Loading a session can replay historical messages; discard them.
                client.chunks.clear()
                client.output_bytes = 0
                prompt = task.input_text
                for correction in range(2):
                    output = await self._prompt(
                        conn, session_id, prompt, client, proxy, cancel
                    )
                    try:
                        result = parse_output(output, manifest["output_schema"])
                        return RunResult(
                            result,
                            session_id,
                            str(base),
                            {
                                "model_requests": proxy.requests,
                                "tool_calls": client.tool_calls,
                                "offered_tools": sorted(proxy.tool_names),
                                "json_corrections": correction,
                            },
                        )
                    except (ValueError, jsonschema.ValidationError):
                        if correction:
                            raise ValueError("invalid_result_json") from None
                        prompt = (
                            "Your answer did not match the required JSON schema. "
                            "Return one JSON object without markdown or commentary. "
                            "Use the conversation; do not invent evidence. Schema:\n"
                            + json.dumps(manifest["output_schema"], ensure_ascii=False)
                        )
                raise AssertionError("unreachable")
            finally:
                await terminate(process)
                await conn.close()

    async def _prompt(self, conn, session_id, text, client, proxy, cancel):
        if cancel.is_set():
            raise asyncio.CancelledError
        client.collecting = True
        client.chunks.clear()
        prompt = asyncio.create_task(
            conn.prompt(
                session_id=session_id,
                prompt=[schema.TextContentBlock(type="text", text=text)],
            )
        )
        watchers = [
            asyncio.create_task(event.wait())
            for event in (
                cancel,
                client.overflow,
                proxy.limit_reached,
            )
        ]
        try:
            await asyncio.wait([prompt, *watchers], return_when=asyncio.FIRST_COMPLETED)
            if any(
                event.is_set()
                for event in (cancel, client.overflow, proxy.limit_reached)
            ):
                with contextlib.suppress(Exception):
                    await conn.cancel(session_id=session_id)
                    await asyncio.wait_for(asyncio.shield(prompt), 5)
                if cancel.is_set():
                    raise asyncio.CancelledError
                raise ValueError(
                    "output_limit"
                    if client.overflow.is_set()
                    else "model_budget_or_tool_policy"
                )
            response = await prompt
            if response.stop_reason != "end_turn":
                raise ValueError("incomplete_turn")
            return "".join(client.chunks)
        finally:
            client.collecting = False
            for item in [prompt, *watchers]:
                item.cancel()
            await asyncio.gather(prompt, *watchers, return_exceptions=True)
