from __future__ import annotations

import subprocess
import sys
from importlib import resources


PACKAGE = "multiagent"


def dashboard_tool_path():
    return resources.files(PACKAGE).joinpath("runtime", "tools", "multiagent-ui")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = [sys.executable, str(dashboard_tool_path()), "--registry", *argv]
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
