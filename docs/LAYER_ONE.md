# Layer One Runtime Contract

Layer One is the local runtime contract for a repository. It owns the fixed
queue format, process launcher, interactive socket convention, and recovery
commands. It does not own project policy; project policy belongs in repo docs
and role files.

## Installed Files

`multiagent local init` installs the stationary repository configuration under
`.multiagent/`:

```text
.multiagent/team.toml
.multiagent/roles/        optional role overrides
.multiagent/specs/        optional
```

Runtime state lives in the user-level MULTIAGENT state tree:

```text
~/.multiagent/state/<instance-id>/
```

The package-owned generic protocol and the effective role instructions are
loaded into Pi context when each agent launches. They are not copied into the
repository or instance state.

`multiagent local update` refreshes instance state shape. Runtime helpers are
package-owned and invoked through `multiagent agent ...`; they are not copied
into the repository. `update` keeps local role edits unless `--roles` is passed.

## Supervisor

`multiagent local start` reads `.multiagent/team.toml`, starts the
supervisor, and launches every configured agent. `multiagent local stop`
terminates the supervisor and managed processes. `multiagent local restart`
rereads the team file.

Running repositories are registered by symlink under:

```text
~/.multiagent/instances/
```

The dashboard reads those links; it does not start agents on its own.

## Team Agents

The team file uses singular `[[agent]]` tables:

```toml
[[agent]]
name = "planner-1"
role = "planner"
mode = "worker"

[[agent]]
name = "operator"
role = "planner"
mode = "interactive"
options = { heartbeat = 15 }
```

`mode = "worker"` agents claim queued jobs and exit after completing one job.
`mode = "interactive"` agents keep a persistent Pi RPC session and accept live
input. Pi is always the runtime; there is no `engine` field in `team.toml`.

## Interactive Input

Each running interactive agent exposes a Unix socket in its instance state:

```text
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.sock
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.json
```

`rpc.json` records the socket path and process metadata. The socket is a live
control channel and is removed when the agent exits.

Send input with:

```sh
multiagent local prompt <agent> "message"
multiagent agent input <agent> "message"
```

By default `multiagent local prompt` follows the agent transcript for the
current turn. Use `--quiet` to send without printing the response.

## Heartbeat

Heartbeat is per-agent configuration. If `options.heartbeat` is absent, heartbeat
is disabled.

```toml
[[agent]]
name = "operator"
role = "planner"
mode = "interactive"
options = { heartbeat = 15 }
```

The supervisor starts one heartbeat helper for each interactive agent with a
heartbeat value. Heartbeat is invalid for worker agents.

## Jobs And Recovery

Jobs live under `~/.multiagent/state/<instance-id>/jobs/`; tasks live under
`~/.multiagent/state/<instance-id>/tasks/`. Agents use `multiagent agent`
helpers to claim, start, finish, release, or fail work.

Operator recovery commands are:

```sh
multiagent agent job reset <job> -m "retry"
multiagent agent job kill <job> -m "stop now"
multiagent agent job orphans
multiagent agent job reset-orphans
multiagent local agents reset <agent>
```

Recovery commands preserve the file protocol and clean recorded runtime state.
