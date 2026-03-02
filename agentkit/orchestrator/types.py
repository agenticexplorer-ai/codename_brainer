from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agentkit.backends.base import BackendName, PermissionMode

AutonomyMode = Literal["full_auto", "mixed", "human_in_loop"]
TaskStatus = Literal[
    "queued",
    "in_progress",
    "implemented",
    "qa_passed",
    "qa_failed",
    "integrated",
    "blocked",
]
RunStatus = Literal["running", "paused", "completed", "failed", "cancelled"]


@dataclass
class RoleSpec:
    role: str
    default_count: int
    min_count: int
    max_count: int


@dataclass
class RolePool:
    specs: dict[str, RoleSpec]

    def count_for(self, role: str) -> int:
        spec = self.specs.get(role)
        if spec is None:
            return 1
        return spec.default_count


@dataclass
class TaskItem:
    id: str
    summary: str
    acceptance: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    state: TaskStatus = "queued"
    assigned_agent: str | None = None
    retries: int = 0
    branch: str | None = None
    worktree: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateDecision:
    gate: str
    approved: bool
    reason: str
    requested_by: str


@dataclass
class RunState:
    run_id: str
    workflow: str
    task: str
    autonomy: AutonomyMode
    backend: BackendName
    permissions: PermissionMode
    status: RunStatus
    team_model: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

