from __future__ import annotations

import asyncio
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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
        self.assertEqual(data["activity_count"], 30)
        self.assertEqual(data["work_package_count"], 5)
        self.assertEqual(data["raid_count"], 7)

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
        self.assertEqual(data["count"], 2)
        self.assertEqual([item["type"] for item in data["raid_items"]], ["Risk", "Risk"])

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
        data = payload(server.rebuild_cutover_reports(rebuild_plan=False))
        self.assertEqual(data["validation"], "valid")
        self.assertEqual(data["overall_rag"], "Red")

    def test_rebuild_plan_and_status_succeeds(self) -> None:
        data = payload(server.rebuild_cutover_reports(rebuild_plan=True))
        self.assertEqual(data["validation"], "valid")
        self.assertEqual(data["overall_rag"], "Red")

    def test_rebuild_returns_stable_sha(self) -> None:
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
            with PatchReportPath("updates", bad_updates):
                result = server.rebuild_cutover_reports(rebuild_plan=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "VALIDATION_FAILED")

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
        commands = [
            [sys.executable, "src/tools/build_cutover_plan.py"],
            [sys.executable, "src/tools/build_cutover_status.py"],
        ]
        for command in commands:
            with self.subTest(command=command[-1]):
                result = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("valid", result.stdout.lower())

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
