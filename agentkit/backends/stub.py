from __future__ import annotations

import json
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


# single dispatcher
def run_role(role: str, payload: dict | str) -> dict:
    if role == "planner":
        return planner_stub(payload if isinstance(payload, str) else json.dumps(payload))
    if role == "implementer":
        # payload expected to be dict result from planner
        return implementer_stub(payload if isinstance(payload, dict) else {})
    if role == "reviewer":
        return reviewer_stub(payload if isinstance(payload, dict) else {})
    # unknown role fallback
    return {"error": f"no stub for role '{role}'"}
