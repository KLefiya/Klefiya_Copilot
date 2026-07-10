"""实体解析：找出遗留供应商主数据里"其实是同一家公司"的重复记录。

用 Splink（Fellegi-Sunter 概率匹配）做 dedupe_only，输出疑似重复组 + 质量评估。
**只诊断、只建议，不修改源数据、不做任何合并。**

【为什么需要标准化预处理】
    脏数据里同一家供应商写成 "Müller GmbH & Co. KG" / "MÜLLER GMBH UND CO. KG" /
    "  müller gmbh&co.kg"。直接比字符串相似度会被大小写、空格、标点、
    法律形式后缀的写法差异淹没。因此先做标准化，再交给 Splink 比对：
        vendor_name -> name_norm（归一）+ name_core（去后缀）+ legal_form（规范后缀）
        country     -> country_code（ISO 3166-1 alpha-2）
    这一步是 ER 的标准做法，不是"偷看答案"：LEGAL_FORMS 编码的是真实世界的
    法律形式（GmbH / K.K. / LLC …），与 generate_legacy_vendors.py 的变体表
    重合是因为二者描述同一个客观事实，不是因为读了它。

【关于 Splink 的 salting bug】
    requirements.txt 里记录过：splink 4.0.16 下 estimate_u_using_random_sampling()
    会抛 "Salting partitions must be specified"。**在 splink 4.0.16 + duckdb 1.5.4
    上实测未复现**（max_pairs 到 1e7、含 seed 均正常），因此这里走标准训练路径：
        deterministic rules 估 λ  ->  random sampling 估 u  ->  EM 估 m
    随机抽样得到的 u 比 EM 反推的 u 更可靠，没有理由为了绕一个不存在的 bug
    而降级。若在别的环境上重新遇到该报错，退路是跳过 estimate_u，改用
    estimate_parameters_using_expectation_maximisation(fix_u_probabilities=False)。

【评估为什么以 cluster 级为主】
    业务问的是"这几条记录有没有被正确归成同一家供应商"，是 cluster 级问题。
    pairwise precision/recall 会被大量 singleton 稀释，且无法区分
    "把一个 3 条的组拆成 2+1"（业务上是漏了）与"完全没找到"。
    因此主指标是 cluster 级精确匹配，pairwise 作为参考一并给出。

用法：
    python src/tools/entity_resolution.py
"""

from __future__ import annotations

import json
import logging
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import attach_run_info  # noqa: E402  复用 _run_info 隔离逻辑

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PATH = PROJECT_ROOT / "data" / "legacy" / "legacy_vendors.json"
GROUND_TRUTH_PATH = PROJECT_ROOT / "data" / "legacy" / "legacy_vendors_ground_truth.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "synthetic" / "vendor_duplicate_report.json"

RECORD_ID_FIELD = "legacy_vendor_id"

# --------------------------------------------------------------------------
# 阈值
# --------------------------------------------------------------------------
MATCH_THRESHOLD = 0.95      # 高于此匹配概率的记录对进入聚类
REVIEW_THRESHOLD = 0.99     # 组内最弱边低于此值 -> needs_review
DETERMINISTIC_RECALL = 0.8  # 确定性规则的假定召回率（λ 估计的先验，见 _meta）
U_SAMPLING_MAX_PAIRS = 1e7
U_SAMPLING_SEED = 20260709

# 展示用：这些字段在组内出现分歧时值得高亮
COMPARED_FIELDS = (
    "vendor_name", "country", "city", "street", "postal_code",
    "tax_number", "email", "phone", "currency", "created_date",
)

# 组内出现冲突就必须人工复核的字段（强标识符，不该在同一实体内取不同值）
CONFLICT_CRITICAL_FIELDS = ("tax_number", "email")


# --------------------------------------------------------------------------
# 标准化：法律形式后缀
# --------------------------------------------------------------------------
# 真实世界的法律形式及其常见书写变体。键是"经 _basic_norm 归一后的表面形式"，
# 值是规范化 token。最长匹配优先（否则 "gmbh and co kg" 会先被 "gmbh" 吃掉）。
LEGAL_FORMS: dict[str, str] = {
    # 德国
    "gmbh and co kg": "gmbh_co_kg",
    "gmbh": "gmbh",
    "mbh": "gmbh",
    "ag": "ag",
    "a g": "ag",
    # 美国
    "inc": "inc",
    "incorporated": "inc",
    "llc": "llc",
    "l l c": "llc",
    "corp": "corp",
    "corporation": "corp",
    # 日本
    "kk": "kk",
    "k k": "kk",
    "kabushiki kaisha": "kk",
    "co ltd": "co_ltd",
    "company limited": "co_ltd",
}

