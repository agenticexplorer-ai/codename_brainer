from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agentkit.backends import build_backend
from agentkit.backends.base import BackendName, PermissionMode
from agentkit.policies.checks import evaluate_implementer_report, load_policy_lines
from agentkit.runner.loaders import load_text, load_workflow

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = REPO_ROOT / "agentkit" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
POLICIES_DIR = REPO_ROOT / "agentkit" / "policies"
MAX_REVIEW_RETRIES = 2
VALID_BACKENDS: set[str] = {"stub", "codex"}
VALID_PERMISSIONS: set[str] = {"read_only", "write_safe"}


def doctor() -> int:
    required = ["git", "python3"]
    optional = ["rg"]  # ripgrep helps later but not required

    missing = [c for c in required if shutil.which(c) is None]
    if missing:
        print("Missing required commands:", ", ".join(missing))
        return 1

    print("✅ Required commands OK:", ", ".join(required))

    missing_opt = [c for c in optional if shutil.which(c) is None]
    if missing_opt:
        print("ℹ️ Optional commands not found:", ", ".join(missing_opt))
    else:
        print("✅ Optional commands OK:", ", ".join(optional))

    print(f"Python: {sys.version.split()[0]}")
    return 0


def print_run_usage() -> None:
    print(
        "Usage: agentkit run <workflow_name_without_yaml> <task>"
        " [--backend stub|codex] [--permissions read_only|write_safe]"
    )


def parse_run_args(args: list[str]) -> tuple[str, str, BackendName, PermissionMode] | None:
    if len(args) < 2:
        return None
    workflow_name = args[0]
    remainder = args[1:]

    task_parts: list[str] = []
    idx = 0
    while idx < len(remainder) and not remainder[idx].startswith("--"):
        task_parts.append(remainder[idx])
        idx += 1

    if not task_parts:
        return None

    backend_flag: str | None = None
    permissions_flag: str | None = None

    while idx < len(remainder):
        flag = remainder[idx]
        idx += 1
        if idx >= len(remainder):
            return None
        value = remainder[idx]
        idx += 1

        if flag == "--backend":
            backend_flag = value
            continue
        if flag == "--permissions":
            permissions_flag = value
            continue
        return None

    backend = resolve_backend_name(backend_flag)
    permissions = resolve_permissions(permissions_flag)
    return workflow_name, " ".join(task_parts), backend, permissions


def resolve_backend_name(backend_flag: str | None) -> BackendName:
    candidate = (backend_flag or os.getenv("AGENTKIT_BACKEND") or "stub").strip()
    if candidate not in VALID_BACKENDS:
        raise ValueError(
            f"Invalid backend '{candidate}'. Allowed values: {', '.join(sorted(VALID_BACKENDS))}"
        )
    return candidate  # type: ignore[return-value]


def resolve_permissions(permissions_flag: str | None) -> PermissionMode:
    candidate = (
        permissions_flag or os.getenv("AGENTKIT_PERMISSIONS") or "read_only"
    ).strip()
    if candidate not in VALID_PERMISSIONS:
        raise ValueError(
            "Invalid permissions "
            f"'{candidate}'. Allowed values: {', '.join(sorted(VALID_PERMISSIONS))}"
        )
    return candidate  # type: ignore[return-value]


def build_stage_input(stage: dict[str, Any], task: str, artifacts: dict[str, Any]) -> Any:
    stage_input = stage.get("input")
    if stage_input == "task":
        return task
    if stage_input == "plan_json":
        return artifacts.get("plan")
    if stage_input == "impl_report_json":
        return artifacts.get("implement")
    return {"task": task, "artifacts": artifacts}


