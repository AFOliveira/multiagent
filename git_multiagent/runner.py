from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_IMAGE = "git-multiagent:local"
DEFAULT_REGISTRY_DIR = "~/.multiagent"
CONTAINER_HOME = Path("/home/gitmultiagent")
HOST_CODEX_PROVIDER = "git-multiagent-host-codex"
HOST_CODEX_AUTH_PROVIDER = "openai-codex"
PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
HOST_AUTH_RELAY_PORT = 17691
PI_WEB_ACCESS_PACKAGE = "npm:pi-web-access"
RUN_LABEL = "git-multiagent.run=1"
REPO_LABEL = "git-multiagent.repo"
SOURCE_FINGERPRINT_LABEL = "multiagent.source-fingerprint"
RUN_CONFIG_FINGERPRINT_LABEL = "multiagent.run-config-fingerprint"
STATE_SUBDIRS = ("tasks", "jobs", "agents", "runs", "logs")
DEFAULT_FORWARD_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)
COMMANDS = {"start", "stop", "destroy", "status", "logs", "build-image"}
CODEX_MODELS = (
    "gpt-5.1",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
)
JWT_CLAIM_PATH = "https://api.openai.com/auth"


class UserError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Mount:
    source: Path
    target: Path
    mode: str = "rw"

    def docker_value(self) -> str:
        value = f"type=bind,src={self.source},dst={self.target}"
        if self.mode == "ro":
            value += ",readonly"
        return value


@dataclass(frozen=True)
class StartConfig:
    repo: Path
    registry_dir: Path
    state_base_dir: Path
    state_dir: Path
    image: str
    source_fingerprint: str
    name: str
    run_dir: Path
    pi_config_dir: Path
    mounts: tuple[Mount, ...]
    devices: tuple[Path, ...]
    projected_pi_home: Path
    host_pi_agent_dir: Path
    auth_socket: Path
    auth_proxy_pid: Path
    auth_proxy_ready: Path
    auth_relay_port: int
    env: tuple[str, ...]
    forward_env: tuple[str, ...]
    foreground: bool
    restart: bool
    network: str
    home: Path


def shell_join(command: Iterable[str]) -> str:
    return shlex.join(list(command))


def normalize_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def path_depth(path: Path) -> int:
    return len(path.parts)


def parse_mount_spec(spec: str) -> Mount:
    mode = "rw"
    path_spec = spec
    for suffix in (":ro", ":rw"):
        if spec.endswith(suffix):
            mode = suffix[1:]
            path_spec = spec[: -len(suffix)]
            break
    path = normalize_path(path_spec)
    if not path.exists():
        raise UserError(f"mount path does not exist: {path}")
    return Mount(path, path, mode)


def mount_covers(existing: Mount, candidate: Mount) -> bool:
    return (
        existing.mode == candidate.mode
        and is_relative_to(candidate.source, existing.source)
        and is_relative_to(candidate.target, existing.target)
    )


def mount_replaces(candidate: Mount, existing: Mount) -> bool:
    return (
        existing.mode == candidate.mode
        and is_relative_to(existing.source, candidate.source)
        and is_relative_to(existing.target, candidate.target)
    )


def merge_mounts(mounts: Iterable[Mount]) -> tuple[Mount, ...]:
    result: list[Mount] = []
    for mount in sorted(mounts, key=lambda item: (path_depth(item.source), str(item.source), str(item.target))):
        if mount.mode not in {"ro", "rw"}:
            raise UserError(f"invalid mount mode for {mount.source}: {mount.mode}")
        redundant = False
        next_result: list[Mount] = []
        for existing in result:
            if existing.source == mount.source and existing.target == mount.target:
                mode = "rw" if "rw" in {existing.mode, mount.mode} else "ro"
                next_result.append(Mount(existing.source, existing.target, mode))
                redundant = True
                continue
            if mount_covers(existing, mount):
                redundant = True
                next_result.append(existing)
                continue
            if mount_replaces(mount, existing):
                continue
            next_result.append(existing)
        result = next_result
        if not redundant:
            result.append(mount)
    return tuple(sorted(result, key=lambda item: (path_depth(item.source), str(item.source), str(item.target))))


