from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "system.toml"
DEFAULT_ROOT = "~/.multiagent"
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 4137
REGISTRY_ENV = "MULTIAGENT_REGISTRY_DIR"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class UserError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class DashboardConfig:
    host: str = DEFAULT_DASHBOARD_HOST
    port: int = DEFAULT_DASHBOARD_PORT


@dataclass
class MountConfig:
    path: Path
    mode: str = "rw"


@dataclass
class DeviceConfig:
    path: Path


@dataclass
class ProjectConfig:
    name: str
    repo: Path
    runtime: str = "docker"


@dataclass
class SystemConfig:
    root: Path
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    mounts: list[MountConfig] = field(default_factory=list)
    devices: list[DeviceConfig] = field(default_factory=list)
    projects: list[ProjectConfig] = field(default_factory=list)


@dataclass
class DockerContainerInfo:
    name: str
    state: str
    repo: Path
    state_dir: Path | None = None
    mounts: list[MountConfig] = field(default_factory=list)
    devices: list[DeviceConfig] = field(default_factory=list)


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def path_is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_name(label: str, value: str) -> None:
    if not value or not NAME_RE.match(value):
        raise UserError(f"invalid {label} '{value}': use letters, numbers, dot, underscore, or hyphen")


def toml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return toml_quote(str(value))


def parse_toml_value(raw: str) -> Any:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def parse_system_toml(text: str) -> SystemConfig:
    root: Path | None = None
    dashboard = DashboardConfig()
    mounts: list[MountConfig] = []
    devices: list[DeviceConfig] = []
    projects: list[ProjectConfig] = []
    section: str | None = None
    current: dict[str, Any] | None = None

    def flush_current() -> None:
        nonlocal current
        if current is None or section is None:
            return
        if section == "mount":
            path = current.get("path")
            if not isinstance(path, str) or not path:
                raise UserError("mount entry requires path")
            mode = str(current.get("mode") or "rw")
            if mode not in {"ro", "rw"}:
                raise UserError(f"invalid mount mode '{mode}'")
            mounts.append(MountConfig(path=normalize_path(path), mode=mode))
        elif section == "device":
            path = current.get("path")
            if not isinstance(path, str) or not path:
                raise UserError("device entry requires path")
            devices.append(DeviceConfig(path=normalize_path(path)))
        elif section == "project":
            name = current.get("name")
            repo = current.get("repo")
            runtime = str(current.get("runtime") or "docker")
            if not isinstance(name, str) or not name:
                raise UserError("project entry requires name")
            if not isinstance(repo, str) or not repo:
                raise UserError("project entry requires repo")
            validate_name("project", name)
            if runtime not in {"docker", "local"}:
                raise UserError(f"invalid runtime '{runtime}' for project '{name}'")
            projects.append(ProjectConfig(name=name, repo=normalize_path(repo), runtime=runtime))
        current = None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            flush_current()
            section = line[2:-2].strip()
            if section not in {"mount", "device", "project"}:
                raise UserError(f"unknown system config section [[{section}]]")
            current = {}
            continue
        if line.startswith("[") and line.endswith("]"):
            flush_current()
            section = line[1:-1].strip()
            if section != "dashboard":
                raise UserError(f"unknown system config section [{section}]")
            current = None
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = parse_toml_value(raw_value)
        if section is None:
            if key == "root" and isinstance(value, str):
                root = normalize_path(value)
            continue
        if section == "dashboard":
            if key == "host":
                dashboard.host = str(value)
            elif key == "port":
                try:
                    dashboard.port = int(value)
                except (TypeError, ValueError) as exc:
                    raise UserError("dashboard port must be an integer") from exc
            continue
        if current is not None:
            current[key] = value
    flush_current()

    if root is None:
        raise UserError("system config requires root")
    return SystemConfig(root=root, dashboard=dashboard, mounts=mounts, devices=devices, projects=projects)


