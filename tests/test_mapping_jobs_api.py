from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import mapping_jobs
from backend.main import app


SENTINEL = "RAW-SENTINEL-VALUE"


class MappingModelError(Exception):
    pass


class SourceProfileError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _payload(scorer: str = "baseline", csv_text: str | None = None, filename: str = "customers.csv") -> dict[str, str]:
    return {
        "contract_id": "generic-customer",
        "filename": filename,
        "csv_text": csv_text if csv_text is not None else f"legacy_client_id,client_name\nC-1,{SENTINEL}\n",
        "scorer": scorer,
    }


def _fake_report(scorer: str = "baseline") -> dict:
    return {
        "_run_info": {"content_sha256": f"{scorer}-content-sha"},
        "_meta": {
            "component": "contract_field_mapping",
            "contract_id": "generic-customer-v1",
            "contract_version": "1.0.0",
            "contract_sha256": "contract-sha",
            "adapter": "generic_csv",
            "domain": "customer_master",
            "source_path": "data/runtime/mapping_jobs/job/source.csv",
            "source_sha256": "source-sha",
            "source_hash_mode": "raw_file_bytes_sha256",
            "source_row_count": 1,
            "source_field_count": 2,
            "target_field_count": 11,
            "scorer_variant": scorer,
            "scorer_id": scorer,
            "feature_version": "precision_tiered_interaction_v1" if scorer == "precision_tiered_v4" else None,
            "ground_truth_used": False,
            "ground_truth_used_for_candidate_generation": False,
            "experimental": scorer == "precision_tiered_v4",
            "production_scorer_modified": False,
        },
        "summary": {
            "suggested": 1,
            "needs_review": 0,
            "possible_false_friend": 0,
            "no_confident_target": 1,
            "alias_based": 0,
            "semantic_based": 1,
            "target_coverage": 0.0909,
        },
        "mappings": [
            {
                "source_field": "legacy_client_id",
                "status": "suggested",
                "recommendation": "customer.customer_id",
                "confidence": 0.91,
                "band": "high",
                "mapping_basis": "semantic",
                "source_profile": {
                    "name": "legacy_client_id",
                    "inferred_kind": "string",
                    "missing_ratio": 0.0,
                    "distinct_ratio": 1.0,
                    "observed_max_length": 3,
                    "sample_values": [SENTINEL],
                },
                "review_reasons": [],
                "top_candidates": [
                    {
                        "target": "customer.customer_id",
                        "rank": 1,
                        "score": 0.91,
                        "semantic_score": 0.8,
                        "fuzzy_score": 0.7,
                        "alias_hit": False,
                        "lexical_overlap": 0.5,
                        "type_gate": 1.0,
                        "value_pattern_evidence": ["identifier_like"],
                        "resource_context_evidence": ["near_customer_name"],
                        "activated_interactions": ["institutional_key_support"],
                        "interaction_evidence": [{"interaction_id": "institutional_key_support", "tier": "supportive"}],
                        "diagnostic_bonus": 0.0,
                        "supportive_bonus": 0.01,
                        "top1_selection_reason": "v3_top1_locked_no_diagnostic_challenger",
                        "warnings": [],
                        "raw_value": SENTINEL,
                    }
                ],
            }
        ],
        "unmapped_target_fields": ["customer.customer_name"],
    }


class MappingJobsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_root = mapping_jobs.RUNTIME_ROOT
        self._original_lock = mapping_jobs.JOB_LOCK
        self._temp_dir = tempfile.TemporaryDirectory(dir=mapping_jobs.PROJECT_ROOT)
        mapping_jobs.RUNTIME_ROOT = Path(self._temp_dir.name) / "mapping_jobs"
        mapping_jobs.JOB_LOCK = threading.Lock()
        self.client = TestClient(app)
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        mapping_jobs.RUNTIME_ROOT = self._original_root
        mapping_jobs.JOB_LOCK = self._original_lock
        self._temp_dir.cleanup()

    def _executor(self, contract, source_path, *, scorer_id, model_name=None, embedding_backend=None):
        self.calls.append(
            {
                "contract_id": contract.contract_id,
                "source_path": Path(source_path),
                "scorer_id": scorer_id,
                "model_name": model_name,
            }
        )
        return _fake_report(scorer_id)

    def _create(self, scorer: str = "baseline", csv_text: str | None = None, filename: str = "customers.csv"):
        with patch.object(mapping_jobs, "suggest_runtime_contract_mappings", self._executor):
            return self.client.post("/api/mapping/jobs", json=_payload(scorer, csv_text, filename))

    def test_01_contract_catalog_contains_registered_contracts_without_paths(self):
        response = self.client.get("/api/mapping/contracts")
        self.assertEqual(response.status_code, 200)
        contracts = response.json()["contracts"]
        self.assertEqual({item["contract_id"] for item in contracts}, {"generic-customer", "supplier-reference", "erpnext-item-price"})
        text = json.dumps(contracts)
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]|/tmp/|ground_truth|sample")
        for item in contracts:
            self.assertIn("precision_tiered_v4", item["supported_scorers"])
            self.assertGreater(item["target_resource_count"], 0)
            self.assertGreater(item["target_field_count"], 0)

    def test_02_create_baseline_job_persists_files_and_filters_response(self):
        response = self._create()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        job_id = body["job"]["job_id"]
        self.assertRegex(job_id, r"^[0-9a-f]{32}$")
        job_dir = mapping_jobs.RUNTIME_ROOT / job_id
        self.assertTrue((job_dir / "source.csv").is_file())
        self.assertTrue((job_dir / "mapping_report.json").is_file())
        self.assertTrue((job_dir / "job.json").is_file())
        self.assertEqual((job_dir / "source.csv").read_text(encoding="utf-8"), _payload()["csv_text"])
        self.assertEqual(body["job"]["status"], "completed")
        self.assertEqual(body["job"]["original_filename"], "customers.csv")
        self.assertEqual(body["job"]["contract_registry_id"], "generic-customer")
        self.assertEqual(body["job"]["scorer"], "baseline")
        self.assertEqual(body["job"]["source"]["row_count"], 1)
        self.assertEqual(body["job"]["source"]["field_count"], 2)
        self.assertEqual(body["job"]["mapping_report"]["content_sha256"], "baseline-content-sha")
        self.assertEqual(self.calls[0]["contract_id"], "generic-customer-v1")
        self.assertEqual(self.calls[0]["scorer_id"], "baseline")
        self.assertTrue(self.calls[0]["source_path"].is_file())
        self.assert_no_private_response_data(body)

    def test_03_create_v4_job_preserves_interaction_evidence(self):
        response = self._create("precision_tiered_v4")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["job"]["scorer"], "precision_tiered_v4")
        candidate = body["mappings"][0]["top_candidates"][0]
        self.assertEqual(candidate["activated_interactions"], ["institutional_key_support"])
        self.assertEqual(candidate["interaction_evidence"][0]["tier"], "supportive")
        self.assertIn("top1_selection_reason", candidate)

    def test_04_get_returns_same_business_content_without_rerunning_scorer(self):
        created = self._create("precision_tiered_v4").json()
        self.calls.clear()
        response = self.client.get(f"/api/mapping/jobs/{created['job']['job_id']}")
        self.assertEqual(response.status_code, 200)
        fetched = response.json()
        self.assertEqual(fetched["job"], created["job"])
        self.assertEqual(fetched["summary"], created["summary"])
        self.assertEqual(fetched["mappings"], created["mappings"])
        self.assertEqual(self.calls, [])

    def test_05_unknown_contract_returns_404(self):
        response = self.client.post("/api/mapping/jobs", json={**_payload(), "contract_id": "nope"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error"], "unknown_mapping_contract")

    def test_06_invalid_scorer_returns_422_before_dispatch(self):
        response = self.client.post("/api/mapping/jobs", json={**_payload(), "scorer": "future"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_07_rejects_bad_filenames(self):
        for filename in ("../x.csv", "nested/x.csv", "nested\\x.csv", "x.txt", "", "a" * 129 + ".csv"):
            with self.subTest(filename=filename):
                response = self.client.post("/api/mapping/jobs", json=_payload(filename=filename))
                self.assertEqual(response.status_code, 422)

    def test_08_rejects_empty_duplicate_ragged_and_url_csv(self):
        cases = [
            ("", 422),
            ("a,a\n1,2\n", 422),
            ("a,b\n1\n", 422),
            ("https://example.invalid/source.csv", 422),
        ]
        for csv_text, expected in cases:
            with self.subTest(csv_text=csv_text):
                response = self.client.post("/api/mapping/jobs", json=_payload(csv_text=csv_text))
                self.assertEqual(response.status_code, expected)

    def test_09_rejects_oversize_overrow_and_overcolumn_csv(self):
        oversize = "a\n" + ("x" * (1024 * 1024))
        over_rows = "a\n" + "\n".join("1" for _ in range(10001)) + "\n"
        over_columns = ",".join(f"c{i}" for i in range(201)) + "\n" + ",".join("1" for _ in range(201)) + "\n"
        for csv_text in (oversize, over_rows, over_columns):
            with self.subTest(length=len(csv_text)):
                response = self.client.post("/api/mapping/jobs", json=_payload(csv_text=csv_text))
                self.assertIn(response.status_code, {413, 422})

    def test_10_invalid_and_missing_job_ids(self):
        invalid = self.client.get("/api/mapping/jobs/not-a-hex")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"]["error"], "invalid_mapping_job_id")

        missing = self.client.get("/api/mapping/jobs/" + "0" * 32)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["error"], "mapping_job_not_found")

    def test_11_corrupted_metadata_or_report_returns_safe_500(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        (mapping_jobs.RUNTIME_ROOT / job_id / "job.json").write_text("{bad", encoding="utf-8")
        response = self.client.get(f"/api/mapping/jobs/{job_id}")
        self.assertEqual(response.status_code, 500)
        text = json.dumps(response.json())
        self.assertIn("malformed_mapping_job", text)
        self.assertNotIn(str(mapping_jobs.RUNTIME_ROOT), text)

        (mapping_jobs.RUNTIME_ROOT / job_id / "job.json").write_text(json.dumps(created["job"]), encoding="utf-8")
        (mapping_jobs.RUNTIME_ROOT / job_id / "mapping_report.json").write_text("{bad", encoding="utf-8")
        response = self.client.get(f"/api/mapping/jobs/{job_id}")
        self.assertEqual(response.status_code, 500)
        text = json.dumps(response.json())
        self.assertIn("malformed_mapping_report", text)
        self.assertNotIn(str(mapping_jobs.RUNTIME_ROOT), text)

    def test_12_lock_busy_returns_409(self):
        self.assertTrue(mapping_jobs.JOB_LOCK.acquire(blocking=False))
        try:
            response = self.client.post("/api/mapping/jobs", json=_payload())
        finally:
            mapping_jobs.JOB_LOCK.release()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"], "mapping_job_in_progress")

    def test_13_failed_scorer_cleans_incomplete_job_directory(self):
        def fail(*args, **kwargs):
            raise MappingModelError("offline")

        with patch.object(mapping_jobs, "suggest_runtime_contract_mappings", fail):
            response = self.client.post("/api/mapping/jobs", json=_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error"], "mapping_model_unavailable")
        self.assertFalse(mapping_jobs.RUNTIME_ROOT.exists() and any(mapping_jobs.RUNTIME_ROOT.iterdir()))

    def test_14_profiler_errors_return_422_and_clean_job_directory(self):
        def fail(*args, **kwargs):
            raise SourceProfileError("duplicate_columns", "duplicate")

        with patch.object(mapping_jobs, "suggest_runtime_contract_mappings", fail):
            response = self.client.post("/api/mapping/jobs", json=_payload())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["error"], "duplicate_columns")
        self.assertFalse(mapping_jobs.RUNTIME_ROOT.exists() and any(mapping_jobs.RUNTIME_ROOT.iterdir()))

    def test_15_existing_static_migration_workspace_endpoint_still_works(self):
        response = self.client.get("/api/migration/workspaces/erpnext-item-price")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspace"]["workspace_id"], "erpnext-item-price")

    def test_16_importing_backend_app_does_not_instantiate_sentence_transformer(self):
        text = Path("backend/mapping_jobs.py").read_text(encoding="utf-8")
        self.assertNotIn("SentenceTransformer(", text)
        self.assertNotIn("load_embedding_backend(", text)

    def assert_no_private_response_data(self, body: dict) -> None:
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("csv_text", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]|/tmp/")
        self.assertNotIn("ground_truth", text)
        self.assertNotIn("expected_targets", text)
        self.assertNotIn("answer_source", text)
        self.assertNotIn("raw_value", text)


if __name__ == "__main__":
    unittest.main()
