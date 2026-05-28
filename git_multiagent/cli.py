from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import socket
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    tomllib = None


PACKAGE = "git_multiagent"
CONFIG_DIR = ".multiagent"
STATE_DIR_NAME = "state"
DEFAULT_REGISTRY_DIR = "~/.multiagent"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
VALID_MODES = {"worker", "interactive"}
PI_COMMAND = "pi"
STATE_SUBDIRS = ("tasks", "jobs", "agents", "runs", "logs")


class UserError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Repo:
    root: Path
    prefix: str
    git_dir: Path
    state_dir: Path
    @property
    def config_dir(self) -> Path:
        return self.root / CONFIG_DIR


@dataclass
class ManagedProcess:
    label: str
    command: list[str]
    proc: subprocess.Popen[bytes]
    agent_name: str | None = None


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_name(label: str, value: str) -> None:
    if not value or not NAME_RE.match(value):
        raise UserError(
            f"invalid {label} '{value}': use letters, numbers, dot, underscore, or hyphen"
        )


def run_git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise UserError(detail or "not inside a Git repository")
    return proc.stdout.rstrip("\n")


def discover_repo(cwd: Path | None = None) -> Repo:
    cwd = cwd or Path.cwd()
    root = Path(run_git(["rev-parse", "--show-toplevel"], cwd)).resolve()
    prefix = run_git(["rev-parse", "--show-prefix"], cwd)
    git_dir = Path(run_git(["rev-parse", "--absolute-git-dir"], cwd)).resolve()
    state_dir = registry_state_dir(root)
    return Repo(
        root=root,
        prefix=prefix,
        git_dir=git_dir,
        state_dir=state_dir.resolve(),
    )


def package_path(*parts: str):
    return resources.files(PACKAGE).joinpath(*parts)


def read_package_text(*parts: str) -> str:
    return package_path(*parts).read_text(encoding="utf-8")


def package_runtime_root() -> Path:
    return Path(str(package_path("runtime"))).resolve()


def package_runtime_bin(name: str) -> Path:
    return package_runtime_root() / "bin" / name


def package_runtime_tool(name: str) -> Path:
    return package_runtime_root() / "tools" / name


def write_bytes_atomic(path: Path, data: bytes, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_bytes(data)
    if executable:
        tmp.chmod(0o755)
    tmp.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def registry_dir() -> Path:
    return Path(os.environ.get("GIT_MULTIAGENT_REGISTRY_DIR", DEFAULT_REGISTRY_DIR)).expanduser()


def registry_instance_id(repo_root: Path) -> str:
    digest = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    repo_name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_root.name).strip(".-")
    if not repo_name:
        repo_name = "repo"
    return f"{repo_name}-{digest[:12]}"


def registry_state_dir(repo_root: Path) -> Path:
    state_override = os.environ.get("GIT_MULTIAGENT_STATE_DIR", "").strip()
    if state_override:
        return Path(state_override).expanduser().resolve()
    return (registry_dir() / STATE_DIR_NAME / registry_instance_id(repo_root)).resolve()


def registry_instances_dir() -> Path:
    return registry_dir() / "instances"


def registry_instance_path(repo: Repo) -> Path:
    return registry_instances_dir() / registry_instance_id(repo.root)


def registry_metadata_path(repo: Repo) -> Path:
    path = registry_instance_path(repo)
    return path.with_name(f"{path.name}.json")


def local_manages_registry() -> bool:
    return os.environ.get("GIT_MULTIAGENT_CONTAINER", "").strip() != "docker"


def read_registry_metadata(repo: Repo) -> dict[str, Any]:
    path = registry_metadata_path(repo)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def registry_host_pid(repo: Repo) -> int | None:
    value = read_registry_metadata(repo).get("hostPid")
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def docker_container_running(name: str | None) -> bool:
    if not name:
        return False
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def registry_supervisor_running(repo: Repo) -> bool:
    metadata = read_registry_metadata(repo)
    host_pid = registry_host_pid(repo)
    if pid_is_running(host_pid):
        return True
    if metadata.get("runtime") == "docker":
        return docker_container_running(str(metadata.get("containerName") or ""))
    return False


