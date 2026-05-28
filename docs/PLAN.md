# MULTIAGENT Plan

This document tracks the current command model and near-term design direction.

## Current Model

MULTIAGENT defines a local team of agents in one repository. There is no
special built-in control agent. Every launched process is an agent from
`.multiagent/team.toml`.

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

Pi is always the runtime. The team file does not configure engines.

## Commands

Team management:

```sh
multiagent local team list
multiagent local team show [agent]
multiagent local team add <agent> --role <role> [--mode worker|interactive]
multiagent local team set <agent> [--role <role>] [--mode worker|interactive]
multiagent local team remove <agent>
multiagent local team edit
```

Runtime:

```sh
multiagent local init
multiagent local update [--roles]
multiagent local start [--restart]
multiagent local stop
multiagent local restart
multiagent local status
```

Interactive input:

```sh
multiagent local prompt <agent> "message"
multiagent local prompt --quiet <agent> "message"
multiagent local log [-f] <agent>
multiagent agent input <agent> "message"
```

Task and job commands remain file-backed in
`~/.multiagent/state/<instance-id>` and exposed through the
`multiagent agent task ...` and `multiagent agent job ...` command groups.

## Interactive Agents

Interactive agents use `pi --mode rpc` and expose:

```text
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.sock
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.json
```

The socket is the live control channel. `rpc.json` makes the socket discoverable
for tools and future integrations. Heartbeat helpers send input through the same
channel.

## Dashboard

`multiagent dashboard` reads running repositories from:

```text
~/.multiagent/instances/
```

The dashboard does not start a hidden agent. It can send messages only to
interactive agents that are already configured and running.

## Future Work

- Consider configurable interactive socket roots for container deployments.
- Add richer `options` validation if more per-agent settings are introduced.
- Keep task/job state file-backed until there is a concrete need for a database.
- Preserve the invariant that a repository's agent system is self-contained.
