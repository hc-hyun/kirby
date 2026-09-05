"""Validate explicitly enabled agent packages and resolve their local assets."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


class UniqueLoader(yaml.SafeLoader):
    """Configuration must not silently replace duplicate IDs or fields."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise ValueError("duplicate_yaml_key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(path):
    value = yaml.load(path.read_text(), Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError("expected_mapping")
    return value


def local_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError("unsafe_path")
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


def enabled_agents(root: Path) -> dict[str, str]:
    registry = read_yaml(local_file(root, "registry.yaml"))
    if (
        set(registry) != {"schema_version", "enabled"}
        or registry["schema_version"] != 1
    ):
        raise ValueError("invalid_registry")
    entries = registry["enabled"]
    if not isinstance(entries, dict) or not entries:
        raise ValueError("empty_registry")
    for role_id, path in entries.items():
        if not isinstance(role_id, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", role_id
        ):
            raise ValueError("invalid_role_id")
        if path != f"{role_id}/agent.yaml":
            raise ValueError("agent_folder_mismatch")
        local_file(root, path)
    return entries


@dataclass(frozen=True)
class LocalRole:
    id: str
    digest: str
    profile_json: bytes
    output_schema_json: bytes
    files_json: bytes
    required: tuple[str, ...]
    fixtures: dict[str, Path]

    @property
    def profile(self):
        return json.loads(self.profile_json)

    def validate_output(self, text: str):
        result = json.loads(text)
        Draft202012Validator(json.loads(self.output_schema_json)).validate(result)
        return result


def load_role(root: Path, role_id: str) -> LocalRole:
    descriptor = local_file(root, enabled_agents(root)[role_id])
    package = descriptor.parent
    profile = read_yaml(descriptor)
    assets = profile.pop("assets", None)
    if not isinstance(assets, dict) or set(assets) != {
        "skills",
        "output_schema",
        "fixtures",
    }:
        raise ValueError("invalid_assets")
    platform = Path(__file__).parent
    schema = json.loads((platform / "schemas/role-profile.schema.json").read_text())
    Draft202012Validator(schema).validate(profile)
    if profile["id"] != role_id:
        raise ValueError("role_id_mismatch")
    references = read_yaml(platform / "references.yaml")
    for field, section in [
        ("model_profile", "model_profiles"),
        ("worker_pool", "worker_pools"),
    ]:
        if profile["runtime"][field] not in references[section]:
            raise ValueError("unknown_runtime_reference")
    if any(
        ref not in references["mcp_servers"]
        for ref in profile["tools"]["mcp_server_refs"]
    ):
        raise ValueError("unknown_mcp_reference")
    refs = profile["skills"]["required"] + profile["skills"]["available"]
    if len({ref["id"] for ref in refs}) != len(refs):
        raise ValueError("duplicate_skill")
    if not isinstance(assets["skills"], dict) or set(assets["skills"]) != {
        ref["id"] for ref in refs
    }:
        raise ValueError("skill_assets_mismatch")
    files, required = {}, []
    skill_folders = []
    total_size = required_size = 0
    for index, ref in enumerate(refs):
        entry = assets["skills"][ref["id"]]
        if not isinstance(entry, dict) or entry.get("version") != ref["version"]:
            raise ValueError("unknown_skill_release")
        if set(entry) == {"path", "version"}:
            skill_root, relative = package, entry["path"]
        elif set(entry) == {"shared", "version"} and entry["shared"] == ref["id"]:
            skill_root = root / "_shared"
            releases = read_yaml(local_file(skill_root, "releases.yaml"))
            versions = releases.get(ref["id"], {})
            release = (
                versions.get(ref["version"], {}) if isinstance(versions, dict) else {}
            )
            if not isinstance(release, dict) or set(release) != {"path"}:
                raise ValueError("unknown_shared_release")
            relative = release["path"]
        else:
            raise ValueError("invalid_skill_asset")
        if not isinstance(relative, str):
            raise ValueError("unsafe_path")
        skill = local_file(skill_root, relative + "/SKILL.md")
        skill_folders.append(skill.parent.resolve())
        if skill.stat().st_size > profile["execution"]["max_skill_bundle_bytes"]:
            raise ValueError("skill_size_limit")
        text = skill.read_text()
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("invalid_skill_frontmatter")
        frontmatter = yaml.load(parts[1], Loader=UniqueLoader)
        if (
            not isinstance(frontmatter, dict)
            or frontmatter.get("name") != ref["id"]
            or skill.parent.name != ref["id"]
        ):
            raise ValueError("skill_name_mismatch")
        if index < len(profile["skills"]["required"]):
            required.append(text)
            required_size += skill.stat().st_size
        for path in sorted(skill.parent.rglob("*")):
            if path.is_symlink():
                raise ValueError("symlink_not_allowed")
            if path.is_file():
                checked = local_file(
                    skill_root, path.relative_to(skill_root).as_posix()
                )
                total_size += checked.stat().st_size
                if total_size > profile["execution"]["max_skill_bundle_bytes"]:
                    raise ValueError("skill_size_limit")
                files[
                    f".agents/skills/{ref['id']}/{path.relative_to(skill.parent).as_posix()}"
                ] = checked.read_text()
    if required_size > profile["execution"]["max_required_skill_bytes"]:
        raise ValueError("skill_size_limit")
    output = json.loads(local_file(package, assets["output_schema"]).read_text())
    Draft202012Validator.check_schema(output)
    fixture_refs = assets["fixtures"]
    if not isinstance(fixture_refs, dict) or set(fixture_refs) != {"input", "result"}:
        raise ValueError("invalid_fixtures")
    fixtures = {name: local_file(package, path) for name, path in fixture_refs.items()}
    if any(
        path.resolve().is_relative_to(folder)
        for path in fixtures.values()
        for folder in skill_folders
    ):
        raise ValueError("fixture_in_skill_bundle")
    digest = hashlib.sha256(
        canonical({"profile": profile, "files": files, "schema": output})
    ).hexdigest()
    return LocalRole(
        role_id,
        digest,
        canonical(profile),
        canonical(output),
        canonical(files),
        tuple(required),
        fixtures,
    )


def load_samples(root: Path) -> dict[str, str]:
    samples = {}
    for role_id in enabled_agents(root):
        role = load_role(root, role_id)
        path = role.fixtures["input"]
        if path.stat().st_size > role.profile["execution"]["max_input_bytes"]:
            raise ValueError("fixture_size_limit")
        samples[role_id] = path.read_text()
    return samples
