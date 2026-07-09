"""为 legacy 扁平字段建议映射到目标 SAP A2X 多实体字段。

本工具只产出【建议】，绝不自动确定映射：高置信度直接建议，中/低置信度标记
needs_review 推人工确认；没有可信目标的字段单独列为 gap（目标 schema 里确实没有
对应落点，例如 email / phone 落在本项目未展开的子实体里）。

打分综合四路信号：
  semantic  字段名语义相似度（sentence-transformers embedding 余弦相似度）
  alias     schema 预埋的 aliases / canonical_vs_legacy / legacy_hint（强信号，命中即高分）
  fuzzy     字段名字符串模糊匹配（辅助）
  type      数据类型、长度、自由文本属性的兼容性（含截断风险检测）

type 是【乘性闸门】而非加分项：类型兼容不能凭空制造一个匹配，只能否决或削弱它。
若做成加性分量，email（最佳语义仅 0.416）会被类型分托到 0.47，越过 no-match 线，
最终把 email 荒谬地建议成 MiddleName。

用法：
    python src/tools/field_mapping.py
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import numpy as np
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import profile_records  # noqa: E402  复用第三步的画像逻辑

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

WEIGHT_SEMANTIC = 0.55          # 名称信号内部的权重，semantic + fuzzy 归一到 1
WEIGHT_FUZZY = 0.20

TYPE_GATE_FLOOR = 0.60          # 类型闸门的下限：完全不兼容也只削到 60%，不清零

ALIAS_CONFIDENCE_FLOOR = 0.90   # alias 命中时的置信度下限（强信号）
COMPUTED_FIELD_PENALTY = 0.85   # 目标字段只读/系统派生 → 降权

HIGH_CONFIDENCE = 0.70          # ≥ 此值：直接建议
MEDIUM_CONFIDENCE = 0.45        # ≥ 此值：needs_review
# < 此值：视为无可信目标，列为 gap。
# 该阈值在本数据集上标定：真实映射的最低分是 vendor_name 0.528 / legacy_vendor_id 0.562，
# 而 phone(0.359) 与 email(0.330) 的 top1 分别是 Language 与 AddressID，均为无意义匹配。
NO_MATCH_THRESHOLD = 0.40

TOP_N_CANDIDATES = 3

# 领域同义词：legacy 说 vendor/supplier，目标端统一叫 business partner。
# 仅用于扩写 embedding 的输入文本，不参与 alias 强信号判定。
#
# 扩写只用来【补召回】，语义分取"原文 / 扩写"两者的较大值：单向替换会稀释原始信号，
# 实测 tax_number 扩成 "tax vat number" 后，BPTaxNumber 的相似度从 0.672 掉到 0.471。
SYNONYM_EXPANSION: dict[str, str] = {
    "vendor": "vendor supplier business partner organization",
    "supplier": "supplier vendor business partner organization",
    "legacy": "old external",
    "tax": "tax vat",
    "zip": "postal",
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PATH = PROJECT_ROOT / "data" / "legacy" / "legacy_vendors.json"
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "business_partner_target_schema.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "synthetic" / "vendor_field_mapping.json"


# --------------------------------------------------------------------------
# 文本归一化
# --------------------------------------------------------------------------
def split_identifier(name: str) -> str:
    """把 OrganizationBPName1 / postal_code 拆成空格分隔的小写词。

    >>> split_identifier("OrganizationBPName1")
    'organization bp name 1'
    >>> split_identifier("postal_code")
    'postal code'
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", spaced)
    return re.sub(r"[_\W]+", " ", spaced).strip().lower()


def normalize_token(name: str) -> str:
    """用于 alias 精确比对：只保留字母，丢弃下划线、数字、大小写。"""
    return re.sub(r"[^a-z]", "", name.lower())


def expand_synonyms(text: str) -> str:
    words = text.split()
    expanded = [SYNONYM_EXPANSION.get(word, word) for word in words]
    return " ".join(expanded)


