from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import migration_workspace as workspace
from backend.main import app


class MigrationWorkspaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_workspace = workspace.WORKSPACE
        self._original_workspaces = dict(workspace.WORKSPACES)
        self._original_build_locks = dict(workspace.BUILD_LOCKS)
        self._temp_dir = tempfile.TemporaryDirectory(dir=workspace.PROJECT_ROOT)
        self.spec = replace(
            workspace.WORKSPACE,
            runtime_root=Path(self._temp_dir.name) / "runtime with space",
        )
        workspace.WORKSPACE = self.spec
        workspace.WORKSPACES = {self.spec.workspace_id: self.spec}
        workspace.BUILD_LOCKS = {self.spec.workspace_id: threading.Lock()}
        self.seed_bytes = self.spec.seed_decision_path.read_bytes()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        try:
            self._clean_runtime()
            self.assertEqual(self.spec.seed_decision_path.read_bytes(), self.seed_bytes)
        finally:
            workspace.WORKSPACE = self._original_workspace
            workspace.WORKSPACES = self._original_workspaces
            workspace.BUILD_LOCKS = self._original_build_locks
            self._temp_dir.cleanup()

    def _clean_runtime(self) -> None:
        if self.spec.runtime_root.exists():
            shutil.rmtree(self.spec.runtime_root)

    def _detail(self) -> dict:
        response = self.client.get("/api/migration/workspaces/erpnext-item-price")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _payload(self, detail: dict, decisions: list[dict] | None = None) -> dict:
        return {
            "expected_mapping_content_sha256": detail["workspace"]["mapping_content_sha256"],
            "expected_decision_sha256": detail["workspace"]["decision_sha256"],
            "decisions": decisions if decisions is not None else detail["decisions"],
        }

    def _save_seed_to_runtime(self) -> dict:
        detail = self._detail()
        response = self.client.put(
            "/api/migration/workspaces/erpnext-item-price/decisions",
            json=self._payload(detail),
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _build(self, detail: dict | None = None) -> dict:
        current = detail or self._detail()
        response = self.client.post(
            "/api/migration/workspaces/erpnext-item-price/build",
            json={
                "expected_mapping_content_sha256": current["workspace"]["mapping_content_sha256"],
                "expected_decision_sha256": current["workspace"]["decision_sha256"],
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_01_workspace_catalog(self) -> None:
        response = self.client.get("/api/migration/workspaces")
        self.assertEqual(response.status_code, 200)
        item = response.json()["workspaces"][0]
        self.assertEqual(item["workspace_id"], "erpnext-item-price")
        self.assertEqual(item["source_rows"], 8)
        self.assertEqual(item["source_fields"], 10)
        self.assertEqual(item["target_fields"], 11)
        self.assertFalse(item["runtime_state"])

    def test_02_unknown_workspace(self) -> None:
        response = self.client.get("/api/migration/workspaces/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error"], "unknown_workspace")

    def test_03_detail_uses_seed_decision(self) -> None:
        detail = self._detail()
        self.assertEqual(detail["workspace"]["decision_source"], "seed")
        self.assertFalse(detail["workspace"]["runtime_state"])

    def test_04_detail_excludes_ground_truth(self) -> None:
        text = json.dumps(self._detail())
        self.assertNotIn("ground_truth", text)

    def test_05_detail_excludes_expected_targets(self) -> None:
        text = json.dumps(self._detail())
        self.assertNotIn("expected_targets", text)

    def test_06_detail_mapping_sha(self) -> None:
        detail = self._detail()
        self.assertEqual(
            detail["workspace"]["mapping_content_sha256"],
            "99007ad5da580b6e764b01e3a9739840bcfcff1b1a16c29cf708124ebbc56703",
        )

    def test_07_save_valid_single_target_decisions(self) -> None:
        detail = self._detail()
        decisions = [item for item in detail["decisions"] if item["target"] != "item_price.item_code"]
        response = self.client.put(
            "/api/migration/workspaces/erpnext-item-price/decisions",
            json=self._payload(detail, decisions),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspace"]["decision_source"], "runtime")

    def test_08_save_valid_multi_target_decisions(self) -> None:
        detail = self._save_seed_to_runtime()
        article = [item for item in detail["decisions"] if item["source_field"] == "article_number"]
        self.assertEqual(len(article), 2)

    def test_09_save_creates_runtime_decision(self) -> None:
        self._save_seed_to_runtime()
        self.assertTrue((self.spec.runtime_root / "mapping_decisions.yaml").is_file())

    def test_10_seed_decision_not_modified(self) -> None:
        self._save_seed_to_runtime()
        self.assertEqual(self.spec.seed_decision_path.read_bytes(), self.seed_bytes)

    def test_11_stale_mapping_sha_returns_409(self) -> None:
        detail = self._detail()
        payload = self._payload(detail)
        payload["expected_mapping_content_sha256"] = "0" * 64
        response = self.client.put("/api/migration/workspaces/erpnext-item-price/decisions", json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"], "stale_mapping")

    def test_12_stale_decision_sha_returns_409(self) -> None:
        detail = self._detail()
        payload = self._payload(detail)
        payload["expected_decision_sha256"] = "0" * 64
        response = self.client.put("/api/migration/workspaces/erpnext-item-price/decisions", json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"], "stale_decision")

    def test_13_duplicate_target_returns_422(self) -> None:
        detail = self._detail()
        decisions = detail["decisions"] + [
            {
                "source_field": "catalogue_caption",
                "target": "item.item_code",
                "decision": "approved",
                "transformation": {"type": "copy"},
            }
        ]
        response = self.client.put("/api/migration/workspaces/erpnext-item-price/decisions", json=self._payload(detail, decisions))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["decision_error"]["code"], "duplicate_approved_target")

    def test_14_duplicate_link_returns_422(self) -> None:
        detail = self._detail()
        decisions = detail["decisions"] + [detail["decisions"][0]]
        response = self.client.put("/api/migration/workspaces/erpnext-item-price/decisions", json=self._payload(detail, decisions))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["decision_error"]["code"], "duplicate_approved_link")

    def test_15_conflicting_decision_returns_422(self) -> None:
        detail = self._detail()
        decisions = detail["decisions"] + [
            {"source_field": "article_number", "target": None, "decision": "rejected", "transformation": {"type": "copy"}}
        ]
        response = self.client.put("/api/migration/workspaces/erpnext-item-price/decisions", json=self._payload(detail, decisions))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["decision_error"]["code"], "conflicting_source_decisions")

    def test_16_target_outside_top3_returns_422(self) -> None:
        detail = self._detail()
        decisions = [
            item
            for item in detail["decisions"]
            if item["source_field"] not in {"data_steward", "retail_amount"}
        ]
        decisions.append(
            {
                "source_field": "data_steward",
                "target": "item_price.price_list_rate",
                "decision": "approved",
                "transformation": {"type": "copy"},
            }
        )
        response = self.client.put("/api/migration/workspaces/erpnext-item-price/decisions", json=self._payload(detail, decisions))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["decision_error"]["code"], "target_not_in_top3")

    def test_17_failed_save_preserves_old_runtime_decision(self) -> None:
        saved = self._save_seed_to_runtime()
        before = (self.spec.runtime_root / "mapping_decisions.yaml").read_bytes()
        decisions = saved["decisions"] + [saved["decisions"][0]]
        response = self.client.put("/api/migration/workspaces/erpnext-item-price/decisions", json=self._payload(saved, decisions))
        self.assertEqual(response.status_code, 422)
        self.assertEqual((self.spec.runtime_root / "mapping_decisions.yaml").read_bytes(), before)

    def test_18_build_from_seed_decision(self) -> None:
        detail = self._build()
        self.assertTrue(detail["build"]["available"])
        self.assertEqual(detail["workspace"]["decision_source"], "seed")

    def test_19_build_from_runtime_decision(self) -> None:
        saved = self._save_seed_to_runtime()
        detail = self._build(saved)
        self.assertTrue(detail["build"]["available"])
        self.assertEqual(detail["workspace"]["decision_source"], "runtime")

    def test_20_build_returns_valid_true(self) -> None:
        detail = self._build()
        self.assertEqual(detail["build"]["status"], "completed")
        self.assertTrue(detail["build"]["validation"]["valid"])

    def test_21_build_findings_zero(self) -> None:
        detail = self._build()
        self.assertEqual(detail["build"]["validation"]["finding_count"], 0)

    def test_22_build_lineage_88(self) -> None:
        detail = self._build()
        self.assertEqual(detail["build"]["summary"]["lineage_entries"], 88)

    def test_23_build_uses_no_subprocess(self) -> None:
        text = Path("backend/migration_workspace.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", text)
        self.assertNotIn("os.system", text)

    def test_24_resource_preview_item(self) -> None:
        self._build()
        response = self.client.get("/api/migration/workspaces/erpnext-item-price/resources/item")
        body = response.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["total_rows"], 8)
        self.assertEqual(len(body["columns"]), 5)

    def test_25_resource_preview_item_price(self) -> None:
        self._build()
        response = self.client.get("/api/migration/workspaces/erpnext-item-price/resources/item_price")
        body = response.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["total_rows"], 8)
        self.assertEqual(len(body["columns"]), 6)

    def test_26_unknown_resource(self) -> None:
        response = self.client.get("/api/migration/workspaces/erpnext-item-price/resources/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error"], "unknown_resource")

    def test_27_resource_path_injection_rejected(self) -> None:
        response = self.client.get("/api/migration/workspaces/erpnext-item-price/resources/..%2Fitem")
        self.assertIn(response.status_code, {404, 422})

    def test_28_lineage_filter_article_number_16(self) -> None:
        self._build()
        response = self.client.get("/api/migration/workspaces/erpnext-item-price/lineage?source_field=article_number")
        self.assertEqual(response.json()["matched_entries"], 16)

    def test_29_lineage_filter_inventory_measure_16(self) -> None:
        self._build()
        response = self.client.get("/api/migration/workspaces/erpnext-item-price/lineage?source_field=inventory_measure")
        self.assertEqual(response.json()["matched_entries"], 16)

    def test_30_lineage_contains_no_raw_source_value(self) -> None:
        self._build()
        response = self.client.get("/api/migration/workspaces/erpnext-item-price/lineage?limit=5")
        text = json.dumps(response.json())
        self.assertIn("source_value_sha256", text)
        self.assertNotIn('"source_value"', text)
        self.assertNotIn('"raw_value"', text)
        self.assertNotIn('"original_value"', text)

    def test_31_reset_removes_runtime_state(self) -> None:
        self._build()
        self.assertTrue(self.spec.runtime_root.exists())
        response = self.client.post("/api/migration/workspaces/erpnext-item-price/reset")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.spec.runtime_root.exists())
        self.assertEqual(response.json()["workspace"]["decision_source"], "seed")

    def test_32_reset_does_not_modify_seed_decision(self) -> None:
        self._build()
        self.client.post("/api/migration/workspaces/erpnext-item-price/reset")
        self.assertEqual(self.spec.seed_decision_path.read_bytes(), self.seed_bytes)

    def test_33_report_post_remains_405(self) -> None:
        response = self.client.post("/api/reports/cutover_daily_report")
        self.assertEqual(response.status_code, 405)

    def test_34_runtime_paths_remain_inside_data_runtime(self) -> None:
        self._build()
        for path in self.spec.runtime_root.rglob("*"):
            path.resolve().relative_to(self.spec.runtime_root.resolve())

    def test_35_api_response_contains_no_local_absolute_path(self) -> None:
        detail = self._detail()
        self.assertNotIn("C:\\Users\\", json.dumps(detail))

    def test_36_no_ground_truth_file_is_read_by_workspace_service(self) -> None:
        original = Path.read_text

        def guarded(path: Path, *args, **kwargs):
            if "ground_truth" in str(path):
                raise AssertionError("ground truth must not be read")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", guarded):
            self._detail()

    def test_37_concurrent_build_lock_prevents_overlapping_write(self) -> None:
        detail = self._detail()
        lock = workspace.BUILD_LOCKS["erpnext-item-price"]
        self.assertTrue(lock.acquire(blocking=False))
        try:
            response = self.client.post(
                "/api/migration/workspaces/erpnext-item-price/build",
                json={
                    "expected_mapping_content_sha256": detail["workspace"]["mapping_content_sha256"],
                    "expected_decision_sha256": detail["workspace"]["decision_sha256"],
                },
            )
        finally:
            lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"], "build_in_progress")

    def test_38_cors_preflight_allows_put_post_for_configured_origins(self) -> None:
        response = self.client.options(
            "/api/migration/workspaces/erpnext-item-price/decisions",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "PUT",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("PUT", response.headers["access-control-allow-methods"])
        self.assertIn("POST", response.headers["access-control-allow-methods"])


if __name__ == "__main__":
    unittest.main()
