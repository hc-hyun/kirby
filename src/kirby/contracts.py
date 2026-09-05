"""Shared records at API, storage, roles and runtime boundaries."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})


@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str
    roles: frozenset[str]


@dataclass
class TaskRecord:
    id: str
    context_id: str
    role_id: str
    tenant: str
    subject: str
    message_id: str
    input_text: str
    state: Literal["queued", "running", "completed", "failed", "canceled"]
    manifest: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    worker_id: str | None = None
    session_id: str | None = None
    session_path: str | None = None


@dataclass(frozen=True)
class RunResult:
    output: dict[str, Any]
    session_id: str
    session_path: str
    usage: dict[str, Any] = field(default_factory=dict)


class NotFound(Exception):
    """Missing or unauthorized resource."""


class Conflict(Exception):
    """Duplicate payload conflict or busy/unusable context."""


class LeaseLost(Exception):
    """Worker no longer owns the execution."""
