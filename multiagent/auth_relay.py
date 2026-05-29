from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path


class RelayHandler(socketserver.BaseRequestHandler):
    server: "ThreadingTCPRelayServer"

    def handle(self) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(str(self.server.socket_path))
            relay_bidirectional(self.request, upstream)
        finally:
            upstream.close()


class ThreadingTCPRelayServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[RelayHandler], socket_path: Path):
        self.socket_path = socket_path
        super().__init__(server_address, handler_cls)


def relay_bidirectional(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    peers = {left: right, right: left}
    while sockets:
        readable, _, _ = select.select(sockets, [], [], 60)
        if not readable:
            continue
        for src in readable:
            try:
                data = src.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                peers[src].sendall(data)
            except OSError:
                return


def parse_listen(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("listen address must be HOST:PORT")
    host, port_text = value.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("listen port must be an integer") from exc
    return host, port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="multiagent-auth-relay")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--listen", required=True, type=parse_listen)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("command is required after --")
    return args


def terminate_child(proc: subprocess.Popen[bytes], signum: int = signal.SIGTERM) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signum)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def run_child(command: list[str]) -> int:
    proc = subprocess.Popen(command, start_new_session=True)

    def forward_signal(signum: int, _frame: object) -> None:
        terminate_child(proc, signum)

    previous_term = signal.signal(signal.SIGTERM, forward_signal)
    previous_int = signal.signal(signal.SIGINT, forward_signal)
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                return rc
            time.sleep(0.1)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        terminate_child(proc)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = ThreadingTCPRelayServer(args.listen, RelayHandler, Path(args.socket))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return run_child(args.command)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
