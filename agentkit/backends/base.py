from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

BackendName = Literal["stub", "codex"]
PermissionMode = Literal["read_only", "write_safe"]


class Backend(Protocol):
    def run_role(
        self,
        role: str,
        persona: str,
        input: Any,
        repo_root: Path,
        thread_id: str | None,
    ) -> dict[str, Any]:
        ...

