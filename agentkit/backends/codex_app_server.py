from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentkit.backends.base import PermissionMode

ROLE_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "planner": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "steps", "risks", "done_when"],
        "properties": {
            "summary": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "action", "notes"],
                    "properties": {
                        "id": {"type": "string"},
                        "action": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
            "risks": {"type": "array", "items": {"type": "string"}},
            "done_when": {"type": "array", "items": {"type": "string"}},
        },
    },
    "implementer": {
        "type": "object",
        "additionalProperties": False,
        "required": ["changes", "commands_ran", "next"],
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "type", "summary"],
                    "properties": {
                        "path": {"type": "string"},
                        "type": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                },
            },
            "commands_ran": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["cmd", "exit_code", "notes"],
                    "properties": {
                        "cmd": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "notes": {"type": "string"},
                    },
                },
            },
            "next": {"type": "string"},
        },
    },
    "reviewer": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "comments", "suggested_followups"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
            "comments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["severity", "text"],
                    "properties": {
                        "severity": {
                            "type": "string",
                            "enum": ["blocker", "major", "minor"],
                        },
                        "text": {"type": "string"},
                    },
                },
            },
            "suggested_followups": {"type": "array", "items": {"type": "string"}},
        },
    },
}


class CodexAppServerBackend:
    def __init__(
        self,
        permissions: PermissionMode = "read_only",
        timeout_seconds: int = 180,
        raw_event_log_file: Path | None = None,
    ) -> None:
        self.permissions = permissions
        self.timeout_seconds = timeout_seconds
        self.raw_event_log_file = raw_event_log_file
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        self._stderr_queue: queue.Queue[str] = queue.Queue()
        self._reader_threads_started = False
        self._next_id = 1
        self._pending_responses: dict[Any, dict[str, Any]] = {}
        self._pending_notifications: list[dict[str, Any]] = []
        self._role_threads: dict[str, str] = {}
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
        self._last_attempt = 1
        self._last_backend_error = None
        self._ensure_started()

        if thread_id:
            self._role_threads[role] = thread_id
        if role not in self._role_threads:
            self._role_threads[role] = self._start_thread(repo_root)

        payload = self._normalize_input(input)
        prompt = self._build_prompt(role, persona, payload)
        schema = ROLE_OUTPUT_SCHEMAS.get(role)

        for attempt in (1, 2):
            self._last_attempt = attempt
            turn_text = self._run_turn(
                thread_id=self._role_threads[role],
                repo_root=repo_root,
                prompt=prompt,
                output_schema=schema,
            )
            try:
                parsed = self._parse_output_json(role, turn_text)
                return parsed
            except Exception as exc:
                if attempt == 2:
                    self._last_backend_error = str(exc)
                    raise RuntimeError(
                        f"Codex output invalid for role '{role}' after repair attempt: {exc}"
                    ) from exc
                prompt = self._build_repair_prompt(role, payload, turn_text, str(exc), schema)

        self._last_backend_error = "unexpected backend state"
        raise RuntimeError("unexpected backend state")

    def get_thread_id(self, role: str) -> str | None:
        return self._role_threads.get(role)

    def get_last_attempt(self) -> int:
        return self._last_attempt

    def get_last_backend_error(self) -> str | None:
        return self._last_backend_error

    def close(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        self._proc = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader_threads_started = False
        self._start_reader_threads()
        self._request(
            "initialize",
            {"clientInfo": {"name": "agentkit", "version": "0.1.0"}},
            timeout_seconds=30,
        )

    def _start_reader_threads(self) -> None:
        if self._proc is None or self._reader_threads_started:
            return

        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        def pump_lines(stream: Any, out_queue: queue.Queue[str]) -> None:
            while True:
                line = stream.readline()
                if line == "":
                    break
                out_queue.put(line.rstrip("\n"))

        stdout_thread = threading.Thread(
            target=pump_lines,
            args=(self._proc.stdout, self._stdout_queue),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=pump_lines,
            args=(self._proc.stderr, self._stderr_queue),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        self._reader_threads_started = True

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        self._send_message({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return self._wait_for_response(req_id, timeout_seconds or self.timeout_seconds)

    def _send_message(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Codex app-server process is not available")
        wire = json.dumps(payload, ensure_ascii=False)
        self._proc.stdin.write(wire + "\n")
        self._proc.stdin.flush()
        self._write_raw_event({"dir": "out", "message": payload})

    def _wait_for_response(self, req_id: Any, timeout_seconds: int) -> dict[str, Any]:
        if req_id in self._pending_responses:
            return self._pending_responses.pop(req_id)

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            msg = self._next_json_message(deadline - time.time())
            if msg is None:
                continue

            if "id" in msg and "method" not in msg:
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"JSON-RPC error for request {req_id}: {msg['error']}")
                    return msg
                self._pending_responses[msg.get("id")] = msg
                continue

            if "method" in msg and "id" in msg:
                self._handle_server_request(msg)
                continue

            if "method" in msg:
                self._pending_notifications.append(msg)
                continue

        raise TimeoutError(f"Timed out waiting for response to request id {req_id}")

    def _next_json_message(self, timeout_seconds: float) -> dict[str, Any] | None:
        if timeout_seconds <= 0:
            return None
        try:
            raw = self._stdout_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            if self._proc is not None and self._proc.poll() is not None:
                err_lines = self._drain_stderr()
                raise RuntimeError(
                    "Codex app-server process exited unexpectedly: "
                    + ("\n".join(err_lines) if err_lines else "no stderr captured")
                )
            return None

        if not raw.strip():
            return None

        self._write_raw_event({"dir": "in", "raw": raw})
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _handle_server_request(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        error_response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32000,
                "message": "agentkit backend does not handle server-initiated requests",
            },
        }
        self._send_message(error_response)

    def _start_thread(self, repo_root: Path) -> str:
        params = {
            "cwd": str(repo_root),
            "approvalPolicy": self._approval_policy(),
            "sandbox": self._sandbox_mode(),
        }
        response = self._request("thread/start", params)
        result = response.get("result", {})
        thread = result.get("thread", {})
        thread_id = thread.get("id") or result.get("threadId")
        if not thread_id:
            raise RuntimeError(f"thread/start did not return thread id: {response}")
        return thread_id

    def _run_turn(
        self,
        thread_id: str,
        repo_root: Path,
        prompt: str,
        output_schema: dict[str, Any] | None,
    ) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(repo_root),
            "approvalPolicy": self._approval_policy(),
            "input": [{"type": "text", "text": prompt}],
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema

        response = self._request("turn/start", params)
        turn_id = self._extract_turn_id(response.get("result"))
        completed_turn_id = self._wait_for_turn_completion(thread_id, turn_id)
        return self._read_turn_final_message(thread_id, completed_turn_id or turn_id)

    def _wait_for_turn_completion(
        self,
        thread_id: str,
        expected_turn_id: str | None,
    ) -> str | None:
        deadline = time.time() + self.timeout_seconds
        turn_id = expected_turn_id
        while time.time() < deadline:
            msg = self._next_notification_or_message(deadline - time.time())
            if msg is None:
                continue

            if "id" in msg and "method" not in msg:
                self._pending_responses[msg.get("id")] = msg
                continue

            if "method" in msg and "id" in msg:
                self._handle_server_request(msg)
                continue

            method = msg.get("method")
            params = msg.get("params", {})
            if params.get("threadId") != thread_id:
                continue

            if method == "turn/started":
                turn_id = turn_id or self._extract_turn_id(params.get("turn"))
                continue

            if method == "turn/completed":
                completed_turn_id = self._extract_turn_id(params.get("turn"))
                if turn_id and completed_turn_id and completed_turn_id != turn_id:
                    continue
                status = self._extract_status(params.get("turn"))
                if status == "failed":
                    turn_error = None
                    turn = params.get("turn")
                    if isinstance(turn, dict):
                        err = turn.get("error")
                        if isinstance(err, dict):
                            turn_error = err.get("message")
                    if isinstance(turn_error, str) and turn_error:
                        raise RuntimeError(
                            f"Codex turn failed for thread {thread_id}: {turn_error}"
                        )
                    raise RuntimeError(f"Codex turn failed for thread {thread_id}")
                return completed_turn_id or turn_id

            if method == "turn/errored":
                err = params.get("error") or params
                raise RuntimeError(f"Codex turn errored: {err}")

        raise TimeoutError(f"Timed out waiting for turn completion in thread {thread_id}")

    def _next_notification_or_message(
        self, timeout_seconds: float
    ) -> dict[str, Any] | None:
        if self._pending_notifications:
            return self._pending_notifications.pop(0)
        return self._next_json_message(timeout_seconds)

    def _read_turn_final_message(
        self, thread_id: str, turn_id: str | None
    ) -> str:
        response = self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
            timeout_seconds=self.timeout_seconds,
        )
        thread = response.get("result", {}).get("thread", {})
        turns = thread.get("turns", [])
        if not isinstance(turns, list):
            raise RuntimeError("thread/read response missing turns list")

        target_turn: dict[str, Any] | None = None
        if turn_id:
            for turn in reversed(turns):
                if isinstance(turn, dict) and turn.get("id") == turn_id:
                    target_turn = turn
                    break
        if target_turn is None and turns:
            candidate = turns[-1]
            if isinstance(candidate, dict):
                target_turn = candidate

        if not target_turn:
            raise RuntimeError("No turn data available to extract assistant output")

        items = target_turn.get("items", [])
        if not isinstance(items, list):
            raise RuntimeError("Turn items missing from thread/read response")

        final_answer_text: str | None = None
        fallback_text: str | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            fallback_text = text
            if item.get("phase") == "final_answer":
                final_answer_text = text

        chosen = final_answer_text or fallback_text
        if chosen is None:
            raise RuntimeError("No agent message text found in completed turn")
        return chosen

    def _build_prompt(self, role: str, persona: str, payload: str) -> str:
        permissions_note = (
            "Permission mode is read_only. Do not run commands that modify files or state."
            " Provide report-style JSON only."
            if self.permissions == "read_only"
            else (
                "Permission mode is write_safe. You may propose safe workspace edits."
                " Avoid dangerous or system-level commands."
            )
        )
        return (
            f"Role: {role}\n\n"
            "Persona instructions:\n"
            f"{persona}\n\n"
            "Stage input:\n"
            f"{payload}\n\n"
            "Response requirements:\n"
            "- Return ONLY a valid JSON object.\n"
            "- Do not wrap in markdown or code fences.\n"
            "- No prose outside the JSON object.\n"
            f"- {permissions_note}\n"
        )

    def _build_repair_prompt(
        self,
        role: str,
        payload: str,
        bad_output: str,
        error_text: str,
        schema: dict[str, Any] | None,
    ) -> str:
        schema_text = json.dumps(schema, ensure_ascii=False, indent=2) if schema else "{}"
        return (
            f"Your previous output for role '{role}' was invalid.\n"
            f"Validation error: {error_text}\n\n"
            "Original stage input:\n"
            f"{payload}\n\n"
            "Previous invalid output:\n"
            f"{bad_output}\n\n"
            "Return ONLY a valid JSON object (no markdown fences) matching this schema:\n"
            f"{schema_text}\n"
        )

    def _parse_output_json(self, role: str, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("output JSON must be an object")
        self._validate_role_output(role, parsed)
        return parsed

    def _validate_role_output(self, role: str, payload: dict[str, Any]) -> None:
        required = {
            "planner": ["summary", "steps", "risks", "done_when"],
            "implementer": ["changes", "commands_ran", "next"],
            "reviewer": ["verdict", "comments", "suggested_followups"],
        }.get(role, [])
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValueError(f"missing required keys for role '{role}': {', '.join(missing)}")

    def _normalize_input(self, input: Any) -> str:
        if isinstance(input, str):
            return input
        return json.dumps(input, ensure_ascii=False, indent=2, default=str)

    def _approval_policy(self) -> str:
        # Keep the first write_safe milestone non-interactive; stricter per-command
        # approval flows can be added once command execution policies are fully enforced.
        if self.permissions == "write_safe":
            return "never"
        return "never"

    def _sandbox_mode(self) -> str:
        if self.permissions == "write_safe":
            return "workspace-write"
        return "read-only"

    def _extract_turn_id(self, source: Any) -> str | None:
        if not isinstance(source, dict):
            return None
        if isinstance(source.get("id"), str):
            return source["id"]
        turn = source.get("turn")
        if isinstance(turn, dict) and isinstance(turn.get("id"), str):
            return turn["id"]
        if isinstance(source.get("turnId"), str):
            return source["turnId"]
        return None

    def _extract_status(self, turn: Any) -> str | None:
        if not isinstance(turn, dict):
            return None
        status = turn.get("status")
        if isinstance(status, str):
            return status
        if isinstance(status, dict):
            status_type = status.get("type")
            if isinstance(status_type, str):
                return status_type
        return None

    def _write_raw_event(self, event: dict[str, Any]) -> None:
        if self.raw_event_log_file is None:
            return
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        self.raw_event_log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.raw_event_log_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _drain_stderr(self) -> list[str]:
        out: list[str] = []
        while True:
            try:
                out.append(self._stderr_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
