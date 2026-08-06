from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import subprocess
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.mcp_servers import cutover_server as server  # noqa: E402


EXPECTED_TOOLS = [
    "get_cutover_plan_summary",
    "get_cutover_status_summary",
    "get_cutover_daily_brief",
    "list_cutover_activities",
    "list_raid_items",
    "rebuild_cutover_reports",
]


def payload(result: dict) -> dict:
    return result["data"]


class PatchReportPath:
    def __init__(self, key: str, value: Path) -> None:
        self.key = key
        self.value = value
        self.original = server.REPORT_PATHS[key]

    def __enter__(self) -> None:
        server.REPORT_PATHS[self.key] = self.value

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        server.REPORT_PATHS[self.key] = self.original


class PatchReportPaths:
    def __init__(self, values: dict[str, Path]) -> None:
        self.values = values
        self.originals = {key: server.REPORT_PATHS[key] for key in values}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            server.REPORT_PATHS[key] = value

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        for key, value in self.originals.items():
            server.REPORT_PATHS[key] = value


def file_sha(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def simple_report(name: str, version: int) -> dict:
    return {"name": name, "version": version}


def simple_report_set(version: int) -> dict[str, dict]:
    return {
        "daily": simple_report("daily", version),
        "migration_findings": simple_report("migration_findings", version),
        "plan": simple_report("plan", version),
        "status": simple_report("status", version),
    }


def create_report_targets(tmp: str, *, missing: set[str] | None = None) -> dict[str, Path]:
    missing = missing or set()
    paths = {key: Path(tmp) / f"{key}.json" for key in simple_report_set(1)}
    for key, path in paths.items():
        if key not in missing:
            path.write_text(json.dumps(simple_report(key, 1), sort_keys=True) + "\n", encoding="utf-8")
    return paths


@contextmanager
def patched_rebuild_report_paths():
    with tempfile.TemporaryDirectory() as tmp:
        paths = {
            key: Path(tmp) / server.REPORT_PATHS[key].name
            for key in ("migration_findings", "plan", "status", "daily")
        }
        for key, path in paths.items():
            shutil.copyfile(server.REPORT_PATHS[key], path)
        with PatchReportPaths(paths):
            yield paths


def snapshot(paths: dict[str, Path]) -> dict[str, bytes | None]:
    return {key: path.read_bytes() if path.exists() else None for key, path in paths.items()}


def assert_snapshot_equal(testcase: unittest.TestCase, paths: dict[str, Path], expected: dict[str, bytes | None]) -> None:
    for key, expected_bytes in expected.items():
        with testcase.subTest(target=key):
            if expected_bytes is None:
                testcase.assertFalse(paths[key].exists())
            else:
                testcase.assertEqual(paths[key].read_bytes(), expected_bytes)


def no_leftover_temp_files(paths: dict[str, Path]) -> bool:
    parent = next(iter(paths.values())).parent
    return not any(path.name.startswith(".") and (".staged" in path.name or ".backup" in path.name) for path in parent.iterdir())


def replace_that_fails_on_call(fail_call: int):
    real_replace = server.os.replace
    calls = {"count": 0}

    def replacement(source: str | Path, target: str | Path) -> None:
        calls["count"] += 1
        if calls["count"] == fail_call:
            raise OSError(f"injected replace failure {fail_call}")
        real_replace(source, target)

    return replacement


class CutoverMcpTests(unittest.TestCase):
    def test_mcp_server_registers_six_tools(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual(len(tools), 6)

    def test_tool_names_are_stable(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual([tool.name for tool in tools], EXPECTED_TOOLS)

    def test_plan_summary_counts_are_correct(self) -> None:
        data = payload(server.get_cutover_plan_summary())
        self.assertTrue(data["validation_valid"])
        self.assertEqual(data["activity_count"], 34)
        self.assertEqual(data["work_package_count"], 5)
        self.assertEqual(data["raid_count"], 29)

    def test_status_summary_counts_are_correct(self) -> None:
        data = payload(server.get_cutover_status_summary())
        self.assertTrue(data["validation_valid"])
        self.assertEqual(data["events_applied_count"], 28)
        self.assertEqual(data["activity_status_counts"]["Completed"], 17)
        self.assertEqual(data["activity_status_counts"]["Blocked"], 2)

    def test_daily_brief_overall_rag_is_red(self) -> None:
        data = payload(server.get_cutover_daily_brief())
        self.assertTrue(data["validation_valid"])
        self.assertEqual(data["overall_rag"], "Red")

    def test_blocked_activity_filter_returns_two_rows(self) -> None:
        data = payload(server.list_cutover_activities(status="Blocked"))
        self.assertEqual(data["count"], 2)
        self.assertEqual(
            [item["activity_id"] for item in data["activities"]],
            ["ACT-EX-024-TEST", "CUT-CUTOVER-READINESS"],
        )

    def test_critical_only_filter_returns_day1_critical_items(self) -> None:
        data = payload(server.list_cutover_activities(critical_only=True))
        self.assertGreater(data["count"], 0)
        self.assertTrue(all(item["is_critical_to_day1"] for item in data["activities"]))

    def test_risk_filter_returns_two_items(self) -> None:
        data = payload(server.list_raid_items(raid_type="Risk"))
        self.assertEqual(data["count"], 9)
        self.assertEqual({item["type"] for item in data["raid_items"]}, {"Risk"})

    def test_invalid_activity_status_returns_invalid_filter(self) -> None:
        result = server.list_cutover_activities(status="Started")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_FILTER")

    def test_invalid_raid_type_returns_invalid_filter(self) -> None:
        result = server.list_raid_items(raid_type="Decision")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_FILTER")

    def test_unknown_owner_role_returns_invalid_filter(self) -> None:
        result = server.list_cutover_activities(owner_role="Unknown Role")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_FILTER")

    def test_missing_report_returns_report_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with PatchReportPath("plan", Path(tmp) / "missing.json"):
                result = server.get_cutover_plan_summary()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "REPORT_NOT_FOUND")

    def test_invalid_json_returns_invalid_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "bad.json"
            invalid.write_text("{", encoding="utf-8")
            with PatchReportPath("daily", invalid):
                result = server.get_cutover_daily_brief()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_REPORT")

    def test_rebuild_status_only_succeeds(self) -> None:
        with patched_rebuild_report_paths():
            data = payload(server.rebuild_cutover_reports(rebuild_plan=False))
        self.assertEqual(data["validation"], "valid")
        self.assertEqual(data["overall_rag"], "Red")

    def test_rebuild_plan_and_status_succeeds(self) -> None:
        with patched_rebuild_report_paths():
            data = payload(server.rebuild_cutover_reports(rebuild_plan=True))
        self.assertEqual(data["validation"], "valid")
        self.assertEqual(data["overall_rag"], "Red")
        self.assertEqual(data["migration_finding_count"], 22)
        self.assertEqual(data["plan_raid_count"], 29)
        self.assertEqual(data["plan_activity_count"], 34)

    def test_rebuild_returns_stable_sha(self) -> None:
        with patched_rebuild_report_paths():
            first = payload(server.rebuild_cutover_reports(rebuild_plan=False))
            second = payload(server.rebuild_cutover_reports(rebuild_plan=False))
        self.assertEqual(first["status_content_sha256"], second["status_content_sha256"])
        self.assertEqual(first["daily_content_sha256"], second["daily_content_sha256"])

    def test_validation_failure_returns_validation_failed(self) -> None:
        updates = copy.deepcopy(server.load_report("updates"))
        updates["events"][0]["new_status"] = "Invalid"
        with tempfile.TemporaryDirectory() as tmp:
            bad_updates = Path(tmp) / "cutover_status_updates.json"
            bad_updates.write_text(json.dumps(updates), encoding="utf-8")
            with patched_rebuild_report_paths(), PatchReportPath("updates", bad_updates):
                result = server.rebuild_cutover_reports(rebuild_plan=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "VALIDATION_FAILED")

    def test_mcp_rebuild_builds_findings_before_plan(self) -> None:
        with patched_rebuild_report_paths():
            data = payload(server.rebuild_cutover_reports(rebuild_plan=True))
            self.assertEqual(data["migration_finding_count"], 22)
            self.assertEqual(data["migration_findings_content_sha256"], server.source_sha(server.load_report("migration_findings")))

    def test_mcp_rebuild_passes_findings_to_plan(self) -> None:
        with patched_rebuild_report_paths():
            data = payload(server.rebuild_cutover_reports(rebuild_plan=True))
            plan = server.load_report("plan")
            self.assertEqual(plan["_meta"]["migration_findings"]["content_sha256"], data["migration_findings_content_sha256"])
            self.assertEqual(data["plan_raid_count"], 29)

    def test_mcp_rebuild_reports_rebase_required(self) -> None:
        updates = copy.deepcopy(server.load_report("updates"))
        updates["_meta"]["source_plan_content_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            bad_updates = Path(tmp) / "cutover_status_updates.json"
            bad_updates.write_text(json.dumps(updates), encoding="utf-8")
            with patched_rebuild_report_paths(), PatchReportPath("updates", bad_updates):
                result = server.rebuild_cutover_reports(rebuild_plan=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "REBASE_REQUIRED")
        self.assertIn("status updates rebase required", result["error"]["message"])

    def test_mcp_rebuild_is_atomic_on_status_sha_failure(self) -> None:
        updates = copy.deepcopy(server.load_report("updates"))
        updates["_meta"]["source_plan_content_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            bad_updates = Path(tmp) / "cutover_status_updates.json"
            bad_updates.write_text(json.dumps(updates), encoding="utf-8")
            with patched_rebuild_report_paths(), PatchReportPath("updates", bad_updates):
                before = {
                    key: server.source_sha(server.load_report(key))
                    for key in ("migration_findings", "plan", "status", "daily")
                }
                result = server.rebuild_cutover_reports(rebuild_plan=True)
                after = {
                    key: server.source_sha(server.load_report(key))
                    for key in ("migration_findings", "plan", "status", "daily")
                }
        self.assertFalse(result["ok"])
        self.assertEqual(before, after)

    def test_batch_commit_success_replaces_all_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp)
            with PatchReportPaths(paths):
                server.write_reports_with_rollback(simple_report_set(2))
            self.assertTrue(all(json.loads(path.read_text(encoding="utf-8"))["version"] == 2 for path in paths.values()))
            self.assertTrue(no_leftover_temp_files(paths))

    def test_second_replace_failure_rolls_back_all_targets(self) -> None:
        self.assert_replace_failure_rolls_back(2)

    def test_third_replace_failure_rolls_back_all_targets(self) -> None:
        self.assert_replace_failure_rolls_back(3)

    def test_final_replace_failure_rolls_back_all_targets(self) -> None:
        self.assert_replace_failure_rolls_back(4)

    def assert_replace_failure_rolls_back(self, fail_call: int) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp)
            before = snapshot(paths)
            with PatchReportPaths(paths), mock.patch.object(server.os, "replace", replace_that_fails_on_call(fail_call)):
                with self.assertRaises(server.McpToolError) as ctx:
                    server.write_reports_with_rollback(simple_report_set(2))
            self.assertEqual(ctx.exception.code, "COMMIT_FAILED")
            assert_snapshot_equal(self, paths, before)
            self.assertTrue(no_leftover_temp_files(paths))

    def test_failure_restores_targets_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp)
            before = snapshot(paths)
            before_sha = {key: file_sha(path) for key, path in paths.items()}
            with PatchReportPaths(paths), mock.patch.object(server.os, "replace", replace_that_fails_on_call(3)):
                with self.assertRaises(server.McpToolError):
                    server.write_reports_with_rollback(simple_report_set(2))
            assert_snapshot_equal(self, paths, before)
            self.assertEqual({key: file_sha(path) for key, path in paths.items()}, before_sha)

    def test_failure_removes_target_that_did_not_exist_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp, missing={"daily"})
            before = snapshot(paths)
            with PatchReportPaths(paths), mock.patch.object(server.os, "replace", replace_that_fails_on_call(2)):
                with self.assertRaises(server.McpToolError):
                    server.write_reports_with_rollback(simple_report_set(2))
            assert_snapshot_equal(self, paths, before)

    def test_backup_creation_failure_writes_no_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp)
            before = snapshot(paths)
            real_write = server.write_bytes_fsynced

            def fail_on_backup(path: Path, data: bytes) -> None:
                if path.name.endswith(".backup"):
                    raise OSError("injected backup failure")
                real_write(path, data)

            with PatchReportPaths(paths), mock.patch.object(server, "write_bytes_fsynced", fail_on_backup):
                with self.assertRaises(server.McpToolError) as ctx:
                    server.write_reports_with_rollback(simple_report_set(2))
            self.assertEqual(ctx.exception.code, "COMMIT_FAILED")
            assert_snapshot_equal(self, paths, before)
            self.assertTrue(no_leftover_temp_files(paths))

    def test_success_cleans_staged_and_backup_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp)
            with PatchReportPaths(paths):
                server.write_reports_with_rollback(simple_report_set(2))
            self.assertTrue(no_leftover_temp_files(paths))

    def test_successive_failed_runs_do_not_collide_on_temp_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp)
            before = snapshot(paths)
            for _ in range(2):
                with PatchReportPaths(paths), mock.patch.object(server.os, "replace", replace_that_fails_on_call(2)):
                    with self.assertRaises(server.McpToolError):
                        server.write_reports_with_rollback(simple_report_set(2))
                assert_snapshot_equal(self, paths, before)
                self.assertTrue(no_leftover_temp_files(paths))

    def test_rollback_failure_reports_manual_recovery_and_preserves_backup(self) -> None:
        real_replace = server.os.replace
        calls = {"commit": 0, "rollback_failed": False}

        def fail_commit_then_rollback(source: str | Path, target: str | Path) -> None:
            source_path = Path(source)
            if source_path.name.endswith(".staged"):
                calls["commit"] += 1
                if calls["commit"] == 2:
                    raise OSError("injected commit failure")
            elif source_path.name.endswith(".backup") and not calls["rollback_failed"]:
                calls["rollback_failed"] = True
                raise OSError("injected rollback failure")
            real_replace(source, target)

        with tempfile.TemporaryDirectory() as tmp:
            paths = create_report_targets(tmp)
            with PatchReportPaths(paths), mock.patch.object(server.os, "replace", fail_commit_then_rollback):
                with self.assertRaises(server.McpToolError) as ctx:
                    server.write_reports_with_rollback(simple_report_set(2))
            self.assertEqual(ctx.exception.code, "COMMIT_FAILED_MANUAL_RECOVERY_REQUIRED")
            self.assertIn("manual recovery required", ctx.exception.message)
            self.assertTrue(any(path.name.endswith(".backup") for path in Path(tmp).iterdir()))

    def test_validation_failure_still_writes_nothing(self) -> None:
        updates = copy.deepcopy(server.load_report("updates"))
        updates["events"][0]["new_status"] = "Invalid"
        with tempfile.TemporaryDirectory() as tmp:
            bad_updates = Path(tmp) / "cutover_status_updates.json"
            bad_updates.write_text(json.dumps(updates), encoding="utf-8")
            with patched_rebuild_report_paths(), PatchReportPath("updates", bad_updates):
                before = {
                    key: server.source_sha(server.load_report(key))
                    for key in ("migration_findings", "plan", "status", "daily")
                }
                result = server.rebuild_cutover_reports(rebuild_plan=True)
                after = {
                    key: server.source_sha(server.load_report(key))
                    for key in ("migration_findings", "plan", "status", "daily")
                }
        self.assertFalse(result["ok"])
        self.assertEqual(before, after)

    def test_mcp_does_not_return_success_sha_chain_after_commit_failure(self) -> None:
        with patched_rebuild_report_paths(), mock.patch.object(server, "write_reports_with_rollback", side_effect=server.McpToolError("COMMIT_FAILED", "failed")):
            result = server.rebuild_cutover_reports(rebuild_plan=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "COMMIT_FAILED")
        self.assertNotIn("data", result)

    def test_mcp_report_paths_include_migration_findings(self) -> None:
        self.assertIn("migration_findings", server.REPORT_PATHS)
        self.assertEqual(server.REPORT_PATHS["migration_findings"].name, "migration_cutover_findings.json")

    def test_mcp_rebuild_returns_full_sha_chain(self) -> None:
        with patched_rebuild_report_paths():
            data = payload(server.rebuild_cutover_reports(rebuild_plan=True))
        for key in (
            "migration_findings_content_sha256",
            "plan_content_sha256",
            "status_content_sha256",
            "daily_content_sha256",
        ):
            self.assertRegex(data[key], r"^[0-9a-f]{64}$")
        self.assertEqual(data["migration_blocker_count"], 1)

    def test_tools_do_not_write_plain_stdout_logs(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            result = server.get_cutover_plan_summary()
        self.assertTrue(result["ok"])
        self.assertEqual(out.getvalue(), "")

    def test_server_does_not_reference_forbidden_inputs(self) -> None:
        source = (PROJECT_ROOT / "src" / "mcp_servers" / "cutover_server.py").read_text(encoding="utf-8")
        for forbidden in (
            "interview_notes_ground_truth",
            "gap_analysis_evaluation",
            "llm_cache",
            "data/legacy",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "Authorization",
            "reasoning_content",
        ):
            self.assertNotIn(forbidden, source)

    def test_tool_schemas_do_not_accept_path_parameters(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        for tool in tools:
            properties = tool.inputSchema.get("properties", {})
            self.assertNotIn("path", properties)
            self.assertNotIn("file_path", properties)
            self.assertNotIn("output_path", properties)

    def test_existing_cli_tools_still_run(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as tmp:
            output_root = Path(tmp)
            findings = output_root / "migration_cutover_findings.json"
            plan = output_root / "cutover_plan_report.json"
            status = output_root / "cutover_status_report.json"
            daily = output_root / "cutover_daily_report.json"
            commands = [
                [sys.executable, "src/tools/build_migration_cutover_findings.py", "--output", str(findings)],
                [sys.executable, "src/tools/build_cutover_plan.py", "--migration-findings", str(findings), "--output", str(plan)],
                [sys.executable, "src/tools/build_cutover_status.py", "--status-output", str(status), "--daily-output", str(daily)],
            ]
            for command in commands:
                with self.subTest(command=command[1]):
                    result = subprocess.run(
                        command,
                        cwd=PROJECT_ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(
                        "valid" in result.stdout.lower()
                        or "content sha" in result.stdout.lower()
                    )

    def test_mcp_stdio_smoke_test_succeeds(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/smoke_test_cutover_mcp.py"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tools discovered: 6", result.stdout)
        self.assertIn("Overall RAG: Red", result.stdout)


if __name__ == "__main__":
    unittest.main()
