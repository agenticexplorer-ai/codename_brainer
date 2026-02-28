# codename_brainer

updated readme

What each is for:
agentkit/personas/ → role definitions (Planner, Implementer, Reviewer…)
agentkit/workflows/ → state machine / pipeline (Plan→Code→Test→Review)
agentkit/policies/ → command allowlist, forbidden paths, approval rules
agentkit/tools/ → wrappers for git/tests/filesystem
agentkit/runner/ → the orchestrator CLI
agentkit/logs/ → local run logs (gitignored)
examples/ → sample “target repos” or demo tasks


## Dev mode

### Cold start
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
agentkit doctor

### If you close the terminal, here’s what happens and what you need to redo.

What you DO NOT need to redo

You do not need to run again:

python3 -m venv .venv
pip install -e .

Those only need to be done:

the first time

or if you delete .venv

or if you recreate the environment from scratch

Your .venv folder is still there on disk.

What you DO need to redo

Every new terminal session, you must run:

source .venv/bin/activate

That’s it.

Why?

Because activation:

changes your PATH

tells the shell to use the Python inside .venv

makes agentkit available

Without activation:

agentkit may not be found

pip would install globally

wrong Python could run

Typical daily workflow

Open terminal → go to project:

cd your-repo
source .venv/bin/activate
agentkit doctor

Done.

How to check if you're activated

Your shell prompt usually shows:

(.venv) your-macbook %

Or you can run:

which python

If activated, it should show something like:

.../your-repo/.venv/bin/python
When would you recreate the venv?

If:

Python version changes

Dependencies break badly

You want a clean environment

Then:

rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

But that’s occasional, not daily.