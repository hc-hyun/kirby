"""Compile packages into self-contained, versioned execution manifests."""

import hashlib
import json
from pathlib import Path

from kirby.roles import canonical, enabled_agents, load_role


def build_manifest(root: Path, role_id: str, model: str) -> dict:
    role = load_role(root, role_id)
    files = json.loads(role.files_json)
    files[".goosehints"] = (
        "\n\n".join(role.required)
        + "\n\nReturn one JSON object matching this schema, without commentary:\n"
        + role.output_schema_json.decode()
    )
    manifest = {
        "role_id": role.id,
        "profile": role.profile,
        "output_schema": json.loads(role.output_schema_json),
        "files": files,
        "model": model,
        "goose_version": "1.49.0",
    }
    manifest["digest"] = hashlib.sha256(canonical(manifest)).hexdigest()
    return manifest


def load_registry(root: Path, model: str) -> dict[str, dict]:
    return {
        role_id: build_manifest(root, role_id, model)
        for role_id in enabled_agents(root)
    }
