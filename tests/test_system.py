from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from multiagent import system


class FakeProcess:
    pid = 43210

    def poll(self) -> int | None:
        return None


class SystemTest(unittest.TestCase):
    def make_repo(self, base: Path, name: str = "repo") -> Path:
        repo = base / name
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return repo

    def test_init_and_add_commands_write_system_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "system.toml"
            root = base / "root"
            shared = base / "shared"
            device = base / "ttyUSB0"
            shared.mkdir()
            device.touch()
            repo = self.make_repo(base)

            self.assertEqual(
                system.main(["--file", str(config_path), "init", "--root", str(root), "--host", "0.0.0.0", "--port", "4200"]),
                0,
            )
            self.assertEqual(system.main(["--file", str(config_path), "add-mount", str(shared), "--mode", "ro"]), 0)
            self.assertEqual(system.main(["--file", str(config_path), "add-dev", str(device)]), 0)
            self.assertEqual(system.main(["--file", str(config_path), "add", str(repo), "--name", "main"]), 0)

            config = system.load_config(config_path)

        self.assertEqual(config.root, root.resolve())
        self.assertEqual(config.dashboard.host, "0.0.0.0")
        self.assertEqual(config.dashboard.port, 4200)
        self.assertEqual(config.mounts, [system.MountConfig(shared.resolve(), "ro")])
        self.assertEqual(config.devices, [system.DeviceConfig(device.resolve())])
        self.assertEqual(config.projects, [system.ProjectConfig("main", repo.resolve(), "docker")])

    def test_init_defaults_to_user_multiagent_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "system.toml"
            home = base / "home"
            home.mkdir()

            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(system.main(["--file", str(config_path), "init"]), 0)
            config = system.load_config(config_path)

        self.assertEqual(config.root, home / ".multiagent")

    def test_start_starts_projects_and_dashboard_under_system_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "system.toml"
            root = base / "root"
            shared = base / "shared"
            device = base / "ttyUSB0"
            repo = self.make_repo(base)
            shared.mkdir()
            device.touch()
            config = system.SystemConfig(
                root=root,
                dashboard=system.DashboardConfig(host="127.0.0.1", port=4999),
                mounts=[system.MountConfig(shared, "rw")],
                devices=[system.DeviceConfig(device)],
                projects=[system.ProjectConfig("main", repo, "docker")],
            )
            system.save_config(config_path, config)
            calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []

            def fake_call(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
                calls.append((command, cwd, env))
                return 0

            with mock.patch.object(system.subprocess, "call", side_effect=fake_call), \
                mock.patch.object(system.subprocess, "Popen", return_value=FakeProcess()), \
                mock.patch.object(system.time, "sleep"):
                self.assertEqual(system.main(["--file", str(config_path), "start"]), 0)

            metadata = json.loads((root / "runs" / "system" / "dashboard.json").read_text(encoding="utf-8"))

        self.assertEqual(calls[0][0], [sys.executable, "-m", "multiagent", "local", "init"])
        self.assertEqual(calls[0][1], repo)
        self.assertEqual(calls[0][2][system.REGISTRY_ENV], str(root))
        self.assertEqual(calls[1][0][:5], [sys.executable, "-m", "multiagent", "docker", "start"])
        self.assertEqual(calls[1][2][system.REGISTRY_ENV], str(root))
        self.assertIn("--mount", calls[1][0])
        self.assertIn(f"{shared}:rw", calls[1][0])
        self.assertIn("--device", calls[1][0])
        self.assertIn(str(device), calls[1][0])
        self.assertEqual(metadata["pid"], FakeProcess.pid)
        self.assertEqual(metadata["url"], "http://127.0.0.1:4999")

    def test_stop_stops_projects_and_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "system.toml"
            root = base / "root"
            repo = self.make_repo(base)
            config = system.SystemConfig(root=root, projects=[system.ProjectConfig("main", repo, "docker")])
            system.save_config(config_path, config)
            (root / "runs" / "system").mkdir(parents=True)
            (root / "runs" / "system" / "dashboard.pid").write_text("43210\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_call(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
                calls.append(command)
                return 0

            with mock.patch.object(system.subprocess, "call", side_effect=fake_call), \
                mock.patch.object(system, "pid_is_running", return_value=True), \
                mock.patch.object(system, "stop_pid_group") as stop_pid_group:
                self.assertEqual(system.main(["--file", str(config_path), "stop"]), 0)

        self.assertEqual(calls[0][:5], [sys.executable, "-m", "multiagent", "docker", "stop"])
        stop_pid_group.assert_called_once_with(43210)
        self.assertFalse((root / "runs" / "system" / "dashboard.pid").exists())

    def test_restart_uses_current_mounts_and_devices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "system.toml"
            root = base / "root"
            shared = base / "shared"
            device = base / "ttyUSB0"
            repo = self.make_repo(base)
            shared.mkdir()
            device.touch()
            config = system.SystemConfig(
                root=root,
                mounts=[system.MountConfig(shared, "rw")],
                devices=[system.DeviceConfig(device)],
                projects=[system.ProjectConfig("main", repo, "docker")],
            )
            system.save_config(config_path, config)
            calls: list[list[str]] = []

            def fake_call(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> int:
                calls.append(command)
                return 0

            with mock.patch.object(system.subprocess, "call", side_effect=fake_call), \
                mock.patch.object(system.subprocess, "Popen", return_value=FakeProcess()), \
                mock.patch.object(system.time, "sleep"):
                self.assertEqual(system.main(["--file", str(config_path), "restart"]), 0)

        start_command = calls[-1]
        self.assertEqual(start_command[:5], [sys.executable, "-m", "multiagent", "docker", "start"])
        self.assertIn("--mount", start_command)
        self.assertIn(f"{shared}:rw", start_command)
        self.assertIn("--device", start_command)
        self.assertIn(str(device), start_command)
        self.assertIn("--restart", start_command)


if __name__ == "__main__":
    unittest.main()