def format_system_toml(config: SystemConfig) -> str:
    lines = [
        "# MULTIAGENT system",
        "version = 1",
        f"root = {toml_value(str(config.root))}",
        "",
        "[dashboard]",
        f"host = {toml_value(config.dashboard.host)}",
        f"port = {toml_value(config.dashboard.port)}",
        "",
    ]
    for mount in config.mounts:
        lines.extend(
            [
                "[[mount]]",
                f"path = {toml_value(str(mount.path))}",
                f"mode = {toml_value(mount.mode)}",
                "",
            ]
        )
    for device in config.devices:
        lines.extend(
            [
                "[[device]]",
                f"path = {toml_value(str(device.path))}",
                "",
            ]
        )
    for project in config.projects:
        lines.extend(
            [
                "[[project]]",
                f"name = {toml_value(project.name)}",
                f"repo = {toml_value(str(project.repo))}",
                f"runtime = {toml_value(project.runtime)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def config_root(args: argparse.Namespace) -> Path:
    value = getattr(args, "command_root", None) or getattr(args, "root", None) or DEFAULT_ROOT
    return normalize_path(value)


def config_path_for_root(root: str | Path) -> Path:
    return normalize_path(root) / DEFAULT_CONFIG


def system_snapshot_path(root: str | Path) -> Path:
    return normalize_path(root) / "runs" / "system" / "system.toml"


def config_path(args: argparse.Namespace) -> Path:
    return config_path_for_root(config_root(args))


def load_config_for_info(args: argparse.Namespace) -> tuple[SystemConfig, Path, Path | None]:
    root = config_root(args)
    canonical = config_path_for_root(root)
    if canonical.exists():
        return load_config(canonical), canonical, None
    snapshot = system_snapshot_path(root)
    if snapshot.exists():
        return load_config(snapshot), snapshot, canonical
    return load_config(canonical), canonical, None


def load_config(path: Path) -> SystemConfig:
    try:
        return parse_system_toml(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UserError(f"cannot read system config {path}: {exc}") from exc


def save_config(path: Path, config: SystemConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_system_toml(config), encoding="utf-8")


def git_repo_root(path: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise UserError(detail or f"not a git repository: {path}")
    return normalize_path(proc.stdout.strip())


def default_project_name(repo: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name).strip(".-")
    return name or "project"


def project_instance_id(repo: Path) -> str:
    digest = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()
    repo_name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name).strip(".-")
    if not repo_name:
        repo_name = "repo"
    return f"{repo_name}-{digest[:12]}"


def project_state_dir(config: SystemConfig, project: ProjectConfig) -> Path:
    return config.root / "state" / project_instance_id(project.repo)


def docker_container_name(repo: Path) -> str:
    digest = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^a-z0-9_.-]+", "-", repo.name.lower()).strip(".-") or "repo"
    return f"multiagent-{stem}-{digest}"


def docker_container_state(name: str) -> str:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return "unknown (docker not found)"
    if proc.returncode != 0:
        return "missing"
    return proc.stdout.strip() or "unknown"


def docker_system_containers(root: Path) -> list[DockerContainerInfo]:
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=multiagent.run=1", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    containers: list[DockerContainerInfo] = []
    for name in [line.strip() for line in proc.stdout.splitlines() if line.strip()]:
        inspect = subprocess.run(
            ["docker", "inspect", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if inspect.returncode != 0:
            continue
        try:
            data = json.loads(inspect.stdout)[0]
        except (IndexError, json.JSONDecodeError, TypeError):
            continue
        env = {}
        for item in data.get("Config", {}).get("Env") or []:
            key, _, value = str(item).partition("=")
            env[key] = value
        registry = env.get(REGISTRY_ENV, "").strip()
        state_value = env.get("MULTIAGENT_STATE_DIR", "").strip()
        registry_root = normalize_path(registry) if registry else None
        state_dir = normalize_path(state_value) if state_value else None
        if registry_root != root and not (state_dir and path_is_under(state_dir, root / "state")):
            continue
        labels = data.get("Config", {}).get("Labels") or {}
        repo_text = labels.get("multiagent.repo") or ""
        if not repo_text:
            continue
        mounts = []
        for mount in data.get("Mounts") or []:
            if mount.get("Type") != "bind":
                continue
            source = normalize_path(str(mount.get("Source") or ""))
            if path_is_under(source, root):
                continue
            mounts.append(MountConfig(source, "rw" if mount.get("RW", True) else "ro"))
        devices = []
        for device in data.get("HostConfig", {}).get("Devices") or []:
            path = device.get("PathOnHost") or device.get("PathInContainer")
            if path:
                devices.append(DeviceConfig(normalize_path(str(path))))
        containers.append(
            DockerContainerInfo(
                name=name,
                state=str(data.get("State", {}).get("Status") or "unknown"),
                repo=normalize_path(repo_text),
                state_dir=state_dir,
                mounts=mounts,
                devices=devices,
            )
        )
    return containers


def discovered_config(root: Path, containers: list[DockerContainerInfo]) -> SystemConfig:
    mounts_by_key: dict[tuple[Path, str], MountConfig] = {}
    devices_by_path: dict[Path, DeviceConfig] = {}
    projects_by_repo: dict[Path, ProjectConfig] = {}
    for container in containers:
        for mount in container.mounts:
            mounts_by_key[(mount.path, mount.mode)] = mount
        for device in container.devices:
            devices_by_path[device.path] = device
        projects_by_repo[container.repo] = ProjectConfig(default_project_name(container.repo), container.repo, "docker")
    return SystemConfig(
        root=root,
        mounts=list(mounts_by_key.values()),
        devices=list(devices_by_path.values()),
        projects=list(projects_by_repo.values()),
    )


def path_state(path: Path) -> str:
    return "exists" if path.exists() else "missing"


def system_env(config: SystemConfig) -> dict[str, str]:
    env = os.environ.copy()
    env[REGISTRY_ENV] = str(config.root)
    return env


def run_multiagent(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
    return subprocess.call([sys.executable, "-m", "multiagent", *args], cwd=cwd, env=env)


def system_run_dir(config: SystemConfig) -> Path:
    return config.root / "runs" / "system"


def dashboard_pid_path(config: SystemConfig) -> Path:
    return system_run_dir(config) / "dashboard.pid"


def dashboard_metadata_path(config: SystemConfig) -> Path:
    return system_run_dir(config) / "dashboard.json"


def dashboard_log_path(config: SystemConfig) -> Path:
    return config.root / "logs" / "system-dashboard.log"


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


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


def stop_pid_group(pid: int, timeout: float = 5) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        os.kill(pid, signal.SIGKILL)


def ensure_root(config: SystemConfig) -> None:
    for name in ("state", "instances", "runs", "logs"):
        (config.root / name).mkdir(parents=True, exist_ok=True)


def start_dashboard(config: SystemConfig, *, restart: bool = False) -> None:
    ensure_root(config)
    pid = read_pid(dashboard_pid_path(config))
    if pid_is_running(pid):
        if not restart:
            return
        assert pid is not None
        stop_pid_group(pid)
    system_run_dir(config).mkdir(parents=True, exist_ok=True)
    dashboard_log_path(config).parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "multiagent",
        "dashboard",
        "--host",
        config.dashboard.host,
        "--port",
        str(config.dashboard.port),
    ]
    with dashboard_log_path(config).open("ab") as log:
        proc = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=system_env(config),
            start_new_session=True,
        )
    time.sleep(0.2)
    if proc.poll() is not None:
        raise UserError(f"dashboard exited early; see {dashboard_log_path(config)}")
    dashboard_pid_path(config).write_text(f"{proc.pid}\n", encoding="utf-8")
    metadata = {
        "pid": proc.pid,
        "host": config.dashboard.host,
        "port": config.dashboard.port,
        "url": f"http://{config.dashboard.host}:{config.dashboard.port}",
        "started_at": timestamp(),
        "root": str(config.root),
    }
    dashboard_metadata_path(config).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def stop_dashboard(config: SystemConfig) -> None:
    pid = read_pid(dashboard_pid_path(config))
    if pid_is_running(pid):
        assert pid is not None
        stop_pid_group(pid)
    for path in (dashboard_pid_path(config), dashboard_metadata_path(config)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def project_start_args(config: SystemConfig, project: ProjectConfig, restart: bool) -> list[str]:
    if project.runtime == "local":
        command = ["local", "start"]
        if restart:
            command.append("--restart")
        return command
    command = ["docker", "start", str(project.repo)]
    for mount in config.mounts:
        command.extend(["--mount", f"{mount.path}:{mount.mode}"])
    for device in config.devices:
        command.extend(["--device", str(device.path)])
    if restart:
        command.append("--restart")
    return command


def project_stop_args(config: SystemConfig, project: ProjectConfig) -> list[str]:
    if project.runtime == "local":
        return ["local", "stop"]
    return ["docker", "stop", str(project.repo)]


def cmd_init(args: argparse.Namespace) -> int:
    path = config_path(args)
    if path.exists() and not args.force:
        raise UserError(f"system config already exists: {path}")
    config = SystemConfig(
        root=config_root(args),
        dashboard=DashboardConfig(host=args.host, port=args.port),
    )
    save_config(path, config)
    print(path)
    return 0


def cmd_add_mount(args: argparse.Namespace) -> int:
    path = config_path(args)
    config = load_config(path)
    mount_path = normalize_path(args.path)
    if not mount_path.exists():
        raise UserError(f"mount path does not exist: {mount_path}")
    if args.mode not in {"ro", "rw"}:
        raise UserError("mount mode must be ro or rw")
    config.mounts = [mount for mount in config.mounts if mount.path != mount_path]
    config.mounts.append(MountConfig(path=mount_path, mode=args.mode))
    save_config(path, config)
    print(path)
    return 0


def cmd_add_dev(args: argparse.Namespace) -> int:
    path = config_path(args)
    config = load_config(path)
    device_path = normalize_path(args.path)
    if not device_path.exists():
        raise UserError(f"device path does not exist: {device_path}")
    if all(device.path != device_path for device in config.devices):
        config.devices.append(DeviceConfig(path=device_path))
    save_config(path, config)
    print(path)
    return 0


def cmd_add_project(args: argparse.Namespace) -> int:
    path = config_path(args)
    config = load_config(path)
    repo = git_repo_root(normalize_path(args.repo))
    name = args.name or default_project_name(repo)
    validate_name("project", name)
    if args.runtime not in {"docker", "local"}:
        raise UserError("runtime must be docker or local")
    existing = [project for project in config.projects if project.name == name or project.repo == repo]
    if existing and not args.replace:
        raise UserError("project already exists; use --replace")
    config.projects = [project for project in config.projects if project.name != name and project.repo != repo]
    config.projects.append(ProjectConfig(name=name, repo=repo, runtime=args.runtime))
    save_config(path, config)
    print(path)
    return 0


def cmd_set_dashboard(args: argparse.Namespace) -> int:
    path = config_path(args)
    config = load_config(path)
    if args.host:
        config.dashboard.host = args.host
    if args.port is not None:
        config.dashboard.port = args.port
    save_config(path, config)
    print(path)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    path = config_path(args)
    config = load_config(path)
    ensure_root(config)
    system_run_dir(config).mkdir(parents=True, exist_ok=True)
    (system_run_dir(config) / "system.toml").write_text(format_system_toml(config), encoding="utf-8")
    env = system_env(config)
    for project in config.projects:
        rc = run_multiagent(["local", "init"], cwd=project.repo, env=env)
        if rc != 0:
            return rc
        rc = run_multiagent(project_start_args(config, project, args.restart), cwd=project.repo, env=env)
        if rc != 0:
            return rc
    start_dashboard(config, restart=args.restart)
    print(f"dashboard: http://{config.dashboard.host}:{config.dashboard.port}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    config = load_config(config_path(args))
    env = system_env(config)
    rc = 0
    for project in reversed(config.projects):
        project_rc = run_multiagent(project_stop_args(config, project), cwd=project.repo, env=env)
        if project_rc != 0 and rc == 0:
            rc = project_rc
    stop_dashboard(config)
    return rc


def cmd_restart(args: argparse.Namespace) -> int:
    stop_rc = cmd_stop(args)
    if stop_rc != 0:
        return stop_rc
    args.restart = True
    return cmd_start(args)


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(config_path(args))
    pid = read_pid(dashboard_pid_path(config))
    dashboard_state = "running" if pid_is_running(pid) else "stopped"
    print(f"dashboard\t{dashboard_state}\thttp://{config.dashboard.host}:{config.dashboard.port}")
    for project in config.projects:
        print(f"{project.name}\tconfigured\t{project.runtime}\t{project.repo}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    config = load_config(config_path(args))
    pid = read_pid(dashboard_pid_path(config))
    if pid_is_running(pid):
        print(f"dashboard running: http://{config.dashboard.host}:{config.dashboard.port}")
        return 0
    print("dashboard stopped")
    return 1


def cmd_info(args: argparse.Namespace) -> int:
    root = config_root(args)
    docker_containers = docker_system_containers(root)
    try:
        config, source, missing_canonical = load_config_for_info(args)
    except UserError:
        if not docker_containers:
            raise
        config = discovered_config(root, docker_containers)
        source = None
        missing_canonical = config_path_for_root(root)
    container_by_repo = {container.repo: container for container in docker_containers}
    pid = read_pid(dashboard_pid_path(config))
    dashboard_running = pid_is_running(pid)
    print(f"root: {config.root}")
    print(f"config: {source if source is not None else 'none'}")
    if missing_canonical is not None:
        print(f"canonical config: {missing_canonical} (missing)")
    print(f"state dir: {config.root / 'state'}")
    print(f"instances dir: {config.root / 'instances'}")
    print(f"runs dir: {config.root / 'runs'}")
    print(f"logs dir: {config.root / 'logs'}")
    print("")
    print("dashboard:")
    print(f"  url: http://{config.dashboard.host}:{config.dashboard.port}")
    print(f"  state: {'running' if dashboard_running else 'stopped'}")
    if pid is not None:
        print(f"  pid: {pid}")
    print(f"  pid file: {dashboard_pid_path(config)}")
    print(f"  metadata: {dashboard_metadata_path(config)}")
    print(f"  log: {dashboard_log_path(config)}")
    print("")
    print("mounts:")
    if config.mounts:
        for mount in config.mounts:
            print(f"  {mount.mode}\t{mount.path}\t{path_state(mount.path)}")
    else:
        print("  none")
    print("")
    print("devices:")
    if config.devices:
        for device in config.devices:
            print(f"  {device.path}\t{path_state(device.path)}")
    else:
        print("  none")
    print("")
    print("projects:")
    if not config.projects:
        print("  none")
        return 0
    for project in config.projects:
        state_dir = project_state_dir(config, project)
        print(f"  {project.name}:")
        print(f"    runtime: {project.runtime}")
        print(f"    repo: {project.repo} ({path_state(project.repo)})")
        container = container_by_repo.get(project.repo)
        if container and container.state_dir:
            state_dir = container.state_dir
        print(f"    state: {state_dir} ({path_state(state_dir)})")
        print(f"    supervisor: {state_dir / 'runs' / 'supervisor.json'}")
        if project.runtime == "docker":
            if container is not None:
                print(f"    container: {container.name} ({container.state})")
            else:
                container_name = docker_container_name(project.repo)
                print(f"    container: {container_name} ({docker_container_state(container_name)})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multiagent system")
    parser.add_argument("-r", "--root", default=None, metavar="ROOT", help=f"system root (default: {DEFAULT_ROOT})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_command_root(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "-r",
            "--root",
            dest="command_root",
            default=None,
            metavar="ROOT",
            help=f"system root (default: {DEFAULT_ROOT})",
        )

    init = subparsers.add_parser("init", help="create a MULTIAGENT system config", add_help=False)
    init.add_argument("--help", action="help", help="show this help message and exit")
    add_command_root(init)
    init.add_argument("-h", "--host", default=DEFAULT_DASHBOARD_HOST)
    init.add_argument("-p", "--port", default=DEFAULT_DASHBOARD_PORT, type=int)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    add_mount = subparsers.add_parser("add-mount", help="add a shared mount for all docker projects")
    add_command_root(add_mount)
    add_mount.add_argument("path")
    add_mount.add_argument("--mode", choices=["ro", "rw"], default="rw")
    add_mount.set_defaults(func=cmd_add_mount)

    add_dev = subparsers.add_parser("add-dev", help="add a device for all docker projects")
    add_command_root(add_dev)
    add_dev.add_argument("path")
    add_dev.set_defaults(func=cmd_add_dev)

    add = subparsers.add_parser("add", help="add a project repository")
    add_command_root(add)
    add.add_argument("repo")
    add.add_argument("--name")
    add.add_argument("--runtime", choices=["docker", "local"], default="docker")
    add.add_argument("--replace", action="store_true")
    add.set_defaults(func=cmd_add_project)

    dashboard = subparsers.add_parser("set-dashboard", help="configure the system dashboard", add_help=False)
    dashboard.add_argument("--help", action="help", help="show this help message and exit")
    add_command_root(dashboard)
    dashboard.add_argument("-h", "--host")
    dashboard.add_argument("-p", "--port", type=int)
    dashboard.set_defaults(func=cmd_set_dashboard)

    start = subparsers.add_parser("start", help="start projects and the dashboard")
    add_command_root(start)
    start.add_argument("--restart", action="store_true")
    start.set_defaults(func=cmd_start)
    stop = subparsers.add_parser("stop", help="stop projects and the dashboard")
    add_command_root(stop)
    stop.set_defaults(func=cmd_stop)
    restart = subparsers.add_parser("restart", help="restart projects and the dashboard")
    add_command_root(restart)
    restart.set_defaults(func=cmd_restart)
    status = subparsers.add_parser("status", help="show system status")
    add_command_root(status)
    status.set_defaults(func=cmd_status)
    info = subparsers.add_parser("info", help="show system configuration and runtime paths")
    add_command_root(info)
    info.set_defaults(func=cmd_info)
    dashboard_status = subparsers.add_parser("dashboard", help="show system dashboard status")
    add_command_root(dashboard_status)
    dashboard_status.set_defaults(func=cmd_dashboard)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
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
