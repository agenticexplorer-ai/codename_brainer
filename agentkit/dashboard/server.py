from __future__ import annotations

import errno
import json
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agentkit.orchestrator.store import (
    append_chat_message,
    list_runs,
    load_run,
    read_jsonl,
    utc_now_iso,
)
from agentkit.orchestrator.team_runner import TeamOrchestrator
from agentkit.orchestrator.types import RunState
from agentkit.runner.loaders import load_workflow

VALID_BACKENDS: set[str] = {"stub", "codex"}
VALID_PERMISSIONS: set[str] = {"read_only", "write_safe"}
VALID_AUTONOMY: set[str] = {"full_auto", "mixed", "human_in_loop"}
VALID_PACING: set[str] = {"realtime", "step_major"}


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError) and exc.errno in {
            errno.ECONNRESET,
            errno.EPIPE,
            errno.ECONNABORTED,
        }:
            return
        super().handle_error(request, client_address)


class DashboardRuntime:
    def __init__(
        self,
        *,
        repo_root: Path,
        state_root: Path,
        logs_dir: Path,
        default_run_id: str | None,
    ) -> None:
        self.repo_root = repo_root
        self.state_root = state_root
        self.logs_dir = logs_dir
        self.default_run_id = default_run_id
        self._lock = threading.Lock()
        self._active_run_id = default_run_id
        self._jobs: dict[str, threading.Thread] = {}

    def start_chat_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message", "")).strip()
        if not message:
            raise ValueError("'message' is required")

        workflow_name = str(payload.get("workflow", "team_factory_v1")).strip() or "team_factory_v1"
        backend = str(payload.get("backend", "stub")).strip() or "stub"
        permissions = str(payload.get("permissions", "read_only")).strip() or "read_only"
        autonomy = str(payload.get("autonomy", "mixed")).strip() or "mixed"
        pacing_mode = str(payload.get("pacing_mode", "realtime")).strip() or "realtime"
        keep_worktrees = bool(payload.get("keep_worktrees", False))

        if backend not in VALID_BACKENDS:
            raise ValueError(f"Invalid backend: {backend}")
        if permissions not in VALID_PERMISSIONS:
            raise ValueError(f"Invalid permissions: {permissions}")
        if autonomy not in VALID_AUTONOMY:
            raise ValueError(f"Invalid autonomy: {autonomy}")
        if pacing_mode not in VALID_PACING:
            raise ValueError(f"Invalid pacing_mode: {pacing_mode}")

        wf_path = self.repo_root / "agentkit" / "workflows" / f"{workflow_name}.yaml"
        if not wf_path.exists():
            raise FileNotFoundError(f"Workflow not found: {wf_path}")
        workflow = load_workflow(wf_path)
        if workflow.kind != "team_orchestrator":
            raise ValueError(
                f"Workflow '{workflow_name}' is not chat-start compatible. "
                "Use a team_orchestrator workflow."
            )

        orchestrator = TeamOrchestrator(
            repo_root=self.repo_root,
            workflow_name=workflow_name,
            workflow=workflow,
            task=message,
            backend_name=backend,  # type: ignore[arg-type]
            permissions=permissions,  # type: ignore[arg-type]
            autonomy=autonomy,  # type: ignore[arg-type]
            pacing_mode=pacing_mode,  # type: ignore[arg-type]
            keep_worktrees=keep_worktrees,
            logs_dir=self.logs_dir,
            state_runs_dir=self.state_root,
            interactive_gates=False,
        )
        run_id = orchestrator.run_id

        now = utc_now_iso()
        run_state = RunState(
            run_id=run_id,
            workflow=workflow_name,
            task=message,
            autonomy=autonomy,  # type: ignore[arg-type]
            backend=backend,  # type: ignore[arg-type]
            permissions=permissions,  # type: ignore[arg-type]
            status="running",
            team_model=orchestrator.team_model_name,
            created_at=now,
            updated_at=now,
            metadata={
                "kind": "team_orchestrator",
                "source": "dashboard_chat",
                "pacing_mode": pacing_mode,
            },
        )
        orchestrator.run_state = run_state
        orchestrator.run_store.write_run(run_state)
        orchestrator.run_store.write_chat(
            role="user",
            kind="message",
            content=message,
            meta={"source": "chat_start"},
        )
        orchestrator.run_store.write_chat(
            role="system",
            kind="message",
            content=(
                f"Run {run_id} started with workflow={workflow_name}, "
                f"backend={backend}, permissions={permissions}, autonomy={autonomy}, "
                f"pacing_mode={pacing_mode}."
            ),
            meta={
                "workflow": workflow_name,
                "backend": backend,
                "permissions": permissions,
                "autonomy": autonomy,
                "pacing_mode": pacing_mode,
            },
        )

        def _run_job() -> None:
            try:
                orchestrator.run()
            finally:
                with self._lock:
                    self._jobs.pop(run_id, None)

        thread = threading.Thread(target=_run_job, name=f"agentkit-run-{run_id}", daemon=True)
        with self._lock:
            self._jobs[run_id] = thread
            self._active_run_id = run_id
        thread.start()

        return {"ok": True, "run_id": run_id, "status": "running"}

    def append_chat(self, run_id: str, message: str, *, role: str = "user") -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise ValueError("'message' is required")
        append_chat_message(
            self.state_root,
            run_id,
            role=role,
            content=text,
            kind="message",
            meta={"source": "chat_message"},
        )
        return {"ok": True, "run_id": run_id}

    def append_action(self, run_id: str, action: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        run_dir = self.state_root / run_id
        run_file = run_dir / "run.json"
        if not run_file.exists():
            raise FileNotFoundError(f"Run not found: {run_id}")
        payload = {
            "timestamp": datetime.now(timezone.utc).timestamp(),
            "run_id": run_id,
            "action": action,
            "meta": meta or {},
        }
        with (run_dir / "actions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return {"ok": True, "action": action, "run_id": run_id}

    def resolve_active_run(self) -> dict[str, Any] | None:
        runs = list_runs(self.state_root)
        if not runs:
            return None

        runs_sorted = sorted(runs, key=lambda item: str(item.get("created_at", "")), reverse=True)
        running = [run for run in runs_sorted if str(run.get("status", "")) == "running"]

        with self._lock:
            preferred = self._active_run_id

        if preferred:
            for run in runs_sorted:
                if str(run.get("run_id", "")) == preferred:
                    if str(run.get("status", "")) == "running":
                        return run
                    break

        if running:
            chosen = running[0]
            with self._lock:
                self._active_run_id = str(chosen.get("run_id", ""))
            return chosen

        chosen = runs_sorted[0]
        with self._lock:
            self._active_run_id = str(chosen.get("run_id", ""))
        return chosen

    def resolve_active_run_id(self) -> str | None:
        run = self.resolve_active_run()
        if run is None:
            return None
        return str(run.get("run_id", ""))

    def run_payload(self, run_id: str) -> dict[str, Any]:
        run = load_run(self.state_root, run_id)
        run_dir = self.state_root / run_id
        events = read_jsonl(run_dir / "events.jsonl")
        run["events"] = events
        run["tasks"] = read_jsonl(run_dir / "tasks.jsonl")
        run["worktrees"] = read_jsonl(run_dir / "worktrees.jsonl")
        run["chat"] = read_jsonl(run_dir / "chat.jsonl")
        run["ui_state"] = self._derive_ui_state(events, run)
        return run

    def _derive_ui_state(self, events: list[dict[str, Any]], run: dict[str, Any]) -> dict[str, Any]:
        pending_gate: str | None = None
        step_waiting = False
        next_stage: str | None = None

        for event in events:
            stage = str(event.get("stage", "")).strip()
            state = str(event.get("state", "")).strip()
            details = event.get("details", {})
            details_obj = details if isinstance(details, dict) else {}

            if state in {"gate_requested", "gate_waiting"} and stage:
                pending_gate = stage
            elif state in {"gate_approved", "gate_rejected"} and stage and pending_gate == stage:
                pending_gate = None

            if state == "step_waiting":
                step_waiting = True
                target = str(details_obj.get("next_stage", "")).strip()
                next_stage = target or None
            elif state == "step_continued":
                step_waiting = False
                next_stage = None

            if stage == "run" and state in {"completed", "failed", "cancelled"}:
                step_waiting = False
                next_stage = None
                pending_gate = None

        if str(run.get("status", "")) != "running":
            step_waiting = False
            pending_gate = None
            next_stage = None

        return {
            "pending_gate": pending_gate,
            "step_waiting": step_waiting,
            "next_stage": next_stage,
        }


class DashboardHandler(BaseHTTPRequestHandler):
    runtime: DashboardRuntime

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(self._index_html())
            return
        if path == "/api/runs":
            self._send_json(list_runs(self.runtime.state_root))
            return
        if path == "/api/runs/active":
            active = self.runtime.resolve_active_run()
            if active is None:
                self._send_json({"run": None})
                return
            run_id = str(active.get("run_id", ""))
            self._send_json({"run": self.runtime.run_payload(run_id)})
            return
        if path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = path.split("/")[3]
            self._stream_events(run_id)
            return
        if path.startswith("/api/runs/"):
            run_id = path.split("/")[3]
            try:
                self._send_json(self.runtime.run_payload(run_id))
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND, "Run not found")
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/chat/start":
            body = self._read_json_body()
            if body is None:
                return
            try:
                out = self.runtime.start_chat_run(body)
            except (FileNotFoundError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json(out)
            return

        if path == "/api/chat/message":
            body = self._read_json_body()
            if body is None:
                return
            run_id = str(body.get("run_id", "")).strip() or (self.runtime.resolve_active_run_id() or "")
            message = str(body.get("message", ""))
            if not run_id:
                self._send_json({"ok": False, "error": "No active run available"}, status=400)
                return
            try:
                out = self.runtime.append_chat(run_id, message, role="user")
            except (FileNotFoundError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json(out)
            return

        if not path.startswith("/api/runs/") or "/actions/" not in path:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        parts = path.split("/")
        if len(parts) < 6:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid action path")
            return
        run_id = parts[3]
        action = parts[5]
        if action not in {"pause", "resume", "cancel", "approve", "reject", "continue"}:
            self.send_error(HTTPStatus.BAD_REQUEST, "Unsupported action")
            return

        body = self._read_json_body(allow_empty=True)
        if body is None:
            return
        try:
            out = self.runtime.append_action(run_id, action, meta=body.get("meta") if isinstance(body, dict) else None)
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=404)
            return
        self._send_json(out)

    def _read_json_body(self, allow_empty: bool = False) -> dict[str, Any] | None:
        raw_length = self.headers.get("Content-Length", "0").strip()
        try:
            length = int(raw_length)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        if length <= 0:
            if allow_empty:
                return {}
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected JSON body")
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return None
        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
            return None
        return payload

    def _send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str, status: int = 200) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_events(self, run_id: str) -> None:
        events_file = self.runtime.state_root / run_id / "events.jsonl"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        offset = 0
        try:
            while True:
                if events_file.exists():
                    lines = events_file.read_text(encoding="utf-8").splitlines()
                    while offset < len(lines):
                        raw = lines[offset].strip()
                        offset += 1
                        if not raw:
                            continue
                        self.wfile.write(f"data: {raw}\\n\\n".encode("utf-8"))
                        self.wfile.flush()
                self.wfile.write(b": ping\\n\\n")
                self.wfile.flush()
                time.sleep(1.0)
        except (ConnectionError, BrokenPipeError):
            return

    def _index_html(self) -> str:
        return """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>AgentKit Chat Dashboard</title>
  <style>
    :root {
      --bg: #f6efe3;
      --panel: #fffaf2;
      --ink: #15252a;
      --muted: #476067;
      --line: #d8c8ad;
      --accent: #0d7a66;
      --warn: #b45309;
      --bad: #a61b1b;
      --good: #166534;
      --shadow: 0 10px 24px rgba(14, 32, 39, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: \"IBM Plex Sans\", \"Segoe UI\", sans-serif;
      height: 100vh;
      overflow: hidden;
      background:
        radial-gradient(circle at 15% 10%, #ffe5bc 0%, transparent 35%),
        radial-gradient(circle at 90% 5%, #d8f3ed 0%, transparent 28%),
        var(--bg);
    }
    .app {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 16px;
      max-width: 1400px;
      margin: 0 auto;
      padding: 16px;
      height: 100vh;
      min-height: 100vh;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
    }
    .chat-wrap {
      display: flex;
      flex-direction: column;
      height: calc(100vh - 32px);
      min-height: 0;
      overflow: hidden;
    }
    .chat-head {
      padding: 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .brand h1 { margin: 0; font-size: 20px; }
    .brand p { margin: 2px 0 0 0; color: var(--muted); font-size: 12px; }
    .active-chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      background: #fff;
      color: var(--muted);
    }
    .chip-row {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .subtle {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .chat-log {
      flex: 1;
      min-height: 0;
      padding: 12px;
      overflow: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .msg {
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      max-width: 88%;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 14px;
      line-height: 1.35;
      background: #fff;
    }
    .msg.user { align-self: flex-end; border-color: #9ac7bd; background: #e9f7f3; }
    .msg.system { align-self: flex-start; border-color: #c8bfd8; background: #f4f0ff; }
    .msg.orchestrator { align-self: flex-start; }
    .msg .meta { color: var(--muted); font-size: 11px; margin-bottom: 6px; }
    .approval {
      display: flex;
      flex-direction: column;
      gap: 8px;
      border-left: 4px solid var(--warn);
      padding-left: 8px;
    }
    .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
    button {
      border: 1px solid var(--accent);
      color: var(--accent);
      background: transparent;
      border-radius: 8px;
      padding: 7px 10px;
      cursor: pointer;
      font-weight: 600;
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    button.primary { background: var(--accent); color: #fff; }
    button.warn { border-color: var(--warn); color: var(--warn); }
    button.bad { border-color: var(--bad); color: var(--bad); }
    button.info { border-color: #0f4c81; color: #0f4c81; }
    button.ghost { border-color: var(--line); color: var(--muted); }
    .composer {
      padding: 12px;
      border-top: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      bottom: 0;
      z-index: 2;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .composer textarea {
      width: 100%;
      min-height: 88px;
      border-radius: 10px;
      border: 1px solid var(--line);
      padding: 10px;
      font: inherit;
      resize: vertical;
      background: #fff;
    }
    .composer-row {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .advanced summary {
      cursor: pointer;
      color: var(--muted);
      font-size: 13px;
    }
    .advanced-grid {
      margin-top: 8px;
      display: grid;
      grid-template-columns: repeat(2, minmax(160px, 1fr));
      gap: 8px;
    }
    label { font-size: 12px; color: var(--muted); display: block; }
    select, input[type=checkbox] {
      margin-top: 4px;
    }
    select {
      width: 100%;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #fff;
      padding: 6px 8px;
    }
    .side {
      display: grid;
      grid-template-rows: auto auto 1fr;
      gap: 12px;
      height: calc(100vh - 32px);
      min-height: 0;
      overflow: auto;
    }
    .section { padding: 12px; }
    .section h3 { margin: 0 0 8px 0; font-size: 15px; }
    .kv { font-size: 13px; line-height: 1.5; color: var(--muted); }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
    }
    .task-columns {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .task-col {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px;
      min-height: 74px;
      background: #fff;
    }
    .task-col h4 { margin: 0 0 6px 0; font-size: 12px; color: var(--muted); text-transform: uppercase; }
    .pill {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 7px;
      margin: 2px 2px 0 0;
      font-size: 11px;
      background: #fff;
    }
    .status-badge {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      background: #fff;
      margin-left: 6px;
    }
    .status-approved { border-color: #5ca57c; color: #166534; }
    .status-rejected { border-color: #d06a6a; color: #a61b1b; }
    .status-continued { border-color: #4f8cc7; color: #0f4c81; }
    .status-sending { border-color: var(--warn); color: var(--warn); }
    .status-superseded { border-color: var(--line); color: var(--muted); }
    .task-item {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 8px;
      margin-bottom: 6px;
    }
    .task-item:last-child { margin-bottom: 0; }
    .task-item .title {
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 4px;
      color: var(--ink);
    }
    .task-item .meta-line {
      font-size: 11px;
      color: var(--muted);
      line-height: 1.35;
      margin-top: 2px;
    }
    .worktree-list {
      display: grid;
      gap: 8px;
    }
    .worktree-item {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 8px;
    }
    .worktree-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
    }
    .worktree-path {
      display: flex;
      gap: 6px;
      align-items: center;
      font-size: 11px;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .legend {
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 8px;
      line-height: 1.35;
    }
    .mono {
      font-family: \"IBM Plex Mono\", \"SFMono-Regular\", Menlo, monospace;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .error { color: var(--bad); font-size: 12px; }
    .good { color: var(--good); }
    .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
    @media (max-width: 1100px) {
      body { height: auto; overflow: auto; }
      .app { grid-template-columns: 1fr; }
      .chat-wrap, .side { height: auto; min-height: 0; }
    }
  </style>
</head>
<body>
  <div class=\"app\">
    <section class=\"panel chat-wrap\">
      <div class=\"chat-head\">
        <div class=\"brand\">
          <h1>AgentKit</h1>
          <p>Chat-first orchestrator control surface</p>
        </div>
        <div class=\"chip-row\">
          <span id=\"activeChip\" class=\"active-chip\">No active run</span>
          <button class=\"primary\" id=\"newChatTopBtn\">New Chat</button>
        </div>
      </div>
      <div id=\"chatLog\" class=\"chat-log\"></div>
      <div class=\"composer\">
        <textarea id=\"promptInput\" placeholder=\"Describe what you want to build...\"></textarea>
        <details class=\"advanced\">
          <summary>Advanced settings</summary>
          <div class=\"advanced-grid\">
            <div>
              <label>Workflow
                <select id=\"workflowSel\">
                  <option value=\"team_factory_v1\" selected>team_factory_v1</option>
                </select>
              </label>
            </div>
            <div>
              <label>Backend
                <select id=\"backendSel\">
                  <option value=\"stub\" selected>stub</option>
                  <option value=\"codex\">codex</option>
                </select>
              </label>
            </div>
            <div>
              <label>Permissions
                <select id=\"permissionsSel\">
                  <option value=\"read_only\" selected>read_only</option>
                  <option value=\"write_safe\">write_safe</option>
                </select>
              </label>
            </div>
            <div>
              <label>Autonomy
                <select id=\"autonomySel\">
                  <option value=\"mixed\" selected>mixed</option>
                  <option value=\"full_auto\">full_auto</option>
                  <option value=\"human_in_loop\">human_in_loop</option>
                </select>
              </label>
            </div>
            <div>
              <label>Pacing
                <select id=\"pacingSel\">
                  <option value=\"realtime\" selected>realtime</option>
                  <option value=\"step_major\">step_major</option>
                </select>
              </label>
            </div>
            <div>
              <label><input type=\"checkbox\" id=\"keepWorktrees\" /> Keep worktrees</label>
            </div>
          </div>
        </details>
        <div class=\"composer-row\">
          <button class=\"primary\" id=\"newChatBtn\">Start New Chat</button>
          <button class=\"ghost\" id=\"noteBtn\">Send Note</button>
          <span id=\"composerStatus\" class=\"mono\"></span>
        </div>
        <div id=\"composerError\" class=\"error\"></div>
      </div>
    </section>

    <aside class=\"side\">
      <section class=\"panel section\">
        <div class=\"section-head\">
          <h3>Now Working</h3>
          <span id=\"modeChip\" class=\"status-badge\">Mode: realtime</span>
        </div>
        <div id=\"nowWorking\" class=\"kv\">No run selected.</div>
        <div class=\"controls\">
          <button id=\"pauseBtn\" class=\"warn\">Pause</button>
          <button id=\"resumeBtn\">Resume</button>
          <button id=\"continueBtn\" class=\"info\">Continue</button>
          <button id=\"cancelBtn\" class=\"bad\">Cancel</button>
        </div>
        <div style=\"margin-top:8px;\">
          <label>Run switcher
            <select id=\"runSwitch\"></select>
          </label>
        </div>
      </section>

      <section class=\"panel section\">
        <h3>Task Board</h3>
        <div class=\"legend\">
          Queued = ready, In Progress = developer active, QA = task-level approval, Integration = merge queue, Done = final, Blocked = requires intervention.
        </div>
        <div id=\"taskBoard\" class=\"task-columns\"></div>
      </section>

      <section class=\"panel section\">
        <h3>Worktrees</h3>
        <div class=\"subtle\" style=\"margin-bottom:8px;\">Task-to-branch mapping and worktree health for each lane.</div>
        <div id=\"worktreeMap\" class=\"worktree-list\">No worktree data.</div>
      </section>
    </aside>
  </div>

<script>
let activeRun = null;
let activeRunId = null;
let eventSource = null;
let refreshTimer = null;
let pendingCardActions = new Map();

const gateActions = new Set(["scope_lock", "integration_start", "release_start"]);
const taskColumns = [
  { key: "queued", label: "Queued", desc: "Ready / not started" },
  { key: "in_progress", label: "In Progress", desc: "Developer active" },
  { key: "qa", label: "QA", desc: "Task-level tester checks" },
  { key: "integration", label: "Integration", desc: "Merge queue" },
  { key: "done", label: "Done", desc: "Completed" },
  { key: "blocked", label: "Blocked", desc: "Needs intervention" },
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { ok: false, error: text || "Invalid JSON response" };
  }
  if (!response.ok) {
    const msg = data && data.error ? data.error : `Request failed (${response.status})`;
    throw new Error(msg);
  }
  return data;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function tsLabel(rawTs) {
  if (!rawTs) return "";
  const d = new Date(rawTs);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString();
}

function setStatus(text) {
  document.getElementById("composerStatus").textContent = text || "";
}

function setError(text) {
  document.getElementById("composerError").textContent = text || "";
}

function pacingLabel(mode) {
  return mode === "step_major" ? "Step (Major Stages)" : "Realtime";
}

function getRunPacingMode(run) {
  if (!run || !run.metadata) return "realtime";
  return run.metadata.pacing_mode || "realtime";
}

function activeChipText() {
  if (!activeRun) return "No active run";
  return `${activeRun.run_id} | ${activeRun.status} | ${activeRun.workflow}`;
}

function updateControls() {
  const status = activeRun ? String(activeRun.status || "") : "";
  const running = status === "running";
  const paused = status === "paused";
  const uiState = activeRun && activeRun.ui_state ? activeRun.ui_state : {};
  const stepWaiting = Boolean(uiState && uiState.step_waiting);

  const noteBtn = document.getElementById("noteBtn");
  const pauseBtn = document.getElementById("pauseBtn");
  const resumeBtn = document.getElementById("resumeBtn");
  const continueBtn = document.getElementById("continueBtn");
  const cancelBtn = document.getElementById("cancelBtn");

  noteBtn.disabled = !running;
  pauseBtn.disabled = !running;
  resumeBtn.disabled = !paused;
  continueBtn.disabled = !(running && stepWaiting);
  cancelBtn.disabled = !(running || paused);
}

function renderNowWorking() {
  const node = document.getElementById("nowWorking");
  document.getElementById("activeChip").textContent = activeChipText();
  const modeChip = document.getElementById("modeChip");
  modeChip.textContent = `Mode: ${pacingLabel(getRunPacingMode(activeRun))}`;
  if (!activeRun) {
    node.textContent = "No run selected.";
    return;
  }
  const events = Array.isArray(activeRun.events) ? activeRun.events : [];
  const latest = events.length ? events[events.length - 1] : null;
  const uiState = activeRun.ui_state || {};
  const pendingGate = uiState.pending_gate || "-";
  const nextStage = uiState.next_stage || "-";
  const lines = [
    `status: ${activeRun.status || "-"}`,
    `workflow: ${activeRun.workflow || "-"}`,
    `backend: ${activeRun.backend || "-"}`,
    `permissions: ${activeRun.permissions || "-"}`,
    `autonomy: ${activeRun.autonomy || "-"}`,
    `pacing: ${getRunPacingMode(activeRun)}`,
    `stage: ${latest ? latest.stage : "-"}`,
    `persona: ${latest ? latest.role : "-"}`,
    `task: ${latest && latest.task_id ? latest.task_id : "-"}`,
    `pending_gate: ${pendingGate}`,
    `step_waiting: ${uiState.step_waiting ? "yes" : "no"}`,
    `next_stage: ${nextStage}`,
  ];
  node.innerHTML = lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("");
}

function taskBucket(state) {
  const mapState = {
    queued: "queued",
    in_progress: "in_progress",
    implemented: "in_progress",
    qa_passed: "qa",
    qa_failed: "blocked",
    integrated: "integration",
    done: "done",
    blocked: "blocked",
  };
  return mapState[String(state || "")] || "queued";
}

function buildTaskSnapshot(tasks) {
  const byId = new Map();
  for (const task of tasks || []) {
    if (!task || !task.task_id) continue;
    byId.set(task.task_id, task);
  }
  return [...byId.values()].sort((a, b) => String(a.task_id).localeCompare(String(b.task_id)));
}

function renderTaskCard(task) {
  const deps = Array.isArray(task.dependencies) ? task.dependencies : [];
  const depBadges = deps.length
    ? deps.map((dep) => `<span class=\"pill\">dep:${escapeHtml(dep)}</span>`).join("")
    : '<span class=\"pill\">dep:none</span>';
  const assigned = task.assigned_agent || "unassigned";
  const retries = Number(task.retries || 0);
  const summary = task.summary || "";
  return `
    <div class=\"task-item\">
      <div class=\"title\"><span class=\"pill\">${escapeHtml(task.task_id || "?")}</span> ${escapeHtml(summary)}</div>
      <div class=\"meta-line\">agent: ${escapeHtml(assigned)} | retries: ${retries}</div>
      <div class=\"meta-line\">${depBadges}</div>
    </div>
  `;
}

function renderTaskBoard() {
  const tasks = buildTaskSnapshot(activeRun ? activeRun.tasks : []);
  const board = document.getElementById("taskBoard");
  board.innerHTML = taskColumns
    .map((column) => {
      const bucketTasks = tasks.filter((task) => taskBucket(task.state) === column.key);
      const cards = bucketTasks.map((task) => renderTaskCard(task)).join("");
      return `
        <div class=\"task-col\">
          <h4>${escapeHtml(column.label)} <span class=\"pill\">${bucketTasks.length}</span></h4>
          <div class=\"subtle\" style=\"margin-bottom:6px;\">${escapeHtml(column.desc)}</div>
          ${cards || "<div class='subtle'>none</div>"}
        </div>
      `;
    })
    .join("");
}

function worktreeStatusInfo(item) {
  if (item && item.error) {
    return { label: "error", cls: "status-rejected" };
  }
  if (item && item.created) {
    return { label: "created", cls: "status-approved" };
  }
  return { label: "not-created", cls: "status-superseded" };
}

function renderWorktrees() {
  const node = document.getElementById("worktreeMap");
  if (!activeRun || !Array.isArray(activeRun.worktrees) || activeRun.worktrees.length === 0) {
    node.innerHTML = "<div class='subtle'>No worktree data.</div>";
    return;
  }
  const latestByTask = new Map();
  for (const item of activeRun.worktrees) {
    if (!item || !item.task_id) continue;
    latestByTask.set(item.task_id, item);
  }
  const rows = [...latestByTask.values()]
    .sort((a, b) => String(a.task_id).localeCompare(String(b.task_id)))
    .map((wt) => {
      const status = worktreeStatusInfo(wt);
      const path = wt.path || "-";
      return `
        <div class=\"worktree-item\">
          <div class=\"worktree-header\">
            <div>
              <span class=\"pill\">task:${escapeHtml(wt.task_id || "?")}</span>
              <span class=\"pill\">branch:${escapeHtml(wt.branch || "-")}</span>
            </div>
            <span class=\"status-badge ${status.cls}\">${escapeHtml(status.label)}</span>
          </div>
          <div class=\"worktree-path\">
            <span>path: ${escapeHtml(path)}</span>
            <button class=\"ghost\" data-copy=\"${escapeHtml(path)}\">Copy</button>
          </div>
          ${wt.error ? `<div class=\"error\" style=\"margin-top:6px;\">${escapeHtml(String(wt.error))}</div>` : ""}
        </div>
      `;
    })
    .join("");
  node.innerHTML = rows;
}

function gateMetaFromEntry(entry) {
  const meta = entry && entry.meta ? entry.meta : {};
  const stage = meta.stage || "";
  const state = meta.state || "";
  if (entry.kind !== "approval") return null;
  if (!gateActions.has(stage)) return null;
  if (state !== "gate_requested" && state !== "gate_waiting") return null;
  return { stage };
}

function stepMetaFromEntry(entry) {
  const meta = entry && entry.meta ? entry.meta : {};
  const state = meta.state || "";
  const details = meta.details || {};
  const checkpoint = details.checkpoint || meta.stage || "";
  if (entry.kind !== "step_wait") return null;
  if (state !== "step_waiting") return null;
  if (!checkpoint) return null;
  return { checkpoint, nextStage: details.next_stage || "" };
}

function entryActionInfo(entry) {
  const gate = gateMetaFromEntry(entry);
  if (gate) {
    return { key: `gate:${gate.stage}`, type: "gate", stage: gate.stage };
  }
  const step = stepMetaFromEntry(entry);
  if (step) {
    return {
      key: `step:${step.checkpoint}`,
      type: "step",
      checkpoint: step.checkpoint,
      nextStage: step.nextStage,
    };
  }
  return null;
}

function buildResolutionMap(events) {
  const out = new Map();
  for (const event of events || []) {
    const stage = String(event.stage || "").trim();
    const state = String(event.state || "").trim();
    const details = event.details && typeof event.details === "object" ? event.details : {};
    if (state === "gate_approved") {
      out.set(`gate:${stage}`, "approved");
    } else if (state === "gate_rejected") {
      out.set(`gate:${stage}`, "rejected");
    } else if (state === "step_continued") {
      const checkpoint = String(details.checkpoint || stage || "").trim();
      if (checkpoint) {
        out.set(`step:${checkpoint}`, "continued");
      }
    }
  }
  return out;
}

function latestOpenCardIndexes(entries, resolutions) {
  const out = new Map();
  for (let i = 0; i < entries.length; i += 1) {
    const info = entryActionInfo(entries[i]);
    if (!info) continue;
    if (resolutions.has(info.key)) continue;
    out.set(info.key, i);
  }
  return out;
}

function statusBadge(text, cls) {
  return `<span class=\"status-badge ${cls}\">${escapeHtml(text)}</span>`;
}

function renderChat() {
  const node = document.getElementById("chatLog");
  const hadExistingEntries = node.childElementCount > 0;
  const wasNearBottom =
    !hadExistingEntries ||
    node.scrollHeight - node.scrollTop - node.clientHeight < 60;
  node.innerHTML = "";
  const entries = activeRun && Array.isArray(activeRun.chat) ? activeRun.chat : [];
  const events = activeRun && Array.isArray(activeRun.events) ? activeRun.events : [];
  const resolutions = buildResolutionMap(events);
  for (const key of [...pendingCardActions.keys()]) {
    if (!activeRun || activeRun.status !== "running" || resolutions.has(key)) {
      pendingCardActions.delete(key);
    }
  }
  const latestOpen = latestOpenCardIndexes(entries, resolutions);
  if (!entries.length) {
    node.innerHTML = '<div class="msg system"><div class="meta">system</div>No chat entries yet. Start a run from the input below.</div>';
    return;
  }
  entries.forEach((entry, idx) => {
    const role = entry.role || "system";
    const kind = entry.kind || "message";
    const meta = entry.meta || {};
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;

    const metaNode = document.createElement("div");
    metaNode.className = "meta";
    metaNode.textContent = `${role} | ${kind} | ${tsLabel(entry.timestamp)}`;
    wrap.appendChild(metaNode);

    const contentNode = document.createElement("div");
    contentNode.textContent = entry.content || "";
    if (kind === "summary") {
      contentNode.className = "good";
    }
    wrap.appendChild(contentNode);

    const actionInfo = entryActionInfo(entry);
    if (actionInfo) {
      wrap.classList.add("approval");
      const resolved = resolutions.get(actionInfo.key) || null;
      const pendingAction = pendingCardActions.get(actionInfo.key) || null;
      const isLatestOpen = latestOpen.get(actionInfo.key) === idx;
      const actions = document.createElement("div");
      actions.className = "btn-row";

      if (pendingAction) {
        actions.innerHTML = statusBadge("Sending...", "status-sending");
        wrap.appendChild(actions);
      } else if (resolved) {
        const cls = resolved === "approved" ? "status-approved" : resolved === "rejected" ? "status-rejected" : "status-continued";
        actions.innerHTML = statusBadge(resolved, cls);
        wrap.appendChild(actions);
      } else if (!isLatestOpen) {
        actions.innerHTML = statusBadge("superseded", "status-superseded");
        wrap.appendChild(actions);
      } else if (activeRun && activeRun.status === "running") {
        if (actionInfo.type === "gate") {
          const approve = document.createElement("button");
          approve.className = "primary";
          approve.textContent = "Approve";
          approve.onclick = () =>
            sendAction("approve", { gate: actionInfo.stage }, actionInfo.key);
          const reject = document.createElement("button");
          reject.className = "bad";
          reject.textContent = "Reject";
          reject.onclick = () =>
            sendAction("reject", { gate: actionInfo.stage }, actionInfo.key);
          actions.appendChild(approve);
          actions.appendChild(reject);
        } else if (actionInfo.type === "step") {
          const cont = document.createElement("button");
          cont.className = "info";
          cont.textContent = "Continue";
          cont.onclick = () =>
            sendAction(
              "continue",
              { checkpoint: actionInfo.checkpoint, next_stage: actionInfo.nextStage || "" },
              actionInfo.key,
            );
          actions.appendChild(cont);
        }
        wrap.appendChild(actions);
      }
    }

    if (meta && Object.keys(meta).length) {
      const info = document.createElement("div");
      info.className = "mono";
      info.textContent = JSON.stringify(meta, null, 2);
      wrap.appendChild(info);
    }

    node.appendChild(wrap);
  });
  if (wasNearBottom) {
    node.scrollTop = node.scrollHeight;
  }
}

function renderAll() {
  renderNowWorking();
  renderTaskBoard();
  renderWorktrees();
  renderChat();
  updateControls();
}

async function refreshRunsDropdown() {
  const runs = await api("/api/runs");
  const select = document.getElementById("runSwitch");
  const current = activeRunId;
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = runs.length ? "Select run" : "No runs yet";
  select.appendChild(empty);
  for (const run of runs) {
    const id = run.run_id || "";
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = `${id} | ${run.status || "-"}`;
    if (current && id === current) {
      opt.selected = true;
    }
    select.appendChild(opt);
  }
}

function subscribeEvents(runId) {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  if (!runId) return;
  eventSource = new EventSource(`/api/runs/${runId}/events`);
  eventSource.onmessage = () => scheduleRefresh();
  eventSource.onerror = () => {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    setTimeout(() => {
      if (activeRunId === runId) {
        subscribeEvents(runId);
      }
    }, 2000);
  };
}

function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    if (!activeRunId) return;
    try {
      await loadRun(activeRunId);
    } catch {
      // keep current screen
    }
  }, 350);
}

async function loadRun(runId) {
  const run = await api(`/api/runs/${runId}`);
  activeRun = run;
  activeRunId = run.run_id || runId;
  renderAll();
  await refreshRunsDropdown();
  subscribeEvents(activeRunId);
}

async function loadActiveRun() {
  const payload = await api("/api/runs/active");
  const run = payload && payload.run ? payload.run : null;
  if (!run) {
    activeRun = null;
    activeRunId = null;
    renderAll();
    await refreshRunsDropdown();
    return;
  }
  activeRun = run;
  activeRunId = run.run_id;
  renderAll();
  await refreshRunsDropdown();
  subscribeEvents(activeRunId);
}

async function startNewChat() {
  setError("");
  const input = document.getElementById("promptInput");
  const message = input.value.trim();
  if (!message) {
    setError("Please type the new request first.");
    return;
  }
  try {
    setStatus("starting run...");
    const payload = {
      message,
      workflow: document.getElementById("workflowSel").value,
      backend: document.getElementById("backendSel").value,
      permissions: document.getElementById("permissionsSel").value,
      autonomy: document.getElementById("autonomySel").value,
      pacing_mode: document.getElementById("pacingSel").value,
      keep_worktrees: document.getElementById("keepWorktrees").checked,
    };
    const started = await api("/api/chat/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    input.value = "";
    await loadRun(started.run_id);
    setStatus("run started");
  } catch (err) {
    setError(err.message || String(err));
    setStatus("");
  }
}

async function sendNote() {
  setError("");
  if (!activeRun || activeRun.status !== "running") {
    setError("Send Note requires an active running run.");
    return;
  }
  const input = document.getElementById("promptInput");
  const message = input.value.trim();
  if (!message) {
    setError("Please type a note first.");
    return;
  }
  try {
    setStatus("sending note...");
    await api("/api/chat/message", {
      method: "POST",
      body: JSON.stringify({ run_id: activeRun.run_id, message }),
    });
    input.value = "";
    await loadRun(activeRun.run_id);
    setStatus("note sent");
  } catch (err) {
    setError(err.message || String(err));
    setStatus("");
  }
}

async function sendAction(action, meta = {}, cardKey = null) {
  if (!activeRunId) {
    setError("No active run selected.");
    return;
  }
  if (cardKey) {
    pendingCardActions.set(cardKey, action);
    renderAll();
  }
  try {
    setStatus(`${action}...`);
    await api(`/api/runs/${activeRunId}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify({ meta }),
    });
    await loadRun(activeRunId);
    setStatus(`${action} sent`);
  } catch (err) {
    setError(err.message || String(err));
    setStatus("");
    if (cardKey) {
      pendingCardActions.delete(cardKey);
      renderAll();
    }
  } finally {
    if (cardKey && (!activeRun || activeRun.status !== "running")) {
      pendingCardActions.delete(cardKey);
      renderAll();
    }
  }
}

document.getElementById("newChatBtn").addEventListener("click", startNewChat);
document.getElementById("newChatTopBtn").addEventListener("click", startNewChat);
document.getElementById("noteBtn").addEventListener("click", sendNote);
document.getElementById("pauseBtn").addEventListener("click", () => sendAction("pause"));
document.getElementById("resumeBtn").addEventListener("click", () => sendAction("resume"));
document.getElementById("continueBtn").addEventListener("click", () => sendAction("continue"));
document.getElementById("cancelBtn").addEventListener("click", () => sendAction("cancel"));
document.getElementById("runSwitch").addEventListener("change", async (ev) => {
  const runId = ev.target.value;
  if (!runId) return;
  await loadRun(runId);
});
document.getElementById("worktreeMap").addEventListener("click", async (ev) => {
  const target = ev.target;
  if (!(target instanceof HTMLElement)) return;
  const value = target.getAttribute("data-copy");
  if (!value) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const tmp = document.createElement("textarea");
      tmp.value = value;
      document.body.appendChild(tmp);
      tmp.select();
      document.execCommand("copy");
      document.body.removeChild(tmp);
    }
    setStatus("path copied");
  } catch {
    setError("Could not copy path.");
  }
});
document.getElementById("promptInput").addEventListener("keydown", (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
    ev.preventDefault();
    startNewChat();
  }
});

loadActiveRun().catch((err) => {
  setError(err.message || String(err));
});
</script>
</body>
</html>
"""


def run_dashboard(state_root: Path, run_id: str | None, port: int) -> None:
    repo_root = state_root.parents[2]
    logs_dir = repo_root / "agentkit" / "logs"
    runtime = DashboardRuntime(
        repo_root=repo_root,
        state_root=state_root,
        logs_dir=logs_dir,
        default_run_id=run_id,
    )

    handler = DashboardHandler
    handler.runtime = runtime

    server = QuietThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Dashboard running at http://127.0.0.1:{port}")
    print(f"State root: {state_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
