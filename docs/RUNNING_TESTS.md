# Running Tests Safely

MULTIAGENT tests and smoke checks often create temporary Git repositories and run
`multiagent` commands inside them. Be strict about test working directories:
tests intended for a temporary repository must actually run there, not in the
MULTIAGENT development checkout.

## Standard Checks

Run these from the repository root:

```sh
python3 -m unittest discover -s tests
git diff --check
python3 -m py_compile multiagent/runtime/tools/agent multiagent/runtime/tools/agent-pi-interactive multiagent/runtime/tools/multiagent-ui multiagent/runtime/tools/heartbeat multiagent/runtime/tools/agent-input multiagent/runtime/tools/agent_input.py multiagent/cli.py multiagent/dashboard.py multiagent/runner.py
python3 -m compileall multiagent tests
```

After any test or smoke command that may initialize MULTIAGENT, also run:

```sh
git status --short .gitignore .multiagent
```

That output must be empty unless the current task intentionally changes those
paths.

## Temp-Repo Smoke Tests

Use this shape for manual smoke tests of `multiagent local init`:

```sh
tmp=$(mktemp -d /tmp/multiagent-smoke.XXXXXX)
git -C "$tmp" init >/dev/null
cd "$tmp"
PYTHONPATH=/path/to/multiagent-checkout python3 -m multiagent local init
find .multiagent -maxdepth 2 \( -type d -o -type f \) | sort
```

The explicit `cd "$tmp"` before `python3 -m multiagent init` is required.

## Bad Smoke Pattern

This is wrong:

```sh
tmp=$(mktemp -d /tmp/multiagent-smoke.XXXXXX)
git -C "$tmp" init >/dev/null
PYTHONPATH=/path/to/multiagent-checkout python3 -m multiagent local init
```

`git -C "$tmp"` applies only to `git init`. The Python command still runs in the
current shell directory and can create `.multiagent/` in the development checkout
by accident.

## Test Hygiene

- Unit tests should use `tempfile.TemporaryDirectory()` for repositories they
  initialize.
- Helpers that run `multiagent` should set `cwd` to the temporary repository.
- Manual smoke commands should either set the tool `workdir` to the temp repo or
  use an explicit `cd "$tmp"` before running `python3 -m multiagent`.
- Never accept `.multiagent/` or `.gitignore` changes in the development
  checkout as a side effect of running tests.
