# agentkit

`codename_brainer` contains `agentkit`, a lightweight multi-agent development orchestrator with a CLI entry point.

## Project layout

- `agentkit/personas/`: role definitions (Planner/Implementer/Reviewer and team roles)
- `agentkit/workflows/`: workflow/state-machine logic
- `agentkit/team_models/`: team cardinality templates
- `agentkit/scheduler/`: scheduler and autonomy policies
- `agentkit/policies/`: command and path safety rules
- `agentkit/orchestrator/`: team runtime (run store, scheduler, worktrees)
- `agentkit/dashboard/`: local dashboard server
- `agentkit/tools/`: wrappers for git, tests, and filesystem operations
- `agentkit/runner/`: CLI/orchestration runtime
- `agentkit/logs/`: local run logs (gitignored)
- `agentkit/state/`: run state store (gitignored)
- `agentkit/worktrees/`: task worktree sandbox folder (gitignored)
- `examples/`: demo targets and sample tasks

## Prerequisites

- Python `3.10+`
- `git`
- Optional: `rg` (ripgrep) for faster search

## Quick start (first-time setup)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
agentkit doctor
```

If setup is successful, `agentkit doctor` should report required commands and your Python version.

## Daily workflow

For each new terminal session:

```bash
cd /path/to/codename_brainer
source .venv/bin/activate
agentkit doctor
```

You only need to reactivate the environment. You do not need to recreate `.venv` or reinstall every day.

## Verify virtual environment is active

```bash
which python
```

Expected output should point to this project, for example:

```text
.../codename_brainer/.venv/bin/python
```

## Recreate environment (only when needed)

Use this only if dependencies are broken, Python changes, or you want a clean reset:

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
agentkit doctor
```

## CLI usage

```bash
agentkit doctor
```

Run linear workflow:

```bash
agentkit run pr_factory "Add a hello endpoint" --backend stub --permissions read_only
```

Run team workflow:

```bash
agentkit run team_factory_v1 "Build feature X" --backend stub --permissions read_only --autonomy full_auto
```

List and inspect team runs:

```bash
agentkit runs list
agentkit runs show <run-id>
```

Launch local dashboard:

```bash
agentkit dashboard --port 8787
```

Then open `http://127.0.0.1:8787`.

## Chat-first dashboard

1. Open the dashboard.
2. Type your request in the chat box.
3. Press `Send` to start a run (defaults: `team_factory_v1`, `stub`, `read_only`, `mixed`).
4. Watch live persona/stage/task/worktree updates in the right-side panels.
5. Approve or reject gate cards inline in chat when they appear.

You can still switch runs from the run dropdown, and CLI-driven runs remain supported.
