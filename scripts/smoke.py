"""Send synthetic role inputs through an already running API and Worker."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from a2a import types as t
from a2a.client.transports.jsonrpc import JsonRpcTransport
from google.protobuf.json_format import MessageToDict, ParseDict

from kirby.roles import load_samples


def artifact_snapshot(task):
    artifacts = [MessageToDict(artifact) for artifact in task.artifacts]
    for artifact in artifacts:
        for part in artifact.get("parts", []):
            if "url" in part:
                # Task Get refreshes expiring MinIO signatures.
                part["url"] = urlsplit(part["url"])._replace(query="").geturl()
    return artifacts


async def main(url: str, credentials_file: Path, output: Path, agents_dir: Path):
    token = json.loads(credentials_file.read_text())[0]["token"]
    report = {"transport": "HTTP JSON-RPC", "checks": []}
    async with httpx.AsyncClient(
        base_url=url,
        timeout=330,
        headers={
            "Authorization": f"Bearer {token}",
            "A2A-Version": "1.0",
        },
    ) as http:
        response = await http.get("/agents")
        response.raise_for_status()
        allowed = {agent["id"] for agent in response.json()["agents"]}
        samples = load_samples(agents_dir)
        for role_id, input_text in samples.items():
            if role_id not in allowed:
                continue
            response = await http.get(f"/agents/{role_id}/.well-known/agent-card.json")
            response.raise_for_status()
            card = ParseDict(response.json(), t.AgentCard())
            client = JsonRpcTransport(http, card, card.supported_interfaces[0].url)
            first = await client.send_message(
                t.SendMessageRequest(
                    message=t.Message(
                        message_id=str(uuid4()),
                        role=t.Role.ROLE_USER,
                        parts=[t.Part(text=input_text)],
                    )
                )
            )
            result = first.task
            report["checks"].append(
                {
                    "role": role_id,
                    "task": result.id,
                    "state": t.TaskState.Name(result.status.state),
                }
            )
            print(role_id, t.TaskState.Name(result.status.state), flush=True)
            if result.status.state == t.TaskState.TASK_STATE_COMPLETED:
                fetched = await client.get_task(t.GetTaskRequest(id=result.id))
                assert artifact_snapshot(fetched) == artifact_snapshot(result)
                followup = await client.send_message(
                    t.SendMessageRequest(
                        message=t.Message(
                            message_id=str(uuid4()),
                            context_id=result.context_id,
                            role=t.Role.ROLE_USER,
                            parts=[
                                t.Part(
                                    text=(
                                        "Re-evaluate the previous input. "
                                        "Keep the same evidence IDs and output schema."
                                    )
                                )
                            ],
                        )
                    )
                )
                report["checks"].append(
                    {
                        "role": role_id,
                        "continuation": True,
                        "task": followup.task.id,
                        "state": t.TaskState.Name(followup.task.status.state),
                    }
                )
                print(
                    role_id,
                    "continuation",
                    t.TaskState.Name(followup.task.status.state),
                    flush=True,
                )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return bool(report["checks"]) and all(
        c["state"] == "TASK_STATE_COMPLETED" for c in report["checks"]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/service-smoke.json")
    )
    parser.add_argument("--agents-dir", type=Path, default=Path("agents"))
    args = parser.parse_args()
    raise SystemExit(
        0
        if asyncio.run(
            main(args.url, args.credentials_file, args.output, args.agents_dir)
        )
        else 1
    )
