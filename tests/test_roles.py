import json
import shutil

import httpx
import pytest
import yaml
from jsonschema import ValidationError

from kirby.api import create_app
from kirby.contracts import Principal
from kirby.roles import load_role, load_samples
from kirby.roles.registry import load_registry


@pytest.mark.parametrize("role", ["voc-analyst", "log-analyst"])
def test_examples_and_pinned_snapshot(examples, tmp_path, role):
    root = tmp_path / "examples"
    shutil.copytree(examples, root)
    first = load_role(root, role)
    result = (root / f"{role}/fixtures/result.synthetic.json").read_text()
    first.validate_output(result)
    skill = root / f"{role}/skills/{role}-role/SKILL.md"
    skill.write_text(skill.read_text() + "\nNew release text.\n")
    second = load_role(root, role)
    assert first.digest != second.digest
    profile = first.profile
    profile["version"] = "9.0.0"
    assert first.profile["version"] == "1.1.0"
    with pytest.raises(ValidationError):
        first.validate_output(json.dumps({"invalid": True}))


@pytest.mark.parametrize(
    "attack",
    [
        "duplicate",
        "mcp",
        "path",
        "symlink",
        "size",
        "shared_version",
        "fixture_escape",
        "fixture_in_skill",
    ],
)
def test_reject_role_attacks(examples, tmp_path, attack):
    root = tmp_path / "examples"
    shutil.copytree(examples, root)
    path = root / "voc-analyst/agent.yaml"
    profile = yaml.safe_load(path.read_text())
    if attack == "duplicate":
        profile["skills"]["available"].append(profile["skills"]["required"][0])
    elif attack == "mcp":
        profile["tools"]["mcp_server_refs"] = ["https://unapproved.invalid"]
    elif attack == "size":
        profile["execution"]["max_required_skill_bytes"] = 1
    elif attack == "symlink":
        target = root / "voc-analyst/skills/voc-analyst-role/SKILL.md"
        target.unlink()
        target.symlink_to(examples / "voc-analyst/skills/voc-analyst-role/SKILL.md")
    elif attack == "shared_version":
        profile["skills"]["available"][-1]["version"] = "9.0.0"
        profile["assets"]["skills"]["file-evidence"]["version"] = "9.0.0"
    elif attack == "fixture_escape":
        profile["assets"]["fixtures"]["input"] = (
            "../log-analyst/fixtures/input.synthetic.json"
        )
    elif attack == "fixture_in_skill":
        profile["assets"]["fixtures"]["input"] = "skills/voc-analyst-role/SKILL.md"
    else:
        profile["assets"]["skills"]["voc-analyst-role"]["path"] = (
            "../log-analyst/skills/log-analyst-role"
        )
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(ValueError):
        load_role(root, "voc-analyst")


def test_migration_keeps_execution_digests(examples):
    # Captured with the previous production loader, before relocating the sources.
    expected = {
        "voc-analyst": (
            "da0f8e5013349e8bee7c77961ea88149b7f404e36f4bb4590a31fa31556e289e"
        ),
        "log-analyst": (
            "233a49af6cc1379c8b8d8521d6cdce8127a4dde6e0774a66f4e5a45dfc32d2b0"
        ),
    }
    manifests = load_registry(examples, "cohere/north-mini-code:free")
    assert {key: manifests[key]["digest"] for key in expected} == expected
    assert all("fixtures/" not in key for m in manifests.values() for key in m["files"])


async def test_third_package_is_explicitly_registered_with_generic_samples_and_api(
    examples, tmp_path
):
    root = tmp_path / "agents"
    shutil.copytree(examples, root)
    third = root / "incident-review"
    shutil.copytree(root / "log-analyst", third)
    path = third / "agent.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["id"] = "incident-review"
    path.write_text(yaml.safe_dump(profile))
    manifests = load_registry(root, "synthetic-model")
    assert "incident-review" not in manifests
    registry_path = root / "registry.yaml"
    registry = yaml.safe_load(registry_path.read_text())
    registry["enabled"]["incident-review"] = "incident-review/agent.yaml"
    registry_path.write_text(yaml.safe_dump(registry))
    manifests = load_registry(root, "synthetic-model")
    assert (
        load_samples(root)["incident-review"]
        == (third / "fixtures/input.synthetic.json").read_text()
    )
    owner = Principal("test", "owner", frozenset(manifests))
    app = create_app(object(), manifests, {"test-token": owner})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        card = await client.get("/agents/incident-review/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["supportedInterfaces"][0]["url"].endswith(
            "/agents/incident-review/rpc"
        )
        catalog = (await client.get("/agents")).json()
        assert "incident-review" in {a["id"] for a in catalog["agents"]}
    registry_path.write_text(
        "schema_version: 1\nenabled:\n"
        "  log-analyst: log-analyst/agent.yaml\n"
        "  log-analyst: incident-review/agent.yaml\n"
    )
    with pytest.raises(ValueError, match="duplicate_yaml_key"):
        load_registry(root, "synthetic-model")
