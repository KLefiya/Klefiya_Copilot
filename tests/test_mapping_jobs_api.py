from __future__ import annotations

import csv
import json
import shutil
import tempfile
import threading
import unittest
from io import StringIO
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
            "feature_version": (
                "precision_tiered_interaction_v1"
                if scorer == "precision_tiered_v4"
                else "entity_identifier_interaction_v1"
                if scorer == "precision_tiered_v5"
                else None
            ),
            "ground_truth_used": False,
            "ground_truth_used_for_candidate_generation": False,
            "experimental": scorer in {"precision_tiered_v4", "precision_tiered_v5"},
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
                        **(
                            {
                                "v4_score": 0.73,
                                "identifier_bonus": 0.14,
                                "identifier_adjusted_score": 0.87,
                                "v5_top1_eligible": True,
                                "v5_top1_selection_reason": "identifier_adjusted_score_strictly_exceeded_v4_top1",
                                "identifier_interaction_evidence": [
                                    {
                                        "interaction_id": "entity_identifier_support",
                                        "tier": "entity_identifier",
                                        "source_concepts": ["client", "identifier"],
                                        "target_concepts": ["customer", "identifier"],
                                        "matched_entity_concepts": ["customer"],
                                        "bonus_weight": 0.4,
                                        "bonus": 0.14,
                                        "may_displace_v4_top1": True,
                                        "_internal_rank_key": "secret",
                                        "expected_targets": ["customer.customer_id"],
                                        "sample_values": [SENTINEL],
                                        "case_id": "fixture-case",
                                    }
                                ],
                                "_private_sort_key": "secret",
                            }
                            if scorer == "precision_tiered_v5"
                            else {}
                        ),
                        "warnings": [],
                        "raw_value": SENTINEL,
                    }
                ],
            },
            {
                "source_field": "client_name",
                "status": "no_confident_target",
                "recommendation": None,
                "confidence": 0.0,
                "band": "low",
                "mapping_basis": "none",
                "source_profile": {
                    "name": "client_name",
                    "inferred_kind": "string",
                    "missing_ratio": 0.0,
                    "distinct_ratio": 1.0,
                    "observed_max_length": 18,
                    "sample_values": [SENTINEL],
                },
                "review_reasons": ["best_score_below_threshold"],
                "top_candidates": [
                    {
                        "target": "customer.customer_name",
                        "rank": 1,
                        "score": 0.39,
                        "semantic_score": 0.4,
                        "fuzzy_score": 0.5,
                        "alias_hit": False,
                        "lexical_overlap": 0.0,
                        "type_gate": 1.0,
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
            self.assertIn("contract_id", item)
            self.assertIn("title", item)
            self.assertIn("domain", item)
            self.assertIn("version", item)
            self.assertIn("target_resource_count", item)
            self.assertIn("target_field_count", item)
            self.assertIn("target_fields", item)
            self.assertIn("supported_scorers", item)
            self.assertIn("precision_tiered_v4", item["supported_scorers"])
            self.assertIn("precision_tiered_v5", item["supported_scorers"])
            self.assertEqual(set(item["supported_scorers"]), {"baseline", "precision_tiered_v4", "precision_tiered_v5"})
            self.assertGreater(item["target_resource_count"], 0)
            self.assertGreater(item["target_field_count"], 0)
            self.assertEqual(len(item["target_fields"]), item["target_field_count"])
            self.assertEqual(len(item["target_fields"]), len(set(item["target_fields"])))
            self.assertTrue(all(isinstance(target, str) and "." in target for target in item["target_fields"]))
        second = self.client.get("/api/mapping/contracts").json()["contracts"]
        self.assertEqual(contracts, second)
        by_id = {item["contract_id"]: item for item in contracts}
        self.assertIn("customer.customer_id", by_id["generic-customer"]["target_fields"])

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

    def test_03b_create_v5_job_preserves_safe_identifier_evidence(self):
        response = self._create("precision_tiered_v5")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["job"]["scorer"], "precision_tiered_v5")
        self.assertEqual(body["job"]["mapping_report"]["content_sha256"], "precision_tiered_v5-content-sha")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["scorer_id"], "precision_tiered_v5")

        candidate = body["mappings"][0]["top_candidates"][0]
        self.assertEqual(candidate["v4_score"], 0.73)
        self.assertEqual(candidate["identifier_bonus"], 0.14)
        self.assertEqual(candidate["identifier_adjusted_score"], 0.87)
        self.assertTrue(candidate["v5_top1_eligible"])
        self.assertEqual(candidate["v5_top1_selection_reason"], "identifier_adjusted_score_strictly_exceeded_v4_top1")
        self.assertEqual(
            candidate["identifier_interaction_evidence"],
            [
                {
                    "interaction_id": "entity_identifier_support",
                    "tier": "entity_identifier",
                    "source_concepts": ["client", "identifier"],
                    "target_concepts": ["customer", "identifier"],
                    "matched_entity_concepts": ["customer"],
                    "bonus_weight": 0.4,
                    "bonus": 0.14,
                    "may_displace_v4_top1": True,
                }
            ],
        )
        self.assert_no_private_response_data(body)

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

    def test_04b_get_returns_v5_job_without_rerunning_scorer(self):
        created = self._create("precision_tiered_v5").json()
        self.calls.clear()
        response = self.client.get(f"/api/mapping/jobs/{created['job']['job_id']}")
        self.assertEqual(response.status_code, 200)
        fetched = response.json()
        self.assertEqual(fetched["job"]["scorer"], "precision_tiered_v5")
        self.assertEqual(fetched["job"], created["job"])
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

    def test_17_review_partial_snapshot_is_persisted_and_returned_by_get(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        response = self.client.put(
            f"/api/mapping/jobs/{job_id}/review",
            json={
                "mapping_report_sha256": "baseline-content-sha",
                "decisions": [
                    {"source_field": "legacy_client_id", "action": "accept_suggestion", "note": "Confirmed by reviewer"}
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        review = response.json()["review"]
        self.assertEqual(review["reviewed_fields"], 1)
        self.assertEqual(review["total_fields"], 2)
        self.assertEqual(review["pending_fields"], 1)
        self.assertFalse(review["export_ready"])
        review_path = mapping_jobs.RUNTIME_ROOT / job_id / "review.json"
        self.assertTrue(review_path.is_file())
        stored = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["decisions"][0]["target_fields"], ["customer.customer_id"])

        fetched = self.client.get(f"/api/mapping/jobs/{job_id}").json()
        self.assertEqual(fetched["review"], review)
        self.assert_no_private_response_data(fetched)

    def test_18_review_repeated_put_is_idempotent(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        payload = {
            "mapping_report_sha256": "baseline-content-sha",
            "decisions": [
                {"source_field": "legacy_client_id", "action": "accept_suggestion"},
            ],
        }
        first = self.client.put(f"/api/mapping/jobs/{job_id}/review", json=payload)
        second = self.client.put(f"/api/mapping/jobs/{job_id}/review", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())

    def test_19_review_accept_override_multi_target_and_mark_unmapped(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        response = self.client.put(
            f"/api/mapping/jobs/{job_id}/review",
            json={
                "mapping_report_sha256": "baseline-content-sha",
                "decisions": [
                    {
                        "source_field": "legacy_client_id",
                        "action": "select_target",
                        "target_fields": ["customer.customer_id", "customer.customer_id", "customer_bank.customer_id"],
                        "note": "Manual multi-target correction",
                    },
                    {"source_field": "client_name", "action": "mark_unmapped"},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        review = response.json()["review"]
        self.assertTrue(review["export_ready"])
        self.assertEqual(review["overridden_count"], 1)
        self.assertEqual(review["unmapped_count"], 1)
        self.assertEqual(
            review["decisions"][0]["target_fields"],
            ["customer.customer_id", "customer_bank.customer_id"],
        )

    def test_20_review_validation_rejects_bad_source_target_stale_and_action_contracts(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        cases = [
            (
                {
                    "mapping_report_sha256": "wrong",
                    "decisions": [{"source_field": "legacy_client_id", "action": "accept_suggestion"}],
                },
                409,
                "mapping_review_stale",
            ),
            (
                {
                    "mapping_report_sha256": "baseline-content-sha",
                    "decisions": [{"source_field": "missing", "action": "accept_suggestion"}],
                },
                422,
                "unknown_mapping_source_field",
            ),
            (
                {
                    "mapping_report_sha256": "baseline-content-sha",
                    "decisions": [
                        {"source_field": "legacy_client_id", "action": "accept_suggestion"},
                        {"source_field": "legacy_client_id", "action": "accept_suggestion"},
                    ],
                },
                422,
                "duplicate_mapping_review_source_field",
            ),
            (
                {
                    "mapping_report_sha256": "baseline-content-sha",
                    "decisions": [
                        {"source_field": "legacy_client_id", "action": "select_target", "target_fields": ["customer.nope"]}
                    ],
                },
                422,
                "unknown_mapping_target_field",
            ),
            (
                {
                    "mapping_report_sha256": "baseline-content-sha",
                    "decisions": [{"source_field": "client_name", "action": "accept_suggestion"}],
                },
                422,
                "mapping_suggestion_unavailable",
            ),
            (
                {
                    "mapping_report_sha256": "baseline-content-sha",
                    "decisions": [
                        {
                            "source_field": "client_name",
                            "action": "mark_unmapped",
                            "target_fields": ["customer.customer_name"],
                        }
                    ],
                },
                422,
                "invalid_mapping_review_targets",
            ),
            (
                {
                    "mapping_report_sha256": "baseline-content-sha",
                    "decisions": [
                        {
                            "source_field": "legacy_client_id",
                            "action": "accept_suggestion",
                            "note": "bad\nnote",
                        }
                    ],
                },
                422,
                "invalid_mapping_review_note",
            ),
        ]
        for payload, status, error in cases:
            with self.subTest(error=error):
                response = self.client.put(f"/api/mapping/jobs/{job_id}/review", json=payload)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"]["error"], error)

    def test_21_review_invalid_and_missing_job_ids_and_lock_busy(self):
        invalid = self.client.put(
            "/api/mapping/jobs/not-a-hex/review",
            json={"mapping_report_sha256": "baseline-content-sha", "decisions": []},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["detail"]["error"], "invalid_mapping_job_id")

        missing = self.client.put(
            "/api/mapping/jobs/" + "0" * 32 + "/review",
            json={"mapping_report_sha256": "baseline-content-sha", "decisions": []},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["error"], "mapping_job_not_found")

        created = self._create().json()
        self.assertTrue(mapping_jobs.JOB_LOCK.acquire(blocking=False))
        try:
            busy = self.client.put(
                f"/api/mapping/jobs/{created['job']['job_id']}/review",
                json={"mapping_report_sha256": "baseline-content-sha", "decisions": []},
            )
        finally:
            mapping_jobs.JOB_LOCK.release()
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json()["detail"]["error"], "mapping_job_in_progress")

    def test_22_incomplete_review_refuses_export(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        response = self.client.put(
            f"/api/mapping/jobs/{job_id}/review",
            json={
                "mapping_report_sha256": "baseline-content-sha",
                "decisions": [{"source_field": "legacy_client_id", "action": "accept_suggestion"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        export = self.client.get(f"/api/mapping/jobs/{job_id}/export?format=json")
        self.assertEqual(export.status_code, 409)
        self.assertEqual(export.json()["detail"]["error"], "mapping_review_incomplete")

    def test_23_completed_review_exports_json_without_rerunning_scorer(self):
        created = self._create("precision_tiered_v4").json()
        job_id = created["job"]["job_id"]
        self._complete_review(job_id, sha="precision_tiered_v4-content-sha")
        self.calls.clear()
        response = self.client.get(f"/api/mapping/jobs/{job_id}/export?format=json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-disposition"],
            f'attachment; filename="mapping-review-{job_id}.json"',
        )
        body = response.json()
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["scorer"], "precision_tiered_v4")
        self.assertEqual(body["mapping_report_sha256"], "precision_tiered_v4-content-sha")
        self.assertEqual(len(body["final_mappings"]), 2)
        self.assertEqual(body["final_mappings"][0]["original_recommendation"], "customer.customer_id")
        self.assertEqual(self.calls, [])
        self.assert_no_private_response_data(body)

    def test_23b_completed_review_exports_v5_json_and_csv(self):
        created = self._create("precision_tiered_v5").json()
        job_id = created["job"]["job_id"]
        self._complete_review(job_id, sha="precision_tiered_v5-content-sha")
        self.calls.clear()

        json_response = self.client.get(f"/api/mapping/jobs/{job_id}/export?format=json")
        self.assertEqual(json_response.status_code, 200)
        document = json_response.json()
        self.assertEqual(document["scorer"], "precision_tiered_v5")
        self.assertEqual(document["mapping_report_sha256"], "precision_tiered_v5-content-sha")
        self.assertEqual(self.calls, [])
        self.assert_no_private_response_data(document)

        csv_response = self.client.get(f"/api/mapping/jobs/{job_id}/export?format=csv")
        self.assertEqual(csv_response.status_code, 200)
        rows = list(csv.reader(StringIO(csv_response.text)))
        self.assertEqual(rows[1][4], "precision_tiered_v5")
        self.assertEqual(rows[1][5], "precision_tiered_v5-content-sha")
        self.assert_no_private_response_data({"csv": csv_response.text})

    def test_24_completed_review_exports_csv_with_stable_columns_and_formula_protection(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        self._complete_review(job_id, note="=SUM(A1:A2)")
        response = self.client.get(f"/api/mapping/jobs/{job_id}/export?format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertEqual(
            response.headers["content-disposition"],
            f'attachment; filename="mapping-review-{job_id}.csv"',
        )
        rows = list(csv.reader(StringIO(response.text)))
        self.assertEqual(
            rows[0],
            [
                "job_id",
                "contract_id",
                "contract_title",
                "contract_version",
                "scorer",
                "mapping_report_sha256",
                "review_updated_at",
                "source_field",
                "action",
                "final_target_fields",
                "original_recommendation",
                "original_status",
                "reviewer_note",
            ],
        )
        self.assertEqual(rows[1][-1], "'=SUM(A1:A2)")
        self.assert_no_private_response_data({"csv": response.text})

    def test_25_review_persistence_round_trip_and_export_format_validation(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        self._complete_review(job_id)
        reloaded = self.client.get(f"/api/mapping/jobs/{job_id}").json()
        self.assertTrue(reloaded["review"]["export_ready"])
        self.assertEqual(reloaded["review"]["accepted_count"], 1)
        self.assertEqual(reloaded["review"]["unmapped_count"], 1)

        invalid = self.client.get(f"/api/mapping/jobs/{job_id}/export?format=xlsx")
        self.assertEqual(invalid.status_code, 422)

    def test_26_atomic_review_write_failure_cleans_temp_file(self):
        created = self._create().json()
        job_id = created["job"]["job_id"]
        job_dir = mapping_jobs.RUNTIME_ROOT / job_id
        client = TestClient(app, raise_server_exceptions=False)
        with patch.object(mapping_jobs.os, "replace", side_effect=OSError("disk full")):
            response = client.put(
                f"/api/mapping/jobs/{job_id}/review",
                json={
                    "mapping_report_sha256": "baseline-content-sha",
                    "decisions": [{"source_field": "legacy_client_id", "action": "accept_suggestion"}],
                },
            )
        self.assertEqual(response.status_code, 500)
        self.assertFalse((job_dir / "review.json").exists())
        self.assertEqual(list(job_dir.glob("*.tmp")), [])

    def _complete_review(self, job_id: str, *, sha: str = "baseline-content-sha", note: str = "reviewed"):
        return self.client.put(
            f"/api/mapping/jobs/{job_id}/review",
            json={
                "mapping_report_sha256": sha,
                "decisions": [
                    {"source_field": "legacy_client_id", "action": "accept_suggestion", "note": note},
                    {"source_field": "client_name", "action": "mark_unmapped"},
                ],
            },
        )

    def assert_no_private_response_data(self, body: dict) -> None:
        text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("csv_text", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]|/tmp/")
        self.assertNotIn("ground_truth", text)
        self.assertNotIn("expected_targets", text)
        self.assertNotIn("answer_source", text)
        self.assertNotIn("raw_value", text)
        self.assertNotIn("sample_values", text)
        self.assertNotIn("case_id", text)
        self.assertNotIn("_private_sort_key", text)
        self.assertNotIn("_internal_rank_key", text)


if __name__ == "__main__":
    unittest.main()
