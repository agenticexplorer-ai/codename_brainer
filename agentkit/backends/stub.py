from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def planner_stub(task: str) -> dict[str, Any]:
    # Simple deterministic planner for testing
    return {
        "summary": f"A plan to: {task}",
        "steps": [
            {
                "id": "S1",
                "action": "create file demo.txt",
                "notes": "tiny demo file with a one-line description",
            },
            {
                "id": "S2",
                "action": "write description in demo.txt",
                "notes": "explain what it does",
            },
            {"id": "S3", "action": "commit changes", "notes": "small commit message"},
        ],
        "risks": ["none for demo"],
        "done_when": ["file demo.txt exists", "commit created"],
    }


def implementer_stub(plan: dict[str, Any]) -> dict[str, Any]:
    # Simulate making a file and committing
    changes = [
        {
            "path": "examples/demo.txt",
            "type": "create",
            "summary": "Added demo.txt with a one-line description",
        }
    ]
    commands = [
        {
            "cmd": "echo 'demo' > examples/demo.txt",
            "exit_code": 0,
            "notes": "created demo file",
        },
        {
            "cmd": "git add examples/demo.txt && git commit -m 'chore: add demo file'",
            "exit_code": 0,
            "notes": "simulated commit (not run)",
        },
    ]
    return {
        "changes": changes,
        "commands_ran": commands,
        "next": "ask reviewer to validate the change",
    }


def reviewer_stub(impl_report: dict[str, Any]) -> dict[str, Any]:
    # Very simple reviewer logic
    return {
        "verdict": "approve",
        "comments": [
            {"severity": "minor", "text": "Consider adding a one-line usage example."}
        ],
        "suggested_followups": ["add usage example if desired"],
    }


class StubBackend:
    def __init__(self) -> None:
        self._thread_ids: dict[str, str] = {}
        self._last_attempt: int = 1
        self._last_backend_error: str | None = None

    def run_role(
        self,
        role: str,
        persona: str,
        input: Any,
        repo_root: Path,
        thread_id: str | None,
    ) -> dict[str, Any]:
        del persona, repo_root
        self._last_attempt = 1
        self._last_backend_error = None
        if thread_id is not None:
            self._thread_ids[role] = thread_id
        elif role not in self._thread_ids:
            self._thread_ids[role] = f"stub-{role}"
        return _dispatch(role, input)

    def get_thread_id(self, role: str) -> str | None:
        return self._thread_ids.get(role)

    def get_last_attempt(self) -> int:
        return self._last_attempt

    def get_last_backend_error(self) -> str | None:
        return self._last_backend_error


def _dispatch(role: str, payload: Any) -> dict[str, Any]:
    if role == "planner":
        return planner_stub(payload if isinstance(payload, str) else json.dumps(payload))
    if role == "implementer":
        # payload expected to be dict result from planner
        return implementer_stub(payload if isinstance(payload, dict) else {})
    if role == "reviewer":
        return reviewer_stub(payload if isinstance(payload, dict) else {})
    # unknown role fallback
    return {"error": f"no stub for role '{role}'"}


def run_role(
    role: str,
    payload: dict | str,
    persona: str | None = None,
    repo_root: Path | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    del persona, repo_root, thread_id
    return _dispatch(role, payload)
