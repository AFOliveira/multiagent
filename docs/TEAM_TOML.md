# team.toml

`team.toml` is the repo-local configuration file that defines which
MULTIAGENT agents are launched by `multiagent local start`. `multiagent local
init` installs the default file at `.multiagent/team.toml`. If the file is
missing, MULTIAGENT falls back to the packaged default team.

The default team has no interactive agents:

```toml
[[agent]]
name = "planner-1"
role = "planner"
mode = "worker"

[[agent]]
name = "implementer-1"
role = "implementer"
mode = "worker"

[[agent]]
name = "reviewer-1"
role = "reviewer"
mode = "worker"

[[agent]]
name = "committer-1"
role = "committer"
mode = "worker"
```

## Creating The File

Create or materialize the file with one of these commands:

```sh
multiagent local init
multiagent local team edit
multiagent local team add my-agent --role implementer
```

The file lives at:

```text
.multiagent/team.toml
```

Because runtime state lives under `~/.multiagent/state/<instance-id>/`, this
file can be committed and reviewed like normal project configuration.

## Format

The file contains one singular `[[agent]]` table per configured agent.

```toml
[[agent]]
name = "implementer-1"
role = "implementer"
mode = "worker"
engine = "codex"
model = "gpt-5.1-codex-max"
thinking_effort = "high"

[[agent]]
name = "operator"
role = "planner"
mode = "interactive"
engine = "pi"
options = { heartbeat = 15 }
```

Supported fields:

- `name`: required stable runtime agent id. It must contain only letters,
  numbers, dots, underscores, and hyphens.
- `role`: required role instructions name, resolved from `.multiagent/roles/`
  overrides or package role templates.
- `mode`: optional; `worker` by default. Valid values are `worker` and
  `interactive`.
- `engine`: optional; `pi` by default. Worker agents support `pi`, `codex`,
  `claude`, `codex-work`, `claude-work`, and `cursor`. Interactive agents must
  use `pi`.
- `model`: optional model passed to the selected CLI.
- `thinking_effort`: optional effort level passed to engines that expose one.
  Pi receives `--thinking`, Codex receives a `model_reasoning_effort` config
  override, and Claude receives `--effort`. Cursor has no separate effort flag.
- `options`: optional inline table. Currently `heartbeat = <minutes>` is
  supported for interactive agents.

## Modes

`mode = "worker"` starts a queued agent. It claims one pending job for its role,
processes that job, records status, and exits.

Worker agents keep per-agent engine session markers in their state directory
when the selected CLI exposes resume support. Pi uses `pi-session/`; Codex,
Claude, and Cursor store engine-specific session/chat id files and resume them
on the next worker run.

`mode = "interactive"` starts a persistent Pi RPC session. It does not claim a
queued job on startup. It reads the configured role and accepts live input via a
Unix socket in its instance state:

```text
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.sock
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.json
```

`multiagent local prompt <agent> ...`, `multiagent agent input`, heartbeats, and
the dashboard use that socket.

## Heartbeat

Heartbeat is configured per interactive agent:

```toml
[[agent]]
name = "operator"
role = "planner"
mode = "interactive"
options = { heartbeat = 15 }
```

If `options.heartbeat` is absent, heartbeat is disabled for that agent.
Heartbeat is invalid for worker agents.

## Commands

Inspect the effective team:

```sh
multiagent local team list
multiagent local team show
multiagent local team show implementer-1
```

Add and update agents:

```sh
multiagent local team add implementer-2 --role implementer
multiagent local team add reviewer-codex --role reviewer --engine codex --model gpt-5.1-codex-max --thinking-effort high
multiagent local team add operator --role planner --mode interactive --heartbeat 15
multiagent local team set implementer-2 --engine claude --model sonnet --thinking-effort max
multiagent local team set operator --no-heartbeat
multiagent local team remove implementer-2
```

Edit the file directly:

```sh
multiagent local team edit
```

If `$EDITOR` is unset, `team edit` prints the path instead of opening an editor.

## Runtime Behavior

Before agents start, MULTIAGENT reads the effective `team.toml` directly. The
Python supervisor launches and restarts each configured agent; there is no
separate runtime team file to edit.

After changing `.multiagent/team.toml`, restart the supervisor for the new
definition to take effect:

```sh
multiagent local restart
```

Removing an agent from `team.toml` prevents it from being launched on future
starts. It does not by itself kill an already running process; use
`multiagent local restart`, `multiagent local stop`, or recovery commands when
you need to stop active runtime agents.

## Current Limits

`team.toml` can configure agent name, role, mode, engine, model,
thinking_effort, and heartbeat. It does not currently configure per-agent
extensions, skills, tool allowlists, provider settings, environment variables,
socket path overrides, or arbitrary engine command-line arguments. Those
settings must currently be handled through the selected CLI's own configuration
or by changing the MULTIAGENT launch implementation.
