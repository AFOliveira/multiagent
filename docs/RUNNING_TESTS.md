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
python3 -m py_compile git_multiagent/runtime/tools/agent git_multiagent/runtime/tools/agent-pi-interactive git_multiagent/runtime/tools/git-multiagent-ui git_multiagent/runtime/tools/heartbeat git_multiagent/runtime/tools/agent-input git_multiagent/runtime/tools/agent_input.py git_multiagent/cli.py git_multiagent/dashboard.py git_multiagent/runner.py
python3 -m compileall git_multiagent tests
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
tmp=$(mktemp -d /tmp/gitmultiagent-smoke.XXXXXX)
git -C "$tmp" init >/dev/null
cd "$tmp"
PYTHONPATH=/path/to/multiagent-checkout python3 -m git_multiagent local init
find .multiagent -maxdepth 2 \( -type d -o -type f \) | sort
```

The explicit `cd "$tmp"` before `python3 -m git_multiagent init` is required.

## Bad Smoke Pattern

This is wrong:

```sh
tmp=$(mktemp -d /tmp/gitmultiagent-smoke.XXXXXX)
git -C "$tmp" init >/dev/null
PYTHONPATH=/path/to/multiagent-checkout python3 -m git_multiagent local init
```

`git -C "$tmp"` applies only to `git init`. The Python command still runs in the
current shell directory and can create `.multiagent/` in the development checkout
by accident.

## Test Hygiene

- Unit tests should use `tempfile.TemporaryDirectory()` for repositories they
  initialize.
- Helpers that run `multiagent` should set `cwd` to the temporary repository.
- Manual smoke commands should either set the tool `workdir` to the temp repo or
  use an explicit `cd "$tmp"` before running `python3 -m git_multiagent`.
- Never accept `.multiagent/` or `.gitignore` changes in the development
  checkout as a side effect of running tests.
