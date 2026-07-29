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
        cls.guide_lower = cls.guide.lower()
        cls.script = read_text("scripts/verify_practical_v2.ps1")
        cls.script_lower = cls.script.lower()
        cls.docs_combined = "\n".join([cls.readme, cls.demo, cls.guide, cls.script])
        cls.docs_lower = cls.docs_combined.lower()

    def test_01_readme_frontend_is_not_described_as_read_only_only(self) -> None:
        self.assertNotIn("前端完全只读", self.readme)
        self.assertIn("Migration Review Workspace", self.readme)

    def test_02_readme_mentions_migration_review_workspace(self) -> None:
        self.assertIn("## Migration Review Workspace", self.readme)

    def test_03_readme_contains_contract_driven_workflow(self) -> None:
        self.assertIn("## practical-v2: Contract-driven Migration Workflow", self.readme)
        self.assertIn("flowchart LR", self.readme)

    def test_04_readme_contains_blind_benchmark(self) -> None:
        self.assertIn("### Frozen-engine blind benchmark", self.readme)

    def test_05_readme_records_top_one_accuracy(self) -> None:
        self.assertIn("Source Top-1 accuracy: 0.2222", self.readme)

    def test_06_readme_records_top_three_recall(self) -> None:
        self.assertIn("Top-3 target-link recall: 1.0000", self.readme)

    def test_07_readme_does_not_claim_full_mapping_accuracy(self) -> None:
        forbidden = "mapping accuracy" + " = " + "100%"
        self.assertNotIn(forbidden, self.readme_lower)

    def test_08_readme_contains_lineage_count(self) -> None:
        self.assertIn("Lineage entries: 88", self.readme)

    def test_09_readme_contains_fixed_workspace_id(self) -> None:
        self.assertIn("workspace_id: erpnext-item-price", self.readme)

    def test_10_readme_contains_process_local_lock_limitation(self) -> None:
        self.assertIn("process-local lock", self.readme_lower)

    def test_11_readme_contains_no_upload_limitation(self) -> None:
        self.assertIn("There is no upload flow", self.readme)

    def test_12_readme_contains_no_database_limitation(self) -> None:
        self.assertIn("There is no database persistence", self.readme)

    def test_13_readme_contains_no_user_permission_limitation(self) -> None:
        self.assertIn("no user authorization or audit identity", self.readme_lower)

    def test_14_readme_quick_start_references_workspace_commands(self) -> None:
        self.assertIn("uvicorn backend.main:app --reload --port 8001", self.readme)
        self.assertIn('$env:VITE_API_BASE="http://127.0.0.1:8001"', self.readme)
        self.assertIn("npm run dev", self.readme)
        self.assertIn("迁移工作台", self.readme)

    def test_15_demo_doc_exists(self) -> None:
        self.assertTrue((ROOT / "docs/practical-v2-demo.md").is_file())

    def test_16_review_guide_exists(self) -> None:
        self.assertTrue((ROOT / "docs/practical-v2-review-guide.md").is_file())

    def test_17_verification_script_exists(self) -> None:
        self.assertTrue((ROOT / "scripts/verify_practical_v2.ps1").is_file())

    def test_18_demo_requires_no_llm_credential(self) -> None:
        self.assertIn("No LLM credential is required.", self.demo)
        self.assertNotIn("llm credential required", self.demo_lower)

    def test_19_reviewer_guide_sha_matches_committed_reports(self) -> None:
        expected = [
            read_json("data/synthetic/erpnext_item_price_blind_mapping.json")["_run_info"]["content_sha256"],
            read_json("data/synthetic/erpnext_item_price_blind_evaluation.json")["_run_info"]["content_sha256"],
            hashlib.sha256((ROOT / "data/examples/blind/erpnext_item_price/blind_protocol_lock.json").read_bytes()).hexdigest(),
            read_json("data/generated/erpnext_item_price_multitarget/package_manifest.json")["_run_info"]["content_sha256"],
            read_json("data/synthetic/erpnext_item_price_multitarget_generated_validation.json")["_run_info"]["content_sha256"],
        ]
        for sha in expected:
            self.assertIn(sha, self.guide)

    def test_20_docs_contain_no_local_absolute_paths(self) -> None:
        self.assertIsNone(re.search(r"[A-Z]:\\\\", self.docs_combined))
        self.assertNotIn("/" + "home/", self.docs_combined)

    def test_21_docs_contain_no_real_secret_markers(self) -> None:
        forbidden = ["Be" + "arer", "Authorization" + ":", "s" + "k-" + "live", "s" + "k-" + "proj"]
        for marker in forbidden:
            self.assertNotIn(marker, self.docs_combined)

    def test_22_docs_do_not_claim_erpnext_contract_authoritative(self) -> None:
        self.assertIn("authoritative=false", self.readme)
        self.assertNotIn("authoritative" + " = " + "true", self.docs_lower)

    def test_23_docs_do_not_claim_automatic_multi_target_identification(self) -> None:
        self.assertIn("does not automatically identify two formal recommendations", self.readme)

    def test_24_docs_contain_python_test_count(self) -> None:
        self.assertIn("Python tests: 417", self.readme)
        self.assertIn("Python tests: 417", self.script)

    def test_25_docs_contain_frontend_test_count(self) -> None:
        self.assertIn("Frontend tests: 45", self.readme)
        self.assertIn("Frontend tests: 45", self.script)

    def test_26_docs_contain_workspace_api_test_count(self) -> None:
        self.assertIn("Workspace API tests: 38", self.readme)
        self.assertIn("Workspace API tests: 38", self.script)

    def test_27_verification_script_does_not_call_remote_fetch_tools(self) -> None:
        forbidden = ["invoke-webrequest", "invoke-restmethod", "curl ", "wget ", "fetch("]
        for marker in forbidden:
            self.assertNotIn(marker, self.script_lower)

    def test_28_verification_script_does_not_start_servers(self) -> None:
        forbidden = ["uvicorn", "npm run dev", "vite --host"]
        for marker in forbidden:
            self.assertNotIn(marker, self.script_lower)

    def test_29_verification_script_does_not_install_dependencies(self) -> None:
        forbidden = ["pip install", "npm install", "npm ci"]
        for marker in forbidden:
            self.assertNotIn(marker, self.script_lower)

    def test_30_verification_script_cleans_runtime_and_frontend_dist(self) -> None:
        self.assertIn('Remove-TreeIfPresent "data/runtime"', self.script)
        self.assertIn('Remove-TreeIfPresent "frontend/dist"', self.script)

    def test_31_demo_contains_required_demo_steps(self) -> None:
        for phrase in [
            "Show the 10 source fields.",
            "Keep both `item_code` targets approved.",
            "Show `valid=true` and `findings=0`.",
            "Confirm that the git worktree remains unchanged.",
        ]:
            self.assertIn(phrase, self.demo)

    def test_32_reviewer_guide_lists_review_order(self) -> None:
        for phrase in [
            "Contract Loader and Validator",
            "Mapping Engine",
            "Blind Benchmark",
            "Migration Workspace API",
            "React Workspace",
        ]:
            self.assertIn(phrase, self.guide)

    def test_33_readme_distinguishes_deterministic_core_from_llm_usage(self) -> None:
        self.assertIn("The deterministic core covers Contract Validation", self.readme)
        self.assertIn("Source Profiling", self.readme)
        self.assertIn("LLMs are used only for Module 2", self.readme)
        self.assertIn("they do not perform the formal business calculation", self.readme)

    def test_34_readme_describes_runtime_as_git_ignored(self) -> None:
        self.assertIn("data/runtime/", self.readme)
        self.assertIn("Git ignored", self.readme)
        self.assertIn("`/api/reports/*`: read-only formal report access.", self.readme)
        self.assertIn("`/api/migration/*`: scoped local writes only", self.readme)
        self.assertIn("Workspace writes are restricted to `data/runtime/`", self.readme)
        self.assertIn("Requests do not accept contract, source, decision, or output paths.", self.readme)
        self.assertIn("No real SAP or ERPNext system is connected.", self.readme)
