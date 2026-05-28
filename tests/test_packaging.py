from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_wheel_contains_manpage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
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
            wheels = sorted(dist.glob("git_multiagent-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())
                entry_points = wheel.read("git_multiagent-0.2.0.dist-info/entry_points.txt").decode()
            self.assertIn(
                "git_multiagent-0.2.0.data/data/share/man/man1/multiagent.1",
                names,
            )
            self.assertIn("multiagent = git_multiagent.cli:main", entry_points)
            self.assertNotIn("git-multiagent = git_multiagent.cli:main", entry_points)
            self.assertNotIn("multiagent-dashboard = git_multiagent.dashboard:main", entry_points)
            self.assertNotIn("multiagent-run = git_multiagent.runner:main", entry_points)
            self.assertIn("git_multiagent/templates/AGENTS.md", names)
            self.assertNotIn("git_multiagent/runtime/AGENTS.md", names)
            self.assertIn("git_multiagent/runtime/bin/job-kill", names)
            self.assertIn("git_multiagent/runtime/bin/job-reset", names)
            self.assertIn("git_multiagent/runtime/tools/agent-input", names)
            self.assertIn("git_multiagent/runtime/tools/agent_input.py", names)
            self.assertIn("git_multiagent/runtime/tools/heartbeat", names)


if __name__ == "__main__":
    unittest.main()
