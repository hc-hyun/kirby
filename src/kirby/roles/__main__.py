"""Validate enabled agent packages and their synthetic output fixtures."""

import argparse
import json
from pathlib import Path

from kirby.roles import enabled_agents, load_role, load_samples
from kirby.roles.registry import build_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("agents"))
    parser.add_argument("--model", default="cohere/north-mini-code:free")
    args = parser.parse_args()
    load_samples(args.root)
    for role_id in enabled_agents(args.root):
        role = load_role(args.root, role_id)
        role.validate_output(role.fixtures["result"].read_text())
        manifest = build_manifest(args.root, role_id, args.model)
        print(
            json.dumps(
                {
                    "id": role_id,
                    "version": role.profile["version"],
                    "digest": manifest["digest"],
                    "status": "PASS",
                }
            )
        )


if __name__ == "__main__":
    main()
