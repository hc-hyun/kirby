"""Environment configuration shared by the two service entry points."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from kirby.contracts import Principal


@dataclass(frozen=True)
class Settings:
    database_url: str
    roles_dir: Path
    sessions_dir: Path
    goose_binary: Path
    model: str
    key_file: Path
    credentials_file: Path
    public_url: str
    worker_id: str

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.environ["KIRBY_DATABASE_URL"],
            roles_dir=Path(os.getenv("KIRBY_ROLES_DIR", "agents")).resolve(),
            sessions_dir=Path(
                os.getenv("KIRBY_SESSIONS_DIR", ".local/sessions")
            ).resolve(),
            goose_binary=Path(
                os.getenv("KIRBY_GOOSE_BINARY", ".tools/goose")
            ).resolve(),
            model=os.getenv("KIRBY_MODEL", "cohere/north-mini-code:free"),
            key_file=Path(
                os.getenv("KIRBY_OPENROUTER_KEY_FILE", "/run/secrets/openrouter_key")
            ),
            credentials_file=Path(
                os.getenv("KIRBY_CREDENTIALS_FILE", "/run/secrets/api_credentials")
            ),
            public_url=os.getenv("KIRBY_PUBLIC_URL", "http://127.0.0.1:8000").rstrip(
                "/"
            ),
            worker_id=os.getenv("KIRBY_WORKER_ID", "worker-1"),
        )


def load_credentials(path: Path) -> dict[str, Principal]:
    entries = json.loads(path.read_text())
    return {
        entry["token"]: Principal(
            entry["tenant"], entry["subject"], frozenset(entry["roles"])
        )
        for entry in entries
    }
