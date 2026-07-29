from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from backend.main import REPORTS, app


class BackendApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_reports_ten_of_ten(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["reports_available"], 10)
        self.assertEqual(body["reports_total"], 10)

    def test_module_three_reports_are_readable(self) -> None:
        for name in (
            "cutover_plan_report",
            "cutover_status_report",
            "cutover_daily_report",
            "cutover_agent_trace",
        ):
            with self.subTest(name=name):
                response = self.client.get(f"/api/reports/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("_run_info", response.json())

    def test_forbidden_reports_remain_unknown(self) -> None:
        for name in (
            "interview_notes_ground_truth",
            "cutover_status_updates",
            "cutover_constraints",
            "cutover_agent_cache",
            "cutover_agent_runs",
        ):
            with self.subTest(name=name):
                response = self.client.get(f"/api/reports/{name}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["detail"]["error"], "unknown_report")

    def test_report_catalog_endpoints_remain_read_only(self) -> None:
        response = self.client.post("/api/reports/cutover_daily_report")
        self.assertEqual(response.status_code, 405)

    def test_report_paths_are_static_whitelist_values(self) -> None:
        filenames = {name: spec.filename for name, spec in REPORTS.items()}
        self.assertEqual(filenames["cutover_agent_trace"], "cutover_agent_trace.json")
        self.assertNotIn("cutover_status_updates", REPORTS)
        self.assertNotIn("cutover_agent_cache", REPORTS)


if __name__ == "__main__":
    unittest.main()
