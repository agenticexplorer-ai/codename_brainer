from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentkit.backends import build_backend
from agentkit.backends.base import BackendName, PermissionMode
from agentkit.orchestrator.config import load_role_pool, load_scheduler_policies
from agentkit.orchestrator.store import RunStore, utc_now_iso
from agentkit.orchestrator.types import AutonomyMode, PacingMode, RunState, TaskItem
from agentkit.orchestrator.worktree import WorktreeManager
from agentkit.runner.loaders import Workflow, load_text

MAJOR_STAGE_ORDER = [
    "intake",
    "architecture",
    "decompose",
    "implement_batch",
    "integrate_queue",
    "system_qa",
    "release_plan",
    "cicd_simulation",
]
MAJOR_GATES = {"scope_lock", "integration_start", "release_start"}


class TeamOrchestrator:
    def __init__(
        self,
        *,
        repo_root: Path,
        workflow_name: str,
        workflow: Workflow,
        task: str,
        backend_name: BackendName,
        permissions: PermissionMode,
        autonomy: AutonomyMode,
        keep_worktrees: bool,
        logs_dir: Path,
        state_runs_dir: Path,
        pacing_mode: PacingMode = "realtime",
        interactive_gates: bool = True,
    ) -> None:
        self.repo_root = repo_root
        self.workflow_name = workflow_name
        self.workflow = workflow
        self.task = task
        self.backend_name = backend_name
        self.permissions = permissions
        self.autonomy = autonomy
        self.pacing_mode = pacing_mode
        self.keep_worktrees = keep_worktrees
        self.interactive_gates = interactive_gates
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.state_runs_dir = state_runs_dir
        self.state_runs_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.team_model_name = str((self.workflow.data or {}).get("team_model", "core_v1"))
        self.team_model_path = self.repo_root / "agentkit" / "team_models" / f"{self.team_model_name}.yaml"
        self.scheduler_policy_path = self.repo_root / "agentkit" / "scheduler" / "policies.yaml"
        self.role_pool = load_role_pool(self.team_model_path)
        self.scheduler_policies = load_scheduler_policies(self.scheduler_policy_path)
        self.personas = self._load_personas()
        self.run_store = RunStore(self.state_runs_dir, self.run_id)
        self.worktree_manager = WorktreeManager(
            repo_root=self.repo_root,
            workspace_root=self.repo_root / "agentkit" / "worktrees",
            run_id=self.run_id,
            permissions=self.permissions,
            keep_worktrees=self.keep_worktrees,
        )
        self.role_thread_ids: dict[str, str] = {}
        self.run_state: RunState | None = None
        self.backend = build_backend(
            backend_name=self.backend_name,
            permissions=self.permissions,
            raw_event_log_file=self.logs_dir / f"raw_events_team_{self.run_id}.jsonl"
            if self.backend_name == "codex"
            else None,
        )
        self.backend_lock = threading.Lock()
        self.store_lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.log_file = self.logs_dir / f"run_{self.workflow_name}_{self.run_id}.jsonl"
        self.max_task_retries = int(
            self.scheduler_policies.get("defaults", {}).get("max_task_retries", 2)
        )
        self.max_developer_concurrency = int(
            self.scheduler_policies.get("defaults", {}).get("max_developer_concurrency", 4)
        )
        self.fail_fast = bool(
            self.scheduler_policies.get("defaults", {}).get("fail_fast_on_backend_errors", True)
        )
        self.actions_file = self.run_store.run_dir / "actions.jsonl"
        self.actions_cursor = 0
        self.pending_actions: list[dict[str, Any]] = []
        self.cancel_requested = False

    def run(self) -> int:
        if self.run_state is not None:
            run_state = self.run_state
            run_state.status = "running"
            run_state.updated_at = utc_now_iso()
            run_state.metadata["kind"] = "team_orchestrator"
            run_state.metadata["pacing_mode"] = self.pacing_mode
        else:
            run_state = RunState(
                run_id=self.run_id,
                workflow=self.workflow_name,
                task=self.task,
                autonomy=self.autonomy,
                backend=self.backend_name,
                permissions=self.permissions,
                status="running",
                team_model=self.team_model_name,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
                metadata={"kind": "team_orchestrator", "pacing_mode": self.pacing_mode},
            )
        self.run_state = run_state
        self.run_store.write_run(run_state)

        try:
            self._event("system", "run", "started", details={"task": self.task})
            print("== Team Workflow ==")
            print(f"Run ID: {self.run_id}")
            print(f"Name: {self.workflow.name}")
            print(f"Autonomy: {self.autonomy}")
            print(f"Pacing: {self.pacing_mode}")
            print(f"Backend: {self.backend_name}")
            print()

            intake = self._run_stage(
                stage_id="intake",
                role="po",
                input_payload={"stage": "intake", "task": self.task},
                artifact_name="intake",
            )
            self._maybe_wait_for_step(checkpoint="intake", next_stage="architecture")
            architecture = self._run_stage(
                stage_id="architecture",
                role="principal_engineer",
                input_payload={
                    "stage": "architecture",
                    "task": self.task,
                    "intake": intake,
                },
                artifact_name="architecture",
            )
            self._maybe_wait_for_step(checkpoint="architecture", next_stage="decompose")
            decompose = self._run_stage(
                stage_id="decompose",
                role="po",
                input_payload={
                    "stage": "decompose",
                    "task": self.task,
                    "intake": intake,
                    "architecture": architecture,
                },
                artifact_name="decompose",
            )

            tasks = self._parse_tasks(decompose)
            for task in tasks:
                self._write_task(task)

            if not self._gate("scope_lock", requested_by="po", details={"task_count": len(tasks)}):
                raise RuntimeError("scope lock gate not approved")

            task_results = self._run_developer_and_task_qa(tasks, architecture)
            self._maybe_wait_for_step(checkpoint="implement_batch", next_stage="integration_start")

            if not self._gate("integration_start", requested_by="integrator", details={"ready_tasks": len(task_results)}):
                raise RuntimeError("integration start gate not approved")

            integrated = self._run_integration_queue(task_results)
            self._maybe_wait_for_step(checkpoint="integrate_queue", next_stage="system_qa")
            self._run_stage(
                stage_id="system_qa",
                role="tester",
                input_payload={"stage": "system_qa", "integrated": integrated},
                artifact_name="system_qa",
                require_approve=self.permissions != "read_only",
            )
            self._maybe_wait_for_step(checkpoint="system_qa", next_stage="release_plan")
            release_plan = self._run_stage(
                stage_id="release_plan",
                role="devops",
                input_payload={"stage": "release_plan", "integrated": integrated},
                artifact_name="release_plan",
            )
            self._maybe_wait_for_step(checkpoint="release_plan", next_stage="release_start")

            if not self._gate("release_start", requested_by="cicd", details={"integrated_tasks": len(integrated)}):
                raise RuntimeError("release start gate not approved")

            cicd = self._run_stage(
                stage_id="cicd_simulation",
                role="cicd",
                input_payload={
                    "stage": "cicd_simulation",
                    "release_plan": release_plan,
                    "integrated": integrated,
                },
                artifact_name="cicd",
                require_pipeline_pass=self.permissions != "read_only",
            )
            self._event("cicd", "cicd_simulation", "completed", details=cicd)

            self._set_run_status("completed")
            self._event("system", "run", "completed", details={"run_id": self.run_id})
            self.run_store.write_chat(
                role="system",
                kind="summary",
                content=f"Run {self.run_id} completed successfully.",
                meta={"status": "completed"},
            )
            print(f"✅ Team run complete. Run ID: {self.run_id}")
            print(f"State: {self.run_store.run_dir}")
            return 0
        except Exception as exc:
            if self.run_state is not None:
                self.run_state.metadata["error"] = str(exc)
            final_status = "cancelled" if (self.run_state and self.run_state.status == "cancelled") else "failed"
            self._set_run_status(final_status)
            self._event("system", "run", final_status, error=str(exc))
            self.run_store.write_chat(
                role="system",
                kind="summary",
                content=f"Run {self.run_id} {final_status}: {exc}",
                meta={"status": final_status},
            )
            print(f"Run failed: {exc}")
            return 1
        finally:
            cleanup_errors = self.worktree_manager.cleanup()
            if cleanup_errors:
                self._event(
                    "system",
                    "worktree_cleanup",
                    "failed",
                    details={"errors": cleanup_errors},
                )
            close_fn = getattr(self.backend, "close", None)
            if callable(close_fn):
                close_fn()

    def _load_personas(self) -> dict[str, str]:
        roles = {stage.get("role") for stage in self.workflow.stages if stage.get("role")}
        personas: dict[str, str] = {}
        for role in roles:
            persona_path = self.repo_root / "agentkit" / "personas" / f"{role}.md"
            if not persona_path.exists():
                raise FileNotFoundError(f"Persona not found: {persona_path}")
            personas[role] = load_text(persona_path)
        return personas

    def _run_stage(
        self,
        *,
        stage_id: str,
        role: str,
        input_payload: dict[str, Any],
        artifact_name: str,
        agent_id: str | None = None,
        task_id: str | None = None,
        require_approve: bool = False,
        require_pipeline_pass: bool = False,
    ) -> dict[str, Any]:
        self._sync_control_actions(wait_if_paused=True)
        agent_label = agent_id or f"{role}-1"
        self._event(role, stage_id, "started", agent_id=agent_label, task_id=task_id)
        output = self._call_backend(
            role=role,
            persona=self.personas[role],
            payload=input_payload,
            task_id=task_id,
        )
        self.run_store.write_artifact(
            f"{artifact_name}{'_' + task_id if task_id else ''}",
            output,
        )
        self._append_run_log(
            {
                "timestamp": utc_now_iso(),
                "run_id": self.run_id,
                "stage": stage_id,
                "role": role,
                "agent_id": agent_label,
                "task_id": task_id,
                "input": input_payload,
                "output": output,
            }
        )

        if require_approve:
            verdict = str(output.get("verdict", "approve"))
            if verdict != "approve":
                raise RuntimeError(f"{stage_id} not approved: {output}")
        if require_pipeline_pass:
            pipeline_status = str(output.get("pipeline_status", "")).lower()
            recommendation = str(output.get("deployment_recommendation", "")).lower()
            if pipeline_status != "pass" and recommendation != "proceed":
                raise RuntimeError(f"{stage_id} failed: {output}")

        self._event(
            role,
            stage_id,
            "completed",
            agent_id=agent_label,
            task_id=task_id,
            details=output,
        )
        return output

    def _run_developer_and_task_qa(
        self,
        tasks: list[TaskItem],
        architecture: dict[str, Any],
    ) -> list[TaskItem]:
        task_by_id = {task.id: task for task in tasks}
        completed: set[str] = set()
        failed: set[str] = set()
        in_flight: dict[Any, TaskItem] = {}
        ready_queue: list[TaskItem] = []

        with ThreadPoolExecutor(max_workers=max(1, min(self.max_developer_concurrency, self.role_pool.count_for("developer")))) as pool:
            while len(completed) + len(failed) < len(tasks):
                for task in tasks:
                    if task.id in completed or task.id in failed:
                        continue
                    if task in in_flight.values():
                        continue
                    if all(dep in completed for dep in task.dependencies):
                        if task not in ready_queue:
                            ready_queue.append(task)

                while ready_queue and len(in_flight) < self.max_developer_concurrency:
                    task = ready_queue.pop(0)
                    future = pool.submit(self._execute_task_pipeline, task, architecture)
                    in_flight[future] = task

                if not in_flight:
                    unresolved = [t.id for t in tasks if t.id not in completed and t.id not in failed]
                    raise RuntimeError(
                        f"No runnable tasks left. Dependency cycle or blocked tasks: {unresolved}"
                    )

                done, _pending = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    task = in_flight.pop(future)
                    ok, err = future.result()
                    if ok:
                        completed.add(task.id)
                    else:
                        failed.add(task.id)
                        if self.fail_fast:
                            raise RuntimeError(f"Task {task.id} failed: {err}")
                    task_by_id[task.id] = task

        ready_for_integration = [t for t in task_by_id.values() if t.state == "qa_passed"]
        if not ready_for_integration:
            raise RuntimeError("No tasks passed task-level QA")
        return ready_for_integration

    def _execute_task_pipeline(
        self, task: TaskItem, architecture: dict[str, Any]
    ) -> tuple[bool, str | None]:
        lane = abs(hash(task.id)) % max(1, self.role_pool.count_for("developer")) + 1
        dev_agent = f"developer-{lane}"
        task.assigned_agent = dev_agent
        task.state = "in_progress"
        self._write_task(task)

        assignment = self.worktree_manager.create_for_task(task.id)
        task.branch = assignment.branch
        task.worktree = str(assignment.path)
        self.run_store.write_worktree(
            {
                "task_id": task.id,
                "branch": assignment.branch,
                "path": str(assignment.path),
                "created": assignment.created,
                "error": assignment.error,
            }
        )

        dev_payload = {
            "stage": "implement",
            "task": asdict(task),
            "architecture": architecture,
            "worktree": {
                "branch": assignment.branch,
                "path": str(assignment.path),
                "created": assignment.created,
            },
            "permissions": self.permissions,
        }
        dev_out = self._run_stage(
            stage_id="implement",
            role="developer",
            input_payload=dev_payload,
            artifact_name="implement",
            agent_id=dev_agent,
            task_id=task.id,
        )

        task.details["developer_output"] = dev_out
        task.state = "implemented"
        self._write_task(task)

        qa_agent = "tester-1"
        for retry in range(self.max_task_retries + 1):
            qa_payload = {
                "stage": "qa_task",
                "task": asdict(task),
                "developer_output": task.details.get("developer_output", {}),
                "retry": retry,
            }
            qa_out = self._run_stage(
                stage_id="qa_task",
                role="tester",
                input_payload=qa_payload,
                artifact_name="qa_task",
                agent_id=qa_agent,
                task_id=task.id,
            )
            verdict = str(qa_out.get("verdict", "approve"))
            task.details["qa_output"] = qa_out
            if verdict == "approve":
                task.state = "qa_passed"
                self._write_task(task)
                return True, None

            task.retries += 1
            if self.permissions == "read_only":
                task.state = "qa_passed"
                task.details["qa_warning"] = "requested_changes_in_read_only_mode"
                self._write_task(task)
                return True, None
            if retry >= self.max_task_retries:
                task.state = "qa_failed"
                self._write_task(task)
                return False, "qa failed after max retries"

            revise_payload = {
                "stage": "implement",
                "task": asdict(task),
                "review_findings": qa_out.get("findings", []),
                "recommended_actions": qa_out.get("recommended_actions", []),
                "worktree": {
                    "branch": assignment.branch,
                    "path": str(assignment.path),
                    "created": assignment.created,
                },
                "permissions": self.permissions,
            }
            dev_out = self._run_stage(
                stage_id="implement",
                role="developer",
                input_payload=revise_payload,
                artifact_name="implement_retry",
                agent_id=dev_agent,
                task_id=task.id,
            )
            task.details["developer_output"] = dev_out
            task.state = "implemented"
            self._write_task(task)

        return False, "task pipeline ended unexpectedly"

    def _run_integration_queue(self, ready_tasks: list[TaskItem]) -> list[dict[str, Any]]:
        by_id = {task.id: task for task in ready_tasks}

        def dependency_depth(task: TaskItem, seen: set[str] | None = None) -> int:
            seen = seen or set()
            if task.id in seen:
                return 0
            seen.add(task.id)
            if not task.dependencies:
                return 0
            depths: list[int] = []
            for dep in task.dependencies:
                dep_task = by_id.get(dep)
                if dep_task is None:
                    continue
                depths.append(1 + dependency_depth(dep_task, seen.copy()))
            return max(depths) if depths else 0

        ordered = sorted(ready_tasks, key=lambda task: (dependency_depth(task), task.id))
        integrated: list[dict[str, Any]] = []
        for pos, task in enumerate(ordered, start=1):
            out = self._run_stage(
                stage_id="integrate_queue",
                role="integrator",
                input_payload={
                    "stage": "integrate_queue",
                    "task": asdict(task),
                    "queue_position": pos,
                    "current_integrated": integrated,
                },
                artifact_name="integrate_queue",
                agent_id="integrator-1",
                task_id=task.id,
            )
            conflict = str(out.get("conflict_check", "pass")).lower()
            merge = str(out.get("merge_decision", "approve")).lower()
            if conflict == "fail" or merge == "block":
                if self.permissions == "read_only":
                    task.state = "integrated"
                    task.details["integration_warning"] = out
                    self._write_task(task)
                    integrated.append(
                        {
                            "task_id": task.id,
                            "queue_position": pos,
                            "branch": task.branch,
                            "worktree": task.worktree,
                            "integration_output": out,
                            "warning": "integration_block_ignored_in_read_only_mode",
                        }
                    )
                    continue
                task.state = "blocked"
                self._write_task(task)
                raise RuntimeError(f"Integration blocked for task {task.id}: {out}")
            task.state = "integrated"
            self._write_task(task)
            integrated.append(
                {
                    "task_id": task.id,
                    "queue_position": pos,
                    "branch": task.branch,
                    "worktree": task.worktree,
                    "integration_output": out,
                }
            )
        return integrated

    def _parse_tasks(self, payload: dict[str, Any]) -> list[TaskItem]:
        raw_tasks = payload.get("tasks")
        tasks: list[TaskItem] = []
        if isinstance(raw_tasks, list):
            for idx, raw in enumerate(raw_tasks, start=1):
                if not isinstance(raw, dict):
                    continue
                task_id = str(raw.get("id") or f"T{idx}")
                summary = str(raw.get("summary") or f"Task {idx}")
                acceptance = raw.get("acceptance")
                dependencies = raw.get("dependencies")
                tasks.append(
                    TaskItem(
                        id=task_id,
                        summary=summary,
                        acceptance=[str(x) for x in acceptance] if isinstance(acceptance, list) else [],
                        dependencies=[str(x) for x in dependencies] if isinstance(dependencies, list) else [],
                    )
                )

        if not tasks:
            tasks = [
                TaskItem(
                    id="T1",
                    summary=f"Implement request: {self.task}",
                    acceptance=["User request is addressed"],
                    dependencies=[],
                )
            ]
        return tasks

    def _call_backend(
        self,
        *,
        role: str,
        persona: str,
        payload: dict[str, Any],
        task_id: str | None,
    ) -> dict[str, Any]:
        with self.backend_lock:
            out = self.backend.run_role(
                role=role,
                persona=persona,
                input=payload,
                repo_root=self.repo_root,
                thread_id=self.role_thread_ids.get(role),
            )
            get_thread = getattr(self.backend, "get_thread_id", None)
            if callable(get_thread):
                thread_id = get_thread(role)
                if isinstance(thread_id, str):
                    self.role_thread_ids[role] = thread_id
            return out if isinstance(out, dict) else {"error": "backend returned non-dict output"}

    def _maybe_wait_for_step(self, *, checkpoint: str, next_stage: str) -> None:
        if self.pacing_mode != "step_major":
            return
        if checkpoint not in MAJOR_STAGE_ORDER:
            return
        if next_stage in MAJOR_GATES:
            return
        self._event(
            "system",
            checkpoint,
            "step_waiting",
            details={"checkpoint": checkpoint, "next_stage": next_stage},
        )
        self._wait_for_continue(checkpoint=checkpoint, next_stage=next_stage)
        self._event(
            "system",
            checkpoint,
            "step_continued",
            details={"checkpoint": checkpoint, "next_stage": next_stage},
        )

    def _wait_for_continue(self, *, checkpoint: str, next_stage: str) -> None:
        if self.interactive_gates and sys.stdin.isatty():
            try:
                answer = input(
                    f"Step wait after {checkpoint}. Continue to {next_stage}? [y/N]: "
                ).strip().lower()
            except EOFError:
                answer = "n"
            if answer in {"y", "yes", "c", "continue"}:
                return
            raise RuntimeError(f"step wait rejected at checkpoint {checkpoint}")

        while True:
            self._sync_control_actions(wait_if_paused=True)
            for action in self._drain_pending_actions():
                action_name = str(action.get("action", "")).lower()
                if action_name == "continue":
                    return
                if action_name == "cancel":
                    raise RuntimeError("run cancelled by user")
            time.sleep(0.5)

    def _gate(self, gate: str, *, requested_by: str, details: dict[str, Any]) -> bool:
        self._sync_control_actions(wait_if_paused=True)
        requires_prompt = False
        if self.autonomy == "human_in_loop":
            requires_prompt = True
        elif self.autonomy == "mixed":
            required = set(
                self.scheduler_policies.get("gates", {}).get("mixed_mode_mandatory", [])
            )
            requires_prompt = gate in required

        self._event(
            requested_by,
            gate,
            "gate_requested",
            details=details,
        )

        if not requires_prompt:
            self._event(requested_by, gate, "gate_approved", details={"mode": self.autonomy})
            return True

        answer = self._wait_for_gate_decision(gate, requested_by, details)
        if answer:
            self._event(requested_by, gate, "gate_approved", details={"mode": self.autonomy})
            return True
        self._event(requested_by, gate, "gate_rejected", details={"mode": self.autonomy})
        return False

    def _prompt_for_gate(self, gate: str, requested_by: str, details: dict[str, Any]) -> bool:
        print(f"Gate approval required: {gate} (requested by {requested_by})")
        print(json.dumps(details, indent=2))
        try:
            answer = input("Approve gate? [y/N]: ").strip().lower()
        except EOFError:
            answer = "n"
        return answer in {"y", "yes"}

    def _wait_for_gate_decision(
        self,
        gate: str,
        requested_by: str,
        details: dict[str, Any],
    ) -> bool:
        if self.interactive_gates and sys.stdin.isatty():
            return self._prompt_for_gate(gate, requested_by, details)

        self._event(
            requested_by,
            gate,
            "gate_waiting",
            details=details,
        )
        while True:
            self._sync_control_actions(wait_if_paused=True)
            for action in self._drain_pending_actions():
                action_name = str(action.get("action", "")).lower()
                meta = action.get("meta", {})
                meta_gate = (
                    str(meta.get("gate", "")).strip()
                    if isinstance(meta, dict)
                    else ""
                )
                if meta_gate and meta_gate != gate:
                    continue
                if action_name == "approve":
                    return True
                if action_name == "reject":
                    return False
                if action_name == "cancel":
                    raise RuntimeError("run cancelled by user")
            time.sleep(1.0)

    def _set_run_status(self, status: str) -> None:
        if self.run_state is None:
            return
        if self.run_state.status == status:
            return
        self.run_state.status = status  # type: ignore[assignment]
        self.run_state.updated_at = utc_now_iso()
        self.run_store.write_run(self.run_state)

    def _read_new_actions(self) -> list[dict[str, Any]]:
        with self.action_lock:
            if not self.actions_file.exists():
                return []
            lines = self.actions_file.read_text(encoding="utf-8").splitlines()
            if self.actions_cursor >= len(lines):
                return []
            out: list[dict[str, Any]] = []
            for raw in lines[self.actions_cursor :]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    out.append(payload)
            self.actions_cursor = len(lines)
            return out

    def _sync_control_actions(self, *, wait_if_paused: bool) -> None:
        paused = self.run_state is not None and self.run_state.status == "paused"
        while True:
            progressed = False
            for action in self._read_new_actions():
                action_name = str(action.get("action", "")).lower()
                if action_name == "pause":
                    paused = True
                    self._set_run_status("paused")
                    self._event("system", "run", "paused", details={"source": "action"})
                    progressed = True
                    continue
                if action_name == "resume":
                    paused = False
                    self._set_run_status("running")
                    self._event("system", "run", "resumed", details={"source": "action"})
                    progressed = True
                    continue
                if action_name == "cancel":
                    self.cancel_requested = True
                    self._set_run_status("cancelled")
                    self._event("system", "run", "cancelled", details={"source": "action"})
                    raise RuntimeError("run cancelled by user")
                self._enqueue_pending_action(action)
            if paused and wait_if_paused:
                time.sleep(1.0)
                continue
            if not progressed:
                return

    def _drain_pending_actions(self) -> list[dict[str, Any]]:
        with self.action_lock:
            out = list(self.pending_actions)
            self.pending_actions = []
            return out

    def _enqueue_pending_action(self, action: dict[str, Any]) -> None:
        with self.action_lock:
            self.pending_actions.append(action)

    def _event_message(
        self,
        *,
        role: str,
        stage: str,
        state: str,
        task_id: str | None,
        details: dict[str, Any] | None,
        error: str | None,
    ) -> tuple[str, str]:
        if error:
            return "message", f"{role} reported an error at {stage}: {error}"
        if stage == "run" and state == "completed":
            return "summary", "Run completed."
        if stage == "run" and state in {"failed", "cancelled"}:
            return "summary", f"Run {state}."
        if state == "gate_requested":
            return "approval", f"Approval requested: {stage}"
        if state == "started":
            suffix = f" (task {task_id})" if task_id else ""
            return "message", f"{role} started {stage}{suffix}."
        if state == "completed":
            suffix = f" (task {task_id})" if task_id else ""
            return "message", f"{role} completed {stage}{suffix}."
        if state == "failed":
            return "message", f"{role} failed {stage}."
        if state in {"paused", "resumed", "cancelled"}:
            return "message", f"Run {state}."
        if state in {"gate_approved", "gate_rejected"}:
            verdict = "approved" if state == "gate_approved" else "rejected"
            return "message", f"Gate {stage} {verdict}."
        if state == "gate_waiting":
            return "approval", f"Waiting for approval on gate {stage}."
        if state == "step_waiting":
            target = ""
            if isinstance(details, dict):
                target = str(details.get("next_stage", "")).strip()
            msg = (
                f"Waiting for Continue: {target}"
                if target
                else f"Waiting for Continue after {stage}"
            )
            return "step_wait", msg
        if state == "step_continued":
            return "message", f"Continued from {stage}."
        if details and "message" in details:
            return "message", str(details["message"])
        return "message", f"{role} {state} at {stage}."

    def _append_run_log(self, payload: dict[str, Any]) -> None:
        with self.store_lock:
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _event(
        self,
        role: str,
        stage: str,
        state: str,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.store_lock:
            self.run_store.write_event(
                role=role,
                stage=stage,
                state=state,
                agent_id=agent_id,
                task_id=task_id,
                details=details,
                error=error,
            )
            kind, content = self._event_message(
                role=role,
                stage=stage,
                state=state,
                task_id=task_id,
                details=details,
                error=error,
            )
            self.run_store.write_chat(
                role="orchestrator",
                kind=kind,
                content=content,
                meta={
                    "stage": stage,
                    "state": state,
                    "role": role,
                    "task_id": task_id,
                    "details": details or {},
                },
            )

    def _write_task(self, task: TaskItem) -> None:
        with self.store_lock:
            self.run_store.write_task(task)