# 国家写法 -> ISO 3166-1 alpha-2。与 pre_migration_validation 标记的
# normalization_required(country) 是同一件事，这里先行落地。
COUNTRY_CODES: dict[str, str] = {
    "de": "DE", "germany": "DE", "deutschland": "DE", "ger": "DE",
    "us": "US", "usa": "US", "united states": "US", "u s a": "US",
    "jp": "JP", "japan": "JP", "jpn": "JP",
}


def _basic_norm(value: str) -> str:
    """小写 + 去音标 + & / und -> and + 标点转空格 + 压缩空白。"""
    text = unicodedata.normalize("NFKD", value.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # "und" 只有作为独立词时才是连接词，不能碰 "Bund" 这类词的内部
    return re.sub(r"\bund\b", "and", text)


def split_legal_form(normalized: str) -> tuple[str, str | None]:
    """把归一后的名字切成 (核心名, 规范法律形式)。匹配不到则法律形式为 None。"""
    for surface in sorted(LEGAL_FORMS, key=lambda s: len(s.split()), reverse=True):
        if normalized == surface:
            return "", LEGAL_FORMS[surface]
        if normalized.endswith(" " + surface):
            return normalized[: -len(surface) - 1].strip(), LEGAL_FORMS[surface]
    return normalized, None


def normalize_name(raw: str) -> tuple[str, str, str | None]:
    """raw -> (name_norm, name_core, legal_form)。name_norm 含规范化后的后缀。"""
    basic = _basic_norm(raw)
    core, form = split_legal_form(basic)
    name_norm = f"{core} {form}".strip() if form else core
    return name_norm, core, form


def normalize_country(raw: str | None) -> str | None:
    if raw is None:
        return None
    return COUNTRY_CODES.get(_basic_norm(raw))


# --------------------------------------------------------------------------
# 差异归因：两个原始名字之间，哪几种书写差异是真正的成因
# --------------------------------------------------------------------------
# 【为什么这里的折叠用"删除"而不是 _basic_norm 的"替换成空格"】
# 归因靠消融：关掉某一步，若两名字不再相等，则该步是差异的成因。这只有在各步
# 互不干扰时才成立。_basic_norm 把标点替换成空格 —— 于是"关掉空白折叠"必然失败，
# whitespace 会被误报到每一对带标点的名字上。改成删除后，四步都是删除/替换，
# 彼此可交换，消融才真正只隔离出一个维度。
#
# 两套折叠服务两个目的，不要合并：
#   _basic_norm  标点 -> 空格，保留分词，供 LEGAL_FORMS 的多词后缀匹配（"k k"）
#   _fold_*      标点 / 空白 -> 删除，供差异归因的消融
_DIFF_DIMENSIONS = ("case", "ampersand", "punctuation", "whitespace")


def _fold_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _fold(text: str, skip: str | None = None) -> str:
    """删除式折叠，可跳过其中一个维度。音标折叠始终执行（生成器不改动音标）。"""
    result = _fold_accents(text)
    if skip != "case":
        result = result.lower()
    if skip != "ampersand":
        # "&" -> "and" 不补两侧空格：补了就会凭空造出一个空白差异，
        # 让 skip="whitespace" 的消融把 ampersand 的效果误报成 whitespace。
        # 大小写不敏感：跳过 case 时仍需折叠 "UND"，否则 case 会被误报。
        result = re.sub(r"\bund\b", "and", result.replace("&", "and"), flags=re.IGNORECASE)
    if skip != "punctuation":
        result = re.sub(r"[^\w\s]+", "", result)  # 删除，不替换成空格
    if skip != "whitespace":
        result = re.sub(r"\s+", "", result)
    return result


def _split_raw_suffix(raw: str) -> tuple[str, str, str | None]:
    """把原始名字切成 (核心的原始写法, 后缀的原始写法, 规范法律形式)。

    后缀在原文里的边界无法由 token 数推出（"K.K." 是 1 个原始 token，
    归一后是 2 个），因此反向扫描：取归一后等于 core_norm 的最长原始前缀。
    """
    basic = _basic_norm(raw)
    core_norm, form = split_legal_form(basic)
    if form is None:
        return raw, "", None
    for cut in range(len(raw), -1, -1):
        if _basic_norm(raw[:cut]) == core_norm:
            return raw[:cut], raw[cut:], form
    return raw, "", form


def diff_tags(a: str, b: str) -> list[str]:
    """a 与 b 之间存在哪几类书写差异。完全归一后仍不等则返回 ["different_name"]。

    诊断/展示用途，不是判定依据——判定由 Splink 的匹配概率负责。
    """
    if a == b:
        return []
    if normalize_name(a)[0] != normalize_name(b)[0]:
        return ["different_name"]

    core_a, suffix_a, form_a = _split_raw_suffix(a)
    core_b, suffix_b, form_b = _split_raw_suffix(b)

    tags: list[str] = []
    if _fold(suffix_a) != _fold(suffix_b):
        # 后缀写法本身不同（Inc. / Incorporated）。把两边的后缀替换成同一个规范
        # token 再做维度消融，否则 "Incorporated" 里多出的字母会把其它维度也点亮。
        tags.append("legal_suffix")
        left, right = f"{core_a} {form_a}", f"{core_b} {form_b}"
    else:
        # 后缀折叠后相同（K.K. / KK）。保留原文，让 punctuation 维度如实报出来。
        left, right = a, b

    tags += [
        dimension for dimension in _DIFF_DIMENSIONS
        if _fold(left, skip=dimension) != _fold(right, skip=dimension)
    ]
    return tags or ["other"]


# --------------------------------------------------------------------------
# 训练与预测
# --------------------------------------------------------------------------
def build_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(records).rename(columns={RECORD_ID_FIELD: "unique_id"})
    normalized = frame["vendor_name"].map(normalize_name)
    frame["name_norm"] = [n for n, _, _ in normalized]
    frame["name_core"] = [c for _, c, _ in normalized]
    frame["legal_form"] = [f for _, _, f in normalized]
    frame["country_code"] = frame["country"].map(normalize_country)
    return frame


def run_splink(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """返回 (edges, clusters, 训练元信息)。"""
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on
    import splink.comparison_library as cl

    # 确定性规则：命中即几乎必然同一实体。用于估计 λ（两条随机记录匹配的先验概率）。
    deterministic_rules = [
        "l.tax_number = r.tax_number",
        "l.email = r.email",
        "l.name_norm = r.name_norm and l.postal_code = r.postal_code",
    ]

    # 阻断规则：只有落进同一个 block 的记录对才会被打分。
    # 变体记录会改名、改 country、并可能抹掉 tax/email/phone，因此单一阻断规则不够，
    # 用四条互补规则取并集。SQL 的 NULL != NULL，缺失值天然不会被错误阻断到一起。
    blocking_rules = [
        block_on("name_norm"),
        block_on("postal_code"),
        block_on("tax_number"),
        block_on("email"),
    ]

    settings = SettingsCreator(
        link_type="dedupe_only",
        comparisons=[
            cl.JaroWinklerAtThresholds("name_core", [0.95, 0.88]),
            cl.ExactMatch("legal_form"),
            cl.ExactMatch("postal_code"),
            cl.ExactMatch("city"),
            cl.LevenshteinAtThresholds("street", [2]),
            cl.ExactMatch("tax_number"),
            cl.ExactMatch("email"),
            cl.ExactMatch("phone"),
            cl.ExactMatch("created_date"),
            cl.ExactMatch("country_code"),
        ],
        blocking_rules_to_generate_predictions=blocking_rules,
        retain_intermediate_calculation_columns=True,
    )

    linker = Linker(frame, settings, DuckDBAPI())

    linker.training.estimate_probability_two_random_records_match(
        deterministic_rules, recall=DETERMINISTIC_RECALL
    )
    # 见模块 docstring：该调用在本环境未触发 salting bug，故走标准路径。
    linker.training.estimate_u_using_random_sampling(
        max_pairs=U_SAMPLING_MAX_PAIRS, seed=U_SAMPLING_SEED
    )

    # EM 估 m。阻断规则里用到的列在该轮 EM 中被固定，因此需要两轮互补的阻断规则
    # 才能覆盖全部比较器：第一轮固定 postal_code，第二轮固定 name_norm。
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("postal_code")
    )
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("name_norm")
    )

    predictions = linker.inference.predict(threshold_match_probability=MATCH_THRESHOLD)
    edges = predictions.as_pandas_dataframe()
    clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(
        predictions, threshold_match_probability=MATCH_THRESHOLD
    ).as_pandas_dataframe()

    training_meta = {
        "probability_two_random_records_match": round(
            linker._settings_obj._probability_two_random_records_match, 8
        ),
        "deterministic_rules": deterministic_rules,
        "deterministic_recall_assumed": DETERMINISTIC_RECALL,
        "blocking_rules": [
            "name_norm", "postal_code", "tax_number", "email",
        ],
        "u_estimation": "random_sampling",
        "u_sampling_max_pairs": U_SAMPLING_MAX_PAIRS,
        "u_sampling_seed": U_SAMPLING_SEED,
        "m_estimation": "expectation_maximisation (2 passes)",
        "model_diagnostics": model_diagnostics(linker._settings_obj),
    }
    return edges, clusters, training_meta


