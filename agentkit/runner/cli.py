import shutil
import sys
from pathlib import Path

from agentkit.runner.loaders import load_text, load_workflow

REPO_ROOT = Path(__file__).resolve().parents[2]


def doctor() -> int:
    required = ["git", "python3"]
    optional = ["rg"]  # ripgrep helps later but not required

    missing = [c for c in required if shutil.which(c) is None]
    if missing:
        print("Missing required commands:", ", ".join(missing))
        return 1

    print("✅ Required commands OK:", ", ".join(required))

    missing_opt = [c for c in optional if shutil.which(c) is None]
    if missing_opt:
        print("ℹ️ Optional commands not found:", ", ".join(missing_opt))
    else:
        print("✅ Optional commands OK:", ", ".join(optional))

    print(f"Python: {sys.version.split()[0]}")
    return 0


def run(workflow_name: str, task: str) -> int:
    wf_path = REPO_ROOT / "agentkit" / "workflows" / f"{workflow_name}.yaml"
    if not wf_path.exists():
        print(f"Workflow not found: {wf_path}")
        return 1

    wf = load_workflow(wf_path)

    # load personas used by this workflow
    roles = [stage["role"] for stage in wf.stages]
    persona_texts = {}
    for role in roles:
        p = REPO_ROOT / "agentkit" / "personas" / f"{role}.md"
        if not p.exists():
            print(f"Persona not found for role '{role}': {p}")
            return 1
        persona_texts[role] = load_text(p)

    print("== Workflow ==")
    print(f"Name: {wf.name}")
    print(f"Description: {wf.description}")
    print("Stages:", " -> ".join([s["id"] for s in wf.stages]))
    print()

    print("== Task ==")
    print(task)
    print()

    print("== Personas loaded ==")
    for role in roles:
        print(f"- {role}: {len(persona_texts[role])} chars")

    print("\n✅ Runner wiring OK (no agent execution yet).")
    return 0


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "doctor":
        raise SystemExit(doctor())

    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        if len(sys.argv) < 4:
            print("Usage: agentkit run <workflow_name_without_yaml> <task>")
            raise SystemExit(2)
        workflow_name = sys.argv[2]
        task = " ".join(sys.argv[3:])
        raise SystemExit(run(workflow_name, task))

    print("agentkit is installed.")
    print("Try: agentkit doctor")
    print("Or:  agentkit run pr_factory \"Add a hello endpoint\"")
