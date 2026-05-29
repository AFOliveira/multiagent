<!-- SPDX-License-Identifier: MIT -->

# MULTIAGENT Tools

This directory contains runtime helper tools and the packaged dashboard server.
These files are package-owned. They are launched through the installed
`multiagent` command and are not copied into repository `.multiagent`
directories.

The helper launchers require `pi`. MULTIAGENT is Pi-based; `agent`,
`agent-pi-interactive`, `multiagent-ui`, and the dashboard entry point use
Python 3 stdlib only.

## `multiagent-ui`

`multiagent-ui` serves the web interface for MULTIAGENT state. It is the
packaged server implementation used by:

```sh
multiagent dashboard
```

Options:

- `--root <path>`: serve a different MULTIAGENT state directory.
- `--registry`: read running MULTIAGENT instances from links in
  `~/.multiagent/instances/`.
- `--registry-dir <path>`: read a different user instances directory.
- `--host <host>`: bind host. Defaults to `127.0.0.1`.
- `--port <port>`: starting port to bind. MULTIAGENT tries this port and then
  increasing ports until one is free. Defaults to `4137`.

The configured team is started by `multiagent local start`, not by the web UI.
`multiagent local start` reads the effective `team.toml` and supervises each
configured agent directly. It also links each running repository's
instance state directory under `~/.multiagent/instances/` for the dashboard.

## `heartbeat`

`heartbeat` sends `heartbeat <time> <date>` to one interactive agent through that
agent's Unix socket. The supervisor launches it only for interactive team entries
that configure `options = { heartbeat = <minutes> }`.

```toml
[[agent]]
name = "operator"
role = "planner"
mode = "interactive"
options = { heartbeat = 15 }
```

If `options.heartbeat` is absent, heartbeat is disabled for that agent.

## `agent-input`

`agent-input` sends one JSON-line message to a running interactive agent through
its socket:

```sh
multiagent local prompt operator "summarize status"
MULTIAGENT_STATE_DIR=~/.multiagent/state/<instance-id> \
  multiagent agent input --mode steer operator "stop and wait"
```

The socket lives at:

```text
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.sock
```

`rpc.json` in the same directory records the socket path and process metadata.

## `agent`

`agent` starts one named worker agent, claims one pending job for that agent's
role, records the job in `agents/<agent-name>/current-job`, and renders Pi event
output to `agents/<agent-name>/transcript.log`. By default it also prints the
rendered transcript to stdout. Use `--headless` to write files only.

It is normally launched by `multiagent local start`. For direct debugging, pass
the state directory explicitly through `MULTIAGENT_STATE_DIR`:

```sh
MULTIAGENT_STATE_DIR=~/.multiagent/state/<instance-id> \
  multiagent agent worker planner planner-1
```

Options:

- `--headless`: do not print the rendered transcript to stdout.
- `-m <model>`: pass a model name to Pi.

CLI stderr is saved in `error.log`.

## `agent-pi-interactive`

`agent-pi-interactive` is launched by `multiagent local start` for team entries
with `mode = "interactive"`. It starts a persistent `pi --mode rpc` process,
writes the rendered transcript to
`~/.multiagent/state/<instance-id>/agents/<agent-name>/transcript.log`, and
accepts input on that agent's Unix socket.

Humans normally talk to interactive agents with `multiagent local prompt <agent>`
or from the dashboard's chat and agent inspector controls.

## Task Commands

Task commands operate on folders under
`~/.multiagent/state/<instance-id>/tasks/`:

```sh
multiagent agent task create <task-id> <spec-file>
multiagent agent task show <task-id>
multiagent agent task comment <task-id> <message>
multiagent agent task state <task-id> open
multiagent agent task state <task-id> done -m "completed"
multiagent agent task result <task-id> <result-file>
multiagent agent task list
```

## `agent new`

`multiagent agent new <agent-id> <role>` creates a named agent directory when needed
and prints its path. If the agent already has a claimed or running job, it exits
with an error instead.

## Job Recovery Commands

The normal job lifecycle is managed by agents through `multiagent agent job
claim`, `multiagent agent job start`, `multiagent agent job done`,
`multiagent agent job release`, and `multiagent agent job fail`. Operator
recovery tools are also available:

```sh
multiagent agent job reset <job-id> -m "retry"
multiagent agent job kill <job-id> -m "stop now"
multiagent agent job orphans
multiagent agent job reset-orphans
multiagent agent job reap 60
```

`job-kill` marks a claimed or running job failed and signals the recorded runner
or Pi process PIDs.
