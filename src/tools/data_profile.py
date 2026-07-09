"""对遗留系统数据做自动化质量体检，输出结构化画像报告。

四个检测维度：
  - 完整性     每字段缺失率（None / 空串 / 纯空格都算缺失）
  - 唯一性     去重取值数、distinct_ratio，标记疑似唯一标识符
  - 格式一致性 把值抽象成"格式签名"，统计同一字段下有几种签名
  - 取值分布   低基数字段附取值分布，用于发现 DE/de/Germany/GER 这类混用

格式一致性只对结构化字段有意义：公司名、街道这类自由文本天然格式多样，
强行检测会大量误报，因此自由文本字段的 format_variants 记为 None（不适用）。
字段是否为自由文本，优先取 schema 的 is_free_text 标注，取不到再用启发式兜底。

用法：
    python src/tools/data_profile.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 阈值（可调）
# --------------------------------------------------------------------------
IDENTIFIER_DISTINCT_RATIO = 0.98   # distinct_ratio 高于此值 → 疑似唯一标识符
FREE_TEXT_DISTINCT_RATIO = 0.50    # 启发式自由文本判定：高基数 …
FREE_TEXT_AVG_LENGTH = 15.0        # … 且平均长度大
LOW_CARDINALITY_MAX_DISTINCT = 20  # 取值数 ≤ 此值 → 附取值分布
MISSING_RATE_FLAG_THRESHOLD = 0.10  # 缺失率高于此值 → 进 quality_flags
MAX_SIGNATURES_IN_REPORT = 6       # 每字段最多列出几种格式签名

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = PROJECT_ROOT / "data" / "legacy" / "legacy_vendors.json"
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "business_partner_target_schema.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "synthetic" / "vendor_profile_report.json"


# --------------------------------------------------------------------------
# 格式签名
# --------------------------------------------------------------------------
def format_signature(value: str) -> str:
    """把值抽象成格式签名：数字→D、字母→A、其它符号原样保留，连续同类压缩。

    >>> format_signature("12-5346539")
    'D-D'
    >>> format_signature("+49(0) 172814513")
    '+D(D) D'
    """
    tokens: list[str] = []
    for char in value:
        if char.isdigit():
            token = "D"
        elif char.isalpha():
            token = "A"
        else:
            token = char
        if not tokens or tokens[-1] != token:
            tokens.append(token)
    return "".join(tokens)


# --------------------------------------------------------------------------
# 缺失判定
# --------------------------------------------------------------------------
def is_missing(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip() == ""


# --------------------------------------------------------------------------
# 自由文本判定：schema 优先，启发式兜底
# --------------------------------------------------------------------------
def _normalize_field_name(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def load_schema_free_text_fields(schema_path: Path) -> set[str]:
    """收集 schema 中所有标了 is_free_text 的目标端字段名（已归一化）。"""
    if not schema_path.exists():
        return set()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    flagged = set()
    for entity in schema.get("entities", {}).values():
        for field in entity.get("fields", []):
            if field.get("is_free_text"):
                flagged.add(_normalize_field_name(field["name"]))
    return flagged


def resolve_free_text(
    field: str,
    distinct_ratio: float,
    avg_length: float,
    schema_free_text: set[str],
) -> tuple[bool, str]:
    """返回 (是否自由文本, 判定来源)。

    遗留字段名（vendor_name）与目标端字段名（OrganizationBPName1）刻意不同，
    因此 schema 匹配只能覆盖少数字段（如 street ⊂ streetname），其余靠启发式。

    已知的两个启发式边界情况，经评审决定【不打补丁】：
      - city  被判为结构化（avg_length 8.4 < 15），格式签名产生弱误报；
      - email 被判为自由文本（distinct_ratio 0.73 且 avg_length 25.3），
        虽然结果恰好正确，但理由是错的——email 其实是结构化字段。
    根因是启发式无法区分"短自由文本"与"长结构化字段"，为此堆规则不划算。
    正确方向：待 field_mapping 建立 legacy→target 映射后，
    自由文本判定改以 schema 的 is_free_text 标注为准，本启发式降级为纯兜底。
    """
    normalized = _normalize_field_name(field)
    if normalized in schema_free_text:
        return True, "schema:exact"
    # 排序遍历：str 哈希随机化会让 set 的迭代顺序逐进程变化，
    # 而 street 同时匹配 streetname / streetprefixname / streetsuffixname，
    # 不排序则报告里的 free_text_source 不可复现。
    for target in sorted(schema_free_text):
        if normalized and (normalized in target or target in normalized):
            return True, f"schema:substring({target})"

    heuristic = distinct_ratio > FREE_TEXT_DISTINCT_RATIO and avg_length > FREE_TEXT_AVG_LENGTH
    return heuristic, "heuristic" if heuristic else "heuristic:structured"


# --------------------------------------------------------------------------
# 单字段画像
# --------------------------------------------------------------------------
def profile_field(
    field: str,
    values: list[Any],
    schema_free_text: set[str],
) -> dict[str, Any]:
    total = len(values)
    present = [v for v in values if not is_missing(v)]
    missing_count = total - len(present)

    as_text = [str(v) for v in present]
    distinct = Counter(as_text)
    distinct_count = len(distinct)
    distinct_ratio = distinct_count / len(present) if present else 0.0
    avg_length = sum(len(v) for v in as_text) / len(as_text) if as_text else 0.0

    is_identifier = distinct_ratio > IDENTIFIER_DISTINCT_RATIO
    is_free_text, free_text_source = resolve_free_text(
        field, distinct_ratio, avg_length, schema_free_text
    )

    report: dict[str, Any] = {
        "record_count": total,
        "missing_count": missing_count,
        "missing_rate": round(missing_count / total, 4) if total else 0.0,
        "distinct_count": distinct_count,
        "distinct_ratio": round(distinct_ratio, 4),
        "avg_length": round(avg_length, 2),
        "is_probable_identifier": is_identifier,
        "is_free_text": is_free_text,
        "free_text_source": free_text_source,
    }

    if is_free_text:
        # 坑 #2：自由文本天然格式多样，做格式一致性检测只会产生噪音。
        report["format_variants"] = None
        report["format_signatures"] = None
    else:
        signatures = Counter(format_signature(v) for v in as_text)
        report["format_variants"] = len(signatures)
        report["format_signatures"] = [
            {
                "signature": sig,
                "count": count,
                "example": next(v for v in as_text if format_signature(v) == sig),
            }
            for sig, count in signatures.most_common(MAX_SIGNATURES_IN_REPORT)
        ]

    if 0 < distinct_count <= LOW_CARDINALITY_MAX_DISTINCT:
        report["value_distribution"] = [
            {"value": value, "count": count}
            for value, count in distinct.most_common()
        ]

    return report


# --------------------------------------------------------------------------
# 汇总层：quality_flags
# --------------------------------------------------------------------------
def build_quality_flags(fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []

    for field, stats in fields.items():
        if stats["missing_rate"] > MISSING_RATE_FLAG_THRESHOLD:
            flags.append({
                "field": field,
                "issue_type": "completeness",
                "severity": "high" if stats["missing_rate"] > 0.25 else "medium",
                "message": (
                    f"字段 `{field}` 缺失率 {stats['missing_rate']:.1%} "
                    f"（{stats['missing_count']}/{stats['record_count']} 条），"
                    f"超过 {MISSING_RATE_FLAG_THRESHOLD:.0%} 阈值。"
                ),
            })

    for field, stats in fields.items():
        variants = stats["format_variants"]
        if variants is None or variants <= 1:
            continue
        if stats["is_probable_identifier"]:
            continue
        examples = ", ".join(
            f"`{s['example']}`" for s in stats["format_signatures"][:3]
        )
        flags.append({
            "field": field,
            "issue_type": "format_consistency",
            "severity": "high" if variants > 5 else "medium",
            "message": (
                f"字段 `{field}` 存在 {variants} 种不同的格式签名，"
                f"取值写法不统一。示例：{examples}。"
            ),
        })

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: (severity_rank[f["severity"]], f["field"]))
    return flags


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def profile_records(
    records: list[dict[str, Any]],
    schema_free_text: set[str] | None = None,
) -> dict[str, Any]:
    schema_free_text = schema_free_text or set()

    field_names: list[str] = []
    for record in records:
        for key in record:
            if key not in field_names:
                field_names.append(key)

    fields = {
        field: profile_field(
            field, [record.get(field) for record in records], schema_free_text
        )
        for field in field_names
    }

    return {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "record_count": len(records),
            "field_count": len(field_names),
            "thresholds": {
                "identifier_distinct_ratio": IDENTIFIER_DISTINCT_RATIO,
                "free_text_distinct_ratio": FREE_TEXT_DISTINCT_RATIO,
                "free_text_avg_length": FREE_TEXT_AVG_LENGTH,
                "low_cardinality_max_distinct": LOW_CARDINALITY_MAX_DISTINCT,
                "missing_rate_flag_threshold": MISSING_RATE_FLAG_THRESHOLD,
            },
        },
        "fields": fields,
        "quality_flags": build_quality_flags(fields),
    }


def main() -> None:
    # flags 里含中文与日文示例值，Windows 控制台默认 GBK 会乱码。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    records = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    schema_free_text = load_schema_free_text_fields(SCHEMA_PATH)

    report = profile_records(records, schema_free_text)
    report["_meta"]["source_file"] = str(INPUT_PATH.relative_to(PROJECT_ROOT))
    report["_meta"]["schema_file"] = (
        str(SCHEMA_PATH.relative_to(PROJECT_ROOT)) if SCHEMA_PATH.exists() else None
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Records : {report['_meta']['record_count']}")
    print(f"Fields  : {report['_meta']['field_count']}")
    print(f"Flags   : {len(report['quality_flags'])}\n")

    print("Quality flags:")
    for flag in report["quality_flags"]:
        print(f"  [{flag['severity']:<6}] {flag['message']}")

    print(f"\nWrote report -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