def _probability(level: Any, kind: str) -> float | str | None:
    """取 m/u 概率的【原始存储值】。

    必须读私有的 _m_probability / _u_probability：同名的公开 property 在参数未训练时
    会返回一个默认值（并打一条 warning），用它判断会把"未训练"误判成"已训练"。
    未训练时原始值是 None 或一句说明字符串。
    """
    for attr in (f"_{kind}_probability", f"{kind}_probability"):
        try:
            return getattr(level, attr)
        except (AttributeError, ValueError):
            continue
    return None


def model_diagnostics(settings_obj: Any) -> dict[str, Any]:
    """把 splink 打到 stderr 的"参数未训练"警告变成报告里的结构化事实。

    m ≈ 1 意味着：在被判为匹配的记录对里，该字段【总是】完全一致。
    整套比较器都退化成 m=1 时，Fellegi-Sunter 的概率加权其实没有起作用——
    模型等价于一组硬性一致性判断。这不是 bug，是数据里没有可供区分的模糊性。
    """
    untrained: list[str] = []
    degenerate: list[str] = []

    for comparison in settings_obj.comparisons:
        column = comparison.output_column_name
        for level in comparison.comparison_levels:
            if level.is_null_level:
                continue
            label = f"{column} :: {level.label_for_charts}"
            m_value = _probability(level, "m")
            u_value = _probability(level, "u")
            if not isinstance(m_value, (int, float)) or not isinstance(u_value, (int, float)):
                untrained.append(label)
            elif m_value >= 1.0 - 1e-9:
                degenerate.append(label)

    return {
        "untrained_levels": sorted(untrained),
        "degenerate_m_equals_one_levels": sorted(degenerate),
        "interpretation_zh": (
            "untrained_levels：该比较层在训练数据中从未被观测到，预测时退回默认值。"
            "本项目里全部是模糊匹配层（Jaro-Winkler / Levenshtein）——"
            "因为标准化之后，真实重复对的名称已经逐字相同，模糊层永远不会命中。"
            " | degenerate_m_equals_one_levels：m=1 表示匹配对中该字段总是完全一致，"
            "概率加权退化为硬性一致判断。二者同时出现，说明本数据集没有为"
            "概率匹配留下可被利用的模糊性。结论见 evaluation.metric_validity。"
        ),
    }


