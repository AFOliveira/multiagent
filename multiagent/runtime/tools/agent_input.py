# SPDX-License-Identifier: MIT
"""Shared interactive agent input helpers for MULTIAGENT runtime tools."""

from __future__ import annotations

import json
import socket
from pathlib import Path


def send_agent_input(
    state_dir: Path,
    agent: str,
    message: str,
    mode: str = "prompt",
    quiet: bool = False,
) -> bool:
    rpc_sock = state_dir / "agents" / agent / "rpc.sock"
    payload = json.dumps({"message": message, "mode": mode}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(rpc_sock))
            client.sendall(payload.encode("utf-8"))
    except OSError as exc:
        if not quiet:
            print(f"agent input unavailable for {agent}: {exc}", flush=True)
        return False
    return True
