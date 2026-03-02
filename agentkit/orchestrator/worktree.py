from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentkit.backends.base import PermissionMode


@dataclass
class WorktreeAssignment:
    task_id: str
    branch: str
    path: Path
    created: bool
    error: str | None = None


class WorktreeManager:
    def __init__(
        self,
        repo_root: Path,
        workspace_root: Path,
        run_id: str,
        permissions: PermissionMode,
        keep_worktrees: bool,
    ) -> None:
        self.repo_root = repo_root
        self.workspace_root = workspace_root / run_id
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.permissions = permissions
        self.keep_worktrees = keep_worktrees
        self.assignments: list[WorktreeAssignment] = []

    def create_for_task(self, task_id: str) -> WorktreeAssignment:
        branch = f"ak/{self.run_id}/{task_id}"
        path = self.workspace_root / task_id
        if self.permissions == "read_only":
            assignment = WorktreeAssignment(
                task_id=task_id, branch=branch, path=path, created=False
            )
            self.assignments.append(assignment)
            return assignment

        if shutil.which("git") is None:
            assignment = WorktreeAssignment(
                task_id=task_id,
                branch=branch,
                path=path,
                created=False,
                error="git not found",
            )
            self.assignments.append(assignment)
            return assignment

        cmd = ["git", "-C", str(self.repo_root), "worktree", "add", str(path), "-b", branch]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            assignment = WorktreeAssignment(
                task_id=task_id,
                branch=branch,
                path=path,
                created=False,
                error=(result.stderr or result.stdout).strip() or "worktree add failed",
            )
            self.assignments.append(assignment)
            return assignment

        assignment = WorktreeAssignment(task_id=task_id, branch=branch, path=path, created=True)
        self.assignments.append(assignment)
        return assignment

    def cleanup(self) -> list[str]:
        if self.keep_worktrees or self.permissions == "read_only":
            return []

        errors: list[str] = []
        for assignment in self.assignments:
            if not assignment.created:
                continue
            cmd = ["git", "-C", str(self.repo_root), "worktree", "remove", str(assignment.path), "--force"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                errors.append((result.stderr or result.stdout).strip() or "worktree remove failed")
        return errors

