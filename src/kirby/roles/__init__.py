"""Validation of local project examples; not a Goose configuration converter."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def local_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe_path")
    target = root / path
    if any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise ValueError("symlink_not_allowed")
    target.resolve().relative_to(root.resolve())
    if not target.is_file():
        raise ValueError("missing_file")
    return target


@dataclass(frozen=True)
class LocalRole:
    id: str
    digest: str
    profile_json: bytes
    output_schema_json: bytes

    @property
    def profile(self):
        return json.loads(self.profile_json)

    def validate_output(self, text: str):
        result = json.loads(text)
        Draft202012Validator(json.loads(self.output_schema_json)).validate(result)
        return result


def load_role(root: Path, role_id: str) -> LocalRole:
    catalog = yaml.safe_load((root / "catalogs.example.yaml").read_text())
    profile = yaml.safe_load(local_file(root, catalog["roles"][role_id]).read_text())
    schema = json.loads((root / "schemas/role-profile.schema.json").read_text())
    Draft202012Validator(schema).validate(profile)
    if profile["id"] != role_id:
        raise ValueError("role_id_mismatch")
    for field, section in [
        ("model_profile", "model_profiles"),
        ("worker_pool", "worker_pools"),
    ]:
        if profile["runtime"][field] not in catalog[section]:
            raise ValueError("unknown_runtime_reference")
    if any(
        ref not in catalog["mcp_servers"] for ref in profile["tools"]["mcp_server_refs"]
    ):
        raise ValueError("unknown_mcp_reference")
    refs = profile["skills"]["required"] + profile["skills"]["available"]
    if len({ref["id"] for ref in refs}) != len(refs):
        raise ValueError("duplicate_skill")
    files = {}
    required_size = 0
    for index, ref in enumerate(refs):
        candidates = [
            s
            for s in catalog["skills"]
            if (s["id"], s["version"]) == (ref["id"], ref["version"])
        ]
        if len(candidates) != 1:
            raise ValueError("unknown_skill_release")
        folder = candidates[0]["source_path"]
        skill = local_file(root, folder + "/SKILL.md")
        frontmatter = yaml.safe_load(skill.read_text().split("---", 2)[1])
        if frontmatter["name"] != ref["id"] or skill.parent.name != ref["id"]:
            raise ValueError("skill_name_mismatch")
        if index < len(profile["skills"]["required"]):
            required_size += skill.stat().st_size
        for path in sorted(skill.parent.rglob("*")):
            if path.is_symlink():
                raise ValueError("symlink_not_allowed")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                data = local_file(root, relative).read_bytes()
                files[relative] = {
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
    limits = profile["execution"]
    if (
        required_size > limits["max_required_skill_bytes"]
        or sum(f["bytes"] for f in files.values()) > limits["max_skill_bundle_bytes"]
    ):
        raise ValueError("skill_size_limit")
    output = local_file(
        root, catalog["output_schemas"][profile["output_schema_ref"]]
    ).read_bytes()
    Draft202012Validator.check_schema(json.loads(output))
    digest = hashlib.sha256(
        canonical({"profile": profile, "files": files, "schema": json.loads(output)})
    ).hexdigest()
    return LocalRole(role_id, digest, canonical(profile), canonical(json.loads(output)))
