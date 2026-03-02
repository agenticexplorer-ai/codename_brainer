from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentkit.orchestrator.types import RunState, TaskItem


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root
        self.run_id = run_id
        self.run_dir = self.root / run_id
        self.artifacts_dir = self.run_dir / "artifacts"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.run_file = self.run_dir / "run.json"
        self.events_file = self.run_dir / "events.jsonl"
        self.tasks_file = self.run_dir / "tasks.jsonl"
        self.worktrees_file = self.run_dir / "worktrees.jsonl"

    def write_run(self, run_state: RunState) -> None:
        payload = {
            "run_schema_version": 1,
            "run_id": run_state.run_id,
            "workflow": run_state.workflow,
            "task": run_state.task,
            "autonomy": run_state.autonomy,
            "backend": run_state.backend,
            "permissions": run_state.permissions,
            "status": run_state.status,
            "team_model": run_state.team_model,
            "created_at": run_state.created_at,
            "updated_at": run_state.updated_at,
            "metadata": run_state.metadata,
        }
        self.run_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def write_event(
        self,
        *,
        role: str,
        stage: str,
        state: str,
        agent_id: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        entry = {
            "timestamp": utc_now_iso(),
            "run_id": self.run_id,
            "role": role,
            "agent_id": agent_id,
            "task_id": task_id,
            "stage": stage,
            "state": state,
            "details": details or {},
            "error": error,
        }
        self._append_jsonl(self.events_file, entry)

    def write_task(self, task: TaskItem) -> None:
        entry = {
            "timestamp": utc_now_iso(),
            "run_id": self.run_id,
            "task_id": task.id,
            "summary": task.summary,
            "acceptance": task.acceptance,
            "dependencies": task.dependencies,
            "state": task.state,
            "assigned_agent": task.assigned_agent,
            "retries": task.retries,
            "branch": task.branch,
            "worktree": task.worktree,
            "details": task.details,
        }
        self._append_jsonl(self.tasks_file, entry)

    def write_worktree(self, payload: dict[str, Any]) -> None:
        entry = {"timestamp": utc_now_iso(), "run_id": self.run_id, **payload}
        self._append_jsonl(self.worktrees_file, entry)

    def write_artifact(self, name: str, payload: Any) -> None:
        safe_name = name.replace("/", "_")
        path = self.artifacts_dir / f"{safe_name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def list_runs(state_root: Path) -> list[dict[str, Any]]:
    runs_dir = state_root
    if not runs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        run_file = run_dir / "run.json"
        if not run_file.exists():
            continue
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append(data)
    return out


def load_run(state_root: Path, run_id: str) -> dict[str, Any]:
    run_file = state_root / run_id / "run.json"
    if not run_file.exists():
        raise FileNotFoundError(f"Run not found: {run_id}")
    return json.loads(run_file.read_text(encoding="utf-8"))