def lexical_overlap(legacy_text: str, target_text: str) -> set[str]:
    """两个字段名（已 split_identifier）之间共享的词元。

    零重叠意味着匹配完全建立在 embedding 相似度上，没有任何字面锚点，
    这正是 currency -> BankAccount 这类"假朋友"的特征。

    >>> sorted(lexical_overlap("vendor name", "business partner name"))
    ['name']
    >>> sorted(lexical_overlap("currency", "bank account"))
    []
    """
    return set(legacy_text.split()) & set(target_text.split())


# --------------------------------------------------------------------------
# 目标字段
# --------------------------------------------------------------------------
@dataclass
class TargetField:
    entity: str
    name: str
    edm_type: str
    max_length: int | None
    is_key: bool
    is_computed: bool
    is_free_text: bool
    description_zh: str
    aliases: list[str] = dc_field(default_factory=list)
    legacy_hint: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.entity}.{self.name}"

    @property
    def embed_text(self) -> str:
        return split_identifier(self.name)


def load_target_fields(schema_path: Path) -> list[TargetField]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    targets: list[TargetField] = []
    for entity_name, entity in schema["entities"].items():
        for spec in entity["fields"]:
            targets.append(TargetField(
                entity=entity_name,
                name=spec["name"],
                edm_type=spec.get("type", "Edm.String"),
                max_length=spec.get("max_length"),
                is_key=bool(spec.get("is_key")),
                is_computed=bool(spec.get("is_computed")),
                is_free_text=bool(spec.get("is_free_text")),
                description_zh=spec.get("description_zh", ""),
                aliases=list(spec.get("aliases", [])),
                legacy_hint=spec.get("legacy_hint"),
            ))
    return targets


def build_alias_index(
    schema_path: Path, targets: list[TargetField]
) -> dict[str, list[tuple[str, str]]]:
    """归一化别名 -> [(目标字段全名, 别名来源)]。

    三个来源，都是第一步埋在 schema 里的种子：
      1. 字段级 aliases（material schema 用得多）
      2. 顶层 naming_note_zh.canonical_vs_legacy 对照表
      3. legacy_hint 的字段部分，如 "ADRC-CITY1" -> "city"
    """
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    index: dict[str, list[tuple[str, str]]] = {}

    def add(alias: str, qualified: str, source: str) -> None:
        key = normalize_token(alias)
        if not key:
            return
        entry = (qualified, source)
        if entry not in index.setdefault(key, []):
            index[key].append(entry)

    by_name: dict[str, list[TargetField]] = {}
    for target in targets:
        by_name.setdefault(target.name, []).append(target)
        for alias in target.aliases:
            add(alias, target.qualified, "schema.aliases")
        if target.legacy_hint and "-" in target.legacy_hint:
            add(target.legacy_hint.split("-", 1)[1], target.qualified, "schema.legacy_hint")

    naming_note = schema.get("naming_note_zh", {})
    for pair in naming_note.get("canonical_vs_legacy", []):
        for target in by_name.get(pair["canonical"], []):
            for alias in pair.get("aliases", []):
                add(alias, target.qualified, "schema.canonical_vs_legacy")

    return index


# --------------------------------------------------------------------------
# legacy 字段画像
# --------------------------------------------------------------------------
@dataclass
class LegacyField:
    name: str
    observed_max_length: int
    distinct_ratio: float
    is_free_text: bool
    inferred_kind: str  # "date" | "string"
    samples: list[str]

    @property
    def embed_text(self) -> str:
        """用于 fuzzy 匹配的规范文本（不扩写，避免同义词干扰字符串相似度）。"""
        return split_identifier(self.name)

    @property
    def embed_variants(self) -> list[str]:
        """用于 semantic 匹配的文本变体，最终取各变体相似度的最大值。"""
        plain = split_identifier(self.name)
        expanded = expand_synonyms(plain)
        return [plain] if expanded == plain else [plain, expanded]


DATE_PATTERN = re.compile(r"^\d{8}$")


