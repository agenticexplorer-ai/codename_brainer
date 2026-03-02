from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def po_stub(payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(payload, str):
        return {
            "problem_statement": payload,
            "goals": ["convert request into execution-ready scope"],
            "constraints": ["use minimal change set"],
            "success_criteria": ["team workflow run completed"],
        }
    stage = str(payload.get("stage", "intake"))
    if stage == "decompose":
        base_task = str(payload.get("task", "Deliver requested change"))
        return {
            "tasks": [
                {
                    "id": "T1",
                    "summary": f"Design approach for: {base_task}",
                    "acceptance": ["Architecture documented"],
                    "dependencies": [],
                },
                {
                    "id": "T2",
                    "summary": f"Implement core code for: {base_task}",
                    "acceptance": ["Core change implemented"],
                    "dependencies": ["T1"],
                },
                {
                    "id": "T3",
                    "summary": "Add tests or validation checks",
                    "acceptance": ["Validation command list provided"],
                    "dependencies": ["T2"],
                },
                {
                    "id": "T4",
                    "summary": "Prepare release notes summary",
                    "acceptance": ["Release summary draft available"],
                    "dependencies": ["T2"],
                },
            ]
        }
    return {
        "problem_statement": str(payload.get("task", "Unknown task")),
        "goals": ["build a deliverable implementation plan"],
        "constraints": ["keep changes reviewable"],
        "success_criteria": ["workflow completes with approved gates"],
    }


def principal_stub(payload: dict[str, Any]) -> dict[str, Any]:
    task = str(payload.get("task", "requested change"))
    return {
        "architecture_summary": f"Layered implementation for: {task}",
        "design_decisions": [
            "Keep workflow deterministic",
            "Keep outputs as JSON artifacts",
            "Use isolated task execution context",
        ],
        "risks": ["integration ordering", "policy drift"],
        "implementation_notes": ["validate dependencies before task dispatch"],
    }


def developer_stub(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task", {})
    task_id = str(task.get("id", "T?")) if isinstance(task, dict) else "T?"
    summary = (
        str(task.get("summary", "implement task"))
        if isinstance(task, dict)
        else "implement task"
    )
    worktree = payload.get("worktree", {})
    branch = str(worktree.get("branch", f"ak/demo/{task_id}")) if isinstance(worktree, dict) else f"ak/demo/{task_id}"
    path = str(worktree.get("path", f"agentkit/worktrees/demo/{task_id}")) if isinstance(worktree, dict) else f"agentkit/worktrees/demo/{task_id}"
    return {
        "task_id": task_id,
        "changes": [
            {
                "path": f"examples/{task_id.lower()}_demo.txt",
                "type": "create",
                "summary": f"Simulated implementation for {summary}",
            }
        ],
        "commands_ran": [
            {
                "cmd": f"echo '{summary}' > examples/{task_id.lower()}_demo.txt",
                "exit_code": 0,
                "notes": "simulated command, not executed",
            }
        ],
        "notes": f"branch={branch} worktree={path}",
    }


def tester_stub(payload: dict[str, Any]) -> dict[str, Any]:
    stage = str(payload.get("stage", "qa_task"))
    if stage == "system_qa":
        return {
            "verdict": "approve",
            "findings": [],
            "recommended_actions": ["system checks look consistent"],
        }
    task = payload.get("task", {})
    task_id = str(task.get("id", "T?")) if isinstance(task, dict) else "T?"
    return {
        "verdict": "approve",
        "findings": [{"severity": "minor", "text": f"{task_id} could add more assertions"}],
        "recommended_actions": ["proceed to integration"],
    }


def integrator_stub(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task", {})
    task_id = str(task.get("id", "T?")) if isinstance(task, dict) else "T?"
    queue_position = int(payload.get("queue_position", 1))
    return {
        "task_id": task_id,
        "queue_position": queue_position,
        "conflict_check": "pass",
        "merge_decision": "approve",
        "notes": "merge simulation approved",
    }


def devops_stub(payload: dict[str, Any]) -> dict[str, Any]:
    integrated = payload.get("integrated", [])
    count = len(integrated) if isinstance(integrated, list) else 0
    return {
        "release_readiness": "ready",
        "infra_checks": ["local env checks passed"],
        "rollout_plan": [f"promote integrated queue with {count} task(s)"],
        "risks": ["manual verification recommended"],
    }


def cicd_stub(payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    return {
        "pipeline_status": "pass",
        "checks": [
            {"name": "lint", "status": "pass", "notes": "simulated"},
            {"name": "tests", "status": "pass", "notes": "simulated"},
        ],
        "deployment_recommendation": "proceed",
        "summary": "local CI simulation succeeded",
    }


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
    if role == "po":
        return po_stub(payload if isinstance(payload, (dict, str)) else str(payload))
    if role == "principal_engineer":
        return principal_stub(payload if isinstance(payload, dict) else {})
    if role == "developer":
        return developer_stub(payload if isinstance(payload, dict) else {})
    if role == "tester":
        return tester_stub(payload if isinstance(payload, dict) else {})
    if role == "integrator":
        return integrator_stub(payload if isinstance(payload, dict) else {})
    if role == "devops":
        return devops_stub(payload if isinstance(payload, dict) else {})
    if role == "cicd":
        return cicd_stub(payload if isinstance(payload, dict) else {})
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
