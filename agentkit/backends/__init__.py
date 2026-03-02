from __future__ import annotations

from pathlib import Path

from agentkit.backends.base import Backend, BackendName, PermissionMode
from agentkit.backends.codex_app_server import CodexAppServerBackend
from agentkit.backends.stub import StubBackend


def build_backend(
    backend_name: BackendName,
    permissions: PermissionMode,
    raw_event_log_file: Path | None = None,
) -> Backend:
    if backend_name == "stub":
        return StubBackend()
    if backend_name == "codex":
        return CodexAppServerBackend(
            permissions=permissions,
            raw_event_log_file=raw_event_log_file,
        )
    raise ValueError(f"Unsupported backend: {backend_name}")
