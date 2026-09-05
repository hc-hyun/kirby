"""Load approved local roles into self-contained, versioned execution manifests."""

import hashlib
import json
from pathlib import Path

import yaml

from kirby.roles import canonical, load_role, local_file


def build_manifest(root: Path, role_id: str, model: str) -> dict:
    role = load_role(root, role_id)
    profile = role.profile
    catalog = yaml.safe_load((root / "catalogs.example.yaml").read_text())
    files = {}
    required = []
    for group in ["required", "available"]:
        for ref in profile["skills"][group]:
            entry = next(
                s
                for s in catalog["skills"]
                if (s["id"], s["version"]) == (ref["id"], ref["version"])
            )
            folder = local_file(root, entry["source_path"] + "/SKILL.md").parent
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(folder).as_posix()
                    files[f".agents/skills/{ref['id']}/{relative}"] = path.read_text()
            if group == "required":
                required.append((folder / "SKILL.md").read_text())
    files[".goosehints"] = (
        "\n\n".join(required)
        + "\n\nReturn one JSON object matching this schema, without commentary:\n"
        + role.output_schema_json.decode()
    )
    manifest = {
        "role_id": role.id,
        "profile": profile,
        "output_schema": json.loads(role.output_schema_json),
        "files": files,
        "model": model,
        "goose_version": "1.49.0",
    }
    manifest["digest"] = hashlib.sha256(canonical(manifest)).hexdigest()
    return manifest


def load_registry(root: Path, model: str) -> dict[str, dict]:
    catalog = yaml.safe_load((root / "catalogs.example.yaml").read_text())
    return {
        role_id: build_manifest(root, role_id, model) for role_id in catalog["roles"]
    }
