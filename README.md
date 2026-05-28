# MULTIAGENT

MULTIAGENT defines local agent systems inside a Git repository. The repo owns
the team file and optional role overrides. Runtime state lives in the user-level
MULTIAGENT state tree, and the generic protocol plus runtime tools are provided
by the installed Python package. There is no system-wide daemon. Running
repositories are discoverable through links under `~/.multiagent/instances/`.

The user-facing command is:

```sh
multiagent <command>
```

## Prerequisites

- Git, Python 3.10 or newer, and `pip`.
- A POSIX `sh` for the runtime helper scripts.
- `pi`, which is the only supported agent runtime.
- A working Pi login/provider configuration for native local runs.
- For Docker runs today, a working host Pi `openai-codex` provider. The Docker
  host-auth bridge is deliberately no-secret: it uses a local proxy and does not
  copy host provider tokens into the container.
- Docker, only for `multiagent docker ...` containerized runs. The current user
  must be able to run `docker`.

## Build And Install

From the project root, not from the `git_multiagent/` import package directory:

```sh
cd /path/to/git-agents
python3 -m pip install --user -e .
```

For a system or virtualenv install, drop `--user`:

```sh
python3 -m pip install -e .
```

Make sure the Python user script directory is on `PATH`; on typical Linux
installs this is `~/.local/bin`. Verify the installed commands with:

```sh
multiagent --help
multiagent local --help
multiagent agent --help
multiagent docker start --help
```

For a full fresh-machine walkthrough, including Pi setup, Docker start, and the
dashboard, see [GETTING_STARTED.md](GETTING_STARTED.md).

The Python install creates the `multiagent` command. The Docker image is built
by the Docker command path, not by pip. `multiagent docker start` builds the
local Docker image on first start, and rebuilds/recreates stale containers when
the installed source or Docker run configuration changes. To prebuild it:

```sh
multiagent docker build-image
```

## Fresh Repository

Initialize the Git repository first, then initialize MULTIAGENT repo config:

```sh
mkdir -p /tmp/demo-repo
cd /tmp/demo-repo
git init
printf "# Demo\n" > README.md
git add README.md
git commit -m "Initial commit"

multiagent local init
git add .multiagent
git commit -m "Initialize MULTIAGENT"
```

`multiagent local init` creates `.multiagent/team.toml`. The default team has
worker agents only. To add an interactive agent:

```sh
multiagent local team list
multiagent local team add operator --role planner --mode interactive --heartbeat 15
git add .multiagent/team.toml
git commit -m "Add interactive MULTIAGENT operator"
```

## Run Locally

From the initialized repository:

```sh
multiagent local start
multiagent local status
multiagent local prompt operator "summarize current status"
multiagent local prompt --quiet operator "leave the response in the log"
multiagent local log -f operator
multiagent local agents list
```

Create durable work through the task/job commands:

```sh
printf "# My Task\n\nDo the thing.\n" > spec.md
multiagent agent task create my-task spec.md
multiagent agent job list
```

Stop or restart the local supervisor with:

```sh
multiagent local stop
multiagent local restart
```

## Run In Docker

Initialize the repository with `multiagent local init` first, as shown above.
Then start the same MULTIAGENT system inside Docker:

```sh
cd /tmp/demo-repo
multiagent docker start "$PWD" --build
multiagent docker status
multiagent dashboard
```

There is no separate Docker init command. `.multiagent/team.toml` is repository
configuration, so `multiagent local init` creates it once; `multiagent docker`
only controls where the supervisor and agents run.

Add transparent host path access with `--mount`. The mount appears at the same
absolute path inside the container:

```sh
multiagent docker start "$PWD" --mount ~/work/shared:ro
multiagent docker start "$PWD" --mount ~/work/shared:rw --device /dev/ttyUSB0
```

Stop keeps the container and its installed packages. Destroy removes the
container writable layer:

```sh
multiagent docker logs "$PWD"
multiagent docker stop "$PWD"
multiagent docker destroy "$PWD"
```

## Repository Layout

The stationary side can be committed:

```text
.multiagent/team.toml
.multiagent/roles/        optional role overrides
.multiagent/specs/        optional
```

Runtime state lives under:

```text
~/.multiagent/state/<instance-id>/
```

`multiagent local init` installs default `team.toml`. The MULTIAGENT generic
protocol is package-owned and injected into Pi context when agents launch. Role
instructions are resolved from repo-local overrides or package templates. Put
repository policy in normal project documentation and repo-local role overrides.

`multiagent local update` refreshes instance state shape. Runtime helpers are
package-owned and reached through `multiagent agent ...`; they are not copied
into the repository. `update` does not rewrite repo-local roles. Use
`multiagent local update --roles` only when you explicitly want to refresh the
local role override templates.

After updating MULTIAGENT itself, run this in each repository that already has
MULTIAGENT installed:

```sh
multiagent local update
multiagent local restart
```

## Team Configuration

Agents are configured in `.multiagent/team.toml` with singular `[[agent]]`
tables. The default team has worker agents only. Interactive agents are created
the same way by setting `mode = "interactive"`. Heartbeat is per-agent
configuration; if `options.heartbeat` is absent, heartbeat is disabled.

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