# --------------------------------------------------------------------------
# 评估：cluster 级为主，pairwise 为辅
# --------------------------------------------------------------------------
def _pairs_within(groups: dict[Any, list[str]]) -> set[tuple[str, str]]:
    return {
        pair
        for members in groups.values()
        for pair in combinations(sorted(members), 2)
    }


def _prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


def _cluster_prf(
    predicted: dict[Any, list[str]], truth: dict[Any, list[str]]
) -> dict[str, Any]:
    """cluster 级精确匹配：一个预测组算对，当且仅当它与某个真实组成员完全相同。"""
    truth_sets = {frozenset(m) for m in truth.values()}
    predicted_sets = [frozenset(m) for m in predicted.values()]
    exact = sum(1 for s in predicted_sets if s in truth_sets)
    return {
        **_prf(
            tp=exact,
            fp=len(predicted_sets) - exact,
            fn=len(truth_sets) - exact,
        ),
        "exactly_recovered": exact,
        "predicted_clusters": len(predicted_sets),
        "true_clusters": len(truth_sets),
    }


def classify_group(members: list[str], truth_of: dict[str, str]) -> dict[str, Any]:
    """一个预测组相对 ground truth 的判定。"""
    true_ids = {truth_of[m] for m in members}
    true_groups = defaultdict(list)
    for member in members:
        true_groups[truth_of[member]].append(member)

    if len(true_ids) > 1:
        verdict = "merged"  # 把多个真实实体错并成一组
    else:
        only = next(iter(true_ids))
        full = {m for m, t in truth_of.items() if t == only}
        verdict = "exact" if set(members) == full else "split"
    return {
        "verdict": verdict,
        "true_cluster_ids": sorted(true_ids),
    }


