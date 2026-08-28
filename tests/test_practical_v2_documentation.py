from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def read_json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class PracticalV2DocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = read_text("README.md")
        cls.readme_lower = cls.readme.lower()
        cls.demo = read_text("docs/practical-v2-demo.md")
        cls.demo_lower = cls.demo.lower()
        cls.guide = read_text("docs/practical-v2-review-guide.md")
        cls.schema_mapping = read_text("docs/schema-mapping.md")
        cls.schema_mapping_lower = cls.schema_mapping.lower()
        cls.customer_review_csv = read_text("examples/schema-matching/customer-review-demo.csv")
        cls.script = read_text("scripts/verify_practical_v2.ps1")
        cls.script_lower = cls.script.lower()
        cls.docs_combined = "\n".join([cls.readme, cls.demo, cls.guide, cls.schema_mapping, cls.script])
        cls.docs_lower = cls.docs_combined.lower()

    def test_01_readme_has_new_main_sections(self) -> None:
        for heading in [
            "## What It Does",
            "## Main Workflows",
            "## Migration Workflow",
            "## Migration Review Workspace",
            "## Blind Benchmark",
            "## Project Structure",
            "## Quick Start",
            "## Run The Verification Suite",
            "## Results",
            "## Design Choices",
            "## Known Limitations",
        ]:
            self.assertIn(heading, self.readme)

    def test_02_readme_omits_forbidden_vendor_terms(self) -> None:
        for term in ["SAP", "S/4HANA", "Business Partner", "SAP SE", "A2X", "Fiori", "ABAP"]:
            self.assertNotIn(term.lower(), self.readme_lower)

    def test_03_readme_describes_general_project_positioning(self) -> None:
        self.assertIn("enterprise carve-out", self.readme)
        self.assertIn("ERP rebuild", self.readme)
        self.assertIn("data migration", self.readme)
        self.assertIn("All examples use synthetic data.", self.readme)

    def test_04_readme_keeps_actual_workflow_capabilities(self) -> None:
        for phrase in [
            "Profiles source data.",
            "Validates target contracts.",
            "Suggests Top-3 field mappings.",
            "Records human mapping decisions.",
            "Generates target CSV resources.",
            "Stores cell-level lineage.",
        ]:
            self.assertIn(phrase, self.readme)

    def test_05_readme_contains_three_workflows(self) -> None:
        self.assertIn("### Data Migration", self.readme)
        self.assertIn("### Fit-to-Standard", self.readme)
        self.assertIn("### Cutover And RAID", self.readme)

    def test_06_readme_contains_simplified_mermaid_flow(self) -> None:
        self.assertIn("flowchart LR", self.readme)
        for node in ["Source CSV", "Source Profile", "Target Contract", "Mapping Candidates", "Human Review", "Package Builder", "Validation"]:
            self.assertIn(node, self.readme)

    def test_07_readme_states_human_review_is_required(self) -> None:
        self.assertIn("The reviewer decides what should be approved, rejected, or deferred.", self.readme)
        self.assertIn("The builder executes the approved result.", self.readme)

    def test_08_readme_explains_lineage_hashes(self) -> None:
        self.assertIn("Lineage records the source field and a SHA for the source value.", self.readme)
        self.assertIn("not complete source values", self.readme)

    def test_09_readme_contains_fixed_workspace_id(self) -> None:
        self.assertIn("workspace_id = erpnext-item-price", self.readme)

    def test_10_readme_contains_workspace_actions(self) -> None:
        for phrase in ["View Top-3 candidates.", "Approve one or more targets.", "Reject or defer a source field.", "Save a Runtime Decision.", "Preview generated resources.", "Reset local runtime state."]:
            self.assertIn(phrase, self.readme)

    def test_11_readme_contains_runtime_boundaries(self) -> None:
        self.assertIn("Workspace paths come from a fixed registry.", self.readme)
        self.assertIn("data/runtime/", self.readme)
        self.assertIn("ignored by Git", self.readme)
        self.assertIn("process-local", self.readme_lower)

    def test_12_readme_records_blind_benchmark_numbers(self) -> None:
        for phrase in [
            "Domain: Item + Item Price",
            "Contract aliases: 0",
            "Source fields: 10",
            "Expected target links: 11",
            "Source Top-1 accuracy: 0.2222",
            "Top-3 target-link recall: 1.0000",
            "Multi-target full Top-3 coverage: 1.0000",
            "No-target accuracy: 1.0000",
            "High-confidence predictions: 0",
        ]:
            self.assertIn(phrase, self.readme)

    def test_13_readme_does_not_claim_full_mapping_accuracy(self) -> None:
        forbidden = ["mapping accuracy = 100%", "mapping accuracy 100%", "automatic mapping success = 100%"]
        for phrase in forbidden:
            self.assertNotIn(phrase, self.readme_lower)
        self.assertIn("correct target ranked first for only 2 of 9 source fields that had a target", self.readme)

    def test_14_readme_records_multi_target_results(self) -> None:
        for phrase in [
            "Approved links: 11",
            "Unique approved source fields: 9",
            "Multi-target source fields: 2",
            "item.csv: 8 rows x 5 fields",
            "item_price.csv: 8 rows x 6 fields",
            "Validation: valid",
            "Findings: 0",
            "Lineage entries: 88",
        ]:
            self.assertIn(phrase, self.readme)

    def test_15_readme_records_multi_target_fields(self) -> None:
        for phrase in ["article_number", "item.item_code", "item_price.item_code", "inventory_measure", "item.stock_uom", "item_price.uom"]:
            self.assertIn(phrase, self.readme)
        self.assertIn("approved by a reviewer from candidate lists", self.readme)
        self.assertIn("engine did not decide multi-target intent by itself", self.readme)

    def test_16_project_structure_is_compact(self) -> None:
        for path in ["backend/", "frontend/", "contracts/", "data/examples/", "data/generated/", "data/runtime/", "data/synthetic/", "docs/", "scripts/", "src/core/", "src/tools/", "tests/"]:
            self.assertIn(path, self.readme)

    def test_17_quick_start_commands_are_present(self) -> None:
        for command in [
            "python -m pip install -r requirements.txt",
            "python -m pip install -r backend/requirements.txt",
            "npm install",
            "uvicorn backend.main:app --reload --port 8001",
            '$env:VITE_API_BASE="http://127.0.0.1:8001"',
            "npm run dev",
            "http://127.0.0.1:5173/",
        ]:
            self.assertIn(command, self.readme)

    def test_18_quick_start_names_default_page(self) -> None:
        self.assertIn("迁移工作台", self.readme)

    def test_19_demo_does_not_require_llm_credential(self) -> None:
        self.assertIn("does not require an LLM credential", self.readme)
        self.assertIn("does not need the Fit-to-Standard report to be regenerated", self.readme)
        self.assertIn("No LLM credential is required.", self.demo)
        self.assertNotIn("llm credential required", self.demo_lower)

    def test_20_verification_command_and_counts_are_present(self) -> None:
        self.assertIn("powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify_practical_v2.ps1", self.readme)
        self.assertIn("Scoped Migration/Cutover tests: 238 passed", self.readme)
        self.assertIn("Full unittest discovery:", self.readme)
        self.assertIn("0 failures, 0 errors", self.readme)
        self.assertNotIn("27 failures", self.readme)
        self.assertNotIn("33 errors", self.readme)
        self.assertNotIn("483 tests passed", self.readme)
        self.assertIn("Frontend tests: 88", self.readme)
        self.assertIn("Workspace API tests: 38", self.readme)
        self.assertIn("Unable to parse full Python test count", self.script)
        self.assertIn("Python tests: $fullPythonCount", self.script)

    def test_20b_readme_records_hash_modes_and_effective_lock_boundary(self) -> None:
        for phrase in [
            "Raw-file SHA256",
            "normalized_text_sha256_v1",
            "Canonical JSON content SHA256",
            "source_hash_mode: normalized_text_sha256_v1",
            "blind_protocol_compatibility_amendment_v1.json",
            "authorizes only the provenance-only `src/core/mapping/profiler.py` normalization change",
            "`src/core/mapping/engine.py` metadata propagation change",
            "does not claim the current profiler or engine was part of the original locked-before-first-mapping engine",
        ]:
            self.assertIn(phrase, self.readme)

    def test_20c_readme_indexes_algorithm_ml_evidence_without_overclaiming(self) -> None:
        for phrase in [
            "## Algorithm/ML Evidence",
            "schema_matching_public_dev_v1.json",
            "Open Food Facts",
            "UK Contracts Finder",
            "combined public development results",
            "35 cases and 25 expected links",
            "same aggregate metrics",
            "not a V5 win claim",
            "pairwise LTR v1 experiment",
            "negative ML result",
            "did not beat V5",
            "V5 correctness calibration experiment",
            "OOF predictions",
            "failure analysis",
            "Existing V5 policy accepted 23/107 cases with 23 correct and 0 incorrect",
            "score-only 95% development policy accepted 51/107 cases with 50 correct and 1 incorrect",
            "outperforming the multifeature calibrator",
            "contract-family-out development evidence",
            "not sealed holdout evidence",
            "no runtime promotion has been made",
            "FDIC sealed banking evidence",
            "first sealed result",
            "Baseline, V4, and V5 ranking all scored Top-1 2/3",
            "accepted 0/14 cases",
            "all 11 no-target cases were safely rejected",
            "development coverage improvement did not reproduce",
            "negative evidence, not a production promotion",
        ]:
            self.assertIn(phrase, self.readme)

    def test_21_results_keep_migration_numbers(self) -> None:
        for phrase in ["Target package: valid", "Findings: 0", "Lineage entries: 88"]:
            self.assertIn(phrase, self.readme)

    def test_22_results_keep_fit_to_standard_numbers(self) -> None:
        for phrase in [
            "Ground truth requirements = 23",
            "Extracted requirements = 24",
            "Matched = 21",
            "Spurious = 3",
            "Missed = 2",
            "Strict Precision = 0.8750",
            "Strict Recall = 0.9130",
            "Strict F1 = 0.8936",
            "Matched-only classification accuracy = 0.9524",
            "Development Backlog = 5",
            "needs_review = 2",
        ]:
            self.assertIn(phrase, self.readme)

    def test_23_results_keep_cutover_numbers(self) -> None:
        for phrase in [
            "Migration findings = 22",
            "Migration RAID = 22",
            "Migration Activities = 4",
            "Activities = 34",
            "Completed = 17",
            "Blocked = 2",
            "Not Started = 15",
            "Work packages = 5",
            "Freeze windows = 3",
            "Approval gates = 4",
            "RAID items = 29",
            "Migration blockers = 1",
        ]:
            self.assertIn(phrase, self.readme)
        self.assertIn("one explicit High, non-review-only migration blocker", self.readme)
        self.assertIn("This is a synthetic demo result", self.readme)
        self.assertNotIn("Activities = 30", self.readme)
        self.assertNotIn("RAID items = 7", self.readme)

    def test_23b_readme_records_migration_cutover_pipeline(self) -> None:
        for phrase in [
            "migration_cutover_findings.json",
            "validation report, duplicate report, field mapping report, and generated package validation report",
            "Blind benchmark accuracy, precision/recall/F1, ground truth",
            "do not become automatic go-live blockers",
            "_meta.source_plan_content_sha256",
            "migration findings, plan, status, daily",
            "Ordinary commit failures trigger rollback",
            "does not promise cross-file recovery during process termination, system crash, or power loss",
            "`11/11` available",
        ]:
            self.assertIn(phrase, self.readme)

    def test_24_design_choices_are_limited_and_concrete(self) -> None:
        self.assertEqual(self.readme.count("Human approval stays between mapping suggestions and package generation."), 1)
        self.assertIn("Generated data is validated with the same contract used to define the target.", self.readme)
        self.assertIn("Runtime workspace files do not modify committed examples.", self.readme)

    def test_25_known_limitations_are_present(self) -> None:
        for phrase in [
            "Only one workspace is registered.",
            "There is no upload flow.",
            "There is no dynamic contract registration.",
            "There is no database.",
            "There are no user accounts or reviewer identities.",
            "The build lock is process-local only.",
            "Multi-target execution needs human approval.",
            "Reference contracts are educational snapshots.",
            "All business data is synthetic.",
            "There is no real ERP connection.",
            "First uncached LLM analysis may require network access.",
            "not a replacement for commercial migration tooling",
        ]:
            self.assertIn(phrase, self.readme)

    def test_26_readme_does_not_claim_universal_or_production_capability(self) -> None:
        for phrase in ["completely vendor-neutral", "universal ERP standards", "supports every ERP platform", "production-ready", "automatically solves migration mapping", "automatic multi-target detection", "authoritative contract", "LLM generates final migration packages"]:
            self.assertNotIn(phrase, self.readme_lower)

    def test_27_documentation_files_exist(self) -> None:
        self.assertTrue((ROOT / "docs/practical-v2-demo.md").is_file())
        self.assertTrue((ROOT / "docs/practical-v2-review-guide.md").is_file())
        self.assertTrue((ROOT / "docs/schema-mapping.md").is_file())
        self.assertTrue((ROOT / "examples/schema-matching/customer-review-demo.csv").is_file())
        self.assertTrue((ROOT / "scripts/verify_practical_v2.ps1").is_file())

    def test_28_reviewer_guide_records_current_report_shas(self) -> None:
        expected = [
            read_json("data/synthetic/erpnext_item_price_blind_mapping.json")["_run_info"]["content_sha256"],
            read_json("data/synthetic/erpnext_item_price_blind_evaluation.json")["_run_info"]["content_sha256"],
            read_json("data/generated/erpnext_item_price_multitarget/package_manifest.json")["_run_info"]["content_sha256"],
            read_json("data/synthetic/erpnext_item_price_multitarget_generated_validation.json")["_run_info"]["content_sha256"],
        ]
        for sha in expected:
            self.assertIn(sha, self.guide)
        self.assertIn("Protocol Lock", self.guide)

    def test_29_docs_contain_no_local_absolute_paths(self) -> None:
        self.assertIsNone(re.search(r"[A-Z]:\\\\", self.docs_combined))
        self.assertNotIn("/" + "home/", self.docs_combined)

    def test_30_docs_contain_no_real_secret_markers(self) -> None:
        forbidden = ["Be" + "arer", "Authorization" + ":", "s" + "k-" + "live", "s" + "k-" + "proj"]
        for marker in forbidden:
            self.assertNotIn(marker, self.docs_combined)

    def test_31_verification_script_does_not_call_remote_fetch_tools(self) -> None:
        for marker in ["invoke-webrequest", "invoke-restmethod", "curl ", "wget ", "fetch("]:
            self.assertNotIn(marker, self.script_lower)

    def test_32_verification_script_does_not_start_servers(self) -> None:
        for marker in ["uvicorn", "npm run dev", "vite --host"]:
            self.assertNotIn(marker, self.script_lower)

    def test_33_verification_script_does_not_install_dependencies(self) -> None:
        for marker in ["pip install", "npm install", "npm ci"]:
            self.assertNotIn(marker, self.script_lower)

    def test_34_verification_script_cleans_runtime_and_frontend_dist(self) -> None:
        self.assertIn('Remove-TreeIfPresent "data/runtime"', self.script)
        self.assertIn('Remove-TreeIfPresent "frontend/dist"', self.script)

    def test_35_schema_mapping_docs_record_runtime_contract_and_metrics(self) -> None:
        self.assertIn("## Dynamic Schema Mapping", self.readme + self.schema_mapping)
        for phrase in [
            "GET  /api/mapping/contracts",
            "POST /api/mapping/jobs",
            "GET  /api/mapping/jobs/{job_id}",
            "PUT  /api/mapping/jobs/{job_id}/review",
            "GET  /api/mapping/jobs/{job_id}/export?format=json|csv",
            "generic-customer",
            "customer.customer_id",
            "full 11-field contract target_fields",
            "precision_tiered_v4",
            "precision_tiered_interaction_v1",
            "precision_tiered_v5",
            "entity_identifier_interaction_v1",
            "data/runtime/",
            "新建字段映射",
            "examples/schema-matching/customer-review-demo.csv",
        ]:
            self.assertIn(phrase, self.schema_mapping)
        for phrase in [
            "5 scenarios",
            "72 cases",
            "59 single-target",
            "5 multi-target",
            "8 no-target",
            "70 target links",
            "Top-1 accuracy: 0.9322",
            "target recall@1: 0.8429",
            "target recall@3: 0.9857",
            "MRR: 0.9095",
            "no-target accuracy: 0.8750",
            "multi-target full recall@3: 1.0000",
            "49a420b69a2e7c77e15f607bfc1353b15c2bbd7b3bb14da895cbadd76acd4d8b",
            "f44d07567ed9fa8b199780fff9990d1b577491709433ed554c7140f552386e57",
        ]:
            self.assertIn(phrase, self.schema_mapping)

    def test_36_schema_mapping_docs_keep_ground_truth_and_review_boundaries(self) -> None:
        for phrase in [
            "Ground truth is used only by benchmark evaluation.",
            "Candidate generation, feature extraction, scoring, ranking, API job creation, review saving, and export do not read answer files.",
            "Top-3 candidates remain visible",
            "Manual target selection uses the full contract `target_fields` allowlist instead of the Top-3 list",
            "Human review corrects and freezes a job result; it does not retrain the mapping model.",
            "This example is not used to tune aliases or hard-code a special case.",
        ]:
            self.assertIn(phrase, self.docs_combined)
        for forbidden in [
            "fully autonomous",
            "AI-powered platform",
            "automatically solves migration mapping",
            "human review retrains",
        ]:
            self.assertNotIn(forbidden.lower(), self.docs_lower)

    def test_37_schema_mapping_mermaid_flow_is_basic_and_linear(self) -> None:
        self.assertIn("```mermaid", self.schema_mapping)
        self.assertIn("flowchart LR", self.schema_mapping)
        for node in [
            "CSV",
            "Profiling",
            "Embedding and candidate scoring",
            "Confidence tier",
            "Human review",
            "Final export",
        ]:
            self.assertIn(node, self.schema_mapping)

    def test_38_customer_review_demo_csv_is_synthetic_and_parseable(self) -> None:
        import csv
        from io import StringIO

        rows = list(csv.reader(StringIO(self.customer_review_csv)))
        self.assertEqual(
            rows[0],
            ["client_number", "full_name", "email_address", "mobile_phone", "country_code", "zip_code"],
        )
        self.assertEqual(len(rows) - 1, 5)
        self.assertEqual(len(set(rows[0])), 6)
        self.assertEqual({len(row) for row in rows}, {6})
        self.assertTrue(self.customer_review_csv.endswith("\n"))
        csv_lower = self.customer_review_csv.lower()
        for forbidden in ["ground_truth", "expected_target", "answer_source", "customer.customer_id"]:
            self.assertNotIn(forbidden, csv_lower)
        for row in rows[1:]:
            for value in row:
                self.assertFalse(value.startswith(("=", "+", "-", "@")))
        for marker in ["john", "jane", "example.com", "555-"]:
            self.assertNotIn(marker, csv_lower)
