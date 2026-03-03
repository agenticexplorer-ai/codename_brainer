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
        keep_worktrees = bool(payload.get("keep_worktrees", False))

        if backend not in VALID_BACKENDS:
            raise ValueError(f"Invalid backend: {backend}")
        if permissions not in VALID_PERMISSIONS:
            raise ValueError(f"Invalid permissions: {permissions}")
        if autonomy not in VALID_AUTONOMY:
            raise ValueError(f"Invalid autonomy: {autonomy}")

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
            metadata={"kind": "team_orchestrator", "source": "dashboard_chat"},
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
                f"backend={backend}, permissions={permissions}, autonomy={autonomy}."
            ),
            meta={
                "workflow": workflow_name,
                "backend": backend,
                "permissions": permissions,
                "autonomy": autonomy,
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
        run["events"] = read_jsonl(run_dir / "events.jsonl")
        run["tasks"] = read_jsonl(run_dir / "tasks.jsonl")
        run["worktrees"] = read_jsonl(run_dir / "worktrees.jsonl")
        run["chat"] = read_jsonl(run_dir / "chat.jsonl")
        return run


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
        if action not in {"pause", "resume", "cancel", "approve", "reject"}:
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
    button.primary { background: var(--accent); color: #fff; }
    button.warn { border-color: var(--warn); color: var(--warn); }
    button.bad { border-color: var(--bad); color: var(--bad); }
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
        <div>
          <span id=\"activeChip\" class=\"active-chip\">No active run</span>
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
              <label><input type=\"checkbox\" id=\"keepWorktrees\" /> Keep worktrees</label>
            </div>
          </div>
        </details>
        <div class=\"composer-row\">
          <button class=\"primary\" id=\"sendBtn\">Send</button>
          <span id=\"composerStatus\" class=\"mono\"></span>
        </div>
        <div id=\"composerError\" class=\"error\"></div>
      </div>
    </section>

    <aside class=\"side\">
      <section class=\"panel section\">
        <h3>Now Working</h3>
        <div id=\"nowWorking\" class=\"kv\">No run selected.</div>
        <div class=\"controls\">
          <button id=\"pauseBtn\" class=\"warn\">Pause</button>
          <button id=\"resumeBtn\">Resume</button>
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
        <div id=\"taskBoard\" class=\"task-columns\"></div>
      </section>

      <section class=\"panel section\">
        <h3>Worktrees</h3>
        <div id=\"worktreeMap\" class=\"mono\">No worktree data.</div>
      </section>
    </aside>
  </div>

<script>
let activeRun = null;
let activeRunId = null;
let eventSource = null;
let refreshTimer = null;

const gateActions = new Set(["scope_lock", "integration_start", "release_start"]);

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

function activeChipText() {
  if (!activeRun) return "No active run";
  return `${activeRun.run_id} | ${activeRun.status} | ${activeRun.workflow}`;
}

function renderNowWorking() {
  const node = document.getElementById("nowWorking");
  document.getElementById("activeChip").textContent = activeChipText();
  if (!activeRun) {
    node.textContent = "No run selected.";
    return;
  }
  const events = Array.isArray(activeRun.events) ? activeRun.events : [];
  const latest = events.length ? events[events.length - 1] : null;
  const lines = [
    `status: ${activeRun.status || "-"}`,
    `workflow: ${activeRun.workflow || "-"}`,
    `backend: ${activeRun.backend || "-"}`,
    `permissions: ${activeRun.permissions || "-"}`,
    `autonomy: ${activeRun.autonomy || "-"}`,
    `stage: ${latest ? latest.stage : "-"}`,
    `persona: ${latest ? latest.role : "-"}`,
    `task: ${latest && latest.task_id ? latest.task_id : "-"}`,
  ];
  node.innerHTML = lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("");
}

function buildTaskSnapshot(tasks) {
  const byId = new Map();
  for (const task of tasks || []) {
    if (!task || !task.task_id) continue;
    byId.set(task.task_id, task);
  }
  return [...byId.values()];
}

function renderTaskBoard() {
  const statuses = ["queued", "in_progress", "qa", "integration", "done", "blocked"];
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
  const board = document.getElementById("taskBoard");
  board.innerHTML = statuses
    .map((status) => {
      const tasks = buildTaskSnapshot(activeRun ? activeRun.tasks : []).filter((task) => {
        const mapped = mapState[String(task.state || "")] || "queued";
        return mapped === status;
      });
      const pills = tasks
        .map((task) => `<span class=\"pill\">${escapeHtml(task.task_id)}</span>`)
        .join("");
      return `<div class=\"task-col\"><h4>${status}</h4>${pills || "<span class='mono'>none</span>"}</div>`;
    })
    .join("");
}

function renderWorktrees() {
  const node = document.getElementById("worktreeMap");
  if (!activeRun || !Array.isArray(activeRun.worktrees) || activeRun.worktrees.length === 0) {
    node.textContent = "No worktree data.";
    return;
  }
  const latestByTask = new Map();
  for (const item of activeRun.worktrees) {
    if (!item || !item.task_id) continue;
    latestByTask.set(item.task_id, item);
  }
  const rows = [...latestByTask.values()]
    .map((wt) => {
      const created = wt.created ? "created" : "not-created";
      return `${wt.task_id}\\n  branch: ${wt.branch || "-"}\\n  path: ${wt.path || "-"}\\n  state: ${created}${wt.error ? `\\n  error: ${wt.error}` : ""}`;
    })
    .join("\\n\\n");
  node.textContent = rows;
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

function renderChat() {
  const node = document.getElementById("chatLog");
  const hadExistingEntries = node.childElementCount > 0;
  const wasNearBottom =
    !hadExistingEntries ||
    node.scrollHeight - node.scrollTop - node.clientHeight < 60;
  node.innerHTML = "";
  const entries = activeRun && Array.isArray(activeRun.chat) ? activeRun.chat : [];
  if (!entries.length) {
    node.innerHTML = '<div class="msg system"><div class="meta">system</div>No chat entries yet. Start a run from the input below.</div>';
    return;
  }
  for (const entry of entries) {
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

    const gateMeta = gateMetaFromEntry(entry);
    if (gateMeta && activeRun && activeRun.status === "running") {
      wrap.classList.add("approval");
      const actions = document.createElement("div");
      actions.className = "btn-row";
      const approve = document.createElement("button");
      approve.className = "primary";
      approve.textContent = "Approve";
      approve.onclick = () => sendAction("approve", gateMeta.stage);
      const reject = document.createElement("button");
      reject.className = "bad";
      reject.textContent = "Reject";
      reject.onclick = () => sendAction("reject", gateMeta.stage);
      actions.appendChild(approve);
      actions.appendChild(reject);
      wrap.appendChild(actions);
    }

    if (meta && Object.keys(meta).length) {
      const info = document.createElement("div");
      info.className = "mono";
      info.textContent = JSON.stringify(meta, null, 2);
      wrap.appendChild(info);
    }

    node.appendChild(wrap);
  }
  if (wasNearBottom) {
    node.scrollTop = node.scrollHeight;
  }
}

function renderAll() {
  renderNowWorking();
  renderTaskBoard();
  renderWorktrees();
  renderChat();
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

async function startOrSendMessage() {
  setError("");
  const input = document.getElementById("promptInput");
  const message = input.value.trim();
  if (!message) {
    setError("Please type a request first.");
    return;
  }
  try {
    setStatus("sending...");
    if (activeRun && activeRun.status === "running") {
      await api("/api/chat/message", {
        method: "POST",
        body: JSON.stringify({ run_id: activeRun.run_id, message }),
      });
      input.value = "";
      await loadRun(activeRun.run_id);
      setStatus("message appended");
      return;
    }

    const payload = {
      message,
      workflow: document.getElementById("workflowSel").value,
      backend: document.getElementById("backendSel").value,
      permissions: document.getElementById("permissionsSel").value,
      autonomy: document.getElementById("autonomySel").value,
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

async function sendAction(action, gate) {
  if (!activeRunId) {
    setError("No active run selected.");
    return;
  }
  try {
    setStatus(`${action}...`);
    await api(`/api/runs/${activeRunId}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify({ meta: gate ? { gate } : {} }),
    });
    await loadRun(activeRunId);
    setStatus(`${action} sent`);
  } catch (err) {
    setError(err.message || String(err));
    setStatus("");
  }
}

document.getElementById("sendBtn").addEventListener("click", startOrSendMessage);
document.getElementById("pauseBtn").addEventListener("click", () => sendAction("pause"));
document.getElementById("resumeBtn").addEventListener("click", () => sendAction("resume"));
document.getElementById("cancelBtn").addEventListener("click", () => sendAction("cancel"));
document.getElementById("runSwitch").addEventListener("change", async (ev) => {
  const runId = ev.target.value;
  if (!runId) return;
  await loadRun(runId);
});
document.getElementById("promptInput").addEventListener("keydown", (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
    ev.preventDefault();
    startOrSendMessage();
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