def git_repo_root(path: Path) -> Path:
    if not path.exists():
        raise UserError(f"repository path does not exist: {path}")
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


def default_container_name(repo: Path) -> str:
    digest = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^a-z0-9_.-]+", "-", repo.name.lower()).strip(".-") or "repo"
    return f"git-multiagent-{stem}-{digest}"


def dockerfile_path() -> Path:
    return Path(__file__).resolve().parents[1] / "docker" / "Dockerfile"


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def git_source_files(root: Path) -> tuple[Path, ...] | None:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return tuple(Path(item) for item in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item)


def filesystem_source_files(root: Path) -> tuple[Path, ...]:
    ignored_dirs = {".git", ".pytest_cache", "__pycache__", "build", "dist"}
    result: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in ignored_dirs or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.is_file() and not path.name.endswith(".pyc"):
            result.append(rel)
    return tuple(result)


def source_fingerprint(root: Path | None = None) -> str:
    root = (source_root() if root is None else root).resolve()
    files = git_source_files(root)
    if files is None:
        files = filesystem_source_files(root)
    digest = hashlib.sha256()
    for rel in sorted(files, key=lambda item: item.as_posix()):
        path = root / rel
        try:
            stat = path.stat()
        except OSError:
            digest.update(rel.as_posix().encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0missing\0")
            continue
        if not path.is_file():
            continue
        digest.update(rel.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_mode & 0o777).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def expand_pi_agent_dir(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value).expanduser()


def resolve_host_pi_agent_dir(home: Path, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get(PI_AGENT_DIR_ENV)
    if configured:
        return expand_pi_agent_dir(configured, home).resolve()
    return (home / ".pi" / "agent").resolve()


def run_dir_for_name(registry_dir: Path, name: str) -> Path:
    return registry_dir / "runs" / name


def state_base_dir_for_registry(registry_dir: Path) -> Path:
    return registry_dir / "state"


def state_dir_for_repo(repo: Path, registry_dir: Path) -> Path:
    return state_base_dir_for_registry(registry_dir) / registry_instance_name(repo)


def build_mount_plan(
    repo: Path,
    state_base_dir: Path,
    run_dir: Path,
    extra_mounts: Iterable[Mount] = (),
) -> tuple[Mount, ...]:
    candidates: list[Mount] = [
        Mount(repo, repo, "rw"),
        Mount(state_base_dir, state_base_dir, "rw"),
        Mount(run_dir, run_dir, "rw"),
    ]
    candidates.extend(extra_mounts)
    return merge_mounts(candidates)


def existing_devices(values: Iterable[str]) -> tuple[Path, ...]:
    devices = []
    for value in values:
        path = normalize_path(value)
        if not path.exists():
            raise UserError(f"device does not exist: {path}")
        devices.append(path)
    return tuple(devices)


def device_groups(devices: Iterable[Path]) -> tuple[int, ...]:
    groups: set[int] = set()
    current_gid = os.getgid()
    for device in devices:
        try:
            gid = device.stat().st_gid
        except OSError:
            continue
        if gid != current_gid:
            groups.add(gid)
    return tuple(sorted(groups))


def start_config_from_args(args: argparse.Namespace) -> StartConfig:
    repo = git_repo_root(normalize_path(args.repo))
    registry_dir = normalize_path(args.registry_dir)
    home = normalize_path(os.environ.get("HOME", str(Path.home())))
    name = args.name or default_container_name(repo)
    state_base_dir = state_base_dir_for_registry(registry_dir)
    state_dir = state_dir_for_repo(repo, registry_dir)
    run_dir = run_dir_for_name(registry_dir, name)
    pi_config_dir = run_dir / "pi-config"
    projected_pi_home = pi_config_dir
    host_pi_agent_dir = resolve_host_pi_agent_dir(home)
    if not args.dry_run:
        registry_dir.mkdir(parents=True, exist_ok=True)
        state_base_dir.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
    extra_mounts = [parse_mount_spec(spec) for spec in args.mount or []]
    mounts = build_mount_plan(repo, state_base_dir, run_dir, extra_mounts)
    devices = existing_devices(args.device or [])
    forward_env = tuple(name for name in DEFAULT_FORWARD_ENV if name in os.environ)
    return StartConfig(
        repo=repo,
        registry_dir=registry_dir,
        state_base_dir=state_base_dir,
        state_dir=state_dir,
        image=args.image,
        source_fingerprint=source_fingerprint(),
        name=name,
        run_dir=run_dir,
        pi_config_dir=pi_config_dir,
        mounts=mounts,
        devices=devices,
        projected_pi_home=projected_pi_home,
        host_pi_agent_dir=host_pi_agent_dir,
        auth_socket=run_dir / "host-auth.sock",
        auth_proxy_pid=run_dir / "host-auth.pid",
        auth_proxy_ready=run_dir / "host-auth.ready",
        auth_relay_port=HOST_AUTH_RELAY_PORT,
        env=tuple(args.env or []),
        forward_env=forward_env,
        foreground=args.foreground,
        restart=args.restart,
        network=args.network,
        home=CONTAINER_HOME,
    )


def container_path() -> str:
    return "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def docker_common_options(config: StartConfig) -> list[str]:
    command: list[str] = []
    command.extend(["--name", config.name])
    command.extend(["--label", RUN_LABEL])
    command.extend(["--label", f"{REPO_LABEL}={config.repo}"])
    command.extend(["--label", f"{SOURCE_FINGERPRINT_LABEL}={config.source_fingerprint}"])
    command.extend(["--label", f"{RUN_CONFIG_FINGERPRINT_LABEL}={run_config_fingerprint(config)}"])
    command.extend(["--workdir", str(config.repo)])
    command.extend(["--network", config.network])
    command.extend(["-e", f"HOME={config.home}"])
    command.extend(["-e", "USER=gitmultiagent"])
    command.extend(["-e", f"PATH={container_path()}"])
    command.extend(["-e", f"GIT_MULTIAGENT_HOST_UID={os.getuid()}"])
    command.extend(["-e", f"GIT_MULTIAGENT_HOST_GID={os.getgid()}"])
    extra_groups = ":".join(str(group) for group in device_groups(config.devices))
    if extra_groups:
        command.extend(["-e", f"GIT_MULTIAGENT_EXTRA_GROUPS={extra_groups}"])
    command.extend(["-e", f"GIT_MULTIAGENT_PI_CONFIG_DIR={config.pi_config_dir}"])
    command.extend(["-e", f"GIT_MULTIAGENT_REGISTRY_DIR={config.registry_dir}"])
    command.extend(["-e", f"GIT_MULTIAGENT_STATE_DIR={config.state_dir}"])
    command.extend(["-e", "GIT_MULTIAGENT_CONTAINER=docker"])
    command.extend(["-e", f"GIT_MULTIAGENT_CONTAINER_NAME={config.name}"])
    for env_name in config.forward_env:
        command.extend(["-e", env_name])
    for env_spec in config.env:
        command.extend(["-e", env_spec])
    for group in device_groups(config.devices):
        command.extend(["--group-add", str(group)])
    for mount in config.mounts:
        command.extend(["--mount", mount.docker_value()])
    for device in config.devices:
        command.extend(["--device", str(device)])
    return command


def supervisor_command(config: StartConfig) -> list[str]:
    command = [
        config.image,
        "python",
        "-m",
        "git_multiagent.auth_relay",
        "--socket",
        str(config.auth_socket),
        "--listen",
        f"127.0.0.1:{config.auth_relay_port}",
        "--",
        "multiagent",
        "local",
        "start",
        "--foreground",
    ]
    if config.restart:
        command.append("--restart")
    return command


def docker_run_command(config: StartConfig) -> list[str]:
    command = ["docker", "run", "--stop-timeout", "30"]
    if not config.foreground:
        command.append("--detach")
    command.extend(docker_common_options(config))
    command.extend(supervisor_command(config))
    return command


def run_config_fingerprint(config: StartConfig) -> str:
    data = {
        "repo": str(config.repo),
        "registry_dir": str(config.registry_dir),
        "state_base_dir": str(config.state_base_dir),
        "state_dir": str(config.state_dir),
        "image": config.image,
        "source_fingerprint": config.source_fingerprint,
        "name": config.name,
        "run_dir": str(config.run_dir),
        "pi_config_dir": str(config.pi_config_dir),
        "mounts": [
            {"source": str(mount.source), "target": str(mount.target), "mode": mount.mode}
            for mount in config.mounts
        ],
        "devices": [str(device) for device in config.devices],
        "env": list(config.env),
        "forward_env": list(config.forward_env),
        "restart": config.restart,
        "network": config.network,
        "home": str(config.home),
    }
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def container_matches_config(config: StartConfig) -> bool:
    return (
        docker_container_label(config.name, SOURCE_FINGERPRINT_LABEL) == config.source_fingerprint
        and docker_container_label(config.name, RUN_CONFIG_FINGERPRINT_LABEL) == run_config_fingerprint(config)
    )


def docker_build_command(image: str, fingerprint: str) -> list[str]:
    return [
        "docker",
        "build",
        "-t",
        image,
        "--label",
        f"{SOURCE_FINGERPRINT_LABEL}={fingerprint}",
        "-f",
        str(dockerfile_path()),
        str(source_root()),
    ]


def docker_image_inspect_command(image: str) -> list[str]:
    return ["docker", "image", "inspect", image]


def docker_image_exists(image: str) -> bool:
    try:
        proc = subprocess.run(
            docker_image_inspect_command(image),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UserError("required command not found: docker") from exc
    return proc.returncode == 0


def docker_container_exists(name: str) -> bool:
    try:
        proc = subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except FileNotFoundError as exc:
        raise UserError("required command not found: docker") from exc
    return proc.returncode == 0


def docker_container_id(name: str) -> str:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UserError("required command not found: docker") from exc
    return proc.stdout.strip() if proc.returncode == 0 else ""


def docker_label_template(label: str) -> str:
    return "{{ index .Config.Labels " + json.dumps(label) + " }}"


def docker_inspect_label(command: list[str]) -> str:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UserError("required command not found: docker") from exc
    if proc.returncode != 0:
        return ""
    value = proc.stdout.strip()
    return "" if value == "<no value>" else value


def docker_image_label(image: str, label: str) -> str:
    return docker_inspect_label(["docker", "image", "inspect", "--format", docker_label_template(label), image])


def docker_container_label(name: str, label: str) -> str:
    return docker_inspect_label(["docker", "inspect", "--format", docker_label_template(label), name])


def docker_container_running(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UserError("required command not found: docker") from exc
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def docker_start_command(name: str, foreground: bool = False) -> list[str]:
    command = ["docker", "start"]
    if foreground:
        command.append("--attach")
    command.append(name)
    return command


def run_command(command: list[str]) -> int:
    try:
        return subprocess.call(command)
    except FileNotFoundError as exc:
        raise UserError(f"required command not found: {command[0]}") from exc


def run_command_capture(command: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, check=False)
    except FileNotFoundError as exc:
        raise UserError(f"required command not found: {command[0]}") from exc
    if proc.stdout:
        print(proc.stdout, end="")
    return proc.returncode, proc.stdout.strip()


def load_settings_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserError(f"failed to read Pi settings: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UserError(f"Pi settings must be a JSON object: {path}")
    return data


def load_host_pi_settings(agent_dir: Path, repo: Path) -> dict[str, object]:
    settings = load_settings_file(agent_dir / "settings.json")
    project_settings = load_settings_file(repo / ".pi" / "settings.json")
    return {**settings, **project_settings}


def base64url_json(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_proxy_token() -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        JWT_CLAIM_PATH: {"chatgpt_account_id": "host-auth"},
        "aud": "git-multiagent-host-auth",
        "jti": secrets.token_urlsafe(32),
    }
    return f"{base64url_json(header)}.{base64url_json(payload)}.hostauth"


def projected_models_json(base_url: str, api_key: str, default_model: str) -> dict[str, object]:
    model_ids = list(CODEX_MODELS)
    if default_model not in model_ids:
        model_ids.append(default_model)
    models = [
        {
            "id": model_id,
            "name": model_id,
            "reasoning": True,
            "input": ["text"] if model_id.endswith("spark") else ["text", "image"],
            "contextWindow": 272000 if not model_id.endswith("spark") else 128000,
            "maxTokens": 128000,
        }
        for model_id in model_ids
    ]
    return {
        "providers": {
            HOST_CODEX_PROVIDER: {
                "baseUrl": base_url,
                "api": "openai-codex-responses",
                "apiKey": api_key,
                "models": models,
            }
        }
    }


def prepare_projected_pi_home(config: StartConfig, proxy_token: str) -> None:
    settings = load_host_pi_settings(config.host_pi_agent_dir, config.repo)
    host_provider = str(settings.get("defaultProvider") or HOST_CODEX_AUTH_PROVIDER)
    if host_provider != HOST_CODEX_AUTH_PROVIDER:
        raise UserError(
            f"multiagent docker host auth requires host Pi defaultProvider={HOST_CODEX_AUTH_PROVIDER!r}; "
            f"{config.host_pi_agent_dir / 'settings.json'} selects {host_provider!r}"
        )
    default_model = str(settings.get("defaultModel") or "gpt-5.5")
    default_thinking = str(settings.get("defaultThinkingLevel") or "xhigh")
    agent_dir = config.projected_pi_home / "agent"
    agent_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    projected_settings = {
        "defaultProvider": HOST_CODEX_PROVIDER,
        "defaultModel": default_model,
        "defaultThinkingLevel": default_thinking,
        "packages": [PI_WEB_ACCESS_PACKAGE],
    }
    projected_models = projected_models_json(
        f"http://127.0.0.1:{config.auth_relay_port}",
        proxy_token,
        default_model,
    )
    (agent_dir / "settings.json").write_text(json.dumps(projected_settings, indent=2) + "\n", encoding="utf-8")
    (agent_dir / "models.json").write_text(json.dumps(projected_models, indent=2) + "\n", encoding="utf-8")
    os.chmod(agent_dir / "settings.json", 0o600)
    os.chmod(agent_dir / "models.json", 0o600)


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def stop_host_auth_proxy_process(run_dir: Path, *, remove_pi_config: bool = False) -> None:
    pid_file = run_dir / "host-auth.pid"
    socket_path = run_dir / "host-auth.sock"
    ready_file = run_dir / "host-auth.ready"
    pid = read_pid(pid_file)
    if pid is not None and process_is_running(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and process_is_running(pid):
            time.sleep(0.05)
        if process_is_running(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    unlink_if_exists(pid_file)
    unlink_if_exists(ready_file)
    unlink_if_exists(socket_path)
    if remove_pi_config:
        unlink_if_exists(run_dir / "pi-config" / "agent" / "models.json")
        unlink_if_exists(run_dir / "pi-config" / "agent" / "settings.json")
        try:
            run_dir.rmdir()
        except OSError:
            pass


def read_projected_proxy_token(config: StartConfig) -> str | None:
    path = config.projected_pi_home / "agent" / "models.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        token = data["providers"][HOST_CODEX_PROVIDER]["apiKey"]
    except (KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


def start_host_auth_proxy(config: StartConfig, proxy_token: str) -> None:
    stop_host_auth_proxy_process(config.run_dir)
    env = os.environ.copy()
    env["GIT_MULTIAGENT_HOST_AUTH_TOKEN"] = proxy_token
    command = [
        sys.executable,
        "-m",
        "git_multiagent.auth_proxy",
        "--socket",
        str(config.auth_socket),
        "--auth-path",
        str(config.host_pi_agent_dir / "auth.json"),
        "--pid-file",
        str(config.auth_proxy_pid),
        "--ready-file",
        str(config.auth_proxy_ready),
    ]
    proc = subprocess.Popen(command, env=env, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if config.auth_proxy_ready.exists():
            return
        rc = proc.poll()
        if rc is not None:
            raise UserError(f"host auth proxy exited during startup with status {rc}")
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    raise UserError("host auth proxy did not become ready")


def stop_host_auth_proxy_run_dir(run_dir: Path) -> None:
    stop_host_auth_proxy_process(run_dir, remove_pi_config=False)


def destroy_host_auth_proxy_run_dir(run_dir: Path) -> None:
    stop_host_auth_proxy_process(run_dir, remove_pi_config=True)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def registry_instance_path(repo: Path, registry_dir: Path) -> Path:
    return registry_dir / "instances" / registry_instance_name(repo)


def registry_metadata_path(repo: Path, registry_dir: Path) -> Path:
    path = registry_instance_path(repo, registry_dir)
    return path.with_name(f"{path.name}.json")


def docker_container_host_pid(name: str) -> int | None:
    try:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Pid}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UserError("required command not found: docker") from exc
    if proc.returncode != 0:
        return None
    try:
        pid = int(proc.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def wait_for_container_host_pid(name: str) -> int | None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pid = docker_container_host_pid(name)
        if pid:
            return pid
        time.sleep(0.05)
    return docker_container_host_pid(name)


def write_host_registry_instance(config: StartConfig, container_id: str) -> None:
    path = registry_instance_path(config.repo, config.registry_dir)
    metadata_path = registry_metadata_path(config.repo, config.registry_dir)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    for name in STATE_SUBDIRS:
        (config.state_dir / name).mkdir(parents=True, exist_ok=True)
    write_text_atomic(config.state_dir / "repo-root", str(config.repo) + "\n")
    write_text_atomic(config.state_dir / "instance-id", registry_instance_name(config.repo) + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    target = config.state_dir.resolve()
    if path.is_symlink():
        try:
            if path.resolve(strict=True) != target:
                path.unlink()
        except OSError:
            path.unlink()
    elif path.exists():
        raise UserError(f"cannot register MULTIAGENT instance; path already exists: {path}")
    if not path.exists():
        os.symlink(target, path, target_is_directory=True)
    host_pid = wait_for_container_host_pid(config.name)
    metadata = {
        "version": 1,
        "runtime": "docker",
        "repoRoot": str(config.repo),
        "stateRoot": str(target),
        "containerName": config.name,
        "containerId": container_id,
        "hostPid": host_pid,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_text_atomic(metadata_path, json.dumps(metadata, indent=2) + "\n")


def remove_registry_instance_for_repo(repo: Path, registry_dir: Path) -> None:
    for path in (registry_metadata_path(repo, registry_dir), registry_instance_path(repo, registry_dir)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            pass


def cmd_build_image(args: argparse.Namespace) -> int:
    command = docker_build_command(args.image, source_fingerprint())
    if args.dry_run:
        print(shell_join(command))
        return 0
    return run_command(command)


def cmd_start(args: argparse.Namespace) -> int:
    config = start_config_from_args(args)
    build_command = docker_build_command(args.image, config.source_fingerprint)
    container_exists = docker_container_exists(config.name) if not args.dry_run else False
    container_running = docker_container_running(config.name) if container_exists and not args.dry_run else False
    container_stale = (
        container_exists
        and (
            args.build
            or not container_matches_config(config)
        )
    )
    start_command = docker_start_command(config.name, foreground=config.foreground) if container_exists else docker_run_command(config)
    if args.dry_run:
        if args.build:
            print(shell_join(build_command))
        print(f"host-auth-proxy --socket {shlex.quote(str(config.auth_socket))}")
        print(shell_join(start_command))
        return 0
    if container_running:
        if container_stale:
            image_current = (
                docker_image_exists(args.image)
                and docker_image_label(args.image, SOURCE_FINGERPRINT_LABEL) == config.source_fingerprint
            )
            if args.build or not image_current:
                rc = run_command(build_command)
                if rc != 0:
                    return rc
            rc = docker_stop_and_remove(config.name)
            if rc != 0:
                return rc
            stop_host_auth_proxy_run_dir(config.run_dir)
            remove_registry_instance_for_repo(config.repo, config.registry_dir)
            container_exists = False
            container_running = False
            start_command = docker_run_command(config)
        else:
            proxy_token = read_projected_proxy_token(config)
            if not proxy_token:
                raise UserError(
                    f"container {config.name} is already running but has no projected host-auth token; "
                    "use multiagent docker stop, then multiagent docker start"
                )
            start_host_auth_proxy(config, proxy_token)
            container_id = docker_container_id(config.name)
            write_host_registry_instance(config, container_id)
            print(config.name)
            return 0
    if container_exists and container_stale:
        image_current = (
            docker_image_exists(args.image)
            and docker_image_label(args.image, SOURCE_FINGERPRINT_LABEL) == config.source_fingerprint
        )
        if args.build or not image_current:
            rc = run_command(build_command)
            if rc != 0:
                return rc
        rc = docker_stop_and_remove(config.name)
        if rc != 0:
            return rc
        stop_host_auth_proxy_run_dir(config.run_dir)
        remove_registry_instance_for_repo(config.repo, config.registry_dir)
        container_exists = False
        start_command = docker_run_command(config)
    if not container_exists:
        image_current = (
            docker_image_exists(args.image)
            and docker_image_label(args.image, SOURCE_FINGERPRINT_LABEL) == config.source_fingerprint
        )
        if args.build or not image_current:
            rc = run_command(build_command)
            if rc != 0:
                return rc
    proxy_token = read_projected_proxy_token(config) if container_exists else None
    if not proxy_token:
        proxy_token = make_proxy_token()
        prepare_projected_pi_home(config, proxy_token)
    start_host_auth_proxy(config, proxy_token)
    if config.foreground:
        rc = run_command(start_command)
        stop_host_auth_proxy_run_dir(config.run_dir)
        return rc
    rc, output = run_command_capture(start_command)
    if rc != 0:
        stop_host_auth_proxy_run_dir(config.run_dir)
        return rc
    container_id = docker_container_id(config.name) if container_exists else output
    write_host_registry_instance(config, container_id)
    return 0


def name_from_repo_or_arg(repo: str | None, name: str | None) -> str:
    if name:
        return name
    if not repo:
        raise UserError("provide a repository path or --name")
    return default_container_name(git_repo_root(normalize_path(repo)))


def registry_from_args(args: argparse.Namespace) -> Path:
    return normalize_path(args.registry_dir)


def registry_instance_name(repo: Path) -> str:
    digest = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:12]
    repo_name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name).strip(".-") or "repo"
    return f"{repo_name}-{digest}"


def docker_call_ignore_missing(command: list[str]) -> int:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UserError("required command not found: docker") from exc
    if proc.returncode != 0 and "No such container" in proc.stderr:
        return 0
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


def docker_stop(name: str) -> int:
    return docker_call_ignore_missing(["docker", "stop", "--timeout", "30", name])


def docker_remove(name: str) -> int:
    return docker_call_ignore_missing(["docker", "rm", name])


def docker_stop_and_remove(name: str) -> int:
    rc = docker_stop(name)
    if rc != 0:
        return rc
    return docker_remove(name)


def cmd_stop(args: argparse.Namespace) -> int:
    repo = git_repo_root(normalize_path(args.repo)) if args.repo else None
    name = args.name or (default_container_name(repo) if repo is not None else None)
    if name is None:
        raise UserError("provide a repository path or --name")
    registry = registry_from_args(args)
    rc = docker_stop(name)
    stop_host_auth_proxy_run_dir(run_dir_for_name(registry, name))
    if repo is not None:
        remove_registry_instance_for_repo(repo, registry)
    return rc


def cmd_destroy(args: argparse.Namespace) -> int:
    repo = git_repo_root(normalize_path(args.repo)) if args.repo else None
    name = args.name or (default_container_name(repo) if repo is not None else None)
    if name is None:
        raise UserError("provide a repository path or --name")
    registry = registry_from_args(args)
    rc = docker_stop_and_remove(name)
    destroy_host_auth_proxy_run_dir(run_dir_for_name(registry, name))
    if repo is not None:
        remove_registry_instance_for_repo(repo, registry)
    return rc


def cmd_logs(args: argparse.Namespace) -> int:
    name = name_from_repo_or_arg(args.repo, args.name)
    command = ["docker", "logs"]
    if args.follow:
        command.append("--follow")
    command.append(name)
    return run_command(command)


def cmd_status(_args: argparse.Namespace) -> int:
    return run_command(
        [
            "docker",
            "ps",
            "--filter",
            f"label={RUN_LABEL}",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Image}}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multiagent docker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="run multiagent local start inside Docker")
    start.add_argument("repo", help="git repository to run")
    start.add_argument("--image", default=DEFAULT_IMAGE)
    start.add_argument("--name")
    start.add_argument("--mount", action="append", help="extra transparent bind mount PATH[:ro|:rw]")
    start.add_argument("--device", action="append", help="device path to pass through")
    start.add_argument("--env", action="append", help="extra environment variable, NAME or NAME=value")
    start.add_argument("--registry-dir", default=os.environ.get("GIT_MULTIAGENT_REGISTRY_DIR", DEFAULT_REGISTRY_DIR))
    start.add_argument("--network", default="bridge")
    start.add_argument("--foreground", action="store_true", help="attach to the container instead of detaching")
    start.add_argument("--restart", action="store_true", help="pass --restart to multiagent local start")
    start.add_argument("--build", action="store_true", help="build the local MULTIAGENT image before running")
    start.add_argument("--dry-run", action="store_true", help="print Docker commands without running them")
    start.set_defaults(func=cmd_start)

    build = subparsers.add_parser("build-image", help="build the local Docker image")
    build.add_argument("--image", default=DEFAULT_IMAGE)
    build.add_argument("--dry-run", action="store_true")
    build.set_defaults(func=cmd_build_image)

    stop = subparsers.add_parser("stop", help="stop a containerized MULTIAGENT system without deleting its container")
    stop.add_argument("repo", nargs="?")
    stop.add_argument("--name")
    stop.add_argument("--registry-dir", default=os.environ.get("GIT_MULTIAGENT_REGISTRY_DIR", DEFAULT_REGISTRY_DIR))
    stop.set_defaults(func=cmd_stop)

    destroy = subparsers.add_parser("destroy", help="stop and delete a containerized MULTIAGENT system")
    destroy.add_argument("repo", nargs="?")
    destroy.add_argument("--name")
    destroy.add_argument("--registry-dir", default=os.environ.get("GIT_MULTIAGENT_REGISTRY_DIR", DEFAULT_REGISTRY_DIR))
    destroy.set_defaults(func=cmd_destroy)

    logs = subparsers.add_parser("logs", help="show container logs")
    logs.add_argument("repo", nargs="?")
    logs.add_argument("--name")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)

    subparsers.add_parser("status", help="list running MULTIAGENT containers").set_defaults(func=cmd_status)
    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    return argv


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(normalize_argv(argv))
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
