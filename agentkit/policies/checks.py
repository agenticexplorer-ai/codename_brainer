from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from agentkit.backends.base import PermissionMode

DEFAULT_DANGEROUS_SNIPPETS = (
    "rm -rf",
    "sudo ",
    "brew ",
    "dd if=",
    "mkfs",
    "chmod 777",
)


def load_policy_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def is_forbidden_path(path_value: str, forbidden_paths: list[str]) -> bool:
    normalized = path_value.replace("\\", "/").strip()
    for rule in forbidden_paths:
        check = rule.replace("\\", "/").strip()
        if not check:
            continue
        if check.endswith("/") and normalized.startswith(check):
            return True
        if check in normalized:
            return True
    return False


def command_allowed(command: str, allowed_commands: list[str]) -> bool:
    if not allowed_commands:
        return True
    segments = split_shell_segments(command)
    if not segments:
        return False
    for segment in segments:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            return False
        executable = tokens[0]
        if executable not in allowed_commands:
            return False
    return True


def split_shell_segments(command: str) -> list[str]:
    parts = re.split(r"\s*(?:\|\||&&|;|\|)\s*", command)
    return [part.strip() for part in parts if part.strip()]


def evaluate_implementer_report(
    report: dict[str, Any],
    allowed_commands: list[str],
    forbidden_paths: list[str],
    permissions: PermissionMode,
) -> list[str]:
    del permissions
    violations: list[str] = []

    changes = report.get("changes", [])
    if isinstance(changes, list):
        for idx, change in enumerate(changes):
            if not isinstance(change, dict):
                continue
            path_value = change.get("path")
            if isinstance(path_value, str) and is_forbidden_path(path_value, forbidden_paths):
                violations.append(
                    f"changes[{idx}] uses forbidden path: {path_value}"
                )

    commands = report.get("commands_ran", [])
    if isinstance(commands, list):
        for idx, entry in enumerate(commands):
            if not isinstance(entry, dict):
                continue
            cmd = entry.get("cmd")
            if not isinstance(cmd, str):
                continue
            lower_cmd = cmd.lower()
            for snippet in DEFAULT_DANGEROUS_SNIPPETS:
                if snippet in lower_cmd:
                    violations.append(
                        f"commands_ran[{idx}] contains blocked snippet '{snippet}': {cmd}"
                    )
            if not command_allowed(cmd, allowed_commands):
                violations.append(
                    f"commands_ran[{idx}] executable not in allowed_commands: {cmd}"
                )

    return violations
