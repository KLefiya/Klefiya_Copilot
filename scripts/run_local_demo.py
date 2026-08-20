from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MIN_PYTHON_VERSION = (3, 12)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8001
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_STARTUP_TIMEOUT = 90.0
HEALTH_INTERVAL_SECONDS = 0.5
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SENSITIVE_MARKERS = ("token", "password", "secret", "authorization", "api_key", "apikey")


@dataclass(frozen=True)
class Prerequisites:
    repo_root: Path
    npm_command: str
    node_command: str
    model_cache_status: str
    model_cache_detail: str


class LauncherError(RuntimeError):
    pass


class ChildProcessExited(LauncherError):
    def __init__(self, label: str, returncode: int):
        super().__init__(f"{label} exited before readiness with exit code {returncode}.")
        self.label = label
        self.returncode = returncode


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "backend" / "main.py").is_file() and (candidate / "frontend" / "package.json").is_file():
            return candidate
    raise LauncherError("Could not locate repository root. Run this script from inside carveops-copilot.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local CarveOps backend and frontend demo.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Loopback host to bind. Default: 127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--offline-model", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    return parser


def validate_loopback_host(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise LauncherError("Refusing to bind a public interface. Use 127.0.0.1, localhost, or ::1.")


def npm_executable(platform: str = sys.platform) -> str | None:
    names = ("npm.cmd", "npm") if platform.startswith("win") else ("npm",)
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def node_executable() -> str | None:
    return shutil.which("node")


def port_is_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def model_cache_candidates(model_id: str = MODEL_ID) -> list[Path]:
    org, name = model_id.split("/", 1)
    hf_model_dir = f"models--{org}--{name}"
    st_model_dir = model_id.replace("/", "_")
    candidates: list[Path] = []

    sentence_home = os.environ.get("SENTENCE_TRANSFORMERS_HOME")
    if sentence_home:
        candidates.append(Path(sentence_home) / st_model_dir)
        candidates.append(Path(sentence_home) / model_id)

    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        candidates.append(Path(hf_home) / "hub" / hf_model_dir)

    transformers_cache = os.environ.get("TRANSFORMERS_CACHE")
    if transformers_cache:
        candidates.append(Path(transformers_cache) / hf_model_dir)

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        candidates.append(Path(xdg_cache) / "huggingface" / "hub" / hf_model_dir)

    try:
        home = Path.home()
    except RuntimeError:
        home = None
    if home is not None:
        candidates.extend(
            [
                home / ".cache" / "huggingface" / "hub" / hf_model_dir,
                home / ".cache" / "torch" / "sentence_transformers" / st_model_dir,
            ]
        )
    return candidates


def detect_model_cache(model_id: str = MODEL_ID) -> tuple[str, str]:
    try:
        for candidate in model_cache_candidates(model_id):
            if (candidate / "snapshots").is_dir() and any((candidate / "snapshots").iterdir()):
                return "cached", str(candidate)
            if candidate.is_dir() and any(candidate.iterdir()):
                return "cached", str(candidate)
    except OSError as exc:
        return "unknown", f"cache inspection failed: {exc}"
    return "unavailable", "local cache directory was not found"


def check_prerequisites(repo_root: Path, host: str, backend_port: int, frontend_port: int) -> Prerequisites:
    validate_loopback_host(host)
    if sys.version_info < MIN_PYTHON_VERSION:
        raise LauncherError("Python 3.12+ is required. Install Python 3.12 and rerun the launcher.")
    if importlib.util.find_spec("uvicorn") is None:
        raise LauncherError("uvicorn is not importable. Run: python -m pip install -r backend/requirements.txt")
    node = node_executable()
    if node is None:
        raise LauncherError("node is not on PATH. Install Node.js, then rerun the launcher.")
    npm = npm_executable()
    if npm is None:
        raise LauncherError("npm is not on PATH. Install Node.js, then rerun the launcher.")
    if not (repo_root / "frontend" / "package.json").is_file():
        raise LauncherError("frontend/package.json is missing.")
    if not (repo_root / "frontend" / "node_modules").is_dir():
        raise LauncherError("frontend/node_modules is missing. Run: cd frontend && npm install")
    if not port_is_available(host, backend_port):
        raise LauncherError(f"Backend port {host}:{backend_port} is already in use. Choose --backend-port.")
    if not port_is_available(host, frontend_port):
        raise LauncherError(f"Frontend port {host}:{frontend_port} is already in use. Choose --frontend-port.")
    status, detail = detect_model_cache()
    return Prerequisites(repo_root, npm, node, status, detail)


def backend_command(host: str, port: int) -> list[str]:
    return [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", host, "--port", str(port)]


def frontend_command(npm_command: str, host: str, port: int) -> list[str]:
    return [npm_command, "run", "dev", "--", "--host", host, "--port", str(port)]


def url_host(host: str) -> str:
    return "[::1]" if host == "::1" else host


def local_url(host: str, port: int) -> str:
    return f"http://{url_host(host)}:{port}"


def frontend_origin(host: str, port: int) -> str:
    return local_url(host, port)


def build_child_env(base_env: dict[str, str], backend_url: str, frontend_origin_value: str, offline_model: bool) -> dict[str, str]:
    env = dict(base_env)
    env["VITE_API_BASE"] = backend_url
    env["CARVEOPS_CORS_ORIGINS"] = frontend_origin_value
    if offline_model:
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def redact_sensitive(text: str, env: dict[str, str] | None = None) -> str:
    redacted = text
    if env:
        for key, value in env.items():
            if value and any(marker in key.lower() for marker in SENSITIVE_MARKERS):
                redacted = redacted.replace(value, "[redacted]")
    for marker in SENSITIVE_MARKERS:
        redacted = redacted.replace(marker.upper(), "[redacted-key]")
        redacted = redacted.replace(marker, "[redacted-key]")
    return redacted


def safe_print(message: str, *, stream=None) -> None:
    target = stream or sys.stdout
    try:
        print(message, file=target, flush=True)
    except UnicodeEncodeError:
        encoding = target.encoding or "utf-8"
        safe = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, file=target, flush=True)


def start_process(label: str, command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["preexec_fn"] = os.setsid
    process = subprocess.Popen(command, **kwargs)
    threading.Thread(target=forward_output, args=(label, process, env), daemon=True).start()
    return process


def forward_output(label: str, process: subprocess.Popen[str], env: dict[str, str]) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        safe_print(f"[{label}] {redact_sensitive(line.rstrip(), env)}")


def http_ready(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 400
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def http_headers(url: str, origin: str, timeout: float = 5.0) -> dict[str, str]:
    request = urllib.request.Request(url, headers={"Origin": origin})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return {key.lower(): value for key, value in response.headers.items()}


def smoke_cors_probe(backend_url: str, allowed_origin: str) -> None:
    allowed_headers = http_headers(f"{backend_url}/api/health", allowed_origin)
    actual = allowed_headers.get("access-control-allow-origin")
    if actual != allowed_origin:
        raise LauncherError(
            f"CORS probe failed: expected Access-Control-Allow-Origin {allowed_origin}, got {actual!r}."
        )
    denied_origin = "http://127.0.0.1:9" if allowed_origin != "http://127.0.0.1:9" else "http://127.0.0.1:10"
    denied_headers = http_headers(f"{backend_url}/api/health", denied_origin)
    if "access-control-allow-origin" in denied_headers:
        raise LauncherError(f"CORS probe failed: unauthorized origin {denied_origin} was allowed.")
    print(f"CORS allowed origin: {allowed_origin}", flush=True)
    print(f"CORS rejected origin: {denied_origin}", flush=True)


def check_children(children: Iterable[tuple[str, subprocess.Popen[str]]]) -> None:
    for label, process in children:
        returncode = process.poll()
        if returncode is not None:
            raise ChildProcessExited(label, returncode)


def wait_for_http(
    label: str,
    url: str,
    timeout: float,
    children: Iterable[tuple[str, subprocess.Popen[str]]] = (),
    ready: Callable[[str], bool] = http_ready,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    deadline = monotonic() + timeout
    child_list = list(children)
    while monotonic() < deadline:
        check_children(child_list)
        if ready(url):
            print(f"{label} healthy: {url}", flush=True)
            return
        sleep(HEALTH_INTERVAL_SECONDS)
    raise LauncherError(f"Timed out waiting for {label} at {url}.")


def terminate_process_tree(label: str, process: subprocess.Popen[str], grace_seconds: float = 4.0) -> None:
    if process.poll() is not None:
        return
    print(f"Stopping {label}...", flush=True)
    try:
        if os.name == "nt":
            try:
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError):
                process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass

    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        print(f"Warning: {label} did not exit after forced termination.", flush=True)


def cleanup_processes(children: list[tuple[str, subprocess.Popen[str]]]) -> None:
    for label, process in reversed(children):
        terminate_process_tree(label, process)


def print_startup_summary(backend_url: str, frontend_url: str, model_status: str, model_detail: str, offline_model: bool) -> None:
    print("")
    print("CarveOps local demo is ready.")
    print(f"Backend URL : {backend_url}")
    print(f"Frontend URL: {frontend_url}")
    print(f'Mapping page: open {frontend_url} and select "New Schema Mapping" in the frontend navigation.')
    print("Stop        : press Ctrl+C to stop backend and frontend")
    print(f"Model cache : {model_status} ({model_detail})")
    print(f"Offline env : {'enabled' if offline_model else 'not set by launcher'}")
    if model_status != "cached":
        print("V4 mapping may be unavailable until the local embedding model cache exists.")
    print("")


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = find_repo_root(Path(__file__))
    children: list[tuple[str, subprocess.Popen[str]]] = []
    stop_requested = {"value": False}

    def request_stop(signum: int, _frame: object) -> None:
        stop_requested["value"] = True
        print(f"Received signal {signum}; cleaning up...", flush=True)

    old_sigterm = None
    if hasattr(signal, "SIGTERM"):
        old_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, request_stop)

    try:
        prereqs = check_prerequisites(repo_root, args.host, args.backend_port, args.frontend_port)
        backend_url = local_url(args.host, args.backend_port)
        frontend_url = local_url(args.host, args.frontend_port)
        frontend_origin_value = frontend_origin(args.host, args.frontend_port)
        env = build_child_env(os.environ, backend_url, frontend_origin_value, args.offline_model)

        print(f"Repository  : {repo_root}")
        print(f"Backend cmd : {' '.join(backend_command(args.host, args.backend_port))}")
        print(f"Frontend cmd: {' '.join(frontend_command(prereqs.npm_command, args.host, args.frontend_port))}")
        backend = start_process("backend", backend_command(args.host, args.backend_port), repo_root, env)
        children.append(("backend", backend))
        wait_for_http("Backend", f"{backend_url}/api/health", args.startup_timeout, children)

        frontend = start_process("frontend", frontend_command(prereqs.npm_command, args.host, args.frontend_port), repo_root / "frontend", env)
        children.append(("frontend", frontend))
        wait_for_http("Frontend", f"{frontend_url}/", args.startup_timeout, children)

        print_startup_summary(backend_url, frontend_url, prereqs.model_cache_status, prereqs.model_cache_detail, args.offline_model)
        if args.open_browser and not args.smoke_test:
            webbrowser.open(frontend_url)
        if args.smoke_test:
            smoke_cors_probe(backend_url, frontend_origin_value)
            print("Demo smoke test: PASS")
            return 0

        while not stop_requested["value"]:
            check_children(children)
            time.sleep(1.0)
        return 0
    except KeyboardInterrupt:
        print("Interrupted; cleaning up...", flush=True)
        return 130
    except ChildProcessExited as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except LauncherError as exc:
        print(f"Launcher error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        cleanup_processes(children)
        if old_sigterm is not None:
            signal.signal(signal.SIGTERM, old_sigterm)


if __name__ == "__main__":
    raise SystemExit(run())
