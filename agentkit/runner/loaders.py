from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Workflow:
    name: str
    description: str
    stages: list[dict]
    kind: str = "linear"
    team_model: str | None = None
    data: dict[str, Any] | None = None


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_workflow(path: Path) -> Workflow:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid workflow YAML: {path}")
    return Workflow(
        name=data["name"],
        description=data.get("description", ""),
        stages=data["stages"],
        kind=data.get("kind", "linear"),
        team_model=data.get("team_model"),
        data=data,
    )
