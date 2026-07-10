"""对照评估：gap_analysis 的判定结果 vs ground truth。

【本文件是唯一读取 ground truth 的地方】。gap_analysis.py 在抽取和判定阶段都不读它，
评估必须在判定完成之后独立进行，不能反向影响判定。

评估三件事：
  1. LLM 判定 vs 基线判定 的准确率与混淆矩阵。若两者相当，说明 LLM 也在靠表面特征。
  2. Configuration vs Enhancement 的混淆（最细腻的边界）。
  3. Development 各条的 rationale 审计：是真引用了"标准无法覆盖"的判据，
     还是只提到了外部系统名这一表面特征。

抽取结果与 ground truth 不一一对应（可能多抽/少抽/合并/拆分），
因此先做需求对齐：用 source_note_id + 描述的词元重叠做贪心匹配，未匹配的单独统计。

用法：
    python src/tools/evaluate_gap_analysis.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import attach_run_info  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = PROJECT_ROOT / "data" / "synthetic" / "gap_analysis_report.json"
GROUND_TRUTH_PATH = PROJECT_ROOT / "data" / "synthetic" / "interview_notes_ground_truth.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "synthetic" / "gap_analysis_evaluation.json"

CATEGORIES = ("Fit", "Configuration", "Enhancement", "Development")
MATCH_THRESHOLD = 0.18  # Jaccard 下限，低于此值视为未匹配

STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "with", "into",
    "from", "at", "by", "as", "is", "are", "be", "that", "this", "it", "when",
    "should", "must", "need", "needs", "want", "wants", "we", "our", "us",
}

# 真推理的判据：rationale 里必须出现"标准覆盖与否"的论证，而不只是外部系统名
REASONING_MARKERS = (
    "no delivered", "not delivered", "does not deliver", "no extension point",
    "beyond the standard", "not part of the delivered", "standard provides no",
    "must be built", "project deliverable", "no standard", "does not exist",
    "not covered", "no object", "cannot read", "has no field",
)
SURFACE_MARKERS = ("external system", "third-party", "outside the erp", "legacy")


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", text.lower()) if t not in STOPWORDS and len(t) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


# --------------------------------------------------------------------------
# 需求对齐
# --------------------------------------------------------------------------
def align(
    predicted: list[dict[str, Any]], truth: list[dict[str, Any]]
) -> tuple[list[tuple[dict, dict, float]], list[dict], list[dict]]:
    """贪心一对一匹配：同一 note 内，按词元 Jaccard 从高到低配对。"""
    candidates = []
    for p in predicted:
        for t in truth:
            if p["source_note_id"] != t["source_note_id"]:
                continue
            score = jaccard(tokens(p["requirement_description"]), tokens(t["expected_need"]))
            if score >= MATCH_THRESHOLD:
                candidates.append((score, p["extracted_id"], t["requirement_id"]))

    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    by_pred = {p["extracted_id"]: p for p in predicted}
    by_truth = {t["requirement_id"]: t for t in truth}

    matched: list[tuple[dict, dict, float]] = []
    used_pred: set[str] = set()
    used_truth: set[str] = set()
    for score, pid, tid in candidates:
        if pid in used_pred or tid in used_truth:
            continue
        used_pred.add(pid)
        used_truth.add(tid)
        matched.append((by_pred[pid], by_truth[tid], round(score, 4)))

    spurious = [p for p in predicted if p["extracted_id"] not in used_pred]
    missed = [t for t in truth if t["requirement_id"] not in used_truth]
    return matched, spurious, missed


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------
def confusion(pairs: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """pairs = [(expected, predicted), ...] -> matrix[expected][predicted]"""
    matrix = {e: {p: 0 for p in CATEGORIES} for e in CATEGORIES}
    for expected, predicted in pairs:
        matrix[expected][predicted] += 1
    return matrix


def per_class(matrix: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    stats = {}
    for category in CATEGORIES:
        support = sum(matrix[category].values())
        correct = matrix[category][category]
        predicted_total = sum(matrix[e][category] for e in CATEGORIES)
        stats[category] = {
            "support": support,
            "correct": correct,
            "recall": round(correct / support, 4) if support else None,
            "precision": round(correct / predicted_total, 4) if predicted_total else None,
        }
    return stats


def score(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    matrix = confusion(pairs)
    total = len(pairs)
    correct = sum(matrix[c][c] for c in CATEGORIES)
    return {
        "n": total,
        "accuracy": round(correct / total, 4) if total else None,
        "confusion_matrix": matrix,
        "per_class": per_class(matrix),
        "config_vs_enhancement": {
            "Configuration_judged_Enhancement": matrix["Configuration"]["Enhancement"],
            "Enhancement_judged_Configuration": matrix["Enhancement"]["Configuration"],
            "total_confusions_on_this_boundary":
                matrix["Configuration"]["Enhancement"] + matrix["Enhancement"]["Configuration"],
        },
    }


# --------------------------------------------------------------------------
# Development rationale 审计
# --------------------------------------------------------------------------
def audit_development(matched: list[tuple[dict, dict, float]]) -> list[dict[str, Any]]:
    audits = []
    for pred, truth, _ in matched:
        if truth["expected_category"] != "Development" or "llm" not in pred:
            continue
        rationale = pred["llm"]["rationale"].lower()
        reasoning = sorted({m for m in REASONING_MARKERS if m in rationale})
        surface = sorted({m for m in SURFACE_MARKERS if m in rationale})
        audits.append({
            "requirement_id": truth["requirement_id"],
            "extracted_id": pred["extracted_id"],
            "description": pred["requirement_description"],
            "llm_category": pred["llm"]["category"],
            "correct": pred["llm"]["category"] == "Development",
            "evidence": pred["llm"]["evidence"],
            "reasoning_markers_found": reasoning,
            "surface_markers_found": surface,
            "verdict_zh": (
                "真推理：引用了『标准无覆盖/无扩展点/需自建』的判据"
                if reasoning else
                "存疑：rationale 中只见外部系统等表面特征，未见标准覆盖性论证"
                if surface else
                "存疑：rationale 中既无标准覆盖性论证，也无明显表面特征"
            ),
            "rationale": pred["llm"]["rationale"],
        })
    return sorted(audits, key=lambda a: a["requirement_id"])


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not REPORT_PATH.exists():
        raise SystemExit("先运行 python src/tools/gap_analysis.py 生成判定报告。")

    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    truth = json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))["requirements"]
    predicted = report["requirements"]
    has_llm = all("llm" in r for r in predicted)

    matched, spurious, missed = align(predicted, truth)

    baseline_pairs = [(t["expected_category"], p["baseline"]["category"]) for p, t, _ in matched]
    baseline_score = score(baseline_pairs)

    llm_score = None
    audits: list[dict[str, Any]] = []
    if has_llm:
        llm_pairs = [(t["expected_category"], p["llm"]["category"]) for p, t, _ in matched]
        llm_score = score(llm_pairs)
        audits = audit_development(matched)

    evaluation = attach_run_info({
        "_meta": {
            "separation_note_zh": (
                "本文件是唯一读取 ground truth 的地方。gap_analysis.py 的抽取与判定阶段"
                "均未读取它，评估在判定完成后独立进行。"
            ),
            "alignment_note_zh": (
                "抽取结果与 ground truth 不一一对应，先按 source_note_id + 描述词元 Jaccard "
                f"做贪心一对一匹配（阈值 {MATCH_THRESHOLD}）；未匹配的分别记为 spurious / missed。"
            ),
            "report_content_sha256": report["_run_info"]["content_sha256"],
            "model": report["_meta"]["model"],
            "extracted": len(predicted),
            "ground_truth": len(truth),
            "matched": len(matched),
            "spurious": len(spurious),
            "missed": len(missed),
        },
        "extraction_quality": {
            "matched": len(matched),
            "spurious_extractions": [
                {"extracted_id": p["extracted_id"], "description": p["requirement_description"]}
                for p in spurious
            ],
            "missed_requirements": [
                {"requirement_id": t["requirement_id"], "expected_need": t["expected_need"]}
                for t in missed
            ],
        },
        "baseline_no_llm": baseline_score,
        "llm": llm_score,
        "llm_vs_baseline": None if llm_score is None else {
            "accuracy_delta": round(llm_score["accuracy"] - baseline_score["accuracy"], 4),
            "config_enhancement_confusions_llm":
                llm_score["config_vs_enhancement"]["total_confusions_on_this_boundary"],
            "config_enhancement_confusions_baseline":
                baseline_score["config_vs_enhancement"]["total_confusions_on_this_boundary"],
            "interpretation_zh": (
                "准确率相当 → LLM 很可能也在靠表面特征；显著超出（尤其在 "
                "Configuration/Enhancement 边界上）→ 推理带来了真实价值。"
            ),
        },
        "development_rationale_audit": audits,
    })

    OUTPUT_PATH.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = evaluation["_meta"]
    print(f"Extracted {meta['extracted']} | ground truth {meta['ground_truth']} | "
          f"matched {meta['matched']} | spurious {meta['spurious']} | missed {meta['missed']}\n")

    def show(name: str, s: dict[str, Any]) -> None:
        print(f"=== {name} ===")
        print(f"accuracy: {s['accuracy']:.3f}  (n={s['n']})")
        header = "expected \\ predicted".ljust(22) + "".join(c[:6].ljust(8) for c in CATEGORIES)
        print(header)
        for expected in CATEGORIES:
            row = expected.ljust(22) + "".join(
                str(s["confusion_matrix"][expected][p]).ljust(8) for p in CATEGORIES
            )
            print(row)
        cve = s["config_vs_enhancement"]
        print(f"Config<->Enhancement confusions: {cve['total_confusions_on_this_boundary']}"
              f"  (C→E {cve['Configuration_judged_Enhancement']},"
              f" E→C {cve['Enhancement_judged_Configuration']})\n")

    show("BASELINE (no LLM, retrieval only)", baseline_score)
    if llm_score:
        show("LLM", llm_score)
        delta = evaluation["llm_vs_baseline"]["accuracy_delta"]
        print(f"accuracy delta (LLM - baseline): {delta:+.3f}\n")

        print("=== Development rationale audit ===")
        for audit in audits:
            mark = "OK " if audit["correct"] else "MISS"
            print(f"  [{mark}] {audit['requirement_id']}: {audit['verdict_zh']}")

    print(f"\nWrote evaluation -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
