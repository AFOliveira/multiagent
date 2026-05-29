# Getting Started

This guide starts from a fresh Linux machine and ends with a full MULTIAGENT
system running in Docker, visible in the dashboard, with one interactive agent
you can chat with.

## 1. Install Host Prerequisites

Install Git, Python, Docker, Node.js, and npm. On a Debian or Ubuntu machine:

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3 python3-pip python3-venv docker.io
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
```

Allow your user to run Docker, then start a new login shell:

```sh
sudo usermod -aG docker "$USER"
newgrp docker
docker run --rm hello-world
```

Install Pi on the host:

```sh
sudo npm install -g @mariozechner/pi-coding-agent
pi --version
```

## 2. Configure Pi On The Host

Run Pi once on the host and complete its login/configuration flow:

```sh
pi --provider openai-codex --model gpt-5.5 "Reply with: pi is configured"
```

For the current Docker path, the host Pi agent settings must use the
`openai-codex` provider. Check:

```sh
grep -q '"defaultProvider"[[:space:]]*:[[:space:]]*"openai-codex"' ~/.pi/agent/settings.json
test -f ~/.pi/agent/auth.json
```

If the `grep` command fails, run `pi config` and set the default provider to
`openai-codex`, then run the test prompt again.

## 3. Install MULTIAGENT

Clone this repository and install it from the project root:

```sh
git clone https://github.com/glguida/multiagent.git ~/multiagent
cd ~/multiagent
python3 -m pip install --user -e .
```

Make sure the Python user script directory is on `PATH`:

```sh
export PATH="$HOME/.local/bin:$PATH"
multiagent --help
multiagent docker start --help
```

Add the `PATH` export to your shell startup file if `multiagent` is not found in
new terminals.

## 4. Create A Fresh Repository

Create a normal Git repository first:

```sh
mkdir -p /tmp/multiagent-demo
cd /tmp/multiagent-demo
git init
git config user.name "MULTIAGENT Demo"
git config user.email "demo@example.invalid"
printf "# MULTIAGENT Demo\n" > README.md
git add README.md
git commit -m "Initial commit"
```

Initialize MULTIAGENT repository configuration:

```sh
multiagent local init
multiagent local team list
```

Add one interactive agent so the dashboard has a chat target:

```sh
multiagent local team add operator --role planner --mode interactive --heartbeat 15 --model gpt-5.5
git add .multiagent README.md
git commit -m "Initialize MULTIAGENT"
```

The default `team.toml` also contains worker agents for planner, implementer,
reviewer, and committer roles.

## 5. Start The System In Docker

From the repository root:

```sh
cd /tmp/multiagent-demo
multiagent docker start "$PWD" --build
multiagent docker status
```

The first start builds the local Docker image. Later starts rebuild and recreate
the container when the installed MULTIAGENT source or Docker run configuration
changes.

Give the agents extra host paths only when needed:

```sh
multiagent docker start "$PWD" --mount ~/work/shared:ro
multiagent docker start "$PWD" --mount ~/work/shared:rw --device /dev/ttyUSB0
```

All mounts appear at the same absolute path inside Docker. Treat `--mount`,
`--device`, `--env`, and `--network` as granting the agents that access.

## 6. Open The Dashboard

Start the dashboard on localhost:

```sh
multiagent dashboard
```

Open the printed URL in a browser. The dashboard reads running systems from:

```text
~/.multiagent/instances/
```

Click the demo system, then open the chat view and select the `operator`
interactive agent. Send a short prompt, for example:

```text
Summarize this repository and tell me what jobs are available.
```

The dashboard has no authentication. Keep the default `127.0.0.1` bind unless
you are deliberately exposing it on a trusted network.

## 7. Create Work For Worker Agents

Create a task from the repository root. This also enqueues the initial planner
job:

```sh
cat > spec.md <<'EOF'
# Improve README

Review the README and propose one small documentation improvement.
EOF

multiagent agent task create readme-docs spec.md
multiagent agent job list
```

The worker agents running in Docker claim pending jobs that match their roles.
Use the dashboard to watch tasks, jobs, agents, and transcripts.

## 8. Stop Or Remove The Docker Run

Stop keeps the container writable layer, including packages installed inside the
container with tools such as `sudo apt install ...`:

```sh
multiagent docker stop "$PWD"
multiagent docker start "$PWD"
```

Destroy removes the container and its writable layer:

```sh
multiagent docker destroy "$PWD"
```

State remains under `~/.multiagent/state/<instance-id>/` unless you remove it
yourself.

## Current Docker Model Limitation

`team.toml` can select a model:

```toml
[[agent]]
name = "operator"
role = "planner"
mode = "interactive"
model = "gpt-5.5"
```

For native local runs, MULTIAGENT passes that value directly to Pi as
`--model <model>`. Pi resolves provider-qualified names such as
`openai-codex/gpt-5.5` or local provider names using the host Pi configuration.

For Docker runs, MULTIAGENT deliberately does not mount or copy the host Pi
configuration into the container. Instead it generates a minimal container Pi
configuration that points at a host-auth proxy. Today that generator only knows
how to create the no-secret `openai-codex` bridge, so Docker runs should use
model IDs available through that bridge, such as `gpt-5.5`, rather than
provider-qualified local model names.

That is why the limitation exists even for tokenless local models: a model name
is not enough. Docker still needs a Pi provider definition, model list, endpoint,
and container-reachable network address. A local model server bound to
`127.0.0.1` on the host is not `127.0.0.1` inside the container. Supporting local
models cleanly means adding a no-secret Docker provider projection for local
endpoints, not putting provider configuration or tokens in `team.toml`.
