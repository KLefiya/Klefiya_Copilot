"""迁移前校验：映射后的数据能否真正加载进目标 SAP 实体。

消费 field_mapping 的映射建议 + 目标 schema 的字段约束 + legacy 数据，
输出不合格记录清单。**只诊断、只建议，不修改源数据、不做任何自动修复。**

【核心：两个正交维度必须分开表达】
    semantic_match  映射语义是否正确（沿用 field_mapping 的结论，不重新判定）
    loadable        目标字段是否可写入（is_creatable or is_updatable）

二者互不蕴含。典型反例：created_date -> A_BusinessPartner.CreationDate
语义完全正确，但该字段 sap:creatable=false 且 sap:updatable=false，
不能作为加载目标——只能作 lineage / 参考。把它简单标成"通过"是错的。

用法：
    python src/tools/pre_migration_validation.py
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import attach_run_info, is_missing  # noqa: E402  复用第三步的逻辑

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PATH = PROJECT_ROOT / "data" / "legacy" / "legacy_vendors.json"
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "business_partner_target_schema.json"
MAPPING_PATH = PROJECT_ROOT / "data" / "synthetic" / "vendor_field_mapping.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "synthetic" / "vendor_validation_report.json"

RECORD_ID_FIELD = "legacy_vendor_id"

# 需要先归一化成 SAP 代码的字段域：字段名子串 -> (目标编码标准, 规范形态)
NORMALIZATION_DOMAINS: dict[str, tuple[str, re.Pattern[str]]] = {
    "country": ("ISO 3166-1 alpha-2", re.compile(r"^[A-Z]{2}$")),
    "language": ("ISO 639-1", re.compile(r"^[A-Z]{2}$")),
    "currency": ("ISO 4217", re.compile(r"^[A-Z]{3}$")),
    "unit": ("SAP 内部计量单位", re.compile(r"^[A-Z0-9]{1,3}$")),
}

# field_mapping 的 status -> semantic_match。None 表示"不确定，需人工确认"。
SEMANTIC_MATCH_BY_STATUS: dict[str, bool | None] = {
    "suggested": True,
    "needs_review": None,
    "possible_false_friend": False,
    "no_confident_target": False,
}

SEVERITY: dict[str, str] = {
    "target_not_creatable_or_updatable": "high",
    "unmapped_target_key": "high",
    "required_field_missing": "high",
    "duplicate_primary_key": "high",
    "max_length_overflow": "high",
    "type_not_parseable": "high",
    "value_not_in_allowed_values": "high",
    "no_target_in_schema": "high",
    "normalization_required": "medium",
    "possible_false_friend_target": "medium",
    "mapping_needs_review": "low",
}

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------
# 目标类型可解析性
# --------------------------------------------------------------------------
DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y")


def parse_as_edm(value: str, edm_type: str) -> bool:
    """源值能否解析成目标 Edm 类型。"""
    if edm_type == "Edm.String":
        return True
    if edm_type in ("Edm.DateTime", "Edm.DateTimeOffset"):
        for fmt in DATE_FORMATS:
            try:
                datetime.strptime(value, fmt)
                return True
            except ValueError:
                continue
        try:
            # DateTimeOffset 允许带时区偏移的 ISO 8601
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    if edm_type == "Edm.Decimal":
        try:
            float(value)
            return True
        except ValueError:
            return False
    if edm_type == "Edm.Boolean":
        return value in ("X", "", "true", "false", "True", "False")
    if edm_type == "Edm.Guid":
        try:
            uuid.UUID(value)
            return True
        except ValueError:
            return False
    return True  # 未知类型不臆断


# --------------------------------------------------------------------------
# 载入
# --------------------------------------------------------------------------
def load_target_index(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{entity_name}.{field['name']}": {**field, "_entity": entity_name}
        for entity_name, entity in schema["entities"].items()
        for field in entity["fields"]
    }


def unverified_disclaimer(schema: dict[str, Any], target: dict[str, Any]) -> str | None:
    """基于 unverified 字段标注得出的结论必须带免责说明（挂载 schema 的 _verification）。"""
    if target.get("verification_status") == "verified":
        return None
    return (
        f"此结论基于未核实的字段标注（{target['_entity']}.{target['name']} "
        f"verification_status={target.get('verification_status', 'unknown')}），"
        f"需以官方 metadata 复核。"
        + schema.get("_verification", {}).get("downstream_note_zh", "")
    )


# --------------------------------------------------------------------------
# 字段级校验
# --------------------------------------------------------------------------
def classify_verdict(semantic_match: bool | None, loadable: bool | None) -> str:
    if loadable is None:
        return "no_target"
    if semantic_match is True and loadable is False:
        return "mapping_ok_but_not_loadable"
    if semantic_match is True and loadable is True:
        return "loadable_ok"
    return "needs_human_decision"


def check_normalization(
    legacy_field: str, values: list[str], target: dict[str, Any] | None
) -> dict[str, Any] | None:
    domain = next(
        (
            (name, spec)
            for name, spec in sorted(NORMALIZATION_DOMAINS.items())
            if name in legacy_field.lower()
        ),
        None,
    )
    if domain is None:
        return None

    name, (standard, canonical) = domain
    distribution = Counter(values)
    offenders = sorted(v for v in distribution if not canonical.match(v))
    if not offenders:
        return None

    max_length = target.get("max_length") if target else None
    return {
        "issue_type": "normalization_required",
        "severity": SEVERITY["normalization_required"],
        "field": legacy_field,
        "detail_zh": (
            f"字段 `{legacy_field}` 需先归一化为 {standard} 代码。"
            f"当前 {len(distribution)} 种写法中有 {len(offenders)} 种不符合规范形态"
            f"（{canonical.pattern}）"
            + (f"，且目标字段上限仅 {max_length} 字符" if max_length else "")
            + "。"
        ),
        "non_conforming_values": [
            {"value": v, "count": distribution[v]} for v in offenders
        ],
        "suggestion_zh": f"建立 {standard} 归一化映射表，在加载前统一转换；不要截断。",
    }


# --------------------------------------------------------------------------
# 主校验
# --------------------------------------------------------------------------
def validate() -> dict[str, Any]:
    records: list[dict[str, Any]] = json.loads(LEGACY_PATH.read_text(encoding="utf-8"))
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    mapping: dict[str, Any] = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    targets = load_target_index(schema)

    field_view: list[dict[str, Any]] = []
    record_issues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    touched_entities: set[str] = set()

    for entry in sorted(mapping["mappings"], key=lambda m: m["legacy_field"]):
        legacy_field = entry["legacy_field"]
        status = entry["status"]
        semantic_match = SEMANTIC_MATCH_BY_STATUS[status]
        qualified = entry["recommendation"]
        target = targets.get(qualified) if qualified else None

        values = [r.get(legacy_field) for r in records]
        present = [(r, str(r[legacy_field])) for r in records if not is_missing(r.get(legacy_field))]

        field_issues: list[dict[str, Any]] = []
        loadable: bool | None = None
        loadable_reason = None

        if target is None:
            loadable_reason = "target_missing"
            field_issues.append({
                "issue_type": "no_target_in_schema",
                "severity": SEVERITY["no_target_in_schema"],
                "field": legacy_field,
                "detail_zh": (
                    f"字段 `{legacy_field}` 在目标 schema 中没有落点"
                    f"（field_mapping status={status}，最高置信度 {entry['confidence']:.3f}）。"
                ),
                "suggestion_zh": (
                    "确认是否属于本 schema 未展开的子实体（如 A_AddressEmailAddress / "
                    "A_AddressPhoneNumber），或作为 Fit-to-Standard 差异项走人工决策。"
                ),
            })
        else:
            touched_entities.add(target["_entity"])
            creatable = bool(target.get("is_creatable"))
            updatable = bool(target.get("is_updatable"))
            loadable = creatable or updatable
            if not loadable:
                loadable_reason = "target_not_creatable_or_updatable"
                field_issues.append({
                    "issue_type": "target_not_creatable_or_updatable",
                    "severity": SEVERITY["target_not_creatable_or_updatable"],
                    "field": legacy_field,
                    "detail_zh": (
                        f"目标字段 `{qualified}` 的 sap:creatable 与 sap:updatable 均为 false，"
                        f"不可写入。"
                        + (
                            "该字段的映射语义是正确的，但它不能作为加载目标。"
                            if semantic_match else ""
                        )
                    ),
                    "suggestion_zh": (
                        "仅可作 lineage / 参考保留，不纳入加载负载；"
                        "若确需保留源系统的创建时间，改用可写的自定义扩展字段。"
                    ),
                })

        if status == "possible_false_friend":
            field_issues.append({
                "issue_type": "possible_false_friend_target",
                "severity": SEVERITY["possible_false_friend_target"],
                "field": legacy_field,
                "detail_zh": (
                    f"field_mapping 将 `{legacy_field}` 标为疑似假朋友："
                    f"候选 `{qualified}` 与其零共享词元且未命中 alias。"
                ),
                "suggestion_zh": "人工决策：目标端很可能没有正确落点，不要据此建立映射。",
            })
        elif status == "needs_review":
            field_issues.append({
                "issue_type": "mapping_needs_review",
                "severity": SEVERITY["mapping_needs_review"],
                "field": legacy_field,
                "detail_zh": (
                    f"映射置信度 {entry['confidence']:.3f}（{entry['band']}），未达 high 档。"
                ),
                "suggestion_zh": "人工确认映射目标后再纳入加载。",
            })

        normalization = check_normalization(legacy_field, [v for _, v in present], target)
        if normalization:
            field_issues.append(normalization)

        # ---- 记录级校验 ----
        counts: Counter[str] = Counter()
        if target is not None:
            disclaimer = unverified_disclaimer(schema, target)
            max_length = target.get("max_length")
            edm_type = target.get("type", "Edm.String")
            allowed = target.get("allowed_values")
            required = target.get("is_key") or target.get("nullable") is False

            if required:
                for record in records:
                    if is_missing(record.get(legacy_field)):
                        counts["required_field_missing"] += 1
                        record_issues[str(record[RECORD_ID_FIELD])].append({
                            "field": legacy_field,
                            "target": qualified,
                            "issue_type": "required_field_missing",
                            "severity": SEVERITY["required_field_missing"],
                            "value": None,
                            "detail_zh": f"目标字段 `{qualified}` 必填（is_key 或 nullable=false），源值缺失。",
                            "suggestion_zh": "补数据，或在加载前用默认值/派生规则填充。",
                            "based_on_unverified": disclaimer is not None,
                            **({"unverified_disclaimer_zh": disclaimer} if disclaimer else {}),
                        })

            if target.get("is_key"):
                seen = Counter(v for _, v in present)
                for record, value in present:
                    if seen[value] > 1:
                        counts["duplicate_primary_key"] += 1
                        record_issues[str(record[RECORD_ID_FIELD])].append({
                            "field": legacy_field,
                            "target": qualified,
                            "issue_type": "duplicate_primary_key",
                            "severity": SEVERITY["duplicate_primary_key"],
                            "value": value,
                            "detail_zh": f"值 `{value}` 在映射到主键字段 `{qualified}` 后出现 {seen[value]} 次。",
                            "suggestion_zh": "先做实体解析/去重，或改用外部编号字段承载遗留主键。",
                            "based_on_unverified": disclaimer is not None,
                            **({"unverified_disclaimer_zh": disclaimer} if disclaimer else {}),
                        })

            for record, value in present:
                if max_length is not None and len(value) > max_length:
                    counts["max_length_overflow"] += 1
                    issue = {
                        "field": legacy_field,
                        "target": qualified,
                        "issue_type": "max_length_overflow",
                        "severity": SEVERITY["max_length_overflow"],
                        "value": value,
                        "detail_zh": (
                            f"源值长度 {len(value)} 超过目标字段 `{qualified}` 上限 {max_length}。"
                        ),
                        "suggestion_zh": (
                            "优先归一化（如国家名 -> ISO 代码）；确无标准代码时才考虑截断，"
                            "并评估是否溢出到后续行字段（如 Name2/Name3）。"
                        ),
                        "based_on_unverified": disclaimer is not None,
                    }
                    if disclaimer:
                        issue["unverified_disclaimer_zh"] = disclaimer
                    record_issues[str(record[RECORD_ID_FIELD])].append(issue)

                if not parse_as_edm(value, edm_type):
                    counts["type_not_parseable"] += 1
                    record_issues[str(record[RECORD_ID_FIELD])].append({
                        "field": legacy_field,
                        "target": qualified,
                        "issue_type": "type_not_parseable",
                        "severity": SEVERITY["type_not_parseable"],
                        "value": value,
                        "detail_zh": f"源值无法解析为目标类型 {edm_type}。",
                        "suggestion_zh": "确认源格式并建立转换规则；日期需明确源时区与格式。",
                        "based_on_unverified": disclaimer is not None,
                        **({"unverified_disclaimer_zh": disclaimer} if disclaimer else {}),
                    })

                if allowed and value not in allowed:
                    counts["value_not_in_allowed_values"] += 1
                    record_issues[str(record[RECORD_ID_FIELD])].append({
                        "field": legacy_field,
                        "target": qualified,
                        "issue_type": "value_not_in_allowed_values",
                        "severity": SEVERITY["value_not_in_allowed_values"],
                        "value": value,
                        "detail_zh": f"值不在目标字段允许的取值集合 {allowed} 内。",
                        "suggestion_zh": "建立受控值映射表，加载前转换。",
                        "based_on_unverified": disclaimer is not None,
                        **({"unverified_disclaimer_zh": disclaimer} if disclaimer else {}),
                    })

        field_view.append({
            "legacy_field": legacy_field,
            "target": qualified,
            "mapping_status": status,
            "mapping_confidence": entry["confidence"],
            "semantic_match": semantic_match,
            "loadable": loadable,
            "loadable_reason": loadable_reason,
            "verdict": classify_verdict(semantic_match, loadable),
            "based_on_unverified": bool(
                target is not None and unverified_disclaimer(schema, target)
            ),
            "target_constraints": None if target is None else {
                "type": target.get("type"),
                "max_length": target.get("max_length"),
                "nullable": target.get("nullable"),
                "is_key": target.get("is_key"),
                "is_creatable": target.get("is_creatable"),
                "is_updatable": target.get("is_updatable"),
                "allowed_values": target.get("allowed_values"),
                "verification_status": target.get("verification_status"),
            },
            "record_issue_counts": dict(sorted(counts.items())),
            "field_issues": sorted(field_issues, key=lambda i: SEVERITY_RANK[i["severity"]]),
        })

    # ---- 目标主键覆盖：被映射触及的实体，其主键是否有源字段承载 ----
    mapped_targets = {e["target"] for e in field_view if e["target"]}
    for entity_name in sorted(touched_entities):
        for key_field in sorted(schema["entities"][entity_name]["keys"]):
            qualified = f"{entity_name}.{key_field}"
            if qualified not in mapped_targets:
                field_view.append({
                    "legacy_field": None,
                    "target": qualified,
                    "mapping_status": "unmapped",
                    "mapping_confidence": None,
                    "semantic_match": None,
                    "loadable": None,
                    "loadable_reason": "no_source_field_mapped",
                    "verdict": "no_source",
                    "based_on_unverified": False,
                    "target_constraints": None,
                    "record_issue_counts": {},
                    "field_issues": [{
                        "issue_type": "unmapped_target_key",
                        "severity": SEVERITY["unmapped_target_key"],
                        "field": qualified,
                        "detail_zh": (
                            f"目标实体 `{entity_name}` 的主键字段 `{key_field}` 没有任何 legacy 字段映射到它。"
                        ),
                        "suggestion_zh": (
                            "主键需由目标系统的编号范围生成（内部编号），"
                            "或由外部编号字段 + 编号转换表推导；不能留空。"
                        ),
                    }],
                })

    # ---- 汇总 ----
    all_issues = [i for entry in field_view for i in entry["field_issues"]]
    all_issues += [i for issues in record_issues.values() for i in issues]

    by_type = Counter(i["issue_type"] for i in all_issues)
    by_severity = Counter(i["severity"] for i in all_issues)

    implemented = {
        "1_readonly_block": "target_not_creatable_or_updatable",
        "2_required_and_key": "required_field_missing / unmapped_target_key",
        "3_duplicate_primary_key": "duplicate_primary_key",
        "4_max_length_overflow": "max_length_overflow",
        "5_type_parseability": "type_not_parseable",
        "6_allowed_values": "value_not_in_allowed_values",
        "7_normalization": "normalization_required",
        "8_missing_target": "no_target_in_schema",
    }

    return {
        "_meta": {
            "disclaimer": "本报告只诊断并给出建议，不修改源数据、不做任何自动修复。",
            "orthogonality_note_zh": (
                "semantic_match 与 loadable 是两个正交维度，报告中分别独立表达。"
                "semantic_match=true 且 loadable=false 的字段（如 created_date -> CreationDate）"
                "映射语义正确但不可作为加载目标，只能作 lineage / 参考，不得记为『通过』。"
            ),
            "reuse_note_zh": (
                "映射结论（阈值、no_confident_target、possible_false_friend）直接沿用 "
                "field_mapping 的输出，本工具不重新判定。"
            ),
            "sources": {
                "legacy": str(LEGACY_PATH.relative_to(PROJECT_ROOT)),
                "schema": str(SCHEMA_PATH.relative_to(PROJECT_ROOT)),
                "schema_version": schema["schema_version"],
                "mapping": str(MAPPING_PATH.relative_to(PROJECT_ROOT)),
                "mapping_content_sha256": mapping["_run_info"]["content_sha256"],
            },
            "record_count": len(records),
            "records_with_issues": len(record_issues),
            "records_clean": len(records) - len(record_issues),
            "checks_implemented": implemented,
        },
        "summary": {
            "by_issue_type": dict(sorted(by_type.items())),
            "by_severity": {k: by_severity.get(k, 0) for k in ("high", "medium", "low")},
            "verdicts": dict(sorted(Counter(e["verdict"] for e in field_view).items())),
        },
        "field_view": field_view,
        "record_view": [
            {
                "record_id": record_id,
                "issue_count": len(issues),
                "issues": sorted(
                    issues, key=lambda i: (SEVERITY_RANK[i["severity"]], i["field"])
                ),
            }
            for record_id, issues in sorted(record_issues.items())
        ],
        "deferred_checks": [
            {
                "check": "country_grouped_format_validation",
                "fields": ["tax_number", "postal_code"],
                "status": "not_implemented",
                "reason_zh": (
                    "税号与邮编的格式因国家而异（DE=DE+9位数字 / US=NN-NNNNNNN / JP=13位数字；"
                    "邮编 DE=5位 / JP=NNN-NNNN / US=NNNNN[-NNNN]）。"
                    "跨国混合校验必然误报，需先按 country 归一化并分组后再逐组校验。"
                ),
                "blocked_by_zh": "依赖 normalization_required（country）先落地。",
            }
        ],
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    report = attach_run_info(validate())

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    meta, summary = report["_meta"], report["summary"]
    print(f"Records          : {meta['record_count']}")
    print(f"  with issues    : {meta['records_with_issues']}")
    print(f"  clean          : {meta['records_clean']}")
    print(f"Content          : sha256 {report['_run_info']['content_sha256'][:16]}\n")

    print("Field verdicts (semantic_match x loadable):")
    for verdict, count in summary["verdicts"].items():
        print(f"  {verdict:<30} {count}")

    print("\nIssues by severity:")
    for severity, count in summary["by_severity"].items():
        print(f"  {severity:<8} {count}")

    print("\nIssues by type:")
    for issue_type, count in summary["by_issue_type"].items():
        print(f"  {issue_type:<36} {count:>4}   [{SEVERITY[issue_type]}]")

    print("\nField view:")
    header = f"  {'legacy field':<16} {'target':<46} {'sem':<6} {'load':<6} verdict"
    print(header)
    for entry in report["field_view"]:
        sem = {True: "true", False: "false", None: "?"}[entry["semantic_match"]]
        load = {True: "true", False: "false", None: "n/a"}[entry["loadable"]]
        print(
            f"  {str(entry['legacy_field'] or '-'):<16} {str(entry['target'] or '-'):<46} "
            f"{sem:<6} {load:<6} {entry['verdict']}"
        )

    print("\nDeferred checks:")
    for deferred in report["deferred_checks"]:
        print(f"  - {deferred['check']} ({deferred['status']}): {deferred['reason_zh'][:40]}…")

    print(f"\nWrote report -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
