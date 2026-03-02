import shutil
import sys
import json
from datetime import datetime
from pathlib import Path

from agentkit.backends import stub as stub_backend
from agentkit.runner.loaders import load_text, load_workflow

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = REPO_ROOT / "agentkit" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


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
    print()

    # run the pipeline with the stub backend
    context = {"task": task}
    artifacts = {}
    run_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    run_log_file = LOGS_DIR / f"run_{workflow_name}_{run_stamp}.jsonl"

    for stage in wf.stages:
        stage_id = stage["id"]
        role = stage["role"]
        print(f"--- Stage: {stage_id} (role: {role}) ---")

        # choose input for the role
        if stage.get("input") == "task":
            role_input = task
        elif stage.get("input") == "plan_json":
            role_input = artifacts.get("plan")
        elif stage.get("input") == "impl_report_json":
            role_input = artifacts.get("implement")
        else:
            # generic fallback: pass the whole context
            role_input = context

        # call stub backend (replace this later with CodexBackend)
        try:
            out = stub_backend.run_role(role, role_input)
        except Exception as e:
            print(f"Error running role '{role}': {e}")
            return 1

        # store artifact keyed by stage id for later stages
        artifacts[stage_id] = out

        # log to file (one JSON line per stage)
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "stage": stage_id,
            "role": role,
            "input_preview": str(role_input)[:100],
            "output": out,
        }
        with open(run_log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        # pretty print the output
        print(json.dumps(out, indent=2, ensure_ascii=False))
        print()

    print(f"✅ Run complete. Logs: {run_log_file}")
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
