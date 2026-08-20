from __future__ import annotations

import io
import os
import re
import signal
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_local_demo as launcher


class FakeProcess:
    def __init__(self, returncode: int | None = None, pid: int = 12345):
        self.returncode = returncode
        self.pid = pid
        self.stdout = []
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class LocalDemoLauncherTests(unittest.TestCase):
    def test_repo_root_resolves_from_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend").mkdir()
            (root / "backend" / "main.py").write_text("", encoding="utf-8")
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
            nested = root / "scripts" / "tools"
            nested.mkdir(parents=True)
            self.assertEqual(launcher.find_repo_root(nested), root.resolve())

    def test_default_arguments_bind_localhost_ports(self) -> None:
        args = launcher.build_parser().parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.backend_port, 8001)
        self.assertEqual(args.frontend_port, 5173)
        self.assertFalse(args.open_browser)
        self.assertFalse(args.offline_model)
        self.assertFalse(args.smoke_test)
        launcher.validate_loopback_host(args.host)
        with self.assertRaises(launcher.LauncherError):
            launcher.validate_loopback_host("0.0.0.0")

    def test_npm_command_uses_cmd_on_windows_and_plain_on_posix(self) -> None:
        with patch("scripts.run_local_demo.shutil.which", side_effect=lambda name: f"C:/bin/{name}" if name in {"npm.cmd", "npm"} else None):
            self.assertEqual(launcher.npm_executable("win32"), "C:/bin/npm.cmd")
            self.assertEqual(launcher.npm_executable("linux"), "C:/bin/npm")

    def test_port_occupied_check(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            self.assertFalse(launcher.port_is_available("127.0.0.1", port))

    def test_prerequisite_missing_uvicorn_reports_fix_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frontend" / "node_modules").mkdir(parents=True)
            (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
            with patch("scripts.run_local_demo.importlib.util.find_spec", return_value=None):
                with self.assertRaisesRegex(launcher.LauncherError, "backend/requirements.txt"):
                    launcher.check_prerequisites(root, "127.0.0.1", 18001, 18002)

    def test_prerequisite_missing_node_modules_reports_fix_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frontend").mkdir(parents=True)
            (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
            with patch("scripts.run_local_demo.importlib.util.find_spec", return_value=object()), \
                patch("scripts.run_local_demo.node_executable", return_value="node"), \
                patch("scripts.run_local_demo.npm_executable", return_value="npm"):
                with self.assertRaisesRegex(launcher.LauncherError, "npm install"):
                    launcher.check_prerequisites(root, "127.0.0.1", 18001, 18002)

    def test_backend_and_frontend_commands_are_argument_lists(self) -> None:
        backend = launcher.backend_command("127.0.0.1", 9001)
        frontend = launcher.frontend_command("npm.cmd", "127.0.0.1", 5173)
        self.assertEqual(backend[-4:], ["--host", "127.0.0.1", "--port", "9001"])
        self.assertEqual(backend[1:4], ["-m", "uvicorn", "backend.main:app"])
        self.assertEqual(frontend, ["npm.cmd", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"])

    def test_offline_env_sets_huggingface_flags_and_api_base(self) -> None:
        env = launcher.build_child_env({"KEEP": "1"}, "http://127.0.0.1:8001", "http://127.0.0.1:5173", True)
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["VITE_API_BASE"], "http://127.0.0.1:8001")
        self.assertEqual(env["CARVEOPS_CORS_ORIGINS"], "http://127.0.0.1:5173")
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(env["TRANSFORMERS_OFFLINE"], "1")

    def test_launcher_default_and_custom_frontend_origin(self) -> None:
        self.assertEqual(launcher.frontend_origin("127.0.0.1", 5173), "http://127.0.0.1:5173")
        self.assertEqual(launcher.frontend_origin("127.0.0.1", 51987), "http://127.0.0.1:51987")
        env = launcher.build_child_env({}, "http://127.0.0.1:18001", "http://127.0.0.1:51987", False)
        self.assertEqual(env["CARVEOPS_CORS_ORIGINS"], "http://127.0.0.1:51987")
        self.assertNotIn("HF_HUB_OFFLINE", env)

    def test_wait_for_http_success_and_timeout(self) -> None:
        clock = {"now": 0.0}

        def monotonic() -> float:
            return clock["now"]

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        launcher.wait_for_http("Frontend", "http://127.0.0.1:5173/", 1, ready=lambda _url: True, sleep=sleep, monotonic=monotonic)
        with self.assertRaisesRegex(launcher.LauncherError, "Timed out"):
            launcher.wait_for_http("Frontend", "http://127.0.0.1:5173/", 1, ready=lambda _url: False, sleep=sleep, monotonic=monotonic)

    def test_wait_for_http_detects_child_early_exit(self) -> None:
        with self.assertRaises(launcher.ChildProcessExited) as ctx:
            launcher.wait_for_http(
                "Backend",
                "http://127.0.0.1:8001/api/health",
                1,
                children=[("backend", FakeProcess(returncode=2))],
                ready=lambda _url: False,
                sleep=lambda _seconds: None,
                monotonic=Mock(side_effect=[0.0, 0.1]),
            )
        self.assertEqual(ctx.exception.label, "backend")
        self.assertEqual(ctx.exception.returncode, 2)

    def test_cleanup_skips_already_exited_child(self) -> None:
        process = FakeProcess(returncode=0, pid=22222)
        launcher.cleanup_processes([("frontend", process)])
        self.assertEqual(process.wait_calls, 0)

    def test_windows_cleanup_requests_child_process_tree_only(self) -> None:
        running = FakeProcess(returncode=None, pid=33333)
        external_pid = "44444"

        def fake_taskkill(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            running.returncode = -9
            return subprocess.CompletedProcess(command, 0)

        with patch("scripts.run_local_demo.os.name", "nt"), \
            patch("scripts.run_local_demo.signal.CTRL_BREAK_EVENT", 21, create=True), \
            patch("scripts.run_local_demo.os.kill"), \
            patch("scripts.run_local_demo.subprocess.run", side_effect=fake_taskkill) as taskkill:
            launcher.terminate_process_tree("backend", running, grace_seconds=0.01)
            taskkill.assert_called_once()
            command = taskkill.call_args.args[0]
            self.assertIn(str(running.pid), command)
            self.assertNotIn(external_pid, command)

    def test_posix_cleanup_requests_child_process_group_only(self) -> None:
        running = FakeProcess(returncode=None, pid=33333)
        external_pid = 44444

        def fake_killpg(pid: int, signum: int) -> None:
            self.assertEqual(pid, running.pid)
            self.assertNotEqual(pid, external_pid)
            self.assertEqual(signum, signal.SIGTERM)
            running.returncode = -15

        with patch("scripts.run_local_demo.os.name", "posix"), \
            patch("scripts.run_local_demo.os.killpg", side_effect=fake_killpg, create=True) as killpg, \
            patch("scripts.run_local_demo.subprocess.run") as taskkill:
            launcher.terminate_process_tree("backend", running, grace_seconds=0.01)
            killpg.assert_called_once_with(running.pid, signal.SIGTERM)
            taskkill.assert_not_called()

    def test_smoke_success_starts_real_commands_then_cleans_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frontend" / "node_modules").mkdir(parents=True)
            (root / "frontend" / "package.json").write_text("{}", encoding="utf-8")
            (root / "backend").mkdir()
            (root / "backend" / "main.py").write_text("", encoding="utf-8")
            backend = FakeProcess()
            frontend = FakeProcess()
            started: list[tuple[str, list[str], Path]] = []

            def fake_start(label: str, command: list[str], cwd: Path, env: dict[str, str]) -> FakeProcess:
                started.append((label, command, cwd))
                return backend if label == "backend" else frontend

            with patch("scripts.run_local_demo.find_repo_root", return_value=root), \
                patch("scripts.run_local_demo.check_prerequisites", return_value=launcher.Prerequisites(root, "npm", "node", "cached", "test-cache")), \
                patch("scripts.run_local_demo.start_process", side_effect=fake_start), \
                patch("scripts.run_local_demo.wait_for_http"), \
                patch("scripts.run_local_demo.smoke_cors_probe"), \
                patch("scripts.run_local_demo.cleanup_processes") as cleanup:
                self.assertEqual(launcher.run(["--smoke-test", "--offline-model"]), 0)
                self.assertEqual([item[0] for item in started], ["backend", "frontend"])
                cleanup.assert_called_once()

    def test_source_does_not_use_shell_true_or_sentence_transformer_constructor(self) -> None:
        source = Path("scripts/run_local_demo.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("SentenceTransformer", source)
        self.assertNotIn("write_text", source)
        self.assertNotIn("write_bytes", source)

    def test_model_cache_detection_does_not_construct_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "abc"
            cache.mkdir(parents=True)
            (cache / "config.json").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"HF_HOME": tmp}, clear=True):
                status, detail = launcher.detect_model_cache()
            self.assertEqual(status, "cached")
            self.assertIn("models--sentence-transformers--all-MiniLM-L6-v2", detail)

    def test_no_pid_log_or_runtime_files_are_written_by_helpers(self) -> None:
        source = Path("scripts/run_local_demo.py").read_text(encoding="utf-8").lower()
        for marker in ["pidfile", "pid_path", "log_path", ".log", "data/runtime"]:
            self.assertNotIn(marker, source)

    def test_sensitive_environment_values_are_redacted(self) -> None:
        text = "token value abc123 password value def456"
        redacted = launcher.redact_sensitive(text, {"API_TOKEN": "abc123", "PASSWORD": "def456"})
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("def456", redacted)
        self.assertIn("[redacted]", redacted)

    def test_startup_summary_is_ascii_and_cp1252_safe(self) -> None:
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            launcher.print_startup_summary(
                "http://127.0.0.1:8001",
                "http://127.0.0.1:5173",
                "cached",
                "local-cache",
                True,
            )
        output = stream.getvalue()
        output.encode("ascii", errors="strict")
        output.encode("cp1252", errors="strict")
        self.assertIn('select "New Schema Mapping"', output)
        self.assertIn("frontend navigation", output)
        self.assertNotIn("\u65b0\u5efa\u5b57\u6bb5\u6620\u5c04", output)

    def test_python_version_check_matches_ci_minimum(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        versions = [tuple(int(part) for part in match.split(".")) for match in re.findall(r'"([0-9]+\.[0-9]+)"', workflow)]
        self.assertIn(launcher.MIN_PYTHON_VERSION, versions)
        self.assertEqual(launcher.MIN_PYTHON_VERSION, min(versions))

    def test_frontend_failure_cleanup_path(self) -> None:
        backend = FakeProcess()
        with patch("scripts.run_local_demo.terminate_process_tree") as terminate:
            launcher.cleanup_processes([("backend", backend)])
            terminate.assert_called_once_with("backend", backend)


if __name__ == "__main__":
    unittest.main()
