from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_TOOL = REPO_ROOT / "git_multiagent" / "runtime" / "tools" / "agent"
INTERACTIVE_AGENT_TOOL = REPO_ROOT / "git_multiagent" / "runtime" / "tools" / "agent-pi-interactive"
HEARTBEAT_TOOL = REPO_ROOT / "git_multiagent" / "runtime" / "tools" / "heartbeat"
AGENT_INPUT_TOOL = REPO_ROOT / "git_multiagent" / "runtime" / "tools" / "agent-input"
GIT_MULTIAGENT_UI_TOOL = REPO_ROOT / "git_multiagent" / "runtime" / "tools" / "git-multiagent-ui"


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init"], cwd=self.repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.env = os.environ.copy()
        pythonpath = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = str(REPO_ROOT) if not pythonpath else f"{REPO_ROOT}{os.pathsep}{pythonpath}"
        self.env["GIT_MULTIAGENT_REGISTRY_DIR"] = str(self.repo / ".registry")

    def tearDown(self) -> None:
        subprocess.run(
            [sys.executable, "-m", "git_multiagent", "local", "stop"],
            cwd=self.repo,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.tmp.cleanup()

    def run_agents(self, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, "-m", "git_multiagent", *args],
            cwd=self.repo,
            env=self.env,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            self.fail(
                f"multiagent {' '.join(args)} failed with {proc.returncode}\n"
                f"stdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}"
            )
        return proc

    def run_agents_with_env(
        self,
        env: dict[str, str],
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, "-m", "git_multiagent", *args],
            cwd=self.repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            self.fail(
                f"multiagent {' '.join(args)} failed with {proc.returncode}\n"
                f"stdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}"
            )
        return proc

    def state_path(self) -> Path:
        from git_multiagent import cli

        return self.repo / ".registry" / "state" / cli.registry_instance_id(self.repo)

    def write_local_role(self, role: str = "planner") -> None:
        roles = self.repo / ".multiagent" / "roles"
        roles.mkdir(parents=True, exist_ok=True)
        (roles / f"{role}.md").write_text(f"# {role.title()} Role\n\nRole body.\n", encoding="utf-8")

    def run_with_agent_socket(self, agent: str, command: list[str]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        agent_dir = self.state_path() / "agents" / agent
        agent_dir.mkdir(parents=True, exist_ok=True)
        sock = agent_dir / "rpc.sock"
        try:
            sock.unlink()
        except FileNotFoundError:
            pass
        lines: list[str] = []
        errors: list[BaseException] = []
        ready = threading.Event()

        def serve() -> None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(sock))
                    server.listen(1)
                    ready.set()
                    conn, _addr = server.accept()
                    with conn:
                        with conn.makefile("r", encoding="utf-8") as stream:
                            lines.append(stream.readline())
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)
                ready.set()

        reader = threading.Thread(target=serve)
        reader.start()
        self.assertTrue(ready.wait(2))
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        reader.join(timeout=2)
        if reader.is_alive():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(sock))
                    client.sendall(b"\n")
            except OSError:
                pass
            reader.join(timeout=1)
        try:
            sock.unlink()
        except OSError:
            pass
        self.assertFalse(reader.is_alive())
        self.assertFalse(errors)
        return proc, lines

    def run_runtime_bin(self, state: Path, *args: str) -> subprocess.CompletedProcess[str]:
        git_multiagent_dir = self.repo / ".multiagent"
        runtime_bin = REPO_ROOT / "git_multiagent" / "runtime" / "bin"
        env = self.env.copy()
        env["GIT_MULTIAGENT_ROOT"] = str(git_multiagent_dir)
        env["GIT_MULTIAGENT_REPO_ROOT"] = str(self.repo)
        env["GIT_MULTIAGENT_STATE_DIR"] = str(state)
        proc = subprocess.run(
            [str(runtime_bin / args[0]), *args[1:]],
            cwd=git_multiagent_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(
                f"{args[0]} failed with {proc.returncode}\n"
                f"stdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}"
            )
        return proc

    def create_demo_task(self, task: str = "demo-task") -> tuple[Path, str]:
        self.run_agents("local", "init")
        spec = self.repo / f"{task}.md"
        spec.write_text(f"# {task}\n\nDo the thing.\n", encoding="utf-8")
        self.run_agents("agent", "task", "create", task, str(spec))
        return self.state_path(), f"{task}-plan"

    def claim_and_start_job(self, state: Path, job: str, agent: str = "planner-1") -> None:
        self.run_runtime_bin(state, "agent-new", agent, "planner")
        self.run_runtime_bin(state, "job-claim", job, "--agent-id", agent)
        self.run_runtime_bin(state, "job-start", job, "--agent-id", agent)
        (state / "agents" / agent / "current-job").write_text(f"{job}\n", encoding="utf-8")

    def load_agent_tool(self):
        loader = importlib.machinery.SourceFileLoader("git_multiagent_runtime_agent_test", str(AGENT_TOOL))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        loader.exec_module(module)
        return module

    def load_interactive_agent_tool(self):
        loader = importlib.machinery.SourceFileLoader(
            "git_multiagent_runtime_interactive_agent_test",
            str(INTERACTIVE_AGENT_TOOL),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        loader.exec_module(module)
        return module

    def load_ui_tool(self):
        loader = importlib.machinery.SourceFileLoader("git_multiagent_runtime_ui_test", str(GIT_MULTIAGENT_UI_TOOL))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        loader.exec_module(module)
        return module

    def test_follow_agent_turn_finishes_partial_line_with_newline(self) -> None:
        from git_multiagent import cli

        agent_dir = self.repo / "operator"
        agent_dir.mkdir()
        (agent_dir / "busy").write_text("0\n", encoding="utf-8")
        transcript = agent_dir / "transcript.log"
        transcript.write_text("agent response without newline", encoding="utf-8")

        output = io.StringIO()
        with (
            mock.patch.object(cli.time, "time", side_effect=[0.0, 0.0, 0.5]),
            mock.patch.object(cli.time, "sleep"),
            contextlib.redirect_stdout(output),
        ):
            cli.follow_agent_turn(agent_dir, transcript, 0)

        self.assertEqual(output.getvalue(), "agent response without newline\n")

    def test_start_and_supervisor_flags_are_minimal(self) -> None:
        from git_multiagent import cli

        parser = cli.build_parser(include_internal=True)

        for namespace in (
            parser.parse_args(["local", "start"]),
            parser.parse_args(["local", "restart"]),
            parser.parse_args(["_supervisor", "--repo-root", str(self.repo), "--state-dir", str(self.state_path())]),
        ):
            self.assertFalse(hasattr(namespace, "heartbeat"))

    def test_serve_is_not_a_multiagent_subcommand(self) -> None:
        from git_multiagent import cli

        parser = cli.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["serve"])

    def test_legacy_top_level_commands_are_not_subcommands(self) -> None:
        from git_multiagent import cli

        parser = cli.build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            for command in ("init", "start", "team", "tasks", "jobs", "agents", "run"):
                with self.assertRaises(SystemExit):
                    parser.parse_args([command])

        self.assertEqual(parser.parse_args(["docker", "status"]).func, cli.cmd_docker)

    def test_heartbeat_is_configured_per_interactive_agent(self) -> None:
        from git_multiagent import cli

        agents = cli.parse_team_text(
            "\n".join(
                [
                    '[[agent]]',
                    'name = "operator"',
                    'role = "planner"',
                    'mode = "interactive"',
                    'options = { heartbeat = 15 }',
                ]
            )
        )
        self.assertEqual(agents[0]["options"], {"heartbeat": 15})

        agents = cli.parse_team_text(
            "\n".join(
                [
                    '[[agent]]',
                    'name = "planner-1"',
                    'role = "planner"',
                    'mode = "worker"',
                ]
            )
        )
        self.assertNotIn("options", agents[0])

        with self.assertRaises(cli.UserError):
            cli.parse_team_text(
                "\n".join(
                    [
                        '[[agent]]',
                        'name = "planner-1"',
                        'role = "planner"',
                        'mode = "worker"',
                        'options = { heartbeat = 15 }',
                    ]
                )
            )

    def test_parse_heartbeat_value(self) -> None:
        from git_multiagent import cli

        self.assertEqual(cli.parse_heartbeat_value(None), 0)
        self.assertEqual(cli.parse_heartbeat_value(15), 15)
        self.assertEqual(cli.parse_heartbeat_value("15m"), 15)
        with self.assertRaises(cli.UserError):
            cli.parse_heartbeat_value(0)

    def test_heartbeat_tool_sends_agent_prompt(self) -> None:
        self.run_agents("local", "init")
        proc, lines = self.run_with_agent_socket(
            "operator",
            [str(HEARTBEAT_TOOL), "--state-dir", str(self.state_path()), "--agent", "operator", "--minutes", "1", "--once"],
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["mode"], "prompt")
        self.assertRegex(payload["message"], r"^heartbeat [0-9]{2}:[0-9]{2}:[0-9]{2}Z [0-9]{4}-[0-9]{2}-[0-9]{2}$")

    def test_heartbeat_waits_for_agent_socket_before_first_send(self) -> None:
        self.run_agents("local", "init")
        agent_dir = self.state_path() / "agents" / "operator"
        sock = agent_dir / "rpc.sock"
        lines: list[str] = []

        proc = subprocess.Popen(
            [str(HEARTBEAT_TOOL), "--state-dir", str(self.state_path()), "--agent", "operator", "--minutes", "1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.2)
            agent_dir.mkdir(parents=True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(sock))
                server.listen(1)
                server.settimeout(3)
                conn, _addr = server.accept()
                with conn:
                    with conn.makefile("r", encoding="utf-8") as stream:
                        lines.append(stream.readline())
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["mode"], "prompt")
            self.assertRegex(payload["message"], r"^heartbeat [0-9]{2}:[0-9]{2}:[0-9]{2}Z [0-9]{4}-[0-9]{2}-[0-9]{2}$")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            try:
                sock.unlink()
            except OSError:
                pass

    def test_agent_input_tool_sends_prompt(self) -> None:
        self.run_agents("local", "init")
        proc, lines = self.run_with_agent_socket(
            "operator",
            [str(AGENT_INPUT_TOOL), "--state-dir", str(self.state_path()), "operator", "hello", "agent"],
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), {"message": "hello agent", "mode": "prompt"})

    def test_ui_registry_lists_running_instances(self) -> None:
        ui_tool = self.load_ui_tool()
        state = self.state_path()
        for name in ("tasks", "jobs", "agents"):
            (state / name).mkdir(parents=True)
        (state / "runs").mkdir(parents=True)
        (state / "repo-root").write_text(str(self.repo) + "\n", encoding="utf-8")
        supervisor_pid = 999999
        (state / "runs" / "supervisor.pid").write_text(f"{supervisor_pid}\n", encoding="utf-8")
        registry_dir = self.repo / ".registry"
        instances_dir = registry_dir / "instances"
        instances_dir.mkdir(parents=True)
        os.symlink(state, instances_dir / "demo", target_is_directory=True)

        config = ui_tool.Config(
            root=state,
            host="127.0.0.1",
            port=0,
            registry=True,
            registry_dir=registry_dir,
        )
        with mock.patch.object(ui_tool, "pid_is_running", return_value=True):
            entries = ui_tool.registry_entries(config)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "demo")
        self.assertEqual(entries[0]["repoRoot"], str(self.repo))
        self.assertEqual(entries[0]["stateRoot"], str(state))
        self.assertEqual(entries[0]["supervisorPid"], supervisor_pid)
        self.assertTrue(entries[0]["running"])
        self.assertTrue(entries[0]["valid"])
        self.assertEqual(ui_tool.default_registry_root(config), str(state))


    def test_ui_registry_uses_multiagent_run_host_pid_metadata(self) -> None:
        ui_tool = self.load_ui_tool()
        state = self.state_path()
        for name in ("tasks", "jobs", "agents"):
            (state / name).mkdir(parents=True)
        (state / "runs").mkdir(parents=True)
        (state / "repo-root").write_text(str(self.repo) + "\n", encoding="utf-8")
        (state / "runs" / "supervisor.pid").write_text("999999999\n", encoding="utf-8")
        registry_dir = self.repo / ".registry"
        instances_dir = registry_dir / "instances"
        instances_dir.mkdir(parents=True)
        link = instances_dir / "demo"
        os.symlink(state, link, target_is_directory=True)
        (instances_dir / "demo.json").write_text(
            json.dumps({"runtime": "docker", "containerName": "demo-container", "hostPid": os.getpid()}) + "\n",
            encoding="utf-8",
        )

        config = ui_tool.Config(
            root=state,
            host="127.0.0.1",
            port=0,
            registry=True,
            registry_dir=registry_dir,
        )
        entries = ui_tool.registry_entries(config)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["supervisorPid"], os.getpid())
        self.assertTrue(entries[0]["running"])
        self.assertEqual(entries[0]["runtime"], "docker")
        self.assertEqual(entries[0]["containerName"], "demo-container")

    def test_status_uses_multiagent_run_host_pid_metadata(self) -> None:
        self.run_agents("local", "init")
        state = self.state_path()
        runs = state / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "supervisor.pid").write_text("999999999\n", encoding="utf-8")
        (runs / "supervisor.json").write_text(
            json.dumps({"container_runtime": "docker", "container_name": "demo-container"}) + "\n",
            encoding="utf-8",
        )
        from git_multiagent import cli

        instances = self.repo / ".registry" / "instances"
        instances.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"GIT_MULTIAGENT_REGISTRY_DIR": str(self.repo / ".registry")}):
            repo = cli.discover_repo(self.repo)
            link = cli.registry_instance_path(repo)
            metadata = cli.registry_metadata_path(repo)
        os.symlink(state, link, target_is_directory=True)
        metadata.write_text(
            json.dumps({"runtime": "docker", "containerName": "demo-container", "hostPid": os.getpid()}) + "\n",
            encoding="utf-8",
        )

        status = self.run_agents("local", "status").stdout

        self.assertIn("supervisor", status)
        self.assertIn("running", status)
        self.assertIn(str(os.getpid()), status)

    def test_ui_marks_interactive_agents_ready_from_rpc_socket(self) -> None:
        ui_tool = self.load_ui_tool()
        state = self.state_path()
        for name in ("tasks", "jobs", "agents"):
            (state / name).mkdir(parents=True)
        (state / "tasks" / "demo").mkdir()
        (state / "jobs" / "demo-plan").mkdir()
        agent_dir = state / "agents" / "operator"
        agent_dir.mkdir()
        (agent_dir / "interactive").write_text("1\n", encoding="utf-8")
        (agent_dir / "name").write_text("Operator\n", encoding="utf-8")
        (agent_dir / "role").write_text("planner\n", encoding="utf-8")
        (agent_dir / "mode").write_text("interactive\n", encoding="utf-8")
        sock = agent_dir / "rpc.sock"
        (agent_dir / "rpc.json").write_text(json.dumps({"socket": str(sock)}) + "\n", encoding="utf-8")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(sock))
            agents = ui_tool.read_agents(state, {})
            summary = ui_tool.instance_summary(state)

        self.assertEqual(agents[0]["id"], "operator")
        self.assertTrue(agents[0]["interactive"])
        self.assertTrue(agents[0]["inputReady"])
        self.assertEqual(agents[0]["rpcPath"], str(sock))
        self.assertEqual(summary["taskCount"], 1)
        self.assertEqual(summary["jobCount"], 1)
        self.assertEqual(summary["agentCount"], 1)
        self.assertEqual(summary["interactiveCount"], 1)
        self.assertEqual(summary["readyInteractiveCount"], 1)

    def test_dashboard_command_runs_registry_viewer(self) -> None:
        from git_multiagent import dashboard

        with mock.patch.object(dashboard.subprocess, "call", return_value=0) as call:
            self.assertEqual(dashboard.main(["--port", "0"]), 0)

        command = call.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertIn("--registry", command)
        self.assertEqual(command[-2:], ["--port", "0"])

        with mock.patch.object(dashboard.subprocess, "call", side_effect=KeyboardInterrupt):
            self.assertEqual(dashboard.main([]), 0)

    def test_rules_are_inspection_only(self) -> None:
        from git_multiagent import cli

        parser = cli.build_parser()

        self.assertEqual(parser.parse_args(["local", "rules", "show"]).func, cli.cmd_rules_show)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["local", "rules", "edit"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["local", "rules", "reset", "--yes"])

    def test_runtime_agent_tools_do_not_expose_engine_selection(self) -> None:
        agent_tool = self.load_agent_tool()
        interactive_tool = self.load_interactive_agent_tool()

        with mock.patch.object(sys, "argv", ["agent", "planner", "planner-1"]):
            self.assertFalse(hasattr(agent_tool.parse_args(), "engine"))
        with mock.patch.object(sys, "argv", ["agent-pi-interactive", "planner", "operator"]):
            self.assertFalse(hasattr(interactive_tool.parse_args(), "engine"))


    def test_agent_renderer_surfaces_pi_message_errors(self) -> None:
        agent_tool = self.load_agent_tool()
        transcript_path = self.repo / "transcript.log"
        transcript = agent_tool.Transcript([transcript_path])
        try:
            renderer = agent_tool.Renderer(transcript)
            renderer.render_line(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": "unauthorized",
                        },
                    }
                )
            )
        finally:
            transcript.close()

        self.assertEqual(renderer.error_message, "unauthorized")
        rendered = transcript_path.read_text(encoding="utf-8")
        self.assertIn("pi error", rendered)
        self.assertIn("unauthorized", rendered)

    def test_interactive_prompts_name_runtime_root(self) -> None:
        interactive_tool = self.load_interactive_agent_tool()
        root = self.repo / ".multiagent"
        repo_root = self.repo

        state_dir = self.repo / ".registry" / "state" / "repo-123"
        self.write_local_role()
        with mock.patch.dict(os.environ, {"GIT_MULTIAGENT_STATE_DIR": str(state_dir)}):
            prompt = interactive_tool.build_prompt(
                "operator",
                "planner",
                root,
                repo_root,
            )
        self.assertIn(f"Your MULTIAGENT root is: {self.repo}", prompt)
        self.assertNotIn(f"Your MULTIAGENT root is: {self.repo / '.multiagent'}", prompt)
        self.assertIn("Your MULTIAGENT instance is: repo-123", prompt)
        self.assertIn(f"Your MULTIAGENT state directory is: {state_dir}", prompt)
        self.assertNotIn("Your target repository root is", prompt)
        self.assertIn("## MULTIAGENT Generic Protocol", prompt)
        self.assertIn("# MULTIAGENT - Generic Agent Protocol", prompt)
        self.assertIn("## Role Instructions: planner", prompt)
        self.assertIn("# Planner Role", prompt)
        self.assertIn("Use the `multiagent agent ...` protocol utilities.", prompt)
        self.assertIn("repository paths as relative to the MULTIAGENT root", prompt)
        self.assertIn("tasks/, jobs/, agents/, runs/, and logs/", prompt)
        self.assertNotIn("Your assigned job is", prompt)

    def test_interactive_busy_state_ignores_rpc_response_ack(self) -> None:
        interactive_tool = self.load_interactive_agent_tool()

        self.assertIs(interactive_tool.rpc_streaming_state({"type": "turn_start"}), True)
        self.assertIsNone(interactive_tool.rpc_streaming_state({"type": "response", "success": True}))
        self.assertIsNone(interactive_tool.rpc_streaming_state({"type": "response", "success": False}))
        self.assertIs(interactive_tool.rpc_streaming_state({"type": "turn_end"}), False)

    def test_interactive_pi_rpc_command_continues_existing_session(self) -> None:
        interactive_tool = self.load_interactive_agent_tool()
        session_dir = self.repo / "pi-session"

        command = interactive_tool.build_pi_rpc_command(None, "prompt", session_dir)
        self.assertNotIn("--continue", command)

        session_dir.mkdir()
        (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
        command = interactive_tool.build_pi_rpc_command("test-model", "prompt", session_dir)

        self.assertIn("--continue", command)
        self.assertIn("--append-system-prompt", command)
        self.assertIn("--session-dir", command)
        self.assertIn("test-model", command)

    def test_worker_pi_command_injects_context_as_system_append(self) -> None:
        agent_tool = self.load_agent_tool()
        agent_dir = self.repo / "agent"

        command = agent_tool.build_command("test-model", agent_dir, "MULTIAGENT context")

        self.assertIn("--append-system-prompt", command)
        self.assertEqual(command[command.index("--append-system-prompt") + 1], "MULTIAGENT context")
        self.assertEqual(command[-1], "Process your assigned MULTIAGENT job now.")
        self.assertIn("--session-dir", command)
        self.assertIn("test-model", command)

    def test_interactive_restart_prompt_points_to_existing_transcript(self) -> None:
        interactive_tool = self.load_interactive_agent_tool()
        root = self.repo / ".multiagent"
        transcript = self.repo / "transcript.log"
        crash = self.repo / "last-crash"
        crash.write_text("exit: 1\nactive_turn: yes\n", encoding="utf-8")
        state_dir = self.repo / ".registry" / "state" / "repo-123"
        self.write_local_role()

        with mock.patch.dict(os.environ, {"GIT_MULTIAGENT_STATE_DIR": str(state_dir)}):
            prompt = interactive_tool.build_prompt(
                "operator",
                "planner",
                root,
                self.repo,
                transcript_path=transcript,
                include_restart_context=True,
                crash_path=crash,
            )

        self.assertIn(f"read enough recent history from {transcript}", prompt)
        self.assertIn("previous Pi process crashed during an active turn", prompt)
        self.assertIn(str(crash), prompt)

    def test_init_creates_git_private_state_without_tracked_config(self) -> None:
        self.run_agents("local", "init")

        state = self.state_path()
        self.assertTrue((state / "tasks").is_dir())
        self.assertTrue((state / "jobs").is_dir())
        self.assertTrue((state / "agents").is_dir())
        self.assertTrue((state / "runs").is_dir())
        self.assertTrue((state / "logs").is_dir())
        self.assertTrue((state / "config.json").is_file())
        self.assertTrue((state / "repo-root").is_file())
        self.assertFalse((state / "bin").exists())
        self.assertFalse((state / "tools").exists())
        self.assertFalse((state / "roles").exists())
        self.assertFalse((state / "AGENTS.md").exists())
        self.assertFalse((state / "protocol").exists())
        self.assertFalse((self.repo / ".multiagent" / "AGENTS.md").exists())
        self.assertFalse((self.repo / ".multiagent" / "bin").exists())
        self.assertFalse((self.repo / ".multiagent" / "tools").exists())
        self.assertFalse((self.repo / ".multiagent" / "roles").exists())
        self.assertTrue((self.repo / ".multiagent" / "team.toml").is_file())
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout
        self.assertNotIn(".multiagent/state", status)
        self.assertIn(".multiagent/", status)

    def test_status_reports_running_managed_processes_without_supervisor(self) -> None:
        self.run_agents("local", "init")
        agent_dir = self.state_path() / "agents" / "operator"
        agent_dir.mkdir(parents=True)
        (agent_dir / "role").write_text("planner\n", encoding="utf-8")
        (agent_dir / "runner.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

        status = self.run_agents("local", "status").stdout

        self.assertIn("managed_processes", status)
        self.assertIn("managed processes running: 1", status)


    def test_status_ignores_container_pids_from_host(self) -> None:
        self.run_agents("local", "init")
        runs = self.state_path() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "supervisor.pid").write_text("999999999\n", encoding="utf-8")
        (runs / "supervisor.json").write_text(
            json.dumps({"container_runtime": "docker", "container_name": "demo"}) + "\n",
            encoding="utf-8",
        )
        agent_dir = self.state_path() / "agents" / "operator"
        agent_dir.mkdir(parents=True)
        (agent_dir / "role").write_text("planner\n", encoding="utf-8")
        (agent_dir / "runner.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

        status = self.run_agents("local", "status").stdout

        self.assertIn("supervisor", status)
        self.assertIn("stopped", status)
        self.assertIn("managed_processes  0", status)

    def test_init_does_not_snapshot_protocol_or_overwrite_repo_agents_file(self) -> None:
        self.run_agents("local", "init")
        protocol = self.repo / ".multiagent" / "AGENTS.md"
        self.assertFalse(protocol.exists())

        protocol.parent.mkdir(parents=True, exist_ok=True)
        protocol.write_text("# Local Rules\n\nRepo-specific protocol.\n", encoding="utf-8")
        self.run_agents("local", "init")

        self.assertEqual("# Local Rules\n\nRepo-specific protocol.\n", protocol.read_text(encoding="utf-8"))
        self.assertFalse((self.state_path() / "AGENTS.md").exists())
        self.assertFalse((self.state_path() / "protocol").exists())

    def test_update_does_not_snapshot_protocol_or_install_runtime_commands(self) -> None:
        self.run_agents("local", "init")
        protocol = self.repo / ".multiagent" / "AGENTS.md"
        protocol.parent.mkdir(parents=True, exist_ok=True)
        protocol.write_text("# stale\n", encoding="utf-8")

        planner_role = self.repo / ".multiagent" / "roles" / "planner.md"
        planner_role.parent.mkdir(parents=True, exist_ok=True)
        planner_role.write_text("# Custom Planner\n", encoding="utf-8")
        self.run_agents("local", "update")

        self.assertEqual("# stale\n", protocol.read_text(encoding="utf-8"))
        self.assertEqual(planner_role.read_text(encoding="utf-8"), "# Custom Planner\n")
        self.assertFalse((self.repo / ".multiagent" / "bin").exists())
        self.assertFalse((self.repo / ".multiagent" / "tools").exists())
        self.assertFalse((self.state_path() / "AGENTS.md").exists())
        self.assertFalse((self.state_path() / "protocol").exists())
        self.assertFalse((self.state_path() / "bin").exists())
        self.assertFalse((self.state_path() / "tools").exists())
        self.assertFalse((self.state_path() / "roles").exists())

    def test_update_roles_explicitly_refreshes_role_templates(self) -> None:
        self.run_agents("local", "init")
        planner_role = self.repo / ".multiagent" / "roles" / "planner.md"
        planner_role.parent.mkdir(parents=True, exist_ok=True)
        planner_role.write_text("# stale planner\n", encoding="utf-8")
        self.run_agents("local", "update", "--roles")

        self.assertIn("Planner Role", planner_role.read_text(encoding="utf-8"))

    def test_tracked_config_materializes_team_without_protocol_snapshot(self) -> None:
        self.run_agents("local", "init", "--tracked-config")

        self.assertFalse((self.repo / ".multiagent" / "AGENTS.md").exists())
        self.assertFalse((self.repo / ".multiagent" / "roles").exists())
        self.assertFalse((self.state_path() / "protocol").exists())
        self.assertIn("[[agent]]", (self.repo / ".multiagent" / "team.toml").read_text())
        self.assertTrue((self.repo / ".multiagent" / "specs").is_dir())

        rules = self.run_agents("local", "rules", "show").stdout
        role = self.run_agents("local", "role", "show", "implementer").stdout
        self.assertIn("Generic Agent Protocol", rules)
        self.assertIn("Absolutely no deferring", role)

    def test_team_add_materializes_local_team(self) -> None:
        self.run_agents("local", "team", "add", "tester-1", "--role", "reviewer", "--model", "test-model")

        team_file = self.repo / ".multiagent" / "team.toml"
        self.assertTrue(team_file.is_file())
        self.assertIn('name = "tester-1"', team_file.read_text())

        listing = self.run_agents("local", "team", "list").stdout
        self.assertIn("tester-1", listing)
        self.assertIn("test-model", listing)

    def test_team_add_accepts_interactive_mode_with_heartbeat(self) -> None:
        self.run_agents("local", "team", "add", "operator", "--role", "reviewer", "--mode", "interactive", "--heartbeat", "15")

        listing = self.run_agents("local", "team", "list").stdout
        self.assertIn("operator", listing)
        self.assertIn("interactive", listing)
        self.assertIn("15", listing)

    def test_team_add_rejects_worker_heartbeat(self) -> None:
        proc = self.run_agents("local", "team", "add", "planner-2", "--role", "planner", "--heartbeat", "15", check=False)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--heartbeat requires --mode interactive", proc.stderr)

    def test_team_list_reads_team_run_pid_state(self) -> None:
        self.run_agents("local", "init")
        state = self.state_path()
        run_dir = state / "agents" / ".team-runs"
        run_dir.mkdir(parents=True)
        (run_dir / "planner-1.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

        listing = self.run_agents("local", "team", "list").stdout
        self.assertIn("planner-1", listing)
        self.assertIn("running", listing)

    def test_team_agent_command_launches_direct_agent_runner(self) -> None:
        from git_multiagent import cli

        git_multiagent_dir = self.repo / ".multiagent"
        command = cli.team_agent_command(
            git_multiagent_dir,
            {
                "name": "reviewer-1",
                "role": "reviewer",
                "mode": "worker",
                "model": "test-model",
            },
        )
        self.assertEqual(command[:4], [sys.executable, "-m", "git_multiagent", "agent"])
        self.assertEqual(command[4:], ["worker", "--headless", "--model", "test-model", "reviewer", "reviewer-1"])

        interactive = cli.team_agent_command(
            git_multiagent_dir,
            {
                "name": "planner-1",
                "role": "planner",
                "mode": "interactive",
            },
        )
        self.assertEqual(interactive[:4], [sys.executable, "-m", "git_multiagent", "agent"])
        self.assertEqual(interactive[4:], ["interactive", "--headless", "planner", "planner-1"])

    def test_agent_runtime_main_forwards_options(self) -> None:
        from git_multiagent import cli

        calls: list[list[str]] = []

        def fake_call(command: list[str], **_kwargs: object) -> int:
            calls.append(command)
            return 0

        with mock.patch.object(cli, "runtime_env_from_context", return_value=({}, self.repo)), \
            mock.patch.object(cli, "package_runtime_tool", return_value=Path("/runtime/agent-pi-interactive")), \
            mock.patch.object(cli.subprocess, "call", side_effect=fake_call):
            rc = cli.agent_runtime_main(["interactive", "--headless", "role", "agent-1"])

        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["/runtime/agent-pi-interactive", "--headless", "role", "agent-1"])

    def test_agent_runtime_strips_argument_delimiter(self) -> None:
        from git_multiagent import cli

        calls: list[list[str]] = []

        def fake_call(command: list[str], **_kwargs: object) -> int:
            calls.append(command)
            return 0

        args = argparse.Namespace(
            runtime_kind="tool",
            runtime_tool="agent",
            runtime_args=["--", "--headless", "role", "agent-1"],
            runtime_help=False,
        )
        with mock.patch.object(cli, "runtime_env_from_context", return_value=({}, self.repo)), \
            mock.patch.object(cli, "package_runtime_tool", return_value=Path("/runtime/agent")), \
            mock.patch.object(cli.subprocess, "call", side_effect=fake_call):
            rc = cli.cmd_agent_runtime(args)

        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["/runtime/agent", "--headless", "role", "agent-1"])

    def test_tasks_and_jobs_list_filesystem_state(self) -> None:
        self.run_agents("local", "init")
        spec = self.repo / "demo-spec.md"
        spec.write_text("# Demo Task\n\nDo the thing.\n", encoding="utf-8")
        self.run_agents("agent", "task", "create", "demo-task", "demo-spec.md")

        tasks = self.run_agents("agent", "task", "list").stdout
        jobs = self.run_agents("agent", "job", "list").stdout
        self.assertIn("demo-task", tasks)
        self.assertIn("Demo Task", tasks)
        self.assertIn("demo-task-plan", jobs)
        self.assertIn("planner", jobs)

        job_spec = self.repo / "job-spec.md"
        job_spec.write_text("# Follow-up\n\nDo more.\n", encoding="utf-8")
        self.run_agents(
            "agent",
            "job",
            "create",
            "demo-review",
            "--role",
            "reviewer",
            "--task-id",
            "demo-task",
            "job-spec.md",
        )
        jobs = self.run_agents("agent", "job", "list").stdout
        self.assertIn("demo-review", jobs)
        self.assertIn("reviewer", jobs)

    def test_jobs_reset_requeues_owned_job(self) -> None:
        state, job = self.create_demo_task()
        self.claim_and_start_job(state, job)

        reset = self.run_agents("agent", "job", "reset", job, "-m", "retry this job")
        self.assertIn(f"{job}: running -> pending", reset.stdout)
        self.assertEqual((state / "jobs" / job / "status").read_text(encoding="utf-8").strip(), "pending")
        self.assertFalse((state / "jobs" / job / "agent-id").exists())
        self.assertFalse((state / "jobs" / job / "lock").exists())
        self.assertEqual((state / "agents" / "planner-1" / "current-job").read_text(encoding="utf-8").strip(), "")

    def test_jobs_kill_marks_owned_job_failed(self) -> None:
        state, job = self.create_demo_task()
        self.claim_and_start_job(state, job)

        killed = self.run_agents("agent", "job", "kill", job, "-m", "stop now")
        self.assertIn(f"{job}: running -> failed", killed.stdout)
        self.assertEqual((state / "jobs" / job / "status").read_text(encoding="utf-8").strip(), "failed")
        self.assertEqual((state / "jobs" / job / "agent-id").read_text(encoding="utf-8").strip(), "planner-1")
        self.assertFalse((state / "jobs" / job / "lock").exists())
        self.assertEqual((state / "agents" / "planner-1" / "current-job").read_text(encoding="utf-8").strip(), "")

    def test_agents_reset_requeues_active_jobs(self) -> None:
        state, job = self.create_demo_task()
        self.claim_and_start_job(state, job)

        reset = self.run_agents("local", "agents", "reset", "planner-1", "--no-kill", "-m", "reset agent")
        self.assertIn("reset agent planner-1: jobs reset=1", reset.stdout)
        self.assertEqual((state / "jobs" / job / "status").read_text(encoding="utf-8").strip(), "pending")
        self.assertFalse((state / "jobs" / job / "agent-id").exists())
        self.assertEqual((state / "agents" / "planner-1" / "current-job").read_text(encoding="utf-8").strip(), "")

    def test_start_and_stop_supervisor(self) -> None:
        self.run_agents("local", "start")
        time.sleep(0.2)

        status = self.run_agents("local", "status").stdout
        self.assertIn("supervisor", status)
        self.assertIn("running", status)
        planner_pid = self.state_path() / "agents" / ".team-runs" / "planner-1.pid"
        deadline = time.time() + 3
        while not planner_pid.exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(planner_pid.is_file())
        registry_entries = list((self.repo / ".registry" / "instances").iterdir())
        self.assertEqual(len(registry_entries), 1)
        self.assertTrue(registry_entries[0].is_symlink())
        self.assertEqual(registry_entries[0].resolve(), self.state_path())

        stopped = self.run_agents("local", "stop").stdout
        self.assertIn("stopped multiagent supervisor", stopped)
        self.assertFalse(registry_entries[0].exists())


    def test_foreground_restart_ignores_stale_current_pid(self) -> None:
        from git_multiagent import cli

        self.run_agents("local", "init")
        with mock.patch.dict(os.environ, {"GIT_MULTIAGENT_REGISTRY_DIR": str(self.repo / ".registry")}):
            repo = cli.discover_repo(self.repo)
        (repo.state_dir / "runs" / "supervisor.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

        with mock.patch.object(cli, "validate_required_commands"), \
            mock.patch.object(cli, "sync_runtime"), \
            mock.patch.object(cli, "cmd_supervisor", return_value=0), \
            mock.patch.object(cli, "stop_supervisor") as stop_supervisor:
            rc = cli.run_supervisor_foreground(repo, restart=True)

        self.assertEqual(rc, 0)
        stop_supervisor.assert_not_called()

    def test_discover_repo_honors_explicit_state_dir(self) -> None:
        from git_multiagent import cli

        self.run_agents("local", "init")
        state_dir = self.repo / ".custom-state"
        with mock.patch.dict(os.environ, {"GIT_MULTIAGENT_STATE_DIR": str(state_dir)}):
            repo = cli.discover_repo(self.repo)

        self.assertEqual(repo.state_dir, state_dir.resolve())

    def test_container_local_runtime_does_not_write_host_registry(self) -> None:
        from git_multiagent import cli

        self.run_agents("local", "init")
        state_dir = self.repo / ".custom-state"
        registry_dir = self.repo / ".registry"
        with mock.patch.dict(
            os.environ,
            {
                "GIT_MULTIAGENT_CONTAINER": "docker",
                "GIT_MULTIAGENT_STATE_DIR": str(state_dir),
                "GIT_MULTIAGENT_REGISTRY_DIR": str(registry_dir),
            },
        ):
            repo = cli.discover_repo(self.repo)
            cli.write_registry_instance(repo)
            cli.remove_registry_instance(repo)

        self.assertFalse((registry_dir / "instances").exists())

    def test_start_validates_pi_before_daemonizing(self) -> None:
        env = self.env.copy()
        env["PATH"] = "/usr/bin:/bin"
        proc = self.run_agents_with_env(env, "local", "start", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("required command not found: pi", proc.stderr)


if __name__ == "__main__":
    unittest.main()