def load_legacy_fields(legacy_path: Path) -> tuple[list[LegacyField], int]:
    records = json.loads(legacy_path.read_text(encoding="utf-8"))
    profile = profile_records(records)

    fields: list[LegacyField] = []
    for name, stats in profile["fields"].items():
        present = [str(r[name]) for r in records if r.get(name) not in (None, "")]
        date_like = sum(1 for v in present if DATE_PATTERN.match(v))
        kind = "date" if present and date_like / len(present) >= 0.9 else "string"
        fields.append(LegacyField(
            name=name,
            observed_max_length=max((len(v) for v in present), default=0),
            distinct_ratio=stats["distinct_ratio"],
            is_free_text=stats["is_free_text"],
            inferred_kind=kind,
            samples=present[:3],
        ))
    return fields, len(records)


# --------------------------------------------------------------------------
# 类型兼容性
# --------------------------------------------------------------------------
def type_compatibility(
    legacy: LegacyField, target: TargetField
) -> tuple[float, list[str]]:
    warnings: list[str] = []

    if target.edm_type == "Edm.String":
        type_score = 1.0 if legacy.inferred_kind == "string" else 0.6
    elif target.edm_type == "Edm.DateTime":
        type_score = 1.0 if legacy.inferred_kind == "date" else 0.15
    else:
        type_score = 0.10  # Boolean / Decimal / Guid：本数据集全是字符串

    if target.max_length is None or legacy.observed_max_length <= target.max_length:
        length_fit = 1.0
    else:
        length_fit = 0.3
        warnings.append(
            f"截断风险：观测到的最大长度 {legacy.observed_max_length} "
            f"超过目标字段上限 {target.max_length}"
        )

    free_text_agreement = 1.0 if legacy.is_free_text == target.is_free_text else 0.5
    score = 0.5 * type_score + 0.3 * length_fit + 0.2 * free_text_agreement
    return score, warnings


# --------------------------------------------------------------------------
# 打分
# --------------------------------------------------------------------------
def confidence_band(score: float) -> str:
    if score >= HIGH_CONFIDENCE:
        return "high"
    if score >= MEDIUM_CONFIDENCE:
        return "medium"
    return "low"


def classify_status(top: dict[str, Any] | None) -> str:
    """把 top1 候选归入四种状态之一，按优先级判定。

    no_confident_target    置信度低于 no-match 线，目标 schema 里根本没有落点
                           （如 email / phone 落在本项目未展开的子实体里）
    possible_false_friend  分数够高，但与目标字段名零共享词元且无 alias 命中——
                           匹配纯靠 embedding，很可能目标端压根没有正确落点
                           （如 currency -> BankAccount：BP 层根本没有货币字段）
    needs_review           有像样的候选，但置信度不到 high，需人工确认
    suggested              高置信度，可直接采纳（仍是建议，不是既成事实）
    """
    if top is None or top["confidence"] < NO_MATCH_THRESHOLD:
        return "no_confident_target"
    if not top["signals"]["alias"] and not top["signals"]["lexical_overlap"]:
        return "possible_false_friend"
    if top["band"] != "high":
        return "needs_review"
    return "suggested"


