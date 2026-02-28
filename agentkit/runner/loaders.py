from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Workflow:
    name: str
    description: str
    stages: list[dict]


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_workflow(path: Path) -> Workflow:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Workflow(
        name=data["name"],
        description=data.get("description", ""),
        stages=data["stages"],
    )