Worker agents claim queued jobs and exit after processing one job. Interactive
agents keep a persistent Pi RPC session and accept live input through a Unix
socket recorded in:

```text
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.sock
~/.multiagent/state/<instance-id>/agents/<agent>/rpc.json
```

`multiagent local prompt <agent> ...` and the dashboard use that socket.

## Docker Details

`multiagent docker` starts a repository's MULTIAGENT system inside Docker while
keeping repository paths transparent between host and container. It is the
containerized launcher for `multiagent local start --foreground`; the host
launcher owns dashboard registration for the containerized run.

The target repository is bind-mounted at the same absolute path inside the
container. The shared state base `~/.multiagent/state/` is also mounted at
the same absolute path so containerized systems use the same instance state tree
as native systems. Each run gets a per-run handoff directory under
`~/.multiagent/runs/<name>`, mounted at the same path, for generated Pi
configuration and the host auth proxy socket. `/home/gitmultiagent` is the
container's own home in its Docker writable layer.

`multiagent docker stop` stops the container and removes the host dashboard
registration, but it does not remove the container. System packages installed
inside the container with normal tools such as `sudo apt install ...` remain in
that container and are available after the next `multiagent docker start`.
`multiagent docker destroy` stops and removes the container, which also removes that
writable-layer state.

Pi runs inside the container with generated `/home/gitmultiagent/.pi` settings
that point model requests at a host auth proxy over a per-run Unix socket. The
current Docker host-auth adapter supports Pi's `openai-codex` provider: the
host proxy owns the real OAuth credentials from the active host Pi agent
directory (`PI_CODING_AGENT_DIR` if set, otherwise `~/.pi/agent`). The container
never receives the host `auth.json`, access token, or refresh token.

### Major Limitation

Native local runs pass each agent's `model` from `.multiagent/team.toml` directly
to Pi as `--model <model>`. That model may be provider-qualified, such as
`anthropic/claude-...` or `openai-codex/gpt-...`; Pi resolves it using the host
Pi configuration.

Docker runs do not currently project arbitrary Pi providers. The Docker auth
projection is hard-coded to the `openai-codex` host-auth proxy and rejects host
Pi configurations whose `defaultProvider` is not `openai-codex`. This is an
implementation limitation of the current no-secret Docker auth bridge, not a
team file concept. Supporting other providers requires provider-specific
no-secret adapters instead of copying host credentials into Docker.

All Pi tools and extensions run inside the container. The base image installs
Pi and `pi-web-access`; the generated Pi settings enable only `npm:pi-web-access`
by default. Extra `--mount PATH[:ro|:rw]` entries are canonicalized and
deduplicated. If a read-only parent mount contains the repository, the repository
is still mounted again read-write so repository writes work normally.

## Dashboard

`multiagent local start` links the running repository into:

```text
~/.multiagent/instances/
```

Each entry points at the instance state directory under
`~/.multiagent/state/<instance-id>/`. The dashboard reads status from that
real state directory; it does not maintain a second copy of state.

```sh
multiagent dashboard
```

The dashboard is served directly from installed package resources.

## Pi-Based Agents

MULTIAGENT is Pi-based. Pi is always the agent runtime; `team.toml` does not
choose runtime backends or auth providers. `model` is configurable per agent
and is passed to Pi as `--model <model>`, so provider-qualified Pi model strings
are valid in native local runs. Docker runs currently project only a no-secret
host-auth adapter for Pi's `openai-codex` provider.

For stronger research and solution-finding, configure Pi directly. For example,
`pi-web-access` adds web search, URL fetching, code/docs search, GitHub cloning,
PDF extraction, and video extraction:

```sh
pi install npm:pi-web-access
```

Whether web search is available is a Pi configuration choice. MULTIAGENT does
not grant web search as part of the task protocol, and it does not provide a
separate web-tool permission layer outside the tools exposed by Pi.

## Current Scope

Implemented now:

- packageable `multiagent` CLI entry point
- Git repository discovery with normal Git plumbing
- clean `init` and runtime refresh with `update`
- multi-repository dashboard with `multiagent dashboard`
- containerized repository runs with `multiagent docker`
- MULTIAGENT-owned generic protocol injected into Pi context at launch
- role, rules, team, task, job, status, log, prompt, and recovery commands
- filesystem-backed runtime state
- worker and interactive agents configured in `.multiagent/team.toml`
- per-interactive-agent heartbeat via `options = { heartbeat = <minutes> }`
- interactive input through per-agent Unix sockets
- `multiagent local start` supervises configured agents and links running
  repositories under `~/.multiagent/instances/`

See [docs/TEAM_TOML.md](docs/TEAM_TOML.md) for the team format,
[docs/LAYER_ONE.md](docs/LAYER_ONE.md) for the installed runtime contract,
[docs/LAYERS.md](docs/LAYERS.md) for the layering model, and
[docs/RUNNING_TESTS.md](docs/RUNNING_TESTS.md) for test-running guardrails.
