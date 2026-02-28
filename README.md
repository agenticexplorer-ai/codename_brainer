# codename_brainer

`codename_brainer` contains `agentkit`, a lightweight multi-agent development orchestrator with a CLI entry point.

## Project layout

- `agentkit/personas/`: role definitions (Planner, Implementer, Reviewer, etc.)
- `agentkit/workflows/`: workflow/state-machine logic
- `agentkit/policies/`: command and path safety rules
- `agentkit/tools/`: wrappers for git, tests, and filesystem operations
- `agentkit/runner/`: CLI/orchestration runtime
- `agentkit/logs/`: local run logs (gitignored)
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

If no command is provided, the CLI prints a short help hint.