def score_candidates(
    legacy: LegacyField,
    targets: list[TargetField],
    semantic_row: np.ndarray,
    alias_index: dict[str, list[tuple[str, str]]],
) -> list[dict[str, Any]]:
    alias_hits = dict(alias_index.get(normalize_token(legacy.name), []))

    candidates: list[dict[str, Any]] = []
    for position, target in enumerate(targets):
        semantic = float(semantic_row[position])
        fuzzy = fuzz.token_sort_ratio(legacy.embed_text, target.embed_text) / 100.0
        type_score, warnings = type_compatibility(legacy, target)

        # 名称信号（semantic + fuzzy）决定匹配强度，类型只作为乘性闸门衰减它。
        name_score = (WEIGHT_SEMANTIC * semantic + WEIGHT_FUZZY * fuzzy) / (
            WEIGHT_SEMANTIC + WEIGHT_FUZZY
        )
        type_gate = TYPE_GATE_FLOOR + (1.0 - TYPE_GATE_FLOOR) * type_score
        base = name_score * type_gate

        evidence: list[str] = []
        alias_source = alias_hits.get(target.qualified)
        if alias_source:
            base = max(base, ALIAS_CONFIDENCE_FLOOR)
            evidence.append(f"alias 命中（来源 {alias_source}），schema 预埋的强信号")

        if target.is_computed:
            base *= COMPUTED_FIELD_PENALTY
            warnings.append("目标字段为系统派生/只读，通常不能直接写入")

        evidence.append(f"语义相似度 {semantic:.3f}")
        evidence.append(f"字符串模糊匹配 {fuzzy:.3f}")
        evidence.append(f"类型/长度兼容性 {type_score:.3f}（乘性闸门 {type_gate:.3f}）")
        if target.is_key:
            evidence.append("目标字段是主键的一部分")

        shared = lexical_overlap(legacy.embed_text, target.embed_text)
        if shared:
            evidence.append(f"共享词元 {sorted(shared)}")
        elif not alias_source:
            evidence.append("与目标字段名零共享词元，匹配仅由 embedding 支撑")

        candidates.append({
            "target_entity": target.entity,
            "target_field": target.name,
            "qualified": target.qualified,
            "confidence": round(min(base, 1.0), 4),
            "band": confidence_band(min(base, 1.0)),
            "signals": {
                "semantic": round(semantic, 4),
                "fuzzy": round(fuzzy, 4),
                "type": round(type_score, 4),
                "alias": alias_source,
                "lexical_overlap": sorted(shared),
            },
            "target_type": target.edm_type,
            "target_max_length": target.max_length,
            "target_description_zh": target.description_zh,
            "evidence": evidence,
            "warnings": warnings,
        })

    # 同名字段在多个实体里重复出现（如 BusinessPartner 作为外键）：按字段名去重，
    # 保留分最高的一条；同分时按实体名排序，保证结果稳定可复现。
    best_by_name: dict[str, dict[str, Any]] = {}
    for candidate in sorted(candidates, key=lambda c: (-c["confidence"], c["target_entity"])):
        best_by_name.setdefault(candidate["target_field"], candidate)

    ranked = sorted(
        best_by_name.values(), key=lambda c: (-c["confidence"], c["qualified"])
    )
    return ranked[:TOP_N_CANDIDATES]


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def build_mapping_suggestions() -> dict[str, Any]:
    legacy_fields, record_count = load_legacy_fields(LEGACY_PATH)
    targets = load_target_fields(SCHEMA_PATH)
    alias_index = build_alias_index(SCHEMA_PATH, targets)

    model = SentenceTransformer(EMBEDDING_MODEL)

    # 每个 legacy 字段可能有多个文本变体（原文 + 同义词扩写），编码后按字段取最大值，
    # 这样扩写只抬高召回、不会稀释原文本已有的强信号。
    variant_texts: list[str] = []
    variant_owner: list[int] = []
    for index, legacy in enumerate(legacy_fields):
        for text in legacy.embed_variants:
            variant_texts.append(text)
            variant_owner.append(index)

    variant_vectors = np.asarray(model.encode(variant_texts, normalize_embeddings=True))
    target_vectors = np.asarray(
        model.encode([t.embed_text for t in targets], normalize_embeddings=True)
    )
    variant_similarity = variant_vectors @ target_vectors.T

    similarity = np.full((len(legacy_fields), len(targets)), -1.0)
    for row, owner in enumerate(variant_owner):
        similarity[owner] = np.maximum(similarity[owner], variant_similarity[row])

    mappings: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    for row, legacy in enumerate(legacy_fields):
        candidates = score_candidates(legacy, targets, similarity[row], alias_index)
        top = candidates[0] if candidates else None
        status = classify_status(top)
        confidence = top["confidence"] if top else 0.0

        if status == "no_confident_target":
            recommendation = None
            gaps.append({
                "legacy_field": legacy.name,
                "status": status,
                "best_candidate": top["qualified"] if top else None,
                "best_confidence": confidence,
                "message": (
                    f"字段 `{legacy.name}` 在目标 schema 中找不到可信落点"
                    f"（最高置信度 {confidence:.3f} < {NO_MATCH_THRESHOLD}），"
                    f"需人工确认是否属于未展开的子实体或 Fit-to-Standard 差异项。"
                ),
            })
        else:
            recommendation = top["qualified"]
            if status == "possible_false_friend":
                gaps.append({
                    "legacy_field": legacy.name,
                    "status": status,
                    "best_candidate": top["qualified"],
                    "best_confidence": confidence,
                    "message": (
                        f"字段 `{legacy.name}` 的最佳候选 `{top['qualified']}` "
                        f"（置信度 {confidence:.3f}）与其零共享词元、且未命中任何 alias，"
                        f"匹配仅由 embedding 支撑，疑似假朋友——目标端很可能没有正确落点。"
                    ),
                })

        mappings.append({
            "legacy_field": legacy.name,
            "legacy_profile": {
                "observed_max_length": legacy.observed_max_length,
                "distinct_ratio": legacy.distinct_ratio,
                "is_free_text": legacy.is_free_text,
                "inferred_kind": legacy.inferred_kind,
                "samples": legacy.samples,
            },
            "recommendation": recommendation,
            "confidence": confidence,
            "band": top["band"] if top else "none",
            "status": status,
            "needs_review": status != "suggested",
            "candidates": candidates,
        })

    return {
        "_meta": {
            "disclaimer": "本表全部为映射【建议】，不构成已确定的映射；任何非 high 置信度的条目都必须人工确认。",
            "embedding_model": EMBEDDING_MODEL,
            "legacy_source": str(LEGACY_PATH.relative_to(PROJECT_ROOT)),
            "target_schema": str(SCHEMA_PATH.relative_to(PROJECT_ROOT)),
            "legacy_record_count": record_count,
            "target_field_count": len(targets),
            "scoring": {
                "formula": "confidence = (w_sem*semantic + w_fuzzy*fuzzy) / (w_sem + w_fuzzy) * type_gate",
                "type_gate": "TYPE_GATE_FLOOR + (1 - TYPE_GATE_FLOOR) * type_score",
                "note": "type 是乘性闸门，不能凭空制造匹配；alias 命中则置信度取 max(base, alias_floor)",
                "weight_semantic": WEIGHT_SEMANTIC,
                "weight_fuzzy": WEIGHT_FUZZY,
                "type_gate_floor": TYPE_GATE_FLOOR,
                "computed_field_penalty": COMPUTED_FIELD_PENALTY,
            },
            "thresholds": {
                "alias_confidence_floor": ALIAS_CONFIDENCE_FLOOR,
                "high": HIGH_CONFIDENCE,
                "medium": MEDIUM_CONFIDENCE,
                "no_match": NO_MATCH_THRESHOLD,
            },
        },
        "mappings": mappings,
        "gaps": gaps,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    report = build_mapping_suggestions()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Legacy fields : {len(report['mappings'])}")
    print(f"Target fields : {report['_meta']['target_field_count']}")
    print(f"Gaps          : {len(report['gaps'])}\n")

    header = f"{'legacy field':<18} {'suggested target':<45} {'conf':>6}  {'band':<7} status"
    print(header)
    print("-" * len(header))
    for entry in report["mappings"]:
        target = entry["recommendation"] or "-"
        print(
            f"{entry['legacy_field']:<18} {target:<45} "
            f"{entry['confidence']:>6.3f}  {entry['band']:<7} {entry['status']}"
        )

    if report["gaps"]:
        print("\nGaps:")
        for gap in report["gaps"]:
            print(f"  - [{gap['status']}] {gap['message']}")

    warned = [
        (m["legacy_field"], w)
        for m in report["mappings"]
        for w in (m["candidates"][0]["warnings"] if m["candidates"] else [])
    ]
    if warned:
        print("\nWarnings on top candidate:")
        for name, warning in warned:
            print(f"  - {name}: {warning}")

    print(f"\nWrote suggestions -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