def field_agreement_within_true_duplicates(
    records: list[dict[str, Any]], truth_of: dict[str, str]
) -> dict[str, Any]:
    """真实重复对中，每个字段"两边完全相同且非空"的比例。

    这是判断评估指标是否可信的直接证据：某字段若在真实重复对里 100% 逐字相同，
    那么"按该字段精确分组"就能复现 ground truth，任何模型在它上面拿到的
    precision/recall 都不构成模型能力的证据。
    """
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_cluster[truth_of[str(record[RECORD_ID_FIELD])]].append(record)

    agreements: Counter[str] = Counter()
    total_pairs = 0
    for members in by_cluster.values():
        for left, right in combinations(members, 2):
            total_pairs += 1
            for field in COMPARED_FIELDS:
                if left.get(field) is not None and left.get(field) == right.get(field):
                    agreements[field] += 1

    return {
        "true_duplicate_pairs": total_pairs,
        "identical_rate": {
            field: round(agreements[field] / total_pairs, 4) if total_pairs else 0.0
            for field in COMPARED_FIELDS
        },
    }


def trivial_baseline(
    records: list[dict[str, Any]], key_of: Any, truth_of: dict[str, str]
) -> dict[str, Any]:
    """一行 SQL 级别的基线：按某个键精确分组，把每组当作一个实体。"""
    groups: dict[Any, list[str]] = defaultdict(list)
    for record in records:
        key = key_of(record)
        if key is None:
            # 键缺失的记录各自成组，不能全部并到一起
            groups[("__null__", record[RECORD_ID_FIELD])] = [str(record[RECORD_ID_FIELD])]
        else:
            groups[key].append(str(record[RECORD_ID_FIELD]))

    truth: dict[str, list[str]] = defaultdict(list)
    for record_id, cluster_id in truth_of.items():
        truth[cluster_id].append(record_id)

    dup_predicted = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    dup_truth = {k: sorted(v) for k, v in truth.items() if len(v) > 1}
    return _cluster_prf(dup_predicted, dup_truth)


def build_metric_validity(
    records: list[dict[str, Any]],
    truth_of: dict[str, str],
    splink_primary: dict[str, Any],
) -> dict[str, Any]:
    """指标有效性自检。宁可报告"这个 1.0 没有意义"，也不把它当成绩单贴出去。"""
    agreement = field_agreement_within_true_duplicates(records, truth_of)
    leaked = sorted(
        field for field, rate in agreement["identical_rate"].items() if rate >= 1.0
    )

    baselines = {
        "group_by_postal_code": trivial_baseline(
            records, lambda r: r.get("postal_code"), truth_of
        ),
        "group_by_name_norm": trivial_baseline(
            records, lambda r: normalize_name(str(r["vendor_name"]))[0], truth_of
        ),
        "group_by_street_and_created_date": trivial_baseline(
            records, lambda r: (r.get("street"), r.get("created_date")), truth_of
        ),
    }

    beaten_by = sorted(
        name for name, result in baselines.items()
        if result["f1"] >= splink_primary["f1"] - 1e-9
    )

    return {
        "verdict": "not_informative" if beaten_by else "informative",
        "splink_beaten_or_matched_by_trivial_baselines": beaten_by,
        "fields_identical_in_every_true_duplicate_pair": leaked,
        "field_agreement_within_true_duplicates": agreement,
        "trivial_baselines": baselines,
        "warning_zh": (
            "⚠ 本数据集上的 precision/recall 不构成模型能力的证据。"
            f"字段 {leaked} 在【每一对】真实重复记录中都逐字相同——"
            "合成数据生成器构造变体时只改写 vendor_name 与 country，其余字段整条复制。"
            f"因此 {_baseline_hint(beaten_by)} 就能取得与 Splink 相同的 F1。"
            "同时模型诊断显示所有比较器的 m 均退化为 1、模糊匹配层从未被观测到"
            "（见 _meta.training.model_diagnostics），即概率匹配的机制并未被真正触发。"
            "要让这套指标具备意义，需要在生成器中注入字符级噪声（拼写错误、字符换位、"
            "地址缩写、邮编错位），而不是只注入可被标准化完全还原的格式变化。"
        ) if beaten_by else (
            "Splink 的表现优于全部平凡基线，指标具备区分力。"
        ),
    }


