from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from git_multiagent import runner


class RunnerTest(unittest.TestCase):
    def test_bare_repo_alias_is_not_supported(self) -> None:
        self.assertEqual(runner.normalize_argv(["/tmp/repo"]), ["/tmp/repo"])
        self.assertEqual(runner.normalize_argv(["status"]), ["status"])

    def test_mount_plan_dedupes_child_paths_covered_by_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = base / "repo"
            registry = base / ".multiagent"
            state_base = registry / "state"
            run_dir = registry / "runs" / "demo"
            repo.mkdir()
            state_base.mkdir(parents=True)
            run_dir.mkdir(parents=True)

            mounts = runner.build_mount_plan(
                repo,
                state_base,
                run_dir,
                [runner.Mount(base, base, "rw")],
            )

        self.assertEqual(mounts, (runner.Mount(base, base, "rw"),))

    def test_mount_plan_keeps_specific_rw_mount_under_ro_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = base / "repo"
            registry = base / ".multiagent"
            state_base = registry / "state"
            run_dir = registry / "runs" / "demo"
            repo.mkdir()
            state_base.mkdir(parents=True)
            run_dir.mkdir(parents=True)

            mounts = runner.build_mount_plan(
                repo,
                state_base,
                run_dir,
                [runner.Mount(base, base, "ro")],
            )

        self.assertIn(runner.Mount(base, base, "ro"), mounts)
        self.assertIn(runner.Mount(repo, repo, "rw"), mounts)
        self.assertIn(runner.Mount(state_base, state_base, "rw"), mounts)
        self.assertIn(runner.Mount(run_dir, run_dir, "rw"), mounts)

    def test_mount_plan_does_not_mount_host_backed_container_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = base / "repo"
            state_base = base / ".multiagent" / "state"
            run_dir = base / ".multiagent" / "runs" / "demo"
            repo.mkdir()
            state_base.mkdir(parents=True)
            run_dir.mkdir(parents=True)

            mounts = runner.build_mount_plan(repo, state_base, run_dir)

        self.assertIn(runner.Mount(state_base, state_base, "rw"), mounts)
        self.assertIn(runner.Mount(run_dir, run_dir, "rw"), mounts)
        self.assertNotIn(runner.CONTAINER_HOME, {mount.target for mount in mounts})

    def make_config(self, base: Path) -> runner.StartConfig:
        repo = base / "repo"
        registry = base / ".multiagent"
        state_base = registry / "state"
        state_dir = state_base / runner.registry_instance_name(repo)
        run_dir = registry / "runs" / "demo"
        pi_config_dir = run_dir / "pi-config"
        repo.mkdir(exist_ok=True)
        state_base.mkdir(parents=True, exist_ok=True)
        pi_config_dir.mkdir(parents=True, exist_ok=True)
        return runner.StartConfig(
            repo=repo,
            registry_dir=registry,
            state_base_dir=state_base,
            state_dir=state_dir,
            image="git-multiagent:test",
            source_fingerprint="source-fingerprint",
            name="demo",
            run_dir=run_dir,
            pi_config_dir=pi_config_dir,
            mounts=(
                runner.Mount(repo, repo, "rw"),
                runner.Mount(state_base, state_base, "rw"),
                runner.Mount(run_dir, run_dir, "rw"),
            ),
            devices=(),
            projected_pi_home=pi_config_dir,
            host_pi_agent_dir=base / ".pi" / "agent",
            auth_socket=run_dir / "host-auth.sock",
            auth_proxy_pid=run_dir / "host-auth.pid",
            auth_proxy_ready=run_dir / "host-auth.ready",
            auth_relay_port=runner.HOST_AUTH_RELAY_PORT,
            env=("EXTRA=value",),
            forward_env=(),
            foreground=False,
            restart=True,
            network="bridge",
            home=runner.CONTAINER_HOME,
        )

    def container_label_side_effect(
        self,
        config: runner.StartConfig,
        *,
        source_fingerprint: str | None = None,
        run_config_fingerprint: str | None = None,
    ):
        source_fingerprint = config.source_fingerprint if source_fingerprint is None else source_fingerprint
        run_config_fingerprint = (
            runner.run_config_fingerprint(config) if run_config_fingerprint is None else run_config_fingerprint
        )

        def side_effect(_name: str, label: str) -> str:
            if label == runner.SOURCE_FINGERPRINT_LABEL:
                return source_fingerprint
            if label == runner.RUN_CONFIG_FINGERPRINT_LABEL:
                return run_config_fingerprint
            return ""

        return side_effect

    def test_docker_command_runs_git_multiagent_through_container_auth_relay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)

            with mock.patch.object(os, "getuid", return_value=1000), mock.patch.object(os, "getgid", return_value=1001):
                command = runner.docker_run_command(config)

        self.assertIn("--stop-timeout", command)
        self.assertIn("--detach", command)
        self.assertIn("--mount", command)
        self.assertNotIn("--user", command)
        self.assertIn("GIT_MULTIAGENT_HOST_UID=1000", command)
        self.assertIn("GIT_MULTIAGENT_HOST_GID=1001", command)
        self.assertIn("EXTRA=value", command)
        self.assertIn(f"HOME={runner.CONTAINER_HOME}", command)
        self.assertNotIn("PIP_USER=1", command)
        self.assertIn(f"GIT_MULTIAGENT_PI_CONFIG_DIR={config.pi_config_dir}", command)
        self.assertIn(f"GIT_MULTIAGENT_REGISTRY_DIR={config.registry_dir}", command)
        self.assertIn(f"GIT_MULTIAGENT_STATE_DIR={config.state_dir}", command)
        self.assertIn(f"{runner.SOURCE_FINGERPRINT_LABEL}={config.source_fingerprint}", command)
        self.assertIn(f"{runner.RUN_CONFIG_FINGERPRINT_LABEL}={runner.run_config_fingerprint(config)}", command)
        self.assertIn(runner.Mount(config.state_base_dir, config.state_base_dir, "rw").docker_value(), command)
        self.assertIn(runner.Mount(config.run_dir, config.run_dir, "rw").docker_value(), command)
        self.assertNotIn(runner.Mount(config.registry_dir, config.registry_dir, "rw").docker_value(), command)
        self.assertNotIn(f"dst={runner.CONTAINER_HOME}", " ".join(command))
        self.assertIn("git_multiagent.auth_relay", command)
        self.assertEqual(command[-5:], ["multiagent", "local", "start", "--foreground", "--restart"])
        self.assertNotIn("docker", command[1:])
        self.assertNotIn("cp", command)

    def test_resolve_host_pi_agent_dir_honors_pi_coding_agent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            resolved = runner.resolve_host_pi_agent_dir(
                base,
                {runner.PI_AGENT_DIR_ENV: "~/pi-alt/agent"},
            )

        self.assertEqual(resolved, (base / "pi-alt" / "agent").resolve())

    def test_projected_pi_home_rejects_non_codex_host_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            host_agent = base / ".pi" / "agent"
            host_agent.mkdir(parents=True)
            (host_agent / "settings.json").write_text(
                json.dumps({"defaultProvider": "anthropic", "defaultModel": "claude-opus"}),
                encoding="utf-8",
            )
            config = self.make_config(base)

            with self.assertRaisesRegex(runner.UserError, "openai-codex"):
                runner.prepare_projected_pi_home(config, "header.payload.signature")

    def test_projected_pi_home_contains_only_non_secret_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            host_agent = base / ".pi" / "agent"
            host_agent.mkdir(parents=True)
            (host_agent / "settings.json").write_text(
                json.dumps(
                    {
                        "defaultProvider": "openai-codex",
                        "defaultModel": "gpt-5.5",
                        "defaultThinkingLevel": "xhigh",
                        "packages": ["npm:some-other-package"],
                    }
                ),
                encoding="utf-8",
            )
            config = self.make_config(base)
            repo_settings = config.repo / ".pi"
            repo_settings.mkdir()
            (repo_settings / "settings.json").write_text(
                json.dumps({"defaultModel": "gpt-5.4-mini", "defaultThinkingLevel": "high"}),
                encoding="utf-8",
            )

            runner.prepare_projected_pi_home(config, "header.payload.signature")

            agent = config.projected_pi_home / "agent"
            settings = json.loads((agent / "settings.json").read_text(encoding="utf-8"))
            models = json.loads((agent / "models.json").read_text(encoding="utf-8"))

        self.assertFalse((agent / "auth.json").exists())
        self.assertEqual(settings["defaultProvider"], runner.HOST_CODEX_PROVIDER)
        self.assertEqual(settings["defaultModel"], "gpt-5.4-mini")
        self.assertEqual(settings["defaultThinkingLevel"], "high")
        self.assertEqual(settings["packages"], [runner.PI_WEB_ACCESS_PACKAGE])
        self.assertIn(runner.HOST_CODEX_PROVIDER, models["providers"])
        rendered = json.dumps({"settings": settings, "models": models})
        self.assertNotIn('"refresh"', rendered)
        self.assertNotIn('"access"', rendered)




    def test_auth_relay_forwards_sigterm_to_child(self) -> None:
        from git_multiagent import auth_relay  # noqa: F401 - ensure module import is covered

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            ready = base / "ready"
            stopped = base / "stopped"
            child = (
                "import pathlib, signal, sys, time\n"
                "ready = pathlib.Path(sys.argv[1])\n"
                "stopped = pathlib.Path(sys.argv[2])\n"
                "def stop(signum, frame):\n"
                "    stopped.write_text('stopped', encoding='utf-8')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "ready.write_text('ready', encoding='utf-8')\n"
                "while True:\n"
                "    time.sleep(0.1)\n"
            )
            env = os.environ.copy()
            pythonpath = env.get("PYTHONPATH")
            repo_root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = repo_root if not pythonpath else f"{repo_root}{os.pathsep}{pythonpath}"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "git_multiagent.auth_relay",
                    "--socket",
                    str(base / "host-auth.sock"),
                    "--listen",
                    "127.0.0.1:0",
                    "--",
                    sys.executable,
                    "-c",
                    child,
                    str(ready),
                    str(stopped),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.time() + 5
                while not ready.exists() and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists())
                proc.terminate()
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertTrue(stopped.exists(), stdout)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)


    def test_write_host_registry_instance_records_host_pid_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)

            with mock.patch.object(runner, "wait_for_container_host_pid", return_value=12345):
                runner.write_host_registry_instance(config, "container-id")

            link = runner.registry_instance_path(config.repo, config.registry_dir)
            metadata_path = runner.registry_metadata_path(config.repo, config.registry_dir)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), config.state_dir)
            self.assertEqual(metadata["runtime"], "docker")
            self.assertEqual(metadata["containerName"], config.name)
            self.assertEqual(metadata["containerId"], "container-id")
            self.assertEqual(metadata["hostPid"], 12345)
            self.assertEqual(metadata["stateRoot"], str(config.state_dir))
            self.assertEqual((config.state_dir / "repo-root").read_text(encoding="utf-8").strip(), str(config.repo))

    def test_stop_removes_registry_instance_and_keeps_container_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = base / "repo"
            registry = base / ".multiagent"
            config_dir = repo / ".multiagent"
            state_dir = registry / "state" / runner.registry_instance_name(repo)
            run_dir = registry / "runs" / "demo"
            model_file = run_dir / "pi-config" / "agent" / "models.json"
            config_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            model_file.parent.mkdir(parents=True)
            model_file.write_text("secret proxy token", encoding="utf-8")
            instances = registry / "instances"
            instances.mkdir(parents=True)
            link = instances / runner.registry_instance_name(repo)
            link.symlink_to(state_dir)
            args = argparse.Namespace(repo=str(repo), name="demo", registry_dir=str(registry))

            with mock.patch.object(runner, "git_repo_root", return_value=repo), \
                mock.patch.object(runner, "docker_stop", return_value=0):
                rc = runner.cmd_stop(args)

            self.assertEqual(rc, 0)
            self.assertFalse(link.exists())
            self.assertTrue(run_dir.exists())
            self.assertTrue(model_file.exists())

    def test_destroy_removes_projected_pi_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = base / "repo"
            registry = base / ".multiagent"
            config_dir = repo / ".multiagent"
            state_dir = registry / "state" / runner.registry_instance_name(repo)
            run_dir = registry / "runs" / "demo"
            model_file = run_dir / "pi-config" / "agent" / "models.json"
            config_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            model_file.parent.mkdir(parents=True)
            model_file.write_text("secret proxy token", encoding="utf-8")
            instances = registry / "instances"
            instances.mkdir(parents=True)
            link = instances / runner.registry_instance_name(repo)
            link.symlink_to(state_dir)
            args = argparse.Namespace(repo=str(repo), name="demo", registry_dir=str(registry))

            with mock.patch.object(runner, "git_repo_root", return_value=repo), \
                mock.patch.object(runner, "docker_stop_and_remove", return_value=0):
                rc = runner.cmd_destroy(args)

            self.assertEqual(rc, 0)
            self.assertFalse(link.exists())
            self.assertFalse(model_file.exists())

    def test_start_builds_image_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            calls: list[list[str]] = []
            capture_calls: list[list[str]] = []

            def fake_run(command: list[str]) -> int:
                calls.append(command)
                return 0

            def fake_capture(command: list[str]) -> tuple[int, str]:
                capture_calls.append(command)
                return 0, "container-id"

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=False), \
                mock.patch.object(runner, "docker_image_exists", return_value=False), \
                mock.patch.object(runner, "run_command", side_effect=fake_run), \
                mock.patch.object(runner, "run_command_capture", side_effect=fake_capture), \
                mock.patch.object(runner, "write_host_registry_instance"), \
                mock.patch.object(runner, "make_proxy_token", return_value="header.payload.signature"), \
                mock.patch.object(runner, "prepare_projected_pi_home"), \
                mock.patch.object(runner, "start_host_auth_proxy"):
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], runner.docker_build_command(config.image, config.source_fingerprint))
        self.assertEqual(capture_calls[0][0:2], ["docker", "run"])

    def test_start_skips_build_when_image_matches_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            calls: list[list[str]] = []
            capture_calls: list[list[str]] = []

            def fake_run(command: list[str]) -> int:
                calls.append(command)
                return 0

            def fake_capture(command: list[str]) -> tuple[int, str]:
                capture_calls.append(command)
                return 0, "container-id"

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=False), \
                mock.patch.object(runner, "docker_image_exists", return_value=True), \
                mock.patch.object(runner, "docker_image_label", return_value=config.source_fingerprint), \
                mock.patch.object(runner, "run_command", side_effect=fake_run), \
                mock.patch.object(runner, "run_command_capture", side_effect=fake_capture), \
                mock.patch.object(runner, "write_host_registry_instance"), \
                mock.patch.object(runner, "make_proxy_token", return_value="header.payload.signature"), \
                mock.patch.object(runner, "prepare_projected_pi_home"), \
                mock.patch.object(runner, "start_host_auth_proxy"):
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 0)
        self.assertEqual(capture_calls[0][0:2], ["docker", "run"])

    def test_start_rebuilds_when_image_fingerprint_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            calls: list[list[str]] = []
            capture_calls: list[list[str]] = []

            def fake_run(command: list[str]) -> int:
                calls.append(command)
                return 0

            def fake_capture(command: list[str]) -> tuple[int, str]:
                capture_calls.append(command)
                return 0, "container-id"

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=False), \
                mock.patch.object(runner, "docker_image_exists", return_value=True), \
                mock.patch.object(runner, "docker_image_label", return_value="old-source"), \
                mock.patch.object(runner, "run_command", side_effect=fake_run), \
                mock.patch.object(runner, "run_command_capture", side_effect=fake_capture), \
                mock.patch.object(runner, "write_host_registry_instance"), \
                mock.patch.object(runner, "make_proxy_token", return_value="header.payload.signature"), \
                mock.patch.object(runner, "prepare_projected_pi_home"), \
                mock.patch.object(runner, "start_host_auth_proxy"):
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], runner.docker_build_command(config.image, config.source_fingerprint))
        self.assertEqual(capture_calls[0][0:2], ["docker", "run"])



    def test_start_reuses_stopped_container_projected_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            agent = config.projected_pi_home / "agent"
            agent.mkdir(parents=True)
            (agent / "models.json").write_text(
                json.dumps({"providers": {runner.HOST_CODEX_PROVIDER: {"apiKey": "persisted.proxy.token"}}}),
                encoding="utf-8",
            )

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=True), \
                mock.patch.object(runner, "docker_container_running", return_value=False), \
                mock.patch.object(runner, "docker_container_label", side_effect=self.container_label_side_effect(config)), \
                mock.patch.object(runner, "run_command_capture", return_value=(0, config.name)), \
                mock.patch.object(runner, "docker_container_id", return_value="existing-id"), \
                mock.patch.object(runner, "write_host_registry_instance"), \
                mock.patch.object(runner, "make_proxy_token") as make_proxy_token, \
                mock.patch.object(runner, "prepare_projected_pi_home") as prepare_home, \
                mock.patch.object(runner, "start_host_auth_proxy") as start_proxy:
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        start_proxy.assert_called_once_with(config, "persisted.proxy.token")
        make_proxy_token.assert_not_called()
        prepare_home.assert_not_called()

    def test_start_reuses_existing_stopped_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            capture_calls: list[list[str]] = []

            def fake_capture(command: list[str]) -> tuple[int, str]:
                capture_calls.append(command)
                return 0, config.name

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=True), \
                mock.patch.object(runner, "docker_container_running", return_value=False), \
                mock.patch.object(runner, "docker_container_label", side_effect=self.container_label_side_effect(config)), \
                mock.patch.object(runner, "docker_image_exists") as image_exists, \
                mock.patch.object(runner, "run_command") as run_command, \
                mock.patch.object(runner, "run_command_capture", side_effect=fake_capture), \
                mock.patch.object(runner, "docker_container_id", return_value="existing-id"), \
                mock.patch.object(runner, "write_host_registry_instance") as write_registry, \
                mock.patch.object(runner, "make_proxy_token", return_value="header.payload.signature"), \
                mock.patch.object(runner, "prepare_projected_pi_home"), \
                mock.patch.object(runner, "start_host_auth_proxy"):
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(capture_calls[0], runner.docker_start_command(config.name, foreground=False))
        image_exists.assert_not_called()
        run_command.assert_not_called()
        write_registry.assert_called_once_with(config, "existing-id")

    def test_start_replaces_stopped_container_when_mount_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            extra = base / "shared"
            extra.mkdir()
            config = self.make_config(base)
            config = replace(
                config,
                mounts=(*config.mounts, runner.Mount(extra, extra, "rw")),
            )
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            capture_calls: list[list[str]] = []

            def fake_capture(command: list[str]) -> tuple[int, str]:
                capture_calls.append(command)
                return 0, "new-container-id"

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=True), \
                mock.patch.object(runner, "docker_container_running", return_value=False), \
                mock.patch.object(
                    runner,
                    "docker_container_label",
                    side_effect=self.container_label_side_effect(config, run_config_fingerprint="old-run-config"),
                ), \
                mock.patch.object(runner, "docker_image_exists", return_value=True), \
                mock.patch.object(runner, "docker_image_label", return_value=config.source_fingerprint), \
                mock.patch.object(runner, "run_command"), \
                mock.patch.object(runner, "run_command_capture", side_effect=fake_capture), \
                mock.patch.object(runner, "docker_stop_and_remove", return_value=0) as stop_remove, \
                mock.patch.object(runner, "stop_host_auth_proxy_run_dir"), \
                mock.patch.object(runner, "remove_registry_instance_for_repo"), \
                mock.patch.object(runner, "write_host_registry_instance"), \
                mock.patch.object(runner, "make_proxy_token", return_value="header.payload.signature"), \
                mock.patch.object(runner, "prepare_projected_pi_home"), \
                mock.patch.object(runner, "start_host_auth_proxy"):
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        stop_remove.assert_called_once_with(config.name)
        self.assertEqual(capture_calls[0][0:2], ["docker", "run"])
        self.assertIn(runner.Mount(extra, extra, "rw").docker_value(), capture_calls[0])

    def test_start_replaces_stale_stopped_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            calls: list[list[str]] = []
            capture_calls: list[list[str]] = []

            def fake_run(command: list[str]) -> int:
                calls.append(command)
                return 0

            def fake_capture(command: list[str]) -> tuple[int, str]:
                capture_calls.append(command)
                return 0, "new-container-id"

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=True), \
                mock.patch.object(runner, "docker_container_running", return_value=False), \
                mock.patch.object(
                    runner,
                    "docker_container_label",
                    side_effect=self.container_label_side_effect(config, source_fingerprint="old-source"),
                ), \
                mock.patch.object(runner, "docker_image_exists", return_value=True), \
                mock.patch.object(runner, "docker_image_label", return_value=config.source_fingerprint), \
                mock.patch.object(runner, "run_command", side_effect=fake_run), \
                mock.patch.object(runner, "run_command_capture", side_effect=fake_capture), \
                mock.patch.object(runner, "docker_stop_and_remove", return_value=0) as stop_remove, \
                mock.patch.object(runner, "stop_host_auth_proxy_run_dir") as stop_proxy, \
                mock.patch.object(runner, "remove_registry_instance_for_repo") as remove_registry, \
                mock.patch.object(runner, "write_host_registry_instance") as write_registry, \
                mock.patch.object(runner, "make_proxy_token", return_value="header.payload.signature"), \
                mock.patch.object(runner, "prepare_projected_pi_home"), \
                mock.patch.object(runner, "start_host_auth_proxy"):
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        stop_remove.assert_called_once_with(config.name)
        stop_proxy.assert_called_once_with(config.run_dir)
        remove_registry.assert_called_once_with(config.repo, config.registry_dir)
        self.assertEqual(capture_calls[0][0:2], ["docker", "run"])
        write_registry.assert_called_once_with(config, "new-container-id")


    def test_start_running_container_repairs_host_auth_proxy_from_projected_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            config = self.make_config(base)
            args = argparse.Namespace(image=config.image, build=False, dry_run=False)
            agent = config.projected_pi_home / "agent"
            agent.mkdir(parents=True)
            (agent / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            runner.HOST_CODEX_PROVIDER: {
                                "apiKey": "existing.proxy.token",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(runner, "start_config_from_args", return_value=config), \
                mock.patch.object(runner, "docker_container_exists", return_value=True), \
                mock.patch.object(runner, "docker_container_running", return_value=True), \
                mock.patch.object(runner, "docker_container_label", side_effect=self.container_label_side_effect(config)), \
                mock.patch.object(runner, "docker_image_exists") as image_exists, \
                mock.patch.object(runner, "run_command") as run_command, \
                mock.patch.object(runner, "run_command_capture") as run_command_capture, \
                mock.patch.object(runner, "docker_container_id", return_value="running-id"), \
                mock.patch.object(runner, "write_host_registry_instance") as write_registry, \
                mock.patch.object(runner, "make_proxy_token") as make_proxy_token, \
                mock.patch.object(runner, "prepare_projected_pi_home") as prepare_home, \
                mock.patch.object(runner, "start_host_auth_proxy") as start_proxy:
                rc = runner.cmd_start(args)

        self.assertEqual(rc, 0)
        start_proxy.assert_called_once_with(config, "existing.proxy.token")
        write_registry.assert_called_once_with(config, "running-id")
        image_exists.assert_not_called()
        run_command.assert_not_called()
        run_command_capture.assert_not_called()
        make_proxy_token.assert_not_called()
        prepare_home.assert_not_called()

    def test_old_pi_home_options_are_not_supported(self) -> None:
        parser = runner.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["start", "/tmp/repo", "--pi-home", "copy"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["start", "/tmp/repo", "--no-pi-home"])


if __name__ == "__main__":
    unittest.main()
