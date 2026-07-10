"""Generate synthetic Fit-to-Standard workshop interview notes.

The output is intentionally split into:
  - interview notes: unstructured meeting-style text, with no judgment labels
  - ground truth: requirement IDs, domains, expected Fit-to-Standard class,
    and short reasons for later evaluation

All content is fictional and synthetic. It describes a manufacturing carve-out
scenario but does not contain real customer data, real SAP system data, or
copied SAP process documentation.

Usage:
    python src/tools/generate_interview_notes.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import attach_run_info  # noqa: E402  reuse reproducibility metadata

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOTES_OUTPUT_PATH = PROJECT_ROOT / "data" / "synthetic" / "interview_notes.json"
GROUND_TRUTH_OUTPUT_PATH = (
    PROJECT_ROOT / "data" / "synthetic" / "interview_notes_ground_truth.json"
)


def build_interview_notes() -> dict[str, Any]:
    """Return synthetic, deliberately messy interview notes."""
    sessions = [
        {
            "note_id": "INT-001",
            "session_title": "Procurement workshop - buying direct and indirect materials",
            "domain_focus": ["P2P", "master_data"],
            "participants": [
                "NewCo procurement lead",
                "plant buyer",
                "AP accountant",
                "data migration analyst",
            ],
            "text": (
                "The procurement team said they want the basic flow to stay boring: "
                "a planner raises a purchase requisition, the buyer converts it into "
                "a purchase order, goods receipt happens at the plant, and invoice "
                "verification follows after that. They were clear that this should not "
                "be reinvented just because NewCo is splitting from the old group. "
                "The messy part is approval. Below 10k EUR they are fine with one "
                "manager, between 10k and 100k they want the plant controller involved, "
                "and anything above that should go to the carve-out procurement board. "
                "Someone also mentioned that rush orders for safety-critical spare "
                "parts need a mandatory justification field before the PO is released; "
                "the buyer called it 'that little field auditors always ask for later'. "
                "There was a side complaint that the old group had a tiny tool called "
                "OrivaBuy that still receives emergency repair orders from one plant, "
                "and the team wants NewCo to keep sending those orders there every night "
                "until the transition service agreement ends."
            ),
        },
        {
            "note_id": "INT-002",
            "session_title": "Procurement and supplier onboarding follow-up",
            "domain_focus": ["P2P", "master_data"],
            "participants": [
                "supplier manager",
                "quality lead",
                "compliance analyst",
                "IT integration owner",
            ],
            "text": (
                "Supplier onboarding came up again, mostly because the plant people do "
                "not trust the inherited vendor list. They want a standard vendor record "
                "with name, address, tax number, bank details, and purchasing data, and "
                "they expect duplicates to be reviewed before go-live. That sounded "
                "straightforward, but then the quality lead added that suppliers for "
                "regulated components must carry an internal supplier risk band, and a "
                "purchase order should warn the buyer if the band is high and the latest "
                "quality certificate is missing. The compliance analyst said countries "
                "need to be cleaned into normal ISO-style codes because the old extract "
                "has DE, Germany, Deutschland, and sometimes just lower-case values. "
                "Toward the end, someone asked whether we can copy supplier risk scores "
                "from a carve-out-only spreadsheet called Nivora Risk Hub. It is not "
                "a corporate standard system; it belongs to the separation team and will "
                "probably disappear after Day 1, but they still want an automated load."
            ),
        },
        {
            "note_id": "INT-003",
            "session_title": "Sales order and billing workshop",
            "domain_focus": ["O2C", "master_data"],
            "participants": [
                "sales operations lead",
                "customer service manager",
                "billing specialist",
                "cutover lead",
            ],
            "text": (
                "Sales operations mainly wants the usual order-to-cash path to work: "
                "customer order entry, availability check, delivery, goods issue, billing, "
                "and the receivable posted for finance. They kept saying that the first "
                "release should not be clever. There are still commercial details though. "
                "NewCo will have its own sales organizations and distribution channels, "
                "so pricing condition records and customer account groups need to reflect "
                "the separated company structure. The customer service manager also wants "
                "an extra check on export orders: if a customer is flagged as distributor "
                "and the ship-to country is outside the home market, the order should ask "
                "for an export-control review reference before delivery is released. "
                "One awkward request was about the old portal. Some strategic customers "
                "still place orders through the legacy Virello customer portal, and the "
                "business wants those portal orders to become NewCo sales orders without "
                "manual re-keying during the first six months."
            ),
        },
        {
            "note_id": "INT-004",
            "session_title": "Returns, credit, and customer master discussion",
            "domain_focus": ["O2C", "R2R", "master_data"],
            "participants": [
                "credit manager",
                "returns coordinator",
                "AR lead",
                "master data steward",
            ],
            "text": (
                "The credit manager said they are comfortable with ordinary credit checks "
                "and blocked order handling, as long as the limits are separate for NewCo "
                "and not accidentally inherited from the former parent. Credit limits need "
                "different thresholds for domestic distributors, export distributors, and "
                "intercompany customers. The returns coordinator described the return flow "
                "in a very practical way: customer complains, return order is created, goods "
                "come back, inspection happens, then a credit memo may follow. They do not "
                "want a separate returns tool. What they do want is a small extra rule: when "
                "a return reason says 'carve-out packaging defect', the system should force "
                "a packaging batch reference because the separation team is tracking those "
                "issues for the first year. The AR lead also complained that the old customer "
                "numbers are inconsistent, and NewCo wants the legacy customer ID retained "
                "for search and reconciliation."
            ),
        },
        {
            "note_id": "INT-005",
            "session_title": "Record-to-report workshop",
            "domain_focus": ["R2R", "master_data"],
            "participants": [
                "NewCo finance controller",
                "GL accountant",
                "tax lead",
                "reporting analyst",
            ],
            "text": (
                "Finance opened with the basics: they need a clean general ledger, journal "
                "posting, period close, open item clearing, and financial statements for "
                "the new legal entities. The controller said this is a standard finance "
                "setup, but the chart of accounts must be NewCo-specific because the former "
                "group chart is too detailed for the smaller operating model. Company codes "
                "and posting periods also need to follow the new legal entity structure. "
                "Then the tax lead added a carve-out wrinkle: certain manual journals must "
                "carry a separation reason code when they relate to transition service costs; "
                "without that code, the journal should not be posted. Finally the reporting "
                "analyst asked for a nightly custom file to be sent to the fictional Auralis "
                "Separation Ledger, because the treasury separation team uses it to reconcile "
                "Day-1 balances outside the ERP for the first two quarters."
            ),
        },
        {
            "note_id": "INT-006",
            "session_title": "Master data cleanup and governance wrap-up",
            "domain_focus": ["master_data", "P2P", "O2C", "R2R"],
            "participants": [
                "master data lead",
                "business process owner",
                "data migration lead",
                "program manager",
            ],
            "text": (
                "The wrap-up was less tidy. Everyone agrees product master data has to be "
                "created with material type, base unit, product group, plant views, and "
                "valuation data before open orders are migrated. The data lead asked for "
                "number ranges to be separated for NewCo so that new records are easy to "
                "spot after go-live. For materials that move to the new plants, planners "
                "want MRP type, procurement type, and safety stock copied where the plant "
                "is in scope. A master data steward added that hazardous components need "
                "an internal carve-out handling class, and sales orders should not ship "
                "those products unless the class has been reviewed. Near the end, the "
                "program manager mentioned a one-off technical ask: they want an upload "
                "from the fictional SeltraBridge archive that brings over product drawings "
                "and links them to product records, because the old document repository "
                "will not be part of NewCo. Nobody was sure who owns that after Day 1."
            ),
        },
    ]

    return {
        "_meta": {
            "dataset_name": "synthetic_fit_to_standard_interview_notes",
            "module": "module_2_fit_to_standard_gap_analysis",
            "scenario": "fictional manufacturing carve-out for NewCo",
            "record_type": "unstructured_interview_notes",
            "requirement_count": 23,
            "note_count": len(sessions),
            "domains_covered": ["P2P", "O2C", "R2R", "master_data"],
            "compliance_note": (
                "Synthetic workshop notes written for this educational project. "
                "They are not copied from SAP documentation and contain no real "
                "customer, project, or system data."
            ),
            "ground_truth_file": "data/synthetic/interview_notes_ground_truth.json",
        },
        "sessions": sessions,
    }


def build_ground_truth() -> dict[str, Any]:
    """Return expected classifications for later extractor/judge evaluation."""
    requirements = [
        {
            "requirement_id": "REQ-P2P-001",
            "source_note_id": "INT-001",
            "domain": "P2P",
            "expected_category": "Fit",
            "expected_need": "Run the standard purchase requisition to purchase order to goods receipt to invoice verification flow.",
            "reason": "The requested P2P backbone follows a common standard procurement flow without special behavior.",
        },
        {
            "requirement_id": "REQ-P2P-002",
            "source_note_id": "INT-001",
            "domain": "P2P",
            "expected_category": "Configuration",
            "expected_need": "Set approval thresholds for purchase orders at 10k EUR and 100k EUR with different approvers.",
            "reason": "Approval levels and release strategy thresholds are standard concepts, but the values and approver groups are company-specific configuration.",
        },
        {
            "requirement_id": "REQ-P2P-003",
            "source_note_id": "INT-001",
            "domain": "P2P",
            "expected_category": "Enhancement",
            "expected_need": "Require a justification field before releasing safety-critical spare-part rush orders.",
            "reason": "The base PO release process exists, but the additional mandatory carve-out-specific field and validation is an extension to standard behavior.",
        },
        {
            "requirement_id": "REQ-P2P-004",
            "source_note_id": "INT-001",
            "domain": "P2P",
            "expected_category": "Development",
            "expected_need": "Send emergency repair orders nightly to the fictional OrivaBuy legacy tool during the transition period.",
            "reason": "A custom outbound integration to a fictional carve-out legacy tool is not standard process configuration.",
        },
        {
            "requirement_id": "REQ-MD-001",
            "source_note_id": "INT-002",
            "domain": "master_data",
            "expected_category": "Fit",
            "expected_need": "Create supplier master records with name, address, tax number, bank details, and purchasing data.",
            "reason": "Supplier master maintenance with core identification, address, bank, tax, and purchasing attributes is a standard master data capability.",
        },
        {
            "requirement_id": "REQ-MD-002",
            "source_note_id": "INT-002",
            "domain": "master_data",
            "expected_category": "Enhancement",
            "expected_need": "Warn buyers when high-risk regulated-component suppliers lack a current quality certificate.",
            "reason": "Supplier and PO processing exist, but the risk-band/certificate warning adds a specific business validation.",
        },
        {
            "requirement_id": "REQ-MD-003",
            "source_note_id": "INT-002",
            "domain": "master_data",
            "expected_category": "Configuration",
            "expected_need": "Normalize country values into ISO-style country codes during migration and governance.",
            "reason": "Country code standardization uses standard field semantics and value mapping, with project-specific conversion rules.",
        },
        {
            "requirement_id": "REQ-MD-004",
            "source_note_id": "INT-002",
            "domain": "master_data",
            "expected_category": "Development",
            "expected_need": "Automatically load supplier risk scores from the fictional Nivora Risk Hub spreadsheet.",
            "reason": "A custom load from a carve-out-only external spreadsheet repository is outside standard process support.",
        },
        {
            "requirement_id": "REQ-O2C-001",
            "source_note_id": "INT-003",
            "domain": "O2C",
            "expected_category": "Fit",
            "expected_need": "Run sales order entry, availability check, delivery, goods issue, billing, and receivables posting.",
            "reason": "The requested O2C backbone matches a standard sales fulfillment and billing flow.",
        },
        {
            "requirement_id": "REQ-O2C-002",
            "source_note_id": "INT-003",
            "domain": "O2C",
            "expected_category": "Configuration",
            "expected_need": "Configure NewCo sales organizations, distribution channels, pricing records, and customer account groups.",
            "reason": "Sales org structures, pricing records, and account groups are standard capabilities configured to the NewCo design.",
        },
        {
            "requirement_id": "REQ-O2C-003",
            "source_note_id": "INT-003",
            "domain": "O2C",
            "expected_category": "Enhancement",
            "expected_need": "Require an export-control review reference before delivery release for distributor export orders.",
            "reason": "Order and delivery processing are standard, but the specific distributor/country validation adds custom control logic.",
        },
        {
            "requirement_id": "REQ-O2C-004",
            "source_note_id": "INT-003",
            "domain": "O2C",
            "expected_category": "Development",
            "expected_need": "Convert orders from the fictional Virello customer portal into NewCo sales orders without manual re-keying.",
            "reason": "A custom inbound integration from a fictional legacy portal requires development beyond standard configuration.",
        },
        {
            "requirement_id": "REQ-O2C-005",
            "source_note_id": "INT-004",
            "domain": "O2C",
            "expected_category": "Configuration",
            "expected_need": "Set separate NewCo credit-limit thresholds for domestic distributors, export distributors, and intercompany customers.",
            "reason": "Credit management is standard, while thresholds and customer segment rules are project-specific configuration.",
        },
        {
            "requirement_id": "REQ-O2C-006",
            "source_note_id": "INT-004",
            "domain": "O2C",
            "expected_category": "Fit",
            "expected_need": "Use standard customer returns with return order, inbound receipt, inspection, and credit memo where appropriate.",
            "reason": "The described return and credit memo flow is a standard returns process pattern.",
        },
        {
            "requirement_id": "REQ-O2C-007",
            "source_note_id": "INT-004",
            "domain": "O2C",
            "expected_category": "Enhancement",
            "expected_need": "Force a packaging batch reference when the return reason is carve-out packaging defect.",
            "reason": "Returns are standard, but the conditional field requirement for a special return reason is custom validation.",
        },
        {
            "requirement_id": "REQ-R2R-001",
            "source_note_id": "INT-005",
            "domain": "R2R",
            "expected_category": "Fit",
            "expected_need": "Run general ledger, journal posting, period close, open item clearing, and financial statements for NewCo legal entities.",
            "reason": "These are standard record-to-report capabilities and do not imply a custom extension.",
        },
        {
            "requirement_id": "REQ-R2R-002",
            "source_note_id": "INT-005",
            "domain": "R2R",
            "expected_category": "Configuration",
            "expected_need": "Define NewCo-specific chart of accounts, company codes, and posting periods.",
            "reason": "The finance structures are standard but must be configured to the separated legal entity model.",
        },
        {
            "requirement_id": "REQ-R2R-003",
            "source_note_id": "INT-005",
            "domain": "R2R",
            "expected_category": "Enhancement",
            "expected_need": "Require a separation reason code for manual journals related to transition service costs.",
            "reason": "Manual journal posting is standard, but the extra posting validation for transition service costs is an enhancement.",
        },
        {
            "requirement_id": "REQ-R2R-004",
            "source_note_id": "INT-005",
            "domain": "R2R",
            "expected_category": "Development",
            "expected_need": "Send a nightly custom file to the fictional Auralis Separation Ledger for Day-1 balance reconciliation.",
            "reason": "A bespoke outbound file to a fictional carve-out ledger is a custom integration and should feed the later development backlog.",
        },
        {
            "requirement_id": "REQ-MD-005",
            "source_note_id": "INT-006",
            "domain": "master_data",
            "expected_category": "Fit",
            "expected_need": "Create product master data with material type, base unit, product group, plant views, and valuation data before open order migration.",
            "reason": "Core product/material master maintenance across basic, plant, and valuation views is standard master data scope.",
        },
        {
            "requirement_id": "REQ-MD-006",
            "source_note_id": "INT-006",
            "domain": "master_data",
            "expected_category": "Configuration",
            "expected_need": "Separate NewCo number ranges for new master data records.",
            "reason": "Number ranges are a standard configuration object adapted to the NewCo operating model.",
        },
        {
            "requirement_id": "REQ-MD-007",
            "source_note_id": "INT-006",
            "domain": "master_data",
            "expected_category": "Enhancement",
            "expected_need": "Prevent shipment of hazardous components until the internal carve-out handling class has been reviewed.",
            "reason": "Product and sales processing are standard, but this handling-class gate is a custom cross-process validation.",
        },
        {
            "requirement_id": "REQ-MD-008",
            "source_note_id": "INT-006",
            "domain": "master_data",
            "expected_category": "Development",
            "expected_need": "Upload product drawings from the fictional SeltraBridge archive and link them to product records.",
            "reason": "A one-off integration with a fictional archive and document-linking load is custom development.",
        },
    ]

    return {
        "_meta": {
            "dataset_name": "synthetic_fit_to_standard_interview_notes_ground_truth",
            "module": "module_2_fit_to_standard_gap_analysis",
            "source_notes_file": "data/synthetic/interview_notes.json",
            "requirement_count": len(requirements),
            "expected_categories": [
                "Fit",
                "Configuration",
                "Enhancement",
                "Development",
            ],
            "category_counts": {
                category: sum(
                    1 for item in requirements if item["expected_category"] == category
                )
                for category in (
                    "Fit",
                    "Configuration",
                    "Enhancement",
                    "Development",
                )
            },
            "compliance_note": (
                "Ground truth is synthetic and separate from the interview text so "
                "later extraction and classification components cannot read labels "
                "from the input notes."
            ),
        },
        "requirements": requirements,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    notes = attach_run_info(build_interview_notes())
    ground_truth = attach_run_info(build_ground_truth())

    write_json(NOTES_OUTPUT_PATH, notes)
    write_json(GROUND_TRUTH_OUTPUT_PATH, ground_truth)

    counts = ground_truth["_meta"]["category_counts"]
    print(f"Notes        : {notes['_meta']['note_count']}")
    print(f"Requirements : {ground_truth['_meta']['requirement_count']}")
    print(
        "Categories   : "
        + ", ".join(f"{name}={count}" for name, count in counts.items())
    )
    print(f"Notes hash   : {notes['_run_info']['content_sha256'][:16]}")
    print(f"Truth hash   : {ground_truth['_run_info']['content_sha256'][:16]}")
    print(f"\nWrote notes        -> {NOTES_OUTPUT_PATH}")
    print(f"Wrote ground truth -> {GROUND_TRUTH_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