def _baseline_hint(beaten_by: list[str]) -> str:
    return "、".join(f"`{name}`" for name in beaten_by) if beaten_by else "平凡基线"


def evaluate(
    clusters: pd.DataFrame, truth_of: dict[str, str]
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    predicted: dict[Any, list[str]] = defaultdict(list)
    for _, row in clusters.iterrows():
        predicted[row["cluster_id"]].append(str(row["unique_id"]))
    predicted = {k: sorted(v) for k, v in predicted.items()}

    truth: dict[str, list[str]] = defaultdict(list)
    for record_id, cluster_id in truth_of.items():
        truth[cluster_id].append(record_id)
    truth = {k: sorted(v) for k, v in truth.items()}

    dup_predicted = {k: v for k, v in predicted.items() if len(v) > 1}
    dup_truth = {k: v for k, v in truth.items() if len(v) > 1}

    predicted_pairs = _pairs_within(predicted)
    truth_pairs = _pairs_within(truth)

    verdicts = Counter(
        classify_group(members, truth_of)["verdict"] for members in predicted.values()
    )

    evaluation = {
        "ground_truth_file": str(GROUND_TRUTH_PATH.relative_to(PROJECT_ROOT)),
        "primary_metric": "cluster_level_duplicate_groups",
        "note_zh": (
            "业务问的是『这几条记录有没有被正确归成同一家供应商』，是 cluster 级问题。"
            "cluster_level_all 含 99 个单条 singleton，它们几乎不可能判错，会把指标稀释得虚高；"
            "因此主指标取 cluster_level_duplicate_groups（只看真实存在重复的 51 组）。"
            "pairwise 一并给出以便与文献对照，但它无法区分『3 条的组拆成 2+1』与『完全没找到』。"
        ),
        "cluster_level_duplicate_groups": _cluster_prf(dup_predicted, dup_truth),
        "cluster_level_all": _cluster_prf(predicted, truth),
        "pairwise": _prf(
            tp=len(predicted_pairs & truth_pairs),
            fp=len(predicted_pairs - truth_pairs),
            fn=len(truth_pairs - predicted_pairs),
        ),
        "error_breakdown": {
            "exact": verdicts.get("exact", 0),
            "split": verdicts.get("split", 0),
            "merged": verdicts.get("merged", 0),
            "meaning_zh": {
                "exact": "预测组与某个真实实体的记录集合完全一致。",
                "split": "真实实体的记录被拆到多个预测组（漏配）。",
                "merged": "多个真实实体的记录被错并进同一预测组（误配）。",
            },
        },
    }
    return evaluation, predicted


# --------------------------------------------------------------------------
# 组装疑似重复组
# --------------------------------------------------------------------------
def _edge_stats(edges: pd.DataFrame, members: list[str]) -> dict[str, Any]:
    member_set = set(members)
    within = edges[
        edges["unique_id_l"].astype(str).isin(member_set)
        & edges["unique_id_r"].astype(str).isin(member_set)
    ]
    probabilities = within["match_probability"].tolist()
    expected = len(members) * (len(members) - 1) // 2
    return {
        "min_match_probability": round(min(probabilities), 6) if probabilities else None,
        "mean_match_probability": (
            round(sum(probabilities) / len(probabilities), 6) if probabilities else None
        ),
        "direct_edges": len(probabilities),
        "possible_edges": expected,
        "formed_by_transitive_closure": len(probabilities) < expected,
    }


def _canonical_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """建议保留的主记录：字段填充最完整者；并列时取 legacy_vendor_id 最小者。

    只是建议。合并决策留给人。
    """
    def completeness(record: dict[str, Any]) -> tuple[int, str]:
        filled = sum(1 for f in COMPARED_FIELDS if record.get(f) not in (None, ""))
        return (-filled, str(record[RECORD_ID_FIELD]))

    return sorted(records, key=completeness)[0]


def build_groups(
    records_by_id: dict[str, dict[str, Any]],
    predicted: dict[Any, list[str]],
    edges: pd.DataFrame,
    truth_of: dict[str, str],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []

    for members in predicted.values():
        if len(members) < 2:
            continue

        member_records = [records_by_id[m] for m in members]
        canonical = _canonical_record(member_records)
        canonical_name = str(canonical["vendor_name"])
        stats = _edge_stats(edges, members)

        field_agreement: dict[str, Any] = {}
        for field in COMPARED_FIELDS:
            values = [r.get(field) for r in member_records]
            distinct = sorted({v for v in values if v is not None}, key=str)
            field_agreement[field] = {
                "agrees": len(distinct) <= 1,
                "distinct_values": distinct,
                "missing_count": sum(1 for v in values if v is None),
            }

        review_reasons: list[str] = []
        if stats["min_match_probability"] is not None and (
            stats["min_match_probability"] < REVIEW_THRESHOLD
        ):
            review_reasons.append(
                f"组内最弱匹配概率 {stats['min_match_probability']:.4f} "
                f"低于复核阈值 {REVIEW_THRESHOLD}。"
            )
        for field in CONFLICT_CRITICAL_FIELDS:
            if len(field_agreement[field]["distinct_values"]) > 1:
                review_reasons.append(
                    f"强标识符 `{field}` 在组内取值冲突："
                    f"{field_agreement[field]['distinct_values']}。同一实体不应如此。"
                )
        if stats["formed_by_transitive_closure"]:
            review_reasons.append(
                f"该组由传递闭包形成（{stats['direct_edges']}/{stats['possible_edges']} "
                f"条直接边），并非两两都被判为匹配。"
            )

        groups.append({
            "group_id": f"G{min(members)}",
            "size": len(members),
            "needs_review": bool(review_reasons),
            "review_reasons": review_reasons,
            "confidence": stats,
            "canonical_suggestion": {
                "legacy_vendor_id": canonical[RECORD_ID_FIELD],
                "vendor_name": canonical_name,
                "reason_zh": "字段填充最完整；仅为建议，合并决策由人工做出。",
            },
            "records": [
                {
                    **{field: record.get(field) for field in COMPARED_FIELDS},
                    RECORD_ID_FIELD: record[RECORD_ID_FIELD],
                    "is_canonical": record[RECORD_ID_FIELD] == canonical[RECORD_ID_FIELD],
                    "name_norm": normalize_name(str(record["vendor_name"]))[0],
                    "name_diff_from_canonical": diff_tags(
                        canonical_name, str(record["vendor_name"])
                    ),
                }
                for record in sorted(member_records, key=lambda r: str(r[RECORD_ID_FIELD]))
            ],
            "field_agreement": field_agreement,
            "ground_truth": classify_group(members, truth_of),
        })

    return sorted(groups, key=lambda g: g["group_id"])


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def resolve() -> dict[str, Any]:
    records: list[dict[str, Any]] = json.loads(LEGACY_PATH.read_text(encoding="utf-8"))
    truth_of: dict[str, str] = json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
    records_by_id = {str(r[RECORD_ID_FIELD]): r for r in records}

    frame = build_frame(records)
    edges, clusters, training_meta = run_splink(frame)
    evaluation, predicted = evaluate(clusters, truth_of)
    evaluation["metric_validity"] = build_metric_validity(
        records, truth_of, evaluation["cluster_level_duplicate_groups"]
    )
    groups = build_groups(records_by_id, predicted, edges, truth_of)

    records_in_groups = sum(g["size"] for g in groups)
    unresolved_country = sorted(
        {str(r["country"]) for r in records if normalize_country(r.get("country")) is None}
    )

    return {
        "_meta": {
            "disclaimer": (
                "本报告只标记疑似重复组并给出建议，不修改源数据、不执行任何合并。"
            ),
            "method": "Splink 4 (Fellegi-Sunter) · dedupe_only",
            "preprocessing_zh": (
                "vendor_name 先标准化为 name_norm / name_core / legal_form，"
                "country 归一为 ISO 3166-1 alpha-2，再交给 Splink 比对。"
                "法律形式表编码的是真实世界的公司法律形式，独立于合成数据生成器。"
            ),
            "training": training_meta,
            "thresholds": {
                "match_probability": MATCH_THRESHOLD,
                "review_probability": REVIEW_THRESHOLD,
            },
            "sources": {
                "legacy": str(LEGACY_PATH.relative_to(PROJECT_ROOT)),
                "ground_truth": str(GROUND_TRUTH_PATH.relative_to(PROJECT_ROOT)),
            },
            "limitations_zh": [
                "合成数据的变体记录逐字复制了基础记录的地址、邮编、币种与建档日期，"
                "只改名称与国家写法。真实世界的重复记录往往地址也不一致，"
                "因此这里的 precision/recall 是乐观估计，不可外推到真实主数据。",
                "λ（两条随机记录匹配的先验概率）由确定性规则 + 假定召回率 "
                f"{DETERMINISTIC_RECALL} 反推。该召回率是假设，不是测量值。",
                "ground truth 仅用于评估，不参与训练、阻断、阈值选择或聚类。",
            ],
            "country_spellings_unresolved": unresolved_country,
        },
        "summary": {
            "record_count": len(records),
            "predicted_cluster_count": len(predicted),
            "duplicate_group_count": len(groups),
            "records_in_duplicate_groups": records_in_groups,
            "records_unique": len(records) - records_in_groups,
            "needs_review_count": sum(1 for g in groups if g["needs_review"]),
            "group_size_distribution": dict(
                sorted(Counter(g["size"] for g in groups).items())
            ),
        },
        "evaluation": evaluation,
        "duplicate_groups": groups,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Splink 的训练日志走 logging，默认会把大段 SQL 与图表提示打到 stderr。
    logging.getLogger("splink").setLevel(logging.ERROR)

    report = attach_run_info(resolve())

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary, evaluation = report["summary"], report["evaluation"]
    print(f"Records              : {summary['record_count']}")
    print(f"Duplicate groups     : {summary['duplicate_group_count']}")
    print(f"  records covered    : {summary['records_in_duplicate_groups']}")
    print(f"  needs_review       : {summary['needs_review_count']}")
    print(f"  size distribution  : {summary['group_size_distribution']}")
    print(f"Content              : sha256 {report['_run_info']['content_sha256'][:16]}\n")

    primary = evaluation["cluster_level_duplicate_groups"]
    print("Cluster-level, duplicate groups only  [PRIMARY]")
    print(f"  precision {primary['precision']:.4f}   recall {primary['recall']:.4f}"
          f"   f1 {primary['f1']:.4f}")
    print(f"  exactly recovered {primary['exactly_recovered']}"
          f" / {primary['true_clusters']} true groups"
          f"  (predicted {primary['predicted_clusters']})")

    every = evaluation["cluster_level_all"]
    print("\nCluster-level, all clusters (singletons included -> inflated)")
    print(f"  precision {every['precision']:.4f}   recall {every['recall']:.4f}"
          f"   f1 {every['f1']:.4f}")

    pair = evaluation["pairwise"]
    print("\nPairwise (reference only)")
    print(f"  precision {pair['precision']:.4f}   recall {pair['recall']:.4f}"
          f"   f1 {pair['f1']:.4f}")
    print(f"  tp {pair['true_positives']}  fp {pair['false_positives']}"
          f"  fn {pair['false_negatives']}")

    errors = evaluation["error_breakdown"]
    print(f"\nGroup verdicts: exact {errors['exact']}  split {errors['split']}"
          f"  merged {errors['merged']}")

    validity = evaluation["metric_validity"]
    print(f"\n{'=' * 68}")
    print(f"METRIC VALIDITY: {validity['verdict'].upper()}")
    print(f"{'=' * 68}")
    print("Trivial baselines (cluster-level, duplicate groups only):")
    for name, result in validity["trivial_baselines"].items():
        print(f"  {name:<34} P {result['precision']:.4f}  R {result['recall']:.4f}"
              f"  F1 {result['f1']:.4f}")
    print(f"\nFields identical in EVERY true duplicate pair:")
    print(f"  {validity['fields_identical_in_every_true_duplicate_pair']}")

    diagnostics = report["_meta"]["training"]["model_diagnostics"]
    print(f"\nUntrained comparison levels : {len(diagnostics['untrained_levels'])}")
    for level in diagnostics["untrained_levels"]:
        print(f"  - {level}")
    print(f"Degenerate levels (m = 1)   : "
          f"{len(diagnostics['degenerate_m_equals_one_levels'])}")

    if validity["verdict"] == "not_informative":
        print(f"\n{validity['warning_zh']}")

    print(f"\nWrote report -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
