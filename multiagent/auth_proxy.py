from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any


CODEX_AUTH_PROVIDER = "openai-codex"
CODEX_UPSTREAM_URL = "https://chatgpt.com/backend-api/codex/responses"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
REFRESH_SKEW_MS = 60_000
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
FORWARDED_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "openai-beta",
    "session_id",
    "x-client-request-id",
    "user-agent",
}


class AuthProxyError(Exception):
    pass


class AuthCredential(dict[str, Any]):
    pass


def base64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def account_id_from_access_token(access_token: str) -> str | None:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(base64url_decode(parts[1]))
        account_id = payload.get(JWT_CLAIM_PATH, {}).get("chatgpt_account_id")
        return account_id if isinstance(account_id, str) and account_id else None
    except Exception:
        return None


def refresh_openai_codex(refresh_token: str) -> AuthCredential:
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OPENAI_CODEX_CLIENT_ID,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AuthProxyError(f"OpenAI Codex token refresh failed with HTTP {exc.code}") from exc
    except Exception as exc:
        raise AuthProxyError(f"OpenAI Codex token refresh failed: {exc}") from exc
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access, str) or not isinstance(refresh, str) or not isinstance(expires_in, (int, float)):
        raise AuthProxyError("OpenAI Codex token refresh response was missing required fields")
    account_id = account_id_from_access_token(access)
    if not account_id:
        raise AuthProxyError("OpenAI Codex token refresh response did not include an account id")
    return AuthCredential(
        type="oauth",
        access=access,
        refresh=refresh,
        expires=int(__import__("time").time() * 1000 + expires_in * 1000),
        accountId=account_id,
    )


def load_codex_credential(auth_path: Path) -> AuthCredential:
    if not auth_path.exists():
        raise AuthProxyError(f"host Pi auth file does not exist: {auth_path}")
    with auth_path.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise AuthProxyError(f"host Pi auth file is not valid JSON: {auth_path}") from exc
            credential = data.get(CODEX_AUTH_PROVIDER)
            if not isinstance(credential, dict) or credential.get("type") != "oauth":
                raise AuthProxyError(f"host Pi auth is missing {CODEX_AUTH_PROVIDER} OAuth credentials")
            access = credential.get("access")
            refresh = credential.get("refresh")
            expires = credential.get("expires")
            if not isinstance(access, str) or not isinstance(refresh, str) or not isinstance(expires, (int, float)):
                raise AuthProxyError(f"host Pi auth has incomplete {CODEX_AUTH_PROVIDER} OAuth credentials")
            now_ms = int(__import__("time").time() * 1000)
            if now_ms + REFRESH_SKEW_MS >= int(expires):
                credential = refresh_openai_codex(refresh)
                data[CODEX_AUTH_PROVIDER] = dict(credential)
                fh.seek(0)
                json.dump(data, fh, indent=2)
                fh.write("\n")
                fh.truncate()
                os.chmod(auth_path, 0o600)
            account_id = credential.get("accountId") or account_id_from_access_token(str(credential.get("access", "")))
            if not isinstance(account_id, str) or not account_id:
                raise AuthProxyError(f"host Pi auth has no {CODEX_AUTH_PROVIDER} account id")
            return AuthCredential(credential, accountId=account_id)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, server_address: str, handler_cls: type[BaseHTTPRequestHandler], *, auth_path: Path, proxy_token: str):
        self.auth_path = auth_path
        self.proxy_token = proxy_token
        super().__init__(server_address, handler_cls)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ThreadingUnixHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_plain(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_plain(200, "ok\n")
            return
        self.send_plain(404, "not found\n")

    def do_POST(self) -> None:
        if self.path != "/codex/responses":
            self.send_plain(404, "not found\n")
            return
        expected = f"Bearer {self.server.proxy_token}"
        if self.headers.get("Authorization") != expected:
            self.send_plain(401, "unauthorized\n")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.send_plain(400, "invalid content length\n")
            return
        body = self.rfile.read(length) if length > 0 else b""
        try:
            credential = load_codex_credential(self.server.auth_path)
            self.forward_codex_request(credential, body)
        except AuthProxyError as exc:
            self.send_plain(502, f"host auth error: {exc}\n")
        except urllib.error.HTTPError as exc:
            self.forward_http_error(exc)
        except Exception as exc:
            self.send_plain(502, f"upstream error: {exc}\n")

    def upstream_headers(self, credential: AuthCredential) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in FORWARDED_REQUEST_HEADERS:
                headers[key] = value
        headers["Authorization"] = f"Bearer {credential['access']}"
        headers["chatgpt-account-id"] = str(credential["accountId"])
        headers["originator"] = "pi"
        return headers

    def forward_codex_request(self, credential: AuthCredential, body: bytes) -> None:
        request = urllib.request.Request(
            CODEX_UPSTREAM_URL,
            data=body,
            headers=self.upstream_headers(credential),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True

    def forward_http_error(self, exc: urllib.error.HTTPError) -> None:
        body = exc.read()
        self.send_response(exc.code)
        for key, value in exc.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="multiagent-host-auth-proxy")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--auth-path", required=True)
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--ready-file", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    proxy_token = os.environ.get("MULTIAGENT_HOST_AUTH_TOKEN")
    if not proxy_token:
        print("error: MULTIAGENT_HOST_AUTH_TOKEN is required", file=sys.stderr)
        return 2
    socket_path = Path(args.socket)
    pid_file = Path(args.pid_file)
    ready_file = Path(args.ready_file)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    old_umask = os.umask(0o177)
    try:
        server = ThreadingUnixHTTPServer(str(socket_path), Handler, auth_path=Path(args.auth_path), proxy_token=proxy_token)
    finally:
        os.umask(old_umask)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    os.chmod(pid_file, 0o600)
    ready_file.write_text("ready\n", encoding="utf-8")
    os.chmod(ready_file, 0o600)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        for path in (socket_path, pid_file, ready_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
