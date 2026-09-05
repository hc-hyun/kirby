import json
import shutil

import pytest
import yaml
from jsonschema import ValidationError

from kirby.roles import load_role


@pytest.mark.parametrize("role", ["voc-analyst", "log-analyst"])
def test_examples_and_pinned_snapshot(examples, tmp_path, role):
    root = tmp_path / "examples"
    shutil.copytree(examples, root)
    first = load_role(root, role)
    result = (root / f"results/{role.split('-')[0]}-result.synthetic.json").read_text()
    first.validate_output(result)
    skill = root / f"skills/{role}-role/SKILL.md"
    skill.write_text(skill.read_text() + "\nNew release text.\n")
    second = load_role(root, role)
    assert first.digest != second.digest
    profile = first.profile
    profile["version"] = "9.0.0"
    assert first.profile["version"] == "1.0.0"
    with pytest.raises(ValidationError):
        first.validate_output(json.dumps({"invalid": True}))


@pytest.mark.parametrize("attack", ["duplicate", "mcp", "path", "symlink", "size"])
def test_reject_role_attacks(examples, tmp_path, attack):
    root = tmp_path / "examples"
    shutil.copytree(examples, root)
    path = root / "roles/voc-analyst.yaml"
    profile = yaml.safe_load(path.read_text())
    if attack == "duplicate":
        profile["skills"]["available"].append(profile["skills"]["required"][0])
    elif attack == "mcp":
        profile["tools"]["mcp_server_refs"] = ["https://unapproved.invalid"]
    elif attack == "size":
        profile["execution"]["max_required_skill_bytes"] = 1
    elif attack == "symlink":
        target = root / "skills/voc-analyst-role/SKILL.md"
        target.unlink()
        target.symlink_to(examples / "skills/voc-analyst-role/SKILL.md")
    else:
        catalog_path = root / "catalogs.example.yaml"
        catalog = yaml.safe_load(catalog_path.read_text())
        catalog["skills"][0]["source_path"] = "../outside"
        catalog_path.write_text(yaml.safe_dump(catalog))
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(ValueError):
        load_role(root, "voc-analyst")
