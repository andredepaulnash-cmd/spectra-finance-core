"""Local desktop dashboard for Spectra Finance Alpha 2.0.

The UI server binds to loopback only. It exposes operational status and a small,
allowlisted set of paper-stack controls; it has no order or live-execution API.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


INSTALL_DIR = Path(os.environ.get("SPECTRA_ALPHA2_HOME", Path.home() / "SpectraFinance-Alpha2"))
CORE_DIR = INSTALL_DIR / "spectra-finance-core"
API_DIR = INSTALL_DIR / "spectra-hummingbot-api"
API_ENV = API_DIR / ".env"
CORE_ENV = CORE_DIR / ".env.core"
WRAPPER = Path.home() / ".local" / "bin" / "spectra-alpha2"
HTML_FILE = Path(__file__).with_name("desktop.html")
ALLOWED_ACTIONS = {"start", "stop", "restart", "once", "doctor", "backup"}



def _container_command(*args: str) -> list[str]:
    configured = os.environ.get("SPECTRA_ALPHA2_CONTAINER_RUNTIME", "").strip()
    if configured:
        if configured not in {"podman", "docker"}:
            raise RuntimeError("SPECTRA_ALPHA2_CONTAINER_RUNTIME must be podman or docker")
        binary = shutil.which(configured)
    else:
        binary = shutil.which("podman") or shutil.which("docker")
    if not binary:
        raise RuntimeError("Neither Podman nor Docker is available")
    return [binary, *args]

def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _api_health() -> dict:
    api_env = _read_env(API_ENV)
    core_env = _read_env(CORE_ENV)
    paper_flags = (
        api_env.get("SPECTRA_PAPER_ONLY") == "true"
        and api_env.get("SPECTRA_ALLOW_LIVE_EXECUTION") == "false"
        and api_env.get("SPECTRA_PAPER_CONNECTORS") == "kraken_paper_trade"
        and core_env.get("SPECTRA_PAPER_ONLY") == "true"
        and core_env.get("SPECTRA_ALLOW_LIVE_EXECUTION") == "false"
    )
    username, password = api_env.get("USERNAME", ""), api_env.get("PASSWORD", "")
    if not username or not password:
        return {"online": False, "paper_flags_valid": paper_flags, "detail": "Local API credentials unavailable"}
    request = urllib.request.Request("http://127.0.0.1:8002/spectra/health")
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read(65536).decode("utf-8", errors="replace")
            try:
                detail = json.loads(body)
            except json.JSONDecodeError:
                detail = body[:500]
            return {
                "online": response.status == 200,
                "http_status": response.status,
                "paper_flags_valid": paper_flags,
                "detail": detail,
            }
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {"online": False, "paper_flags_valid": paper_flags, "detail": str(exc)[:300]}


def _core_status() -> dict:
    result = subprocess.run(
        _container_command("exec", "spectra-alpha2-core", "python", "-m", "spectra_alpha2", "status"),
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Core status command failed").strip()[-1200:])
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("Core status response was not a JSON object")
    return payload


def status_payload() -> dict:
    api = _api_health()
    try:
        core = _core_status()
        core_online = True
        core_error = ""
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        core = {}
        core_online = False
        core_error = str(exc)[:500]
    return {
        "paper_only": api.get("paper_flags_valid", False),
        "live_execution": False,
        "connector": "kraken_paper_trade",
        "api": api,
        "core_online": core_online,
        "core_error": core_error,
        "core": core,
    }


def run_action(action: str) -> dict:
    if action not in ALLOWED_ACTIONS:
        raise ValueError("Unsupported desktop action")
    if not WRAPPER.is_file():
        raise RuntimeError(f"Alpha 2.0 operator command is missing: {WRAPPER}")
    result = subprocess.run(
        [str(WRAPPER), action],
        capture_output=True,
        text=True,
        timeout=360 if action in {"once", "doctor", "backup", "start", "restart"} else 60,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    return {"ok": result.returncode == 0, "action": action, "returncode": result.returncode, "output": output[-12000:]}


def recent_logs() -> str:
    result = subprocess.run(
        _container_command("logs", "--tail", "120", "spectra-alpha2-core"),
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return (result.stdout + "\n" + result.stderr).strip()[-16000:]


class DesktopHandler(BaseHTTPRequestHandler):
    server_version = "SpectraAlpha2Desktop/1.0"

    def _host_allowed(self) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._json(403, {"error": "Only the loopback host is accepted"})
            return
        if self.path in {"/", "/index.html"}:
            try:
                body = HTML_FILE.read_bytes()
            except OSError:
                self._json(500, {"error": "Desktop dashboard asset is missing"})
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._json(200, {"ok": True, "bind": "127.0.0.1", "paper_only": True})
        elif self.path == "/api/status":
            try:
                self._json(200, status_payload())
            except Exception as exc:
                self._json(503, {"error": str(exc)[:500], "paper_only": True, "live_execution": False})
        elif self.path == "/api/logs":
            self._json(200, {"logs": recent_logs()})
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._host_allowed():
            self._json(403, {"error": "Only the loopback host is accepted"})
            return
        if self.path != "/api/action":
            self._json(404, {"error": "Not found"})
            return
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin") != expected_origin:
            self._json(403, {"error": "Cross-origin requests are not accepted"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "application/json is required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 1 or length > 1024:
            self._json(413, {"error": "Invalid request size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            action = payload.get("action") if isinstance(payload, dict) else None
            result = run_action(action)
            self._json(200 if result["ok"] else 502, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)[:1000]})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[SPECTRA DESKTOP] {fmt % args}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="spectra-alpha2-desktop")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("The desktop dashboard may only bind to loopback")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DesktopHandler)
    print(f"Spectra Finance Alpha 2.0 desktop dashboard listening on http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
