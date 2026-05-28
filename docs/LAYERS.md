# MULTIAGENT Layers

MULTIAGENT has one public command, `multiagent`, with three explicit
operating layers.

## Layer 1: Agent Utilities

`multiagent agent ...` is the package-owned interface used by agents and by the
supervisor. It provides the generic task, job, heartbeat, input, worker, and
interactive utilities:

- `multiagent agent task ...`
- `multiagent agent job ...`
- `multiagent agent worker ...`
- `multiagent agent interactive ...`
- `multiagent agent input ...`
- `multiagent agent heartbeat ...`

These utilities operate on the active instance state under
`~/.multiagent/state/<instance-id>`. They are not copied into repository
runtime directories.

## Layer 2: Local Control

`multiagent local ...` is the native current-machine control plane. It discovers
the current Git repository, reads `.multiagent/team.toml`, starts or stops
the local supervisor, sends prompts to interactive agents, and manages local
roles, rules, team configuration, specs, and recovery commands.

The repo-owned configuration is:

```text
.multiagent/team.toml
.multiagent/roles/*.md     optional role overrides
.multiagent/specs/        optional
```

The generic agent protocol is package-owned and injected into Pi context when
agents launch. It is not copied into the repository or instance state.

`team.toml` declares the agent team with singular `[[agent]]` tables:

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

Pi is always the runtime. `mode` chooses worker versus interactive behavior.
`options.heartbeat` is only valid for interactive agents and is disabled when
absent.

## Layer 3: Containerized Control

`multiagent docker ...` is the external/container control layer. It starts
`multiagent local start --foreground` inside Docker, keeps the repository path
transparent inside the container, mounts the shared MULTIAGENT state tree, and
registers the containerized instance for the dashboard from the host side.

Containerized agents install system packages into the container writable layer.
No host programs are mapped into the container as tools.

## Runtime State

All layers share the same instance state model:

```text
~/.multiagent/state/<instance-id>/tasks/
~/.multiagent/state/<instance-id>/jobs/
~/.multiagent/state/<instance-id>/agents/
```

Worker agents claim queued jobs. Interactive agents can create tasks or jobs on
request, but live input itself is not durable work; durable work must be recorded
through `multiagent agent task ...` or `multiagent agent job ...`.

## Multi-Repository View

When a supervisor starts, it links the repository into:

```text
~/.multiagent/instances/
```

The dashboard reads those links and the state below
`~/.multiagent/state/<instance-id>`.

## Security Direction

The default `multiagent docker` mode runs the whole repository agent system in a
container. The repository and shared MULTIAGENT state are mounted
transparently, while Pi auth is mediated by host auth proxy sockets rather than
by copying host tokens into Docker.
