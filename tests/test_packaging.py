from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def build_wheel(self, dist: Path) -> Path:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                str(REPO_ROOT),
                "--no-build-isolation",
                "-w",
                str(dist),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        wheels = sorted(dist.glob("multiagent-*.whl"))
        self.assertEqual(len(wheels), 1)
        return wheels[0]

    def test_wheel_contains_manpage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            wheel_path = self.build_wheel(dist)
            with zipfile.ZipFile(wheel_path) as wheel:
                names = set(wheel.namelist())
                entry_points = wheel.read("multiagent-0.2.0.dist-info/entry_points.txt").decode()
            self.assertIn(
                "multiagent-0.2.0.data/data/share/man/man1/multiagent.1",
                names,
            )
            self.assertIn("multiagent = multiagent.cli:main", entry_points)
            old_distribution_name = "git" "-multiagent"
            old_import_name = "git" "_multiagent"
            self.assertNotIn(old_distribution_name, entry_points)
            self.assertNotIn(old_import_name, entry_points)
            self.assertNotIn("multiagent-dashboard = multiagent.dashboard:main", entry_points)
            self.assertNotIn("multiagent-run = multiagent.runner:main", entry_points)
            self.assertIn("multiagent/templates/AGENTS.md", names)
            self.assertIn("multiagent/templates/teams/isa-migration.toml", names)
            self.assertNotIn("multiagent/runtime/AGENTS.md", names)
            self.assertIn("multiagent/runtime/bin/job-kill", names)
            self.assertIn("multiagent/runtime/bin/job-reset", names)
            self.assertIn("multiagent/runtime/tools/agent-input", names)
            self.assertIn("multiagent/runtime/tools/agent_input.py", names)
            self.assertIn("multiagent/runtime/tools/heartbeat", names)
            self.assertIn("multiagent/docker_context/Dockerfile", names)
            self.assertIn("multiagent/docker_context/entrypoint.sh", names)
            self.assertIn("multiagent/docker_context/pyproject.toml", names)
            self.assertIn("multiagent/.dockerignore", names)

    def test_installed_wheel_resolves_docker_build_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            dist = base / "dist"
            site = base / "site"
            dist.mkdir()
            wheel_path = self.build_wheel(dist)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(site),
                    str(wheel_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(site)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "\n".join(
                        [
                            "import json",
                            "from multiagent import runner",
                            "dockerfile = runner.dockerfile_path()",
                            "source_root = runner.source_root()",
                            "command = runner.docker_build_command('multiagent:test', 'fingerprint')",
                            "print(json.dumps({",
                            "    'dockerfile': str(dockerfile),",
                            "    'dockerfile_exists': dockerfile.is_file(),",
                            "    'source_root': str(source_root),",
                            "    'source_root_exists': source_root.is_dir(),",
                            "    'command': command,",
                            "}))",
                        ]
                    ),
                ],
                cwd=base,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            data = json.loads(proc.stdout)
            cli_proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "multiagent",
                    "docker",
                    "build-image",
                    "--dry-run",
                ],
                cwd=base,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

        self.assertTrue(data["dockerfile_exists"], data)
        self.assertTrue(data["source_root_exists"], data)
        self.assertTrue(data["dockerfile"].startswith(str(site)), data)
        self.assertTrue(data["source_root"].startswith(str(site)), data)
        self.assertIn(data["dockerfile"], data["command"])
        self.assertIn(data["source_root"], data["command"])
        self.assertIn(data["dockerfile"], cli_proc.stdout)
        self.assertIn(data["source_root"], cli_proc.stdout)


if __name__ == "__main__":
    unittest.main()