def run(
    workflow_name: str,
    task: str,
    backend_name: BackendName,
    permissions: PermissionMode,
) -> int:
    wf_path = REPO_ROOT / "agentkit" / "workflows" / f"{workflow_name}.yaml"
    if not wf_path.exists():
        print(f"Workflow not found: {wf_path}")
        return 1

    wf = load_workflow(wf_path)

    # load personas used by this workflow
    roles = [stage["role"] for stage in wf.stages]
    persona_texts = {}
    for role in roles:
        p = REPO_ROOT / "agentkit" / "personas" / f"{role}.md"
        if not p.exists():
            print(f"Persona not found for role '{role}': {p}")
            return 1
        persona_texts[role] = load_text(p)

    print("== Workflow ==")
    print(f"Name: {wf.name}")
    print(f"Description: {wf.description}")
    print("Stages:", " -> ".join([s["id"] for s in wf.stages]))
    print()

    print("== Task ==")
    print(task)
    print()

    print("== Personas loaded ==")
    for role in roles:
        print(f"- {role}: {len(persona_texts[role])} chars")
    print()

    run_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run_log_file = LOGS_DIR / f"run_{workflow_name}_{run_stamp}.jsonl"
    raw_event_log_file = (
        LOGS_DIR / f"raw_events_{workflow_name}_{run_stamp}.jsonl"
        if backend_name == "codex"
        else None
    )

    backend = build_backend(
        backend_name=backend_name,
        permissions=permissions,
        raw_event_log_file=raw_event_log_file,
    )

    allowed_commands = load_policy_lines(POLICIES_DIR / "allowed_commands.txt")
    forbidden_paths = load_policy_lines(POLICIES_DIR / "forbidden_paths.txt")

    artifacts = {}
    role_thread_ids: dict[str, str] = {}

    def execute_stage(
        stage: dict[str, Any],
        role_input: Any,
        retry_index: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        stage_id = stage["id"]
        role = stage["role"]
        retry_suffix = f" [retry {retry_index}]" if retry_index > 0 else ""
        print(f"--- Stage: {stage_id} (role: {role}){retry_suffix} ---")

        backend_error: str | None = None
        stage_output: dict[str, Any] | None = None

        try:
            stage_output = backend.run_role(
                role=role,
                persona=persona_texts[role],
                input=role_input,
                repo_root=REPO_ROOT,
                thread_id=role_thread_ids.get(role),
            )
        except Exception as exc:
            backend_error = str(exc)
            stage_output = {"error": backend_error}

        thread_id_getter = getattr(backend, "get_thread_id", None)
        if callable(thread_id_getter):
            backend_thread_id = thread_id_getter(role)
            if isinstance(backend_thread_id, str):
                role_thread_ids[role] = backend_thread_id
        thread_id = role_thread_ids.get(role)

        if backend_error is None and role == "implementer":
            violations = evaluate_implementer_report(
                report=stage_output if isinstance(stage_output, dict) else {},
                allowed_commands=allowed_commands,
                forbidden_paths=forbidden_paths,
                permissions=permissions,
            )
            if violations:
                backend_error = " | ".join(violations)
                if isinstance(stage_output, dict):
                    stage_output["policy_violations"] = violations

        attempt_getter = getattr(backend, "get_last_attempt", None)
        attempt = attempt_getter() if callable(attempt_getter) else 1
        error_getter = getattr(backend, "get_last_backend_error", None)
        backend_side_error = error_getter() if callable(error_getter) else None
        backend_error = backend_error or backend_side_error

        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "stage": stage_id,
            "role": role,
            "retry_index": retry_index,
            "backend": backend_name,
            "permissions": permissions,
            "thread_id": thread_id,
            "attempt": attempt,
            "input_preview": str(role_input)[:200],
            "output": stage_output,
            "backend_error": backend_error,
        }
        with run_log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        print(json.dumps(stage_output, indent=2, ensure_ascii=False))
        print()

        if backend_error:
            return None, backend_error
        return stage_output, None

    implement_stage = next((s for s in wf.stages if s.get("id") == "implement"), None)
    review_stage = next((s for s in wf.stages if s.get("id") == "review"), None)

    try:
        for stage in wf.stages:
            stage_id = stage["id"]
            role_input = build_stage_input(stage, task, artifacts)
            out, err = execute_stage(stage=stage, role_input=role_input, retry_index=0)
            if err:
                print(f"Stage '{stage_id}' failed: {err}")
                return 1
            assert out is not None
            artifacts[stage_id] = out

            if stage_id == "review" and isinstance(out, dict):
                retries = 0
                verdict = out.get("verdict")
                if verdict == "request_changes" and permissions == "read_only":
                    print(
                        "Review requested changes, but runner is in read_only mode; "
                        "skipping retry loop."
                    )
                    continue
                while verdict == "request_changes" and retries < MAX_REVIEW_RETRIES:
                    if implement_stage is None or review_stage is None:
                        print("Workflow retry requested but implement/review stages are missing.")
                        return 1
                    retries += 1
                    retry_input = {
                        "plan": artifacts.get("plan"),
                        "previous_impl_report": artifacts.get("implement"),
                        "review_comments": out.get("comments", []),
                        "suggested_followups": out.get("suggested_followups", []),
                    }
                    impl_out, impl_err = execute_stage(
                        stage=implement_stage,
                        role_input=retry_input,
                        retry_index=retries,
                    )
                    if impl_err:
                        print(f"Retry implement stage failed: {impl_err}")
                        return 1
                    assert impl_out is not None
                    artifacts["implement"] = impl_out

                    out, review_err = execute_stage(
                        stage=review_stage,
                        role_input=impl_out,
                        retry_index=retries,
                    )
                    if review_err:
                        print(f"Retry review stage failed: {review_err}")
                        return 1
                    assert out is not None
                    artifacts["review"] = out
                    verdict = out.get("verdict") if isinstance(out, dict) else None

                if verdict == "request_changes":
                    print(
                        f"Review requested further changes after {MAX_REVIEW_RETRIES} retries."
                    )
                    return 1
    finally:
        close_fn = getattr(backend, "close", None)
        if callable(close_fn):
            close_fn()

    print(f"✅ Run complete. Logs: {run_log_file}")
    if raw_event_log_file is not None:
        print(f"Raw events: {raw_event_log_file}")
    return 0


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "doctor":
        raise SystemExit(doctor())

    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        try:
            parsed = parse_run_args(sys.argv[2:])
            if parsed is None:
                print_run_usage()
                raise SystemExit(2)
            workflow_name, task, backend_name, permissions = parsed
        except ValueError as exc:
            print(str(exc))
            print_run_usage()
            raise SystemExit(2)
        raise SystemExit(run(workflow_name, task, backend_name, permissions))

    print("agentkit is installed.")
    print("Try: agentkit doctor")
    print(
        "Or:  agentkit run pr_factory \"Add a hello endpoint\" --backend stub --permissions read_only"
    )
