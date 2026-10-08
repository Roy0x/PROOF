"""Loopback HTTP host for the worker-produced SQLite login application."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx


LOGIN_HTML = """<!doctype html><html><body><h1>Sign in</h1>
<input id="email" value="demo@proof.local"><input id="password" type="password">
<button id="submit">Sign in</button><p id="login-error"></p>
<script>
document.querySelector('#submit').onclick = async () => {
  const response = await fetch('/api/login', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email: document.querySelector('#email').value,
      password: document.querySelector('#password').value})});
  if (response.ok) location.href = '/dashboard';
  else document.querySelector('#login-error').textContent =
    'Authentication unavailable: ' + (await response.json()).error;
};
</script></body></html>"""


class ControlledLoginServer:
    """Runs only trusted, freshly worker-written fixture code against its own temp DB."""

    def __init__(self, workspace: Path, database: Path) -> None:
        workspace, database = workspace.resolve(), database.resolve()
        if database.parent != workspace or not database.is_file():
            raise ValueError("Production database must be inside the assigned workspace")
        source = workspace / "login_service.py"
        if not source.is_file():
            raise ValueError("Worker-produced login source is missing")
        spec = importlib.util.spec_from_file_location(f"proof_login_{uuid4().hex}", source)
        if spec is None or spec.loader is None:
            raise ValueError("Worker-produced login source cannot be loaded")
        service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def send_body(self, status: int, body: bytes, content_type: str,
                          cookie: str | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body)

            def send_json(self, status: int, payload: dict, cookie: str | None = None) -> None:
                self.send_body(status, json.dumps(payload).encode("utf-8"),
                               "application/json", cookie)

            def do_GET(self) -> None:
                if self.path == "/":
                    self.send_body(200, b"<h1>Production online</h1>", "text/html")
                elif self.path == "/login":
                    self.send_body(200, LOGIN_HTML.encode("utf-8"), "text/html")
                elif self.path == "/health/schema":
                    with closing(sqlite3.connect(database)) as connection:
                        present = connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_sessions'"
                        ).fetchone() is not None
                    self.send_json(200, {"auth_sessions_exists": present})
                elif self.path == "/dashboard":
                    session_id = next((part.split("=", 1)[1] for part in
                                       self.headers.get("Cookie", "").split("; ")
                                       if part.startswith("session=")), "")
                    try:
                        with closing(sqlite3.connect(database)) as connection:
                            email = service.session_user(connection, session_id)
                    except sqlite3.OperationalError:
                        self.send_json(500, {"error": "authentication_storage_unavailable"})
                        return
                    if not email:
                        self.send_json(401, {"error": "authentication_required"})
                        return
                    self.send_body(200, b"<h1 id='dashboard-heading'>Deployment dashboard</h1>",
                                   "text/html")
                else:
                    self.send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if self.path != "/api/login":
                    self.send_json(404, {"error": "not_found"})
                    return
                try:
                    self.connection.settimeout(2)
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= size <= 1024:
                        raise ValueError
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str):
                        raise ValueError
                except (ValueError, UnicodeDecodeError):
                    self.send_json(400, {"error": "invalid_request"})
                    return
                try:
                    with closing(sqlite3.connect(database)) as connection:
                        session_id = service.authenticate(connection, data["email"], data["password"])
                except sqlite3.OperationalError as exc:
                    error = ("missing_auth_sessions_table" if "no such table: auth_sessions" in str(exc)
                             else "authentication_storage_unavailable")
                    self.send_json(500, {"error": error})
                    return
                if session_id is None:
                    self.send_json(401, {"error": "invalid_credentials"})
                    return
                self.send_json(200, {"authenticated": True},
                               f"session={session_id}; HttpOnly; SameSite=Strict; Path=/")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._started = False

    def start(self, timeout_seconds: float = 3.0) -> None:
        self._thread.start()
        self._started = True
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            try:
                if httpx.get(self.base_url + "/", timeout=0.25).status_code == 200:
                    return
            except httpx.RequestError:
                pass
            sleep(0.05)
        self.close()
        raise TimeoutError("Controlled login server did not start")

    def close(self) -> None:
        if self._started:
            self._server.shutdown()
            self._thread.join(timeout=3)
            self._started = False
        self._server.server_close()
