from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentkit.orchestrator.types import RolePool, RoleSpec


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"YAML at {path} must be an object")
    return data


def load_role_pool(path: Path) -> RolePool:
    data = load_yaml(path)
    raw_roles = data.get("roles", [])
    if not isinstance(raw_roles, list):
        raise ValueError(f"'roles' in {path} must be a list")
    specs: dict[str, RoleSpec] = {}
    for raw in raw_roles:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", "")).strip()
        if not role:
            continue
        specs[role] = RoleSpec(
            role=role,
            default_count=int(raw.get("default_count", 1)),
            min_count=int(raw.get("min_count", 1)),
            max_count=int(raw.get("max_count", 1)),
        )
    return RolePool(specs=specs)


def load_scheduler_policies(path: Path) -> dict[str, Any]:
    return load_yaml(path)

