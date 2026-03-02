from __future__ import annotations

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agentkit.orchestrator.store import list_runs, load_run


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


class DashboardHandler(BaseHTTPRequestHandler):
    state_root: Path = Path(".")
    default_run_id: str | None = None

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(self._index_html())
            return
        if path == "/api/runs":
            self._send_json(list_runs(self.state_root))
            return
        if path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = path.split("/")[3]
            self._stream_events(run_id)
            return
        if path.startswith("/api/runs/"):
            run_id = path.split("/")[3]
            self._send_json(self._run_payload(run_id))
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/runs/") or "/actions/" not in path:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        parts = path.split("/")
        if len(parts) < 6:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid action path")
            return
        run_id = parts[3]
        action = parts[5]
        payload = {
            "timestamp": time.time(),
            "run_id": run_id,
            "action": action,
        }
        run_dir = self.state_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        actions_file = run_dir / "actions.jsonl"
        with actions_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        self._send_json({"ok": True, "action": action})

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

    def _run_payload(self, run_id: str) -> dict:
        run = load_run(self.state_root, run_id)
        run_dir = self.state_root / run_id
        run["events"] = _read_jsonl(run_dir / "events.jsonl")
        run["tasks"] = _read_jsonl(run_dir / "tasks.jsonl")
        run["worktrees"] = _read_jsonl(run_dir / "worktrees.jsonl")
        return run

    def _stream_events(self, run_id: str) -> None:
        events_file = self.state_root / run_id / "events.jsonl"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        offset = 0
        try:
            while True:
                if events_file.exists():
                    text = events_file.read_text(encoding="utf-8")
                    lines = text.splitlines()
                    while offset < len(lines):
                        raw = lines[offset].strip()
                        offset += 1
                        if not raw:
                            continue
                        self.wfile.write(f"data: {raw}\n\n".encode("utf-8"))
                        self.wfile.flush()
                time.sleep(1.0)
        except (ConnectionError, BrokenPipeError):
            return

    def _index_html(self) -> str:
        run_id = self.default_run_id or ""
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>AgentKit Dashboard</title>
  <style>
    :root {{ --bg: #f3efe7; --card: #fffdf8; --ink: #102225; --muted: #47646a; --line: #d8cdb6; --accent: #0f766e; }}
    body {{ font-family: "IBM Plex Sans", "Segoe UI", sans-serif; margin: 0; background: radial-gradient(circle at top right, #f8e8d4, var(--bg)); color: var(--ink); }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; letter-spacing: 0.02em; }}
    .row {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px; }}
    .col {{ flex: 1; min-width: 280px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; max-height: 380px; overflow: auto; }}
    button {{ border: 1px solid var(--accent); background: transparent; color: var(--accent); padding: 8px 10px; border-radius: 8px; cursor: pointer; }}
    input {{ padding: 8px; border: 1px solid var(--line); border-radius: 8px; min-width: 320px; }}
    .meta {{ color: var(--muted); font-size: 13px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>AgentKit Dashboard</h1>
    <div class="meta">Run timeline, tasks, worktrees, and live events</div>
    <div class="row">
      <input id="runId" value="{run_id}" placeholder="Run ID" />
      <button onclick="loadRun()">Load</button>
      <button onclick="sendAction('pause')">Pause</button>
      <button onclick="sendAction('resume')">Resume</button>
      <button onclick="sendAction('cancel')">Cancel</button>
      <button onclick="sendAction('approve')">Approve</button>
      <button onclick="sendAction('reject')">Reject</button>
    </div>
    <div class="row">
      <div class="card col"><h3>Run</h3><pre id="run"></pre></div>
      <div class="card col"><h3>Tasks</h3><pre id="tasks"></pre></div>
    </div>
    <div class="row">
      <div class="card col"><h3>Worktrees</h3><pre id="worktrees"></pre></div>
      <div class="card col"><h3>Live Events</h3><pre id="events"></pre></div>
    </div>
  </div>
<script>
let evt = null;
async function loadRun() {{
  const runId = document.getElementById('runId').value.trim();
  if (!runId) return;
  const res = await fetch(`/api/runs/${{runId}}`);
  if (!res.ok) {{
    document.getElementById('run').textContent = 'Run not found';
    return;
  }}
  const data = await res.json();
  document.getElementById('run').textContent = JSON.stringify(data, null, 2);
  document.getElementById('tasks').textContent = JSON.stringify(data.tasks || [], null, 2);
  document.getElementById('worktrees').textContent = JSON.stringify(data.worktrees || [], null, 2);
  subscribe(runId);
}}
function subscribe(runId) {{
  if (evt) evt.close();
  evt = new EventSource(`/api/runs/${{runId}}/events`);
  evt.onmessage = (e) => {{
    const node = document.getElementById('events');
    node.textContent += e.data + "\\n";
    node.scrollTop = node.scrollHeight;
  }};
}}
async function sendAction(action) {{
  const runId = document.getElementById('runId').value.trim();
  if (!runId) return;
  await fetch(`/api/runs/${{runId}}/actions/${{action}}`, {{ method: 'POST' }});
}}
if (document.getElementById('runId').value.trim()) {{
  loadRun();
}}
</script>
</body>
</html>"""


def run_dashboard(state_root: Path, run_id: str | None, port: int) -> None:
    handler = DashboardHandler
    handler.state_root = state_root
    handler.default_run_id = run_id
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Dashboard running at http://127.0.0.1:{port}")
    print(f"State root: {state_root}")
    server.serve_forever()