def write_registry_instance(repo: Repo) -> None:
    if not local_manages_registry():
        return
    path = registry_instance_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = repo.state_dir.resolve()
    if path.is_symlink():
        try:
            if path.resolve(strict=True) == target:
                try:
                    registry_metadata_path(repo).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                return
        except OSError:
            pass
        path.unlink()
    elif path.exists():
        raise UserError(f"cannot register MULTIAGENT instance; path already exists: {path}")

    os.symlink(target, path, target_is_directory=True)
    try:
        registry_metadata_path(repo).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def remove_registry_instance(repo: Repo) -> None:
    if not local_manages_registry():
        return
    for path in (registry_metadata_path(repo), registry_instance_path(repo)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            pass
        except OSError:
            pass


def read_text(path: Path, fallback: str = "", max_bytes: int = 512 * 1024) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return fallback



def ensure_state(repo: Repo) -> None:
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    for name in STATE_SUBDIRS:
        (repo.state_dir / name).mkdir(parents=True, exist_ok=True)
    write_text_atomic(repo.state_dir / "repo-root", str(repo.root) + "\n")
    write_text_atomic(repo.state_dir / "instance-id", registry_instance_id(repo.root) + "\n")
    config = repo.state_dir / "config.json"
    if not config.exists():
        write_text_atomic(
            config,
            json.dumps(
                {
                    "version": 1,
                    "created_at": timestamp(),
                    "state": "filesystem",
                },
                indent=2,
            )
            + "\n",
        )


def sync_runtime(repo: Repo) -> None:
    ensure_state(repo)
    materialize_team(repo)


def update_runtime(repo: Repo, refresh_roles: bool = False) -> None:
    ensure_state(repo)
    materialize_team(repo)
    if refresh_roles:
        refresh_all_roles(repo)


def pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").replace("\0", " ").strip()


def supervisor_pid_is_running(pid: int | None) -> bool:
    if not pid_is_running(pid):
        return False
    assert pid is not None
    if pid == os.getpid():
        return False
    cmdline = proc_cmdline(pid)
    if not cmdline:
        return True
    if "git_multiagent.cli" in cmdline and "_supervisor" in cmdline:
        return True
    if "multiagent" in cmdline and "local" in cmdline and "start" in cmdline and "--foreground" in cmdline:
        return True
    if "git_multiagent" in cmdline and "local" in cmdline and "start" in cmdline and "--foreground" in cmdline:
        return True
    return False


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def list_dirs(path: Path) -> list[str]:
    try:
        return sorted(item.name for item in path.iterdir() if item.is_dir())
    except OSError:
        return []


def print_table(headers: list[str], rows: list[list[Any]]) -> None:
    values = [[str(cell) if cell is not None and str(cell) else "-" for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in values:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in values:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def user_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def run_runtime_tool(repo: Repo, tool: str, args: list[str]) -> int:
    sync_runtime(repo)
    command = [str(package_runtime_bin(tool)), *args]
    env = os.environ.copy()
    env["GIT_MULTIAGENT_ROOT"] = str(repo.config_dir)
    env["GIT_MULTIAGENT_REPO_ROOT"] = str(repo.root)
    env["GIT_MULTIAGENT_STATE_DIR"] = str(repo.state_dir)
    proc = subprocess.run(command, cwd=repo.config_dir, env=env, check=False)
    return proc.returncode


def required_runtime_commands(repo: Repo) -> set[str]:
    agents, _source = effective_team(repo)
    return {PI_COMMAND} if agents else set()


def validate_required_commands(repo: Repo) -> None:
    missing = [command for command in required_runtime_commands(repo) if shutil.which(command) is None]
    if missing:
        raise UserError("required command not found: " + ", ".join(sorted(set(missing))))


def packaged_role_names() -> list[str]:
    base = package_path("templates", "roles")
    return sorted(
        path.name.removesuffix(".md")
        for path in base.iterdir()
        if path.is_file() and path.name.endswith(".md")
    )


def local_role_path(repo: Repo, name: str) -> Path:
    return repo.config_dir / "roles" / f"{name}.md"


def packaged_role_text(name: str) -> str | None:
    path = package_path("templates", "roles", f"{name}.md")
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def effective_role_text(repo: Repo, name: str) -> tuple[str, str]:
    validate_name("role", name)
    local = local_role_path(repo, name)
    if local.is_file():
        return local.read_text(encoding="utf-8"), str(local)
    packaged = packaged_role_text(name)
    if packaged is None:
        raise UserError(f"role not found: {name}")
    return packaged, "package"


def local_role_names(repo: Repo) -> list[str]:
    roles_dir = repo.config_dir / "roles"
    try:
        return sorted(
            path.name.removesuffix(".md")
            for path in roles_dir.iterdir()
            if path.is_file() and path.name.endswith(".md")
        )
    except OSError:
        return []


def materialize_role(repo: Repo, name: str) -> Path:
    text, _source = effective_role_text(repo, name)
    path = local_role_path(repo, name)
    if not path.exists():
        write_text_atomic(path, text)
    return path


def materialize_all_roles(repo: Repo) -> None:
    for name in packaged_role_names():
        materialize_role(repo, name)


def refresh_all_roles(repo: Repo) -> None:
    for name in packaged_role_names():
        text = packaged_role_text(name)
        if text is not None:
            write_text_atomic(local_role_path(repo, name), text)


def effective_rules_text(repo: Repo) -> tuple[str, str]:
    return read_package_text("templates", "AGENTS.md"), "package"


def parse_toml_value(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if value.startswith("{") and value.endswith("}"):
        body = value[1:-1].strip()
        result: dict[str, Any] = {}
        if not body:
            return result
        for part in body.split(","):
            if "=" not in part:
                raise UserError("inline options must use key = value entries")
            key, raw = part.split("=", 1)
            result[key.strip()] = parse_toml_value(raw)
        return result
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def parse_toml_subset(text: str) -> dict[str, Any]:
    agents: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "[[agent]]":
            current = {}
            agents.append(current)
            continue
        if line == "[[agents]]":
            raise UserError("team config must use [[agent]], not [[agents]]")
        if line.startswith("["):
            current = None
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = parse_toml_value(value)
    return {"agent": agents}


def parse_heartbeat_value(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise UserError("agent heartbeat must be a positive minute count")
    if isinstance(value, int):
        minutes = value
    else:
        text = str(value).strip().lower()
        if not text:
            return 0
        for suffix in ("minutes", "minute", "mins", "min", "m"):
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                break
        try:
            minutes = int(text)
        except ValueError as exc:
            raise UserError('agent heartbeat must be a positive minute count, such as 15 or "15m"') from exc
    if minutes < 1:
        raise UserError("agent heartbeat must be at least 1 minute; omit options.heartbeat to disable it")
    return minutes


def parse_agent_options(value: Any, agent_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise UserError(f"options for agent '{agent_name}' must be an inline table")
    options: dict[str, Any] = {}
    heartbeat = parse_heartbeat_value(value.get("heartbeat"))
    if heartbeat:
        options["heartbeat"] = heartbeat
    return options


def parse_team_text(text: str) -> list[dict[str, Any]]:
    if tomllib is not None:
        data = tomllib.loads(text)
    else:
        data = parse_toml_subset(text)
    if "agents" in data:
        raise UserError("team config must use [[agent]], not [[agents]]")
    agents = data.get("agent", [])
    if not isinstance(agents, list):
        raise UserError("team config must use [[agent]] entries")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(agents, start=1):
        if not isinstance(item, dict):
            raise UserError(f"team agent #{index} must be a table")
        name = str(item.get("name", "")).strip()
        role = str(item.get("role", "")).strip()
        mode = str(item.get("mode", "worker") or "worker").strip()
        model = str(item.get("model", "") or "").strip()
        validate_name("agent", name)
        validate_name("role", role)
        if mode not in VALID_MODES:
            raise UserError(
                f"invalid mode '{mode}' for agent '{name}': expected "
                + ", ".join(sorted(VALID_MODES))
            )
        options = parse_agent_options(item.get("options"), name)
        if options.get("heartbeat") and mode != "interactive":
            raise UserError(f"agent '{name}' must use mode = \"interactive\" to enable heartbeat")
        row: dict[str, Any] = {"name": name, "role": role, "mode": mode}
        if model:
            row["model"] = model
        if options:
            row["options"] = options
        result.append(row)
    return result


def toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_team(agents: list[dict[str, Any]]) -> str:
    lines = [
        "# MULTIAGENT team",
        "# Edit with: multiagent local team edit",
        "",
    ]
    for agent in agents:
        lines.append("[[agent]]")
        lines.append(f"name = {toml_quote(agent['name'])}")
        lines.append(f"role = {toml_quote(agent['role'])}")
        lines.append(f"mode = {toml_quote(agent.get('mode', 'worker'))}")
        if agent.get("model"):
            lines.append(f"model = {toml_quote(agent['model'])}")
        heartbeat = (agent.get("options") or {}).get("heartbeat")
        if heartbeat:
            lines.append(f"options = {{ heartbeat = {int(heartbeat)} }}")
        lines.append("")
    return "\n".join(lines)


def effective_team_text(repo: Repo) -> tuple[str, str]:
    local = repo.config_dir / "team.toml"
    if local.is_file():
        return local.read_text(encoding="utf-8"), str(local)
    return read_package_text("templates", "team.toml"), "package"


def effective_team(repo: Repo) -> tuple[list[dict[str, Any]], str]:
    text, source = effective_team_text(repo)
    return parse_team_text(text), source


def materialize_team(repo: Repo) -> Path:
    path = repo.config_dir / "team.toml"
    if not path.exists():
        text, _source = effective_team_text(repo)
        write_text_atomic(path, text)
    return path


def write_local_team(repo: Repo, agents: list[dict[str, str]]) -> None:
    path = repo.config_dir / "team.toml"
    write_text_atomic(path, format_team(agents))


def first_markdown_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
    return fallback


def iso_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    except OSError:
        return ""


def task_records(repo: Repo) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for task_id in list_dirs(repo.state_dir / "tasks"):
        task_dir = repo.state_dir / "tasks" / task_id
        spec = read_text(task_dir / "spec.md")
        records.append(
            {
                "id": task_id,
                "state": read_text(task_dir / "state", "open", 1024).strip() or "open",
                "title": first_markdown_title(spec, task_id),
                "updated": iso_mtime(task_dir),
            }
        )
    return records


def job_records(repo: Repo) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for job_id in list_dirs(repo.state_dir / "jobs"):
        job_dir = repo.state_dir / "jobs" / job_id
        records.append(
            {
                "id": job_id,
                "status": read_text(job_dir / "status", "unknown", 1024).strip() or "unknown",
                "task_id": read_text(job_dir / "task-id", "", 1024).strip(),
                "role": read_text(job_dir / "role", "", 1024).strip(),
                "agent_id": read_text(job_dir / "agent-id", "", 1024).strip(),
            }
        )
    return records


def supervisor_metadata(repo: Repo) -> dict[str, Any]:
    text = read_text(repo.state_dir / "runs" / "supervisor.json", "{}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def trust_recorded_pids(repo: Repo) -> bool:
    metadata = supervisor_metadata(repo)
    container_runtime = metadata.get("container_runtime")
    if not container_runtime:
        return True
    return os.environ.get("GIT_MULTIAGENT_CONTAINER") == container_runtime


def agent_records(repo: Repo, trust_pids: bool = True) -> list[dict[str, Any]]:
    jobs = job_records(repo)
    records: list[dict[str, Any]] = []
    for agent_id in list_dirs(repo.state_dir / "agents"):
        if agent_id.startswith("."):
            continue
        agent_dir = repo.state_dir / "agents" / agent_id
        runner_pid = read_pid(agent_dir / "runner.pid")
        engine_pid = read_pid(agent_dir / "engine.pid")
        active_jobs = [
            job["id"]
            for job in jobs
            if job.get("agent_id") == agent_id and job.get("status") in {"claimed", "running"}
        ]
        records.append(
            {
                "id": agent_id,
                "role": read_text(agent_dir / "role", "", 1024).strip(),
                "mode": "interactive" if (agent_dir / "interactive").is_file() else "worker",
                "current_job": read_text(agent_dir / "current-job", "", 1024).strip(),
                "runner_pid": runner_pid,
                "engine_pid": engine_pid,
                "running": trust_pids and (pid_is_running(runner_pid) or pid_is_running(engine_pid)),
                "active_jobs": active_jobs,
            }
        )
    return records


def status_data(repo: Repo) -> dict[str, Any]:
    tasks = task_records(repo) if (repo.state_dir / "tasks").is_dir() else []
    jobs = job_records(repo) if (repo.state_dir / "jobs").is_dir() else []
    trust_pids = trust_recorded_pids(repo)
    host_pid = registry_host_pid(repo)
    host_supervisor_running = registry_supervisor_running(repo)
    agents = agent_records(repo, trust_pids=trust_pids) if (repo.state_dir / "agents").is_dir() else []
    running_agents = sum(1 for agent in agents if agent.get("running"))
    recorded_supervisor_pid = read_pid(repo.state_dir / "runs" / "supervisor.pid")
    supervisor_pid = host_pid or recorded_supervisor_pid
    return {
        "repo_root": str(repo.root),
        "git_dir": str(repo.git_dir),
        "state_dir": str(repo.state_dir),
        "config_dir": str(repo.config_dir),
        "initialized": repo.state_dir.is_dir(),
        "supervisor_pid": supervisor_pid,
        "supervisor_running": host_supervisor_running or (trust_pids and supervisor_pid_is_running(recorded_supervisor_pid)),
        "running_agent_count": running_agents,
        "task_count": len(tasks),
        "job_count": len(jobs),
        "failed_job_count": sum(1 for job in jobs if job.get("status") == "failed"),
    }


def cmd_init(args: argparse.Namespace) -> int:
    repo = discover_repo()
    sync_runtime(repo)
    if args.tracked_config:
        (repo.config_dir / "specs").mkdir(parents=True, exist_ok=True)
    print(f"Initialized multiagent state: {repo.state_dir}")
    print(f"Installed multiagent config: {repo.config_dir}")
    if args.tracked_config:
        print(f"Created optional specs directory: {repo.config_dir / 'specs'}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    repo = discover_repo()
    update_runtime(repo, refresh_roles=args.roles)
    print(f"Updated multiagent instance state: {repo.state_dir}")
    if args.roles:
        print(f"Updated default role templates: {repo.config_dir / 'roles'}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    data = status_data(repo)
    if data["supervisor_running"]:
        supervisor = "running"
    elif data["running_agent_count"]:
        supervisor = f"stopped (managed processes running: {data['running_agent_count']})"
    elif data["supervisor_pid"]:
        supervisor = "stopped (stale pid file)"
    else:
        supervisor = "stopped"
    rows = [
        ["initialized", data["initialized"]],
        ["supervisor", supervisor],
        ["supervisor_pid", data["supervisor_pid"] or ""],
        ["managed_processes", data["running_agent_count"]],
        ["tasks", data["task_count"]],
        ["jobs", data["job_count"]],
        ["failed_jobs", data["failed_job_count"]],
        ["state", data["state_dir"]],
        ["config", data["config_dir"]],
    ]
    print_table(["field", "value"], rows)
    return 0


def cmd_role_list(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    names = sorted(set(packaged_role_names()) | set(local_role_names(repo)))
    rows: list[list[str]] = []
    for name in names:
        local = local_role_path(repo, name)
        packaged = packaged_role_text(name)
        if local.is_file():
            source = "local"
            changed = "yes" if packaged is not None and local.read_text(encoding="utf-8") != packaged else "no"
        else:
            source = "package"
            changed = "no"
        rows.append([name, source, changed])
    print_table(["role", "source", "changed"], rows)
    return 0


def cmd_role_show(args: argparse.Namespace) -> int:
    repo = discover_repo()
    text, _source = effective_role_text(repo, args.name)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_role_add(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("role", args.name)
    path = local_role_path(repo, args.name)
    if path.exists():
        raise UserError(f"role already exists: {args.name}")
    if args.from_role:
        text, _source = effective_role_text(repo, args.from_role)
        text = text.replace(f"# {args.from_role.title()}", f"# {args.name.title()}", 1)
    else:
        text = f"# {args.name.title()}\n\nDescribe the {args.name} role here.\n"
    write_text_atomic(path, text)
    print(path)
    return 0


def cmd_role_edit(args: argparse.Namespace) -> int:
    repo = discover_repo()
    path = materialize_role(repo, args.name)
    editor = os.environ.get("EDITOR")
    if not editor:
        print(path)
        print("Set EDITOR to open this file automatically.", file=sys.stderr)
        return 0
    proc = subprocess.run([*shlex.split(editor), str(path)], check=False)
    return proc.returncode


def cmd_role_diff(args: argparse.Namespace) -> int:
    repo = discover_repo()
    names = [args.name] if args.name else sorted(set(packaged_role_names()) | set(local_role_names(repo)))
    emitted = False
    for name in names:
        validate_name("role", name)
        local = local_role_path(repo, name)
        packaged = packaged_role_text(name)
        if not local.exists():
            continue
        if packaged is None:
            packaged = ""
        diff = difflib.unified_diff(
            packaged.splitlines(keepends=True),
            local.read_text(encoding="utf-8").splitlines(keepends=True),
            fromfile=f"package/{name}.md",
            tofile=str(local),
        )
        for line in diff:
            print(line, end="")
            emitted = True
    if not emitted and args.name:
        print(f"role {args.name} has no local changes")
    return 0


def cmd_role_reset(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("role", args.name)
    packaged = packaged_role_text(args.name)
    if packaged is None:
        raise UserError(f"package role not found: {args.name}")
    path = local_role_path(repo, args.name)
    if path.exists() and not args.yes:
        if not sys.stdin.isatty():
            raise UserError("refusing to overwrite role without --yes")
        answer = input(f"Reset {path}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            return 1
    write_text_atomic(path, packaged)
    print(path)
    return 0


def cmd_rules_show(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    text, _source = effective_rules_text(repo)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_team_list(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    agents, source = effective_team(repo)
    rows = []
    run_dir = repo.state_dir / "agents" / ".team-runs"
    for agent in agents:
        pid = read_pid(run_dir / f"{agent['name']}.pid")
        last_status = read_text(run_dir / f"{agent['name']}.last-status", "", 1024).strip()
        if pid_is_running(pid):
            state = "running"
        elif last_status and last_status != "0":
            state = "failed"
        else:
            state = "stopped"
        rows.append(
            [
                agent["name"],
                agent["role"],
                agent["mode"],
                agent.get("model", ""),
                str((agent.get("options") or {}).get("heartbeat", "")),
                state,
                source,
            ]
        )
    print_table(["agent", "role", "mode", "model", "heartbeat", "state", "source"], rows)
    return 0


def cmd_team_show(args: argparse.Namespace) -> int:
    repo = discover_repo()
    text, source = effective_team_text(repo)
    if not args.agent:
        print(text, end="" if text.endswith("\n") else "\n")
        return 0
    agents = parse_team_text(text)
    for agent in agents:
        if agent["name"] == args.agent:
            print(json.dumps({"source": source, **agent}, indent=2) + "\n")
            return 0
    raise UserError(f"agent not found in team: {args.agent}")


def cmd_team_add(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("agent", args.agent)
    validate_name("role", args.role)
    materialize_team(repo)
    agents, _source = effective_team(repo)
    if any(agent["name"] == args.agent for agent in agents):
        raise UserError(f"agent already exists: {args.agent}")
    row: dict[str, Any] = {"name": args.agent, "role": args.role, "mode": args.mode}
    if args.model:
        row["model"] = args.model
    if args.heartbeat is not None:
        row["options"] = {"heartbeat": parse_heartbeat_value(args.heartbeat)}
    if (row.get("options") or {}).get("heartbeat") and row["mode"] != "interactive":
        raise UserError("--heartbeat requires --mode interactive")
    agents.append(row)
    write_local_team(repo, agents)
    print(repo.config_dir / "team.toml")
    return 0


def cmd_team_remove(args: argparse.Namespace) -> int:
    repo = discover_repo()
    materialize_team(repo)
    agents, _source = effective_team(repo)
    kept = [agent for agent in agents if agent["name"] != args.agent]
    if len(kept) == len(agents):
        raise UserError(f"agent not found in team: {args.agent}")
    write_local_team(repo, kept)
    print(repo.config_dir / "team.toml")
    return 0


def cmd_team_set(args: argparse.Namespace) -> int:
    repo = discover_repo()
    materialize_team(repo)
    agents, _source = effective_team(repo)
    found = False
    for agent in agents:
        if agent["name"] != args.agent:
            continue
        found = True
        if args.role:
            validate_name("role", args.role)
            agent["role"] = args.role
        if args.mode:
            agent["mode"] = args.mode
        if args.model is not None:
            if args.model:
                agent["model"] = args.model
            else:
                agent.pop("model", None)
        if args.heartbeat is not None:
            agent.setdefault("options", {})["heartbeat"] = parse_heartbeat_value(args.heartbeat)
        if args.no_heartbeat:
            options = agent.get("options") or {}
            options.pop("heartbeat", None)
            if options:
                agent["options"] = options
            else:
                agent.pop("options", None)
        if (agent.get("options") or {}).get("heartbeat") and agent["mode"] != "interactive":
            raise UserError('heartbeat requires mode = "interactive"')
    if not found:
        raise UserError(f"agent not found in team: {args.agent}")
    if not any([args.role, args.mode, args.model is not None, args.heartbeat is not None, args.no_heartbeat]):
        raise UserError("team set requires --role, --mode, --model, --heartbeat, or --no-heartbeat")
    write_local_team(repo, agents)
    print(repo.config_dir / "team.toml")
    return 0


def cmd_team_edit(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    path = materialize_team(repo)
    editor = os.environ.get("EDITOR")
    if not editor:
        print(path)
        print("Set EDITOR to open this file automatically.", file=sys.stderr)
        return 0
    proc = subprocess.run([*shlex.split(editor), str(path)], check=False)
    return proc.returncode


def cmd_tasks_list(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    rows = [[task["id"], task["state"], task["title"], task["updated"]] for task in task_records(repo)]
    print_table(["task", "state", "title", "updated"], rows)
    return 0


def cmd_tasks_create(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("task", args.task)
    return run_runtime_tool(repo, "task-create", [args.task, str(user_path(args.spec_file))])


def cmd_tasks_show(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("task", args.task)
    task_dir = repo.state_dir / "tasks" / args.task
    if not task_dir.is_dir():
        raise UserError(f"task not found: {args.task}")
    print(f"task: {args.task}")
    print(f"state: {read_text(task_dir / 'state', 'open', 1024).strip() or 'open'}")
    for name in ("spec.md", "log.md", "result.md"):
        path = task_dir / name
        if path.is_file():
            print(f"\n## {name}\n")
            print(read_text(path), end="")
    return 0


def cmd_tasks_comment(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("task", args.task)
    message = " ".join(args.message)
    if not message:
        raise UserError("message required")
    return run_runtime_tool(repo, "task-comment", [args.task, message])


def cmd_tasks_state(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("task", args.task)
    command = [args.task, args.state]
    if args.message:
        command.extend(["-m", args.message])
    return run_runtime_tool(repo, "task-state", command)


def cmd_tasks_result(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("task", args.task)
    return run_runtime_tool(repo, "task-result", [args.task, str(user_path(args.result_file))])


def cmd_jobs_list(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    rows = [
        [job["id"], job["status"], job["role"], job["task_id"], job["agent_id"]]
        for job in job_records(repo)
    ]
    print_table(["job", "status", "role", "task", "agent"], rows)
    return 0


def cmd_jobs_create(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("job", args.job)
    validate_name("role", args.role)
    validate_name("task", args.task)
    return run_runtime_tool(
        repo,
        "job-create",
        [args.job, "-r", args.role, "-t", args.task, str(user_path(args.spec_file))],
    )


def cmd_jobs_reset(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("job", args.job)
    command = [args.job]
    if args.message:
        command.extend(["-m", args.message])
    if args.force:
        command.append("--force")
    return run_runtime_tool(repo, "job-reset", command)


def cmd_jobs_kill(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("job", args.job)
    command = [args.job]
    if args.message:
        command.extend(["-m", args.message])
    if args.force:
        command.append("--force")
    return run_runtime_tool(repo, "job-kill", command)


def cmd_jobs_orphans(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    return run_runtime_tool(repo, "job-orphans", [])


def cmd_jobs_reset_orphans(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    return run_runtime_tool(repo, "job-reset-orphans", [])


def cmd_jobs_reap(args: argparse.Namespace) -> int:
    repo = discover_repo()
    command = [str(args.minutes)] if args.minutes is not None else []
    return run_runtime_tool(repo, "job-reap", command)


def terminate_recorded_agent_processes(agent_dir: Path) -> int:
    signaled = 0
    engine_pid = read_pid(agent_dir / "engine.pid")
    runner_pid = read_pid(agent_dir / "runner.pid")

    if engine_pid and engine_pid != os.getpid():
        try:
            os.killpg(engine_pid, signal.SIGTERM)
            signaled += 1
        except ProcessLookupError:
            try:
                os.kill(engine_pid, signal.SIGTERM)
                signaled += 1
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise UserError(f"cannot signal engine pid {engine_pid}: {exc}") from exc
        except PermissionError:
            try:
                os.kill(engine_pid, signal.SIGTERM)
                signaled += 1
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise UserError(f"cannot signal engine pid {engine_pid}: {exc}") from exc

    if runner_pid and runner_pid != os.getpid():
        try:
            os.kill(runner_pid, signal.SIGTERM)
            signaled += 1
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise UserError(f"cannot signal runner pid {runner_pid}: {exc}") from exc

    deadline = time.time() + 3
    while time.time() < deadline:
        if not (pid_is_running(engine_pid) or pid_is_running(runner_pid)):
            return signaled
        time.sleep(0.1)

    if engine_pid and pid_is_running(engine_pid):
        try:
            os.killpg(engine_pid, signal.SIGKILL)
        except ProcessLookupError:
            try:
                os.kill(engine_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise UserError(f"cannot kill engine pid {engine_pid}: {exc}") from exc
        except PermissionError:
            try:
                os.kill(engine_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise UserError(f"cannot kill engine pid {engine_pid}: {exc}") from exc
    if runner_pid and pid_is_running(runner_pid):
        try:
            os.kill(runner_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise UserError(f"cannot kill runner pid {runner_pid}: {exc}") from exc

    return signaled


def clear_agent_runtime_files(agent_dir: Path) -> None:
    for name in ("engine.pid", "runner.pid", "busy", "rpc.sock", "rpc.json"):
        try:
            (agent_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def cmd_agents_list(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    rows = []
    for agent in agent_records(repo):
        if agent["running"] and agent["active_jobs"]:
            state = "busy"
        elif agent["running"]:
            state = "running"
        else:
            state = "stopped"
        rows.append(
            [
                agent["id"],
                agent["role"],
                agent["mode"],
                agent["current_job"],
                ",".join(agent["active_jobs"]),
                state,
            ]
        )
    print_table(["agent", "role", "mode", "current", "active_jobs", "state"], rows)
    return 0


def cmd_agents_reset(args: argparse.Namespace) -> int:
    repo = discover_repo()
    sync_runtime(repo)
    validate_name("agent", args.agent)

    agent_dir = repo.state_dir / "agents" / args.agent
    if not agent_dir.is_dir():
        raise UserError(f"agent not found: {args.agent}")

    message = args.message or f"Agent {args.agent} reset."
    reset_count = 0
    for job in job_records(repo):
        if job.get("agent_id") != args.agent or job.get("status") not in {"claimed", "running"}:
            continue
        command = [job["id"], "-m", message]
        if args.force:
            command.append("--force")
        rc = run_runtime_tool(repo, "job-reset", command)
        if rc != 0:
            return rc
        reset_count += 1

    signaled = 0
    if not args.no_kill:
        signaled = terminate_recorded_agent_processes(agent_dir)

    write_text_atomic(agent_dir / "current-job", "")
    clear_agent_runtime_files(agent_dir)
    print(f"reset agent {args.agent}: jobs reset={reset_count}, processes signaled={signaled}")
    return 0


def stop_supervisor(repo: Repo, quiet: bool = False) -> int:
    pid_file = repo.state_dir / "runs" / "supervisor.pid"
    pid = read_pid(pid_file)
    if not supervisor_pid_is_running(pid):
        try:
            pid_file.unlink()
        except OSError:
            pass
        remove_registry_instance(repo)
        if not quiet:
            print("multiagent supervisor is not running")
        return 0
    assert pid is not None
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise UserError(f"cannot stop supervisor pid {pid}: {exc}") from exc
    deadline = time.time() + 5
    while time.time() < deadline and pid_is_running(pid):
        time.sleep(0.1)
    if pid_is_running(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        pid_file.unlink()
    except OSError:
        pass
    remove_registry_instance(repo)
    if not quiet:
        print(f"stopped multiagent supervisor pid={pid}")
    return 0


def start_supervisor(
    repo: Repo,
    restart: bool = False,
) -> int:
    validate_required_commands(repo)
    sync_runtime(repo)
    pid_file = repo.state_dir / "runs" / "supervisor.pid"
    existing = read_pid(pid_file)
    if supervisor_pid_is_running(existing):
        if not restart:
            raise UserError(f"multiagent supervisor is already running pid={existing}")
        stop_supervisor(repo, quiet=True)
    log_path = repo.state_dir / "logs" / "supervisor.log"
    command = [
        sys.executable,
        "-m",
        "git_multiagent.cli",
        "_supervisor",
        "--repo-root",
        str(repo.root),
        "--state-dir",
        str(repo.state_dir),
    ]
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            command,
            cwd=repo.root,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(0.2)
    if proc.poll() is not None:
        raise UserError(f"supervisor exited early; see {log_path}")
    write_text_atomic(pid_file, f"{proc.pid}\n")
    write_registry_instance(repo)
    print(f"started multiagent supervisor pid={proc.pid}")
    return 0


def run_supervisor_foreground(repo: Repo, restart: bool = False) -> int:
    validate_required_commands(repo)
    sync_runtime(repo)
    existing = read_pid(repo.state_dir / "runs" / "supervisor.pid")
    if supervisor_pid_is_running(existing):
        if not restart:
            raise UserError(f"multiagent supervisor is already running pid={existing}")
        stop_supervisor(repo, quiet=True)
    write_registry_instance(repo)
    try:
        return cmd_supervisor(
            argparse.Namespace(
                repo_root=str(repo.root),
                state_dir=str(repo.state_dir),
            )
        )
    finally:
        remove_registry_instance(repo)


def cmd_start(args: argparse.Namespace) -> int:
    repo = discover_repo()
    if args.foreground:
        return run_supervisor_foreground(repo, restart=args.restart)
    return start_supervisor(repo, restart=args.restart)


def cmd_stop(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    return stop_supervisor(repo)


def cmd_restart(_args: argparse.Namespace) -> int:
    repo = discover_repo()
    ensure_state(repo)
    stop_supervisor(repo, quiet=True)
    return start_supervisor(repo, restart=False)


def team_agent_command(_git_multiagent_dir: Path, agent: dict[str, Any]) -> list[str]:
    mode = agent["mode"]
    if mode == "interactive":
        command = [
            sys.executable,
            "-m",
            "git_multiagent",
            "agent",
            "interactive",
        ]
    elif mode == "worker":
        command = [
            sys.executable,
            "-m",
            "git_multiagent",
            "agent",
            "worker",
        ]
    else:
        raise UserError(f"invalid mode '{mode}' for agent '{agent['name']}'")
    command.append("--headless")
    if agent.get("model"):
        command.extend(["--model", str(agent["model"])])
    command.extend([agent["role"], agent["name"]])
    return command


def record_team_agent_start(state_dir: Path, agent_name: str, pid: int) -> None:
    run_dir = state_dir / "agents" / ".team-runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(run_dir / f"{agent_name}.pid", f"{pid}\n")


def record_team_agent_exit(state_dir: Path, agent_name: str, rc: int) -> None:
    run_dir = state_dir / "agents" / ".team-runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(run_dir / f"{agent_name}.last-status", f"{rc}\n")
    write_text_atomic(run_dir / f"{agent_name}.last-exit", timestamp() + "\n")


def cmd_supervisor(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).resolve()
    state_dir = Path(args.state_dir).resolve()
    repo = discover_repo(root)
    git_multiagent_dir = repo.config_dir
    for name in STATE_SUBDIRS:
        (state_dir / name).mkdir(parents=True, exist_ok=True)
    team, team_source = effective_team(repo)
    pid = os.getpid()
    write_text_atomic(state_dir / "runs" / "supervisor.pid", f"{pid}\n")
    write_text_atomic(
        state_dir / "runs" / "supervisor.json",
        json.dumps(
            {
                "pid": pid,
                "repo_root": str(root),
                "started_at": timestamp(),
                "state_root": str(state_dir),
                "team_source": team_source,
                "container_runtime": os.environ.get("GIT_MULTIAGENT_CONTAINER", ""),
                "container_name": os.environ.get("GIT_MULTIAGENT_CONTAINER_NAME", ""),
            },
            indent=2,
        )
        + "\n",
    )
    stopping = False
    children: list[ManagedProcess] = []

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def launch(label: str, command: list[str]) -> subprocess.Popen[bytes]:
        log_path = state_dir / "logs" / f"{label}.log"
        log = log_path.open("ab")
        env = os.environ.copy()
        env["GIT_MULTIAGENT_REPO_ROOT"] = str(root)
        env["GIT_MULTIAGENT_ROOT"] = str(git_multiagent_dir)
        env["GIT_MULTIAGENT_STATE_DIR"] = str(state_dir)
        env["GIT_MULTIAGENT_INSTANCE_ID"] = read_text(state_dir / "instance-id", state_dir.name, 4096).strip() or state_dir.name
        try:
            proc = subprocess.Popen(
                command,
                cwd=git_multiagent_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        finally:
            log.close()
        print(f"{timestamp()} started {label} pid={proc.pid}", flush=True)
        return proc

    def start_managed(
        label: str,
        command: list[str],
        agent_name: str | None = None,
    ) -> ManagedProcess:
        proc = launch(label, command)
        if agent_name is not None:
            record_team_agent_start(state_dir, agent_name, proc.pid)
        return ManagedProcess(label=label, command=command, proc=proc, agent_name=agent_name)

    def terminate(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            proc.wait(timeout=5)

    print(f"{timestamp()} supervisor started for {root}", flush=True)
    try:
        if not team:
            raise UserError(f"team config has no agents: {team_source}")
        for agent in team:
            children.append(
                start_managed(
                    f"agent-{agent['name']}",
                    team_agent_command(git_multiagent_dir, agent),
                    agent_name=agent["name"],
                )
            )
            heartbeat = int((agent.get("options") or {}).get("heartbeat") or 0)
            if heartbeat:
                heartbeat_command = [
                    sys.executable,
                    "-m",
                    "git_multiagent",
                    "agent",
                    "heartbeat",
                    "--state-dir",
                    str(state_dir),
                    "--agent",
                    agent["name"],
                    "--minutes",
                    str(heartbeat),
                ]
                children.append(start_managed(f"heartbeat-{agent['name']}", heartbeat_command))

        while not stopping:
            write_text_atomic(state_dir / "runs" / "supervisor-heartbeat", timestamp() + "\n")
            for managed in list(children):
                rc = managed.proc.poll()
                if rc is None:
                    continue
                if managed.agent_name is not None:
                    record_team_agent_exit(state_dir, managed.agent_name, rc)
                print(f"{timestamp()} {managed.label} exited rc={rc}; restarting", flush=True)
                time.sleep(1)
                if stopping:
                    break
                managed.proc = launch(managed.label, managed.command)
                if managed.agent_name is not None:
                    record_team_agent_start(state_dir, managed.agent_name, managed.proc.pid)
            time.sleep(1)
    finally:
        print(f"{timestamp()} supervisor stopping", flush=True)
        for managed in reversed(children):
            terminate(managed.proc)
        try:
            (state_dir / "runs" / "supervisor.pid").unlink()
        except OSError:
            pass
        remove_registry_instance(repo)
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    repo = discover_repo()
    validate_name("agent", args.agent)
    path = repo.state_dir / "agents" / args.agent / "transcript.log"
    if not path.is_file():
        raise UserError(f"transcript not found: {path}")
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        print(stream.read(), end="")
        if not args.follow:
            return 0
        while True:
            chunk = stream.read()
            if chunk:
                print(chunk, end="")
                sys.stdout.flush()
            time.sleep(0.5)


def read_flag(path: Path) -> str:
    return read_text(path, "", 1024).strip()


def follow_agent_turn(agent_dir: Path, transcript_path: Path, start_offset: int) -> None:
    busy_path = agent_dir / "busy"
    saw_activity = False
    emitted_output = False
    output_ended_with_newline = True
    idle_since: float | None = None
    deadline = time.time() + 10
    with transcript_path.open("r", encoding="utf-8", errors="replace") as stream:
        stream.seek(start_offset)
        while True:
            chunk = stream.read()
            if chunk:
                saw_activity = True
                emitted_output = True
                output_ended_with_newline = chunk.endswith("\n")
                idle_since = None
                print(chunk, end="")
                sys.stdout.flush()

            busy = read_flag(busy_path)
            if busy == "1":
                saw_activity = True
                idle_since = None
            elif saw_activity:
                idle_since = idle_since or time.time()
                if time.time() - idle_since >= 0.4:
                    break
            elif time.time() > deadline:
                raise UserError("timed out waiting for agent output")

            time.sleep(0.1)
    if emitted_output and not output_ended_with_newline:
        print()


def configured_agent(repo: Repo, agent_name: str) -> dict[str, Any] | None:
    agents, _source = effective_team(repo)
    for agent in agents:
        if agent["name"] == agent_name:
            return agent
    return None


def send_agent_message(agent_dir: Path, message: str, mode: str = "prompt") -> None:
    rpc_sock = agent_dir / "rpc.sock"
    payload = json.dumps({"message": message, "mode": mode}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(rpc_sock))
            client.sendall(payload.encode("utf-8"))
    except OSError as exc:
        raise UserError(f"interactive agent is not accepting input: {exc}") from exc


def cmd_prompt(args: argparse.Namespace) -> int:
    repo = discover_repo()
    ensure_state(repo)
    validate_name("agent", args.agent)
    agent = configured_agent(repo, args.agent)
    if agent is None:
        raise UserError(f"agent not found in team: {args.agent}")
    if agent.get("mode") != "interactive":
        raise UserError(f"agent is not interactive: {args.agent}")
    if args.message:
        message = " ".join(args.message)
    elif not sys.stdin.isatty():
        message = sys.stdin.read()
    else:
        raise UserError("prompt requires a message argument or stdin")
    message = message.rstrip()
    if not message:
        raise UserError("prompt requires a message argument or stdin")
    agent_dir = repo.state_dir / "agents" / args.agent
    transcript = agent_dir / "transcript.log"
    transcript_offset = transcript.stat().st_size if transcript.exists() else 0
    send_agent_message(agent_dir, message, "prompt")
    if not args.quiet:
        transcript.touch()
        follow_agent_turn(agent_dir, transcript, transcript_offset)
    return 0


def cmd_spec_build(_args: argparse.Namespace) -> int:
    raise UserError("spec build is not implemented yet", 2)


def runtime_env_from_context() -> tuple[dict[str, str], Path]:
    env = os.environ.copy()
    root_value = env.get("GIT_MULTIAGENT_ROOT", "").strip()
    state_value = env.get("GIT_MULTIAGENT_STATE_DIR", "").strip()
    repo_value = env.get("GIT_MULTIAGENT_REPO_ROOT", "").strip()
    if root_value and state_value:
        cwd = Path(repo_value or root_value).expanduser().resolve()
        return env, cwd
    repo = discover_repo()
    env.setdefault("GIT_MULTIAGENT_ROOT", str(repo.config_dir))
    env.setdefault("GIT_MULTIAGENT_REPO_ROOT", str(repo.root))
    env.setdefault("GIT_MULTIAGENT_STATE_DIR", str(repo.state_dir))
    return env, repo.root


def cmd_agent_runtime(args: argparse.Namespace) -> int:
    env, cwd = runtime_env_from_context()
    if args.runtime_kind == "bin":
        executable = package_runtime_bin(args.runtime_tool)
    else:
        executable = package_runtime_tool(args.runtime_tool)
    runtime_args = list(args.runtime_args)
    if runtime_args[:1] == ["--"]:
        runtime_args = runtime_args[1:]
    if getattr(args, "runtime_help", False):
        runtime_args.append("--help")
    command = [str(executable), *runtime_args]
    try:
        rc = subprocess.call(command, cwd=cwd, env=env)
    except FileNotFoundError as exc:
        raise UserError(f"required runtime tool not found: {executable}") from exc
    if getattr(args, "runtime_help", False) and rc == 1:
        return 0
    return rc


TASK_AGENT_COMMANDS = {"create", "list", "show", "comment", "state", "result"}
JOB_AGENT_COMMANDS = {
    "create",
    "list",
    "claim",
    "start",
    "done",
    "fail",
    "release",
    "reset",
    "kill",
    "orphans",
    "reset-orphans",
    "reap",
    "wait",
    "mine",
    "watch",
}
DIRECT_AGENT_COMMANDS = {
    "new": ("bin", "agent-new"),
    "input": ("tool", "agent-input"),
    "heartbeat": ("tool", "heartbeat"),
    "worker": ("tool", "agent"),
    "interactive": ("tool", "agent-pi-interactive"),
}


def agent_runtime_main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: multiagent agent {task,job,new,input,heartbeat,worker,interactive} ...")
        return 0
    if argv[0] == "task":
        if len(argv) < 2 or argv[1] not in TASK_AGENT_COMMANDS:
            raise UserError("usage: multiagent agent task {create,list,show,comment,state,result} ...")
        args = argparse.Namespace(
            runtime_kind="bin",
            runtime_tool=f"task-{argv[1]}",
            runtime_args=argv[2:],
            runtime_help=False,
        )
        return cmd_agent_runtime(args)
    if argv[0] == "job":
        if len(argv) < 2 or argv[1] not in JOB_AGENT_COMMANDS:
            raise UserError(
                "usage: multiagent agent job "
                "{create,list,claim,start,done,fail,release,reset,kill,orphans,reset-orphans,reap,wait,mine,watch} ..."
            )
        args = argparse.Namespace(
            runtime_kind="bin",
            runtime_tool=f"job-{argv[1]}",
            runtime_args=argv[2:],
            runtime_help=False,
        )
        return cmd_agent_runtime(args)
    direct = DIRECT_AGENT_COMMANDS.get(argv[0])
    if direct is None:
        raise UserError(f"unknown agent command: {argv[0]}")
    args = argparse.Namespace(
        runtime_kind=direct[0],
        runtime_tool=direct[1],
        runtime_args=argv[1:],
        runtime_help=False,
    )
    return cmd_agent_runtime(args)


def cmd_dashboard(args: argparse.Namespace) -> int:
    from . import dashboard

    return dashboard.main(args.dashboard_args)


def cmd_docker(args: argparse.Namespace) -> int:
    from . import runner

    return runner.main(args.docker_args)


def add_role_parser(subparsers: argparse._SubParsersAction) -> None:
    role = subparsers.add_parser("role", help="manage roles")
    role_sub = role.add_subparsers(dest="role_command", required=True)
    role_sub.add_parser("list", help="list roles").set_defaults(func=cmd_role_list)
    show = role_sub.add_parser("show", help="show effective role text")
    show.add_argument("name")
    show.set_defaults(func=cmd_role_show)
    add = role_sub.add_parser("add", help="add a local role")
    add.add_argument("name")
    add.add_argument("--from", dest="from_role")
    add.set_defaults(func=cmd_role_add)
    edit = role_sub.add_parser("edit", help="edit a local role")
    edit.add_argument("name")
    edit.set_defaults(func=cmd_role_edit)
    diff = role_sub.add_parser("diff", help="diff local roles against package templates")
    diff.add_argument("name", nargs="?")
    diff.set_defaults(func=cmd_role_diff)
    reset = role_sub.add_parser("reset", help="reset a role from the package template")
    reset.add_argument("name")
    reset.add_argument("--yes", action="store_true")
    reset.set_defaults(func=cmd_role_reset)


def add_rules_parser(subparsers: argparse._SubParsersAction) -> None:
    rules = subparsers.add_parser("rules", help="inspect the generic MULTIAGENT protocol")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_sub.add_parser("show", help="show the installed generic protocol").set_defaults(func=cmd_rules_show)


def add_team_parser(subparsers: argparse._SubParsersAction) -> None:
    team = subparsers.add_parser("team", help="manage the configured team")
    team_sub = team.add_subparsers(dest="team_command", required=True)
    team_sub.add_parser("list", help="list configured agents").set_defaults(func=cmd_team_list)
    show = team_sub.add_parser("show", help="show team config or one agent")
    show.add_argument("agent", nargs="?")
    show.set_defaults(func=cmd_team_show)
    add = team_sub.add_parser("add", help="add a configured agent")
    add.add_argument("agent")
    add.add_argument("--role", required=True)
    add.add_argument("--mode", choices=sorted(VALID_MODES), default="worker")
    add.add_argument("--model")
    add.add_argument("--heartbeat", type=int)
    add.set_defaults(func=cmd_team_add)
    remove = team_sub.add_parser("remove", help="remove a configured agent")
    remove.add_argument("agent")
    remove.set_defaults(func=cmd_team_remove)
    set_cmd = team_sub.add_parser("set", help="update a configured agent")
    set_cmd.add_argument("agent")
    set_cmd.add_argument("--role")
    set_cmd.add_argument("--mode", choices=sorted(VALID_MODES))
    set_cmd.add_argument("--model")
    set_cmd.add_argument("--heartbeat", type=int)
    set_cmd.add_argument("--no-heartbeat", action="store_true")
    set_cmd.set_defaults(func=cmd_team_set)
    team_sub.add_parser("edit", help="edit the team config").set_defaults(func=cmd_team_edit)


def add_tasks_parser(subparsers: argparse._SubParsersAction) -> None:
    tasks = subparsers.add_parser("tasks", help="inspect tasks")
    tasks_sub = tasks.add_subparsers(dest="tasks_command", required=True)
    create = tasks_sub.add_parser("create", help="create a task and initial planner job")
    create.add_argument("task")
    create.add_argument("spec_file")
    create.set_defaults(func=cmd_tasks_create)
    tasks_sub.add_parser("list", help="list tasks").set_defaults(func=cmd_tasks_list)
    show = tasks_sub.add_parser("show", help="show a task")
    show.add_argument("task")
    show.set_defaults(func=cmd_tasks_show)
    comment = tasks_sub.add_parser("comment", help="append a task comment")
    comment.add_argument("task")
    comment.add_argument("message", nargs="+")
    comment.set_defaults(func=cmd_tasks_comment)
    state = tasks_sub.add_parser("state", help="set task state")
    state.add_argument("task")
    state.add_argument("state", choices=["open", "done"])
    state.add_argument("-m", "--message")
    state.set_defaults(func=cmd_tasks_state)
    result = tasks_sub.add_parser("result", help="record task result and mark done")
    result.add_argument("task")
    result.add_argument("result_file")
    result.set_defaults(func=cmd_tasks_result)


def add_jobs_parser(subparsers: argparse._SubParsersAction) -> None:
    jobs = subparsers.add_parser("jobs", help="inspect and recover jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    create = jobs_sub.add_parser("create", help="create a job")
    create.add_argument("job")
    create.add_argument("--role", required=True)
    create.add_argument("--task", required=True)
    create.add_argument("spec_file")
    create.set_defaults(func=cmd_jobs_create)
    jobs_sub.add_parser("list", help="list jobs").set_defaults(func=cmd_jobs_list)
    reset = jobs_sub.add_parser("reset", help="force a job back to pending")
    reset.add_argument("job")
    reset.add_argument("-m", "--message")
    reset.add_argument("--force", action="store_true", help="allow completed jobs and non-empty locks")
    reset.set_defaults(func=cmd_jobs_reset)
    kill = jobs_sub.add_parser("kill", help="stop a claimed or running job immediately")
    kill.add_argument("job")
    kill.add_argument("-m", "--message")
    kill.add_argument("--force", action="store_true", help="allow removing a non-empty lock")
    kill.set_defaults(func=cmd_jobs_kill)
    jobs_sub.add_parser("orphans", help="list claimed/running jobs with missing owners").set_defaults(func=cmd_jobs_orphans)
    jobs_sub.add_parser("reset-orphans", help="reset orphaned jobs to pending").set_defaults(func=cmd_jobs_reset_orphans)
    reap = jobs_sub.add_parser("reap", help="reset stale locked jobs")
    reap.add_argument("minutes", nargs="?", type=int)
    reap.set_defaults(func=cmd_jobs_reap)


def add_agents_parser(subparsers: argparse._SubParsersAction) -> None:
    agents = subparsers.add_parser("agents", help="inspect and recover runtime agents")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_sub.add_parser("list", help="list runtime agents").set_defaults(func=cmd_agents_list)
    reset = agents_sub.add_parser("reset", help="reset one runtime agent")
    reset.add_argument("agent")
    reset.add_argument("-m", "--message")
    reset.add_argument("--force", action="store_true", help="force resetting non-empty job locks")
    reset.add_argument("--no-kill", action="store_true", help="clear state without signaling recorded processes")
    reset.set_defaults(func=cmd_agents_reset)


def add_spec_parser(subparsers: argparse._SubParsersAction) -> None:
    spec = subparsers.add_parser("spec", help="spec workflows")
    spec_sub = spec.add_subparsers(dest="spec_command", required=True)
    spec_sub.add_parser("build", help="build a task spec").set_defaults(func=cmd_spec_build)


def add_remainder_command(
    subparsers: argparse._SubParsersAction,
    name: str,
    runtime_kind: str,
    runtime_tool: str,
    help_text: str,
) -> None:
    parser = subparsers.add_parser(name, help=help_text, add_help=False)
    parser.add_argument("-h", "--help", action="store_true", dest="runtime_help")
    parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    parser.set_defaults(func=cmd_agent_runtime, runtime_kind=runtime_kind, runtime_tool=runtime_tool)


def add_agent_runtime_parser(subparsers: argparse._SubParsersAction) -> None:
    agent = subparsers.add_parser("agent", help="agent protocol utilities")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)

    task = agent_sub.add_parser("task", help="task state utilities")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    for name in ("create", "list", "show", "comment", "state", "result"):
        add_remainder_command(task_sub, name, "bin", f"task-{name}", f"run task-{name}")

    job = agent_sub.add_parser("job", help="job state utilities")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    for name in (
        "create",
        "list",
        "claim",
        "start",
        "done",
        "fail",
        "release",
        "reset",
        "kill",
        "orphans",
        "reset-orphans",
        "reap",
        "wait",
        "mine",
        "watch",
    ):
        add_remainder_command(job_sub, name, "bin", f"job-{name}", f"run job-{name}")

    add_remainder_command(agent_sub, "new", "bin", "agent-new", "create or refresh an agent state directory")
    add_remainder_command(agent_sub, "input", "tool", "agent-input", "send input to an interactive agent")
    add_remainder_command(agent_sub, "heartbeat", "tool", "heartbeat", "send heartbeat prompts to an interactive agent")
    add_remainder_command(agent_sub, "worker", "tool", "agent", "run one worker agent")
    add_remainder_command(agent_sub, "interactive", "tool", "agent-pi-interactive", "run one interactive agent")


def add_local_parser(subparsers: argparse._SubParsersAction, include_internal: bool = False) -> None:
    local = subparsers.add_parser("local", help="native local repository control")
    local_sub = local.add_subparsers(dest="local_command", required=True)

    init = local_sub.add_parser("init", help="initialize repository configuration and state")
    init.add_argument("--tracked-config", action="store_true", help="also create optional .multiagent/specs")
    init.set_defaults(func=cmd_init)

    update = local_sub.add_parser("update", help="refresh MULTIAGENT state and optional role templates")
    update.add_argument("--roles", action="store_true", help="also refresh default role templates")
    update.set_defaults(func=cmd_update)

    start = local_sub.add_parser("start", help="start the native agent supervisor")
    start.add_argument("--restart", action="store_true")
    start.add_argument("--foreground", action="store_true", help="run the supervisor in the foreground")
    start.set_defaults(func=cmd_start)

    local_sub.add_parser("stop", help="stop the native agent supervisor").set_defaults(func=cmd_stop)
    local_sub.add_parser("restart", help="restart the native agent supervisor").set_defaults(func=cmd_restart)
    local_sub.add_parser("status", help="show repository agent status").set_defaults(func=cmd_status)

    log = local_sub.add_parser("log", help="show an agent transcript")
    log.add_argument("-f", "--follow", action="store_true")
    log.add_argument("agent")
    log.set_defaults(func=cmd_log)

    prompt = local_sub.add_parser("prompt", help="send input to an interactive agent")
    prompt.add_argument("-q", "--quiet", action="store_true", help="send the prompt without printing the agent turn")
    prompt.add_argument("agent")
    prompt.add_argument("message", nargs="*")
    prompt.set_defaults(func=cmd_prompt)

    add_role_parser(local_sub)
    add_rules_parser(local_sub)
    add_team_parser(local_sub)
    add_agents_parser(local_sub)
    add_spec_parser(local_sub)

    if include_internal:
        supervisor = local_sub.add_parser("_supervisor")
        supervisor.add_argument("--repo-root", required=True)
        supervisor.add_argument("--state-dir", required=True)
        supervisor.set_defaults(func=cmd_supervisor)


def build_parser(include_internal: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multiagent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_local_parser(subparsers, include_internal=False)
    add_agent_runtime_parser(subparsers)
    docker = subparsers.add_parser("docker", help="containerized MULTIAGENT control", add_help=False)
    docker.add_argument("docker_args", nargs=argparse.REMAINDER)
    docker.set_defaults(func=cmd_docker)
    dashboard_parser = subparsers.add_parser("dashboard", help="serve the multiagent dashboard", add_help=False)
    dashboard_parser.add_argument("dashboard_args", nargs=argparse.REMAINDER)
    dashboard_parser.set_defaults(func=cmd_dashboard)
    if include_internal:
        supervisor = subparsers.add_parser("_supervisor")
        supervisor.add_argument("--repo-root", required=True)
        supervisor.add_argument("--state-dir", required=True)
        supervisor.set_defaults(func=cmd_supervisor)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "docker":
            from . import runner

            return runner.main(argv[1:])
        if argv and argv[0] == "dashboard":
            from . import dashboard

            return dashboard.main(argv[1:])
        if argv and argv[0] == "agent":
            return agent_runtime_main(argv[1:])
        parser = build_parser(include_internal=bool(argv and argv[0] == "_supervisor"))
        args = parser.parse_args(argv)
        return int(args.func(args))
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
