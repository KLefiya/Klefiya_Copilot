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

【报告里三处自我拆台的地方，都是刻意的】
    evaluation.metric_validity   平凡基线（单字段 GROUP BY）能不能打平本模型。
                                 能打平就说明指标没有意义，报告会直说。
    model_diagnostics.veto_levels 模型里存在匹配权重 -50 bit 以下的比较层，
                                 一对记录落进去就概率归零，其余证据全部作废。
    borderline_pairs             阈值下方、模型犹豫的候选对。needs_review 只能标出
                                 "可能被错并"的组，标不出"本该并却没并"的记录。

用法：
    python src/tools/entity_resolution.py
"""

from __future__ import annotations

import json
import logging
import math
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
MANIFEST_PATH = PROJECT_ROOT / "data" / "legacy" / "legacy_vendors_variant_manifest.json"
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

# 打分下界。低于 MATCH_THRESHOLD 但不低于此值的记录对不进入聚类，
# 但会作为"模型犹豫的对"（borderline_pairs）单独列出。
#
# 【为什么需要它】needs_review 只能标出"可能被错并到一起"的组——那是精确度风险。
# 它在结构上标不出"本该并进来却没并"的记录：那条记录成了 singleton，
# 根本不属于任何组，没有任何组会为它亮灯。漏配只能从阈值下方的候选对里看见。
BORDERLINE_MIN_PROBABILITY = 0.30
MAX_BORDERLINE_PAIRS_IN_REPORT = 25

NAME_PREFIX_LENGTH = 4      # name_prefix 阻断键取核心名的前几个字符

# --------------------------------------------------------------------------
# 阻断（blocking）规则
#
# 【为什么这是整条流水线里最不可逆的一步】
# 只有落进同一个 block 的记录对才会被打分。阻断阶段漏掉的对，后面无论模型多好
# 都救不回来——它给 recall 定了一个天花板。因此这里同时保留放宽前后的规则集，
# 报告会如实给出两者的天花板与候选对数量，让"召回 vs 计算量"的权衡可被审阅。
#
# 实测（224 条记录，全部可能对 24976，真实重复对 98）：
#     4 条基线规则                   候选对   86   天花板 recall 86.7%
#     + city                        候选对  121   天花板 recall 98.0%
#     + city + name_prefix          候选对  204   天花板 recall 99.0%   <- 采用
#     + country_code+legal_form     候选对 3324   天花板 recall 100.0%
# 最后一档为了多召回 1 对，把候选对放大 16 倍，是明显的边际收益崩溃，不采用。
# phone 单独能覆盖 57 对，但全部已被其它规则覆盖，加进来一条新候选对都不产生，故不加。
#
# SQL 的 NULL != NULL：税号/邮箱/邮编缺失的记录不会被错误地阻断到一起。
# --------------------------------------------------------------------------
BLOCKING_KEYS_BASELINE: tuple[tuple[str, ...], ...] = (
    ("name_norm",), ("postal_code",), ("tax_number",), ("email",),
)
BLOCKING_KEYS: tuple[tuple[str, ...], ...] = (
    ("name_norm",), ("postal_code",), ("tax_norm",), ("email",),
    ("city",), ("name_prefix",),
)

# --------------------------------------------------------------------------
# EM 训练用的阻断规则
#
# 【为什么不能用 postal_code / name_norm 来训练 EM】
# EM 只在阻断规则圈出的记录对上估计 m。若用强精确键（postal_code、name_norm）圈，
# 圈进来的几乎全是"干净的"重复对——地址与名字本就一致的那批。于是 m 被系统性地
# 推向"处处一致"，模型对脏对的要求过严。实测症状：postal_code 的精确匹配层 m = 1，
# 而我们明知邮编在 23.5% 的真实重复对里并不相同；dirty 档召回率只有 0.55。
#
# 改用 name_prefix + city：
#   - name_prefix 【不是】比较器所用的列，因此该轮 EM 不固定任何比较器的参数，
#     全部 m 都能被估计，且候选集里含大量脏对；
#   - city 只固定 city 自身，其余在上一轮已训练。
# 结果：未训练层从 3 降到 0，dirty 档召回率 0.55 -> 0.91。
# --------------------------------------------------------------------------
EM_TRAINING_KEYS: tuple[tuple[str, ...], ...] = (("name_prefix",), ("city",))

# 展示用：这些字段在组内出现分歧时值得高亮
COMPARED_FIELDS = (
    "vendor_name", "country", "city", "street", "postal_code",
    "tax_number", "email", "phone", "currency", "created_date",
)

# 组内冲突即必须人工复核的字段：一个法人实体只有一个税号。
#
# 【必须比标准化后的值】`DE 229888629` 与 `DE229888629` 是同一个税号。
# 早前的版本直接比原始字段，把 10 个组标成"强标识符冲突"，
# 而其中【真正冲突的是 0 个】——全部只是分隔符写法不同。既然我们已经认定
# 这种差异不算冲突（tax_norm 就是为此而设），复核标记就不能再按原始值判。
#
# 【email 不在此列】生成器把 info@ 改成 sales@，但同一家公司本来就可以有多个
# 联系邮箱。邮箱不同不是矛盾，不该逼人工复核。它作为字段分歧在 field_agreement
# 里如实展示，仅此而已。phone 同理。
CONFLICT_CRITICAL_FIELDS: dict[str, Any] = {}  # 在 normalize_tax_number 定义后填充


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


def normalize_tax_number(raw: str | None) -> str | None:
    """去掉分隔符：`DE 161220319` / `DE161220319`、`85-9650149` / `859650149` 视为同一个。

    data_profile 独立报出了 tax_number 的 format_consistency 告警。
    对一个写法不统一的字段做精确匹配是明知的建模错误，先标准化再比。
    """
    if raw is None:
        return None
    return re.sub(r"[^A-Za-z0-9]", "", raw).upper()


def normalize_phone(raw: str | None) -> str | None:
    """只留数字：`+49(0)3893 536081` 与 `0038935360 81` 的差异不该被当成不同号码。"""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits or None


CONFLICT_CRITICAL_FIELDS = {"tax_number": normalize_tax_number}


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
    frame["tax_norm"] = frame["tax_number"].map(normalize_tax_number)
    frame["phone_norm"] = frame["phone"].map(normalize_phone)
    # 核心名前缀：名字被打坏时仍可能保住开头，是一条便宜的补充阻断键。
    # 首字符发生换位（"steuer" -> "setuer"）时它同样失效，报告里如实说明。
    frame["name_prefix"] = [
        c[:NAME_PREFIX_LENGTH] if c else None for _, c, _ in normalized
    ]
    return frame


# --------------------------------------------------------------------------
# 阻断覆盖率：recall 的天花板
# --------------------------------------------------------------------------
def _true_pairs(truth_of: dict[str, str]) -> set[tuple[str, str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for record_id, cluster_id in truth_of.items():
        groups[cluster_id].append(record_id)
    return _pairs_within(groups)


def _candidate_pairs(
    frame: pd.DataFrame, keysets: tuple[tuple[str, ...], ...]
) -> set[tuple[str, str]]:
    """各阻断规则产生的候选对的并集。任一键为空的记录不参与该规则（NULL != NULL）。"""
    pairs: set[tuple[str, str]] = set()
    for keys in keysets:
        buckets: dict[tuple[Any, ...], list[str]] = defaultdict(list)
        for row in frame[["unique_id", *keys]].itertuples(index=False):
            values = tuple(row[1:])
            if any(value is None or pd.isna(value) for value in values):
                continue
            buckets[values].append(str(row[0]))
        for members in buckets.values():
            if len(members) > 1:
                pairs |= set(combinations(sorted(members), 2))
    return pairs


def blocking_coverage(
    frame: pd.DataFrame, truth_of: dict[str, str], keysets: tuple[tuple[str, ...], ...]
) -> dict[str, Any]:
    candidates = _candidate_pairs(frame, keysets)
    truth = _true_pairs(truth_of)
    covered = len(candidates & truth)
    record_count = len(frame)
    all_pairs = record_count * (record_count - 1) // 2
    return {
        "rules": ["+".join(keys) for keys in keysets],
        "candidate_pairs": len(candidates),
        "candidate_pairs_share_of_all": round(len(candidates) / all_pairs, 5),
        "true_pairs_covered": covered,
        "true_pairs_total": len(truth),
        "recall_ceiling": round(covered / len(truth), 4) if truth else 0.0,
    }


def run_splink(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """返回 (edges, clusters, 训练元信息)。"""
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on
    import splink.comparison_library as cl

    # 确定性规则：命中即几乎必然同一实体。用于估计 λ（两条随机记录匹配的先验概率）。
    # 用标准化后的 tax_norm，与下面的比较器和阻断键保持一致。
    deterministic_rules = [
        "l.tax_norm = r.tax_norm",
        "l.email = r.email",
        "l.name_norm = r.name_norm and l.postal_code = r.postal_code",
    ]

    blocking_rules = [block_on(*keys) for keys in BLOCKING_KEYS]

    settings = SettingsCreator(
        link_type="dedupe_only",
        comparisons=[
            # 三级台阶（0.95 / 0.88 / 0.80，splink 惯用的梯度）。两级时 <0.88 的兜底层
            # 会吸收掉"被打坏但仍明显相似"的名字，而该层的 m 被 EM 估成 ~0，
            # 于是它变成一票否决（见 model_diagnostics.veto_levels）。补一级台阶
            # 不能消除否决，但能让更多真实重复对不落进兜底层。
            cl.JaroWinklerAtThresholds("name_core", [0.95, 0.88, 0.80]),
            cl.ExactMatch("legal_form"),
            cl.ExactMatch("postal_code"),
            cl.ExactMatch("city"),
            cl.LevenshteinAtThresholds("street", [2]),
            cl.ExactMatch("tax_norm"),
            cl.ExactMatch("email"),
            cl.ExactMatch("phone_norm"),
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

    # EM 估 m。见 EM_TRAINING_KEYS 上方关于训练集偏倚的说明。
    for keys in EM_TRAINING_KEYS:
        linker.training.estimate_parameters_using_expectation_maximisation(block_on(*keys))

    # 打分打到 BORDERLINE_MIN_PROBABILITY，聚类仍只用 MATCH_THRESHOLD 以上的边。
    # 阈值下方的边不进聚类，但要留下来作为"模型犹豫的对"。
    predictions = linker.inference.predict(
        threshold_match_probability=BORDERLINE_MIN_PROBABILITY
    )
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
        "blocking_rules": ["+".join(keys) for keys in BLOCKING_KEYS],
        "u_estimation": "random_sampling",
        "u_sampling_max_pairs": U_SAMPLING_MAX_PAIRS,
        "u_sampling_seed": U_SAMPLING_SEED,
        "m_estimation": "expectation_maximisation",
        "em_training_blocking_rules": ["+".join(keys) for keys in EM_TRAINING_KEYS],
        "em_training_note_zh": (
            "EM 的阻断规则刻意避开强精确键（postal_code / name_norm）。用它们圈出的"
            "训练对几乎全是干净的重复对，会把 m 系统性地推向『处处一致』，"
            "使模型对脏对要求过严。name_prefix 不是任何比较器所用的列，"
            "该轮 EM 因此不固定任何参数，且候选集里含大量脏对。"
        ),
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


# 这两个字段在同一实体的重复记录之间【本就】不会变化：一家公司不会在两条档案里
# 换掉法律形式或国籍（国家写法的差异已被 country_code 标准化吸收）。
# 它们的 m=1 是对事实的正确估计，不是模型失灵——这类字段只对"判非"有贡献。
EXPECTED_DEGENERATE_COLUMNS = frozenset({"legal_form", "country_code"})

# 一个比较层的匹配权重 log2(m/u) 低于此值时，它单独就能压过其余全部正证据之和，
# 从而对整对记录形成【一票否决】。取 -20 bit：本模型全部正证据合计约 +69 bit，
# 单层 -20 bit 已足以抵消其中的绝大部分。
VETO_WEIGHT_BITS = -20.0


def _match_weight(m_value: float, u_value: float) -> float | None:
    if m_value <= 0 or u_value <= 0:
        return None  # log2(0) = -inf：绝对否决
    return math.log2(m_value / u_value)


def _round_sig(value: float, digits: int = 6) -> float:
    """按有效数字舍入。跨数量级的概率不能用 round(x, n)：m 小到 1e-107。

    【为什么必须舍入】EM 在 duckdb 里做并行浮点求和，加法不满足结合律，
    m 的末位会随线程调度抖动（实测 4.610490895533171e-107 vs 4.6104908955331746e-107）。
    这点噪声在 1e-107 量级上毫无意义，却足以让整份报告的 content_sha256 每次都变，
    使可复现性承诺失效。聚类结果与全部指标本身是完全确定的。
    """
    if value == 0 or not math.isfinite(value):
        return value
    return float(f"{value:.{digits - 1}e}")


def model_diagnostics(settings_obj: Any) -> dict[str, Any]:
    """把 splink 打到 stderr 的"参数未训练"警告变成报告里的结构化事实。

    检查三类病灶，前两类是镜像关系，第三类是前者的后果：

      m ≈ 1（degenerate）  匹配对里该字段【总是】一致。
          良性：该字段在同一实体的记录间本就不会变（legal_form / country_code）。
          病理：本该有分歧却被估成 1，说明 EM 训练集有偏（全是干净对）。
          若【全部】比较器都 m=1，概率加权完全没有起作用。

      m ≈ 0（veto）  匹配对里该字段【从未】不一致，于是 EM 把 m 估成 ~1e-15 甚至更小，
          log2(m/u) 变成 -50 乃至 -350 bit。任何一对记录落进这一层，概率直接归零，
          无论其余字段多么吻合——这不是在权衡证据，是硬性 AND。
          m=0 编码的是"不可能"，而它其实只是【有限样本里没见过】。
          正确的缓解是给 m 加平滑下限；splink 4 未暴露该接口，只能改私有属性，
          因此本项目选择【如实报告】而非绕过。代价记在 _meta.limitations_zh 里。

      untrained  该层在训练数据中从未被观测到，预测时退回默认值。
    """
    untrained: list[str] = []
    degenerate: list[str] = []
    unexpected_degenerate: list[str] = []
    vetoes: list[dict[str, Any]] = []
    max_positive_bits = 0.0

    for comparison in settings_obj.comparisons:
        column = comparison.output_column_name
        best_positive = 0.0
        for level in comparison.comparison_levels:
            if level.is_null_level:
                continue
            label = f"{column} :: {level.label_for_charts}"
            m_value = _probability(level, "m")
            u_value = _probability(level, "u")
            if not isinstance(m_value, (int, float)) or not isinstance(u_value, (int, float)):
                untrained.append(label)
                continue

            if m_value >= 1.0 - 1e-9:
                degenerate.append(label)
                if column not in EXPECTED_DEGENERATE_COLUMNS:
                    unexpected_degenerate.append(label)

            weight = _match_weight(m_value, u_value)
            if weight is None or weight < VETO_WEIGHT_BITS:
                vetoes.append({
                    "level": label,
                    "m_probability": _round_sig(m_value),
                    "u_probability": _round_sig(u_value),
                    "match_weight_bits": None if weight is None else round(weight, 1),
                })
            elif weight > best_positive:
                best_positive = weight
        max_positive_bits += best_positive

    all_degenerate = len(degenerate) >= len(settings_obj.comparisons)

    return {
        "untrained_levels": sorted(untrained),
        "degenerate_m_equals_one_levels": sorted(degenerate),
        "expected_degenerate_columns": sorted(EXPECTED_DEGENERATE_COLUMNS),
        "unexpected_degenerate_levels": sorted(unexpected_degenerate),
        "all_comparisons_degenerate": all_degenerate,
        "probabilistic_weighting_active": not all_degenerate and not untrained,
        "max_positive_evidence_bits": round(max_positive_bits, 1),
        "veto_threshold_bits": VETO_WEIGHT_BITS,
        "veto_levels": sorted(vetoes, key=lambda v: v["match_weight_bits"] or -1e9),
        "interpretation_zh": (
            "untrained_levels 为空表示每个比较层都被真实观测到过。"
            " | degenerate_m_equals_one_levels：m=1 表示匹配对中该字段总是完全一致；"
            f"其中 {sorted(EXPECTED_DEGENERATE_COLUMNS)} 属于【预期】退化——"
            "同一家公司不会在两条档案之间更换法律形式或国籍。"
            " | unexpected_degenerate_levels 非空通常意味着 EM 训练集有偏。"
            " | veto_levels：匹配权重低于 "
            f"{VETO_WEIGHT_BITS} bit 的层。全部正证据合计仅 "
            f"{round(max_positive_bits, 1)} bit，因此落进任一否决层的记录对概率归零，"
            "其余字段再吻合也无用。这是 EM 在零观测下把 m 估成 ~0 的产物，"
            "不是『该字段不一致就必然不是同一实体』的事实。"
            " | probabilistic_weighting_active 为 false 时，模型退化成一组硬性一致判断。"
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


SOLVES_IT_F1 = 0.99  # 单字段 GROUP BY 达到此 F1 即视为该字段直接泄漏了 ground truth


def build_metric_validity(
    records: list[dict[str, Any]],
    truth_of: dict[str, str],
    splink_primary: dict[str, Any],
) -> dict[str, Any]:
    """指标有效性自检。宁可报告"这个数字没有意义"，也不把它当成绩单贴出去。

    【泄漏判据是 GROUP BY 的 F1，不是"组内一致率"】
    组内 100% 一致只是必要条件，不是充分条件——该字段还必须【跨实体有区分度】。
    反例是 currency：同一家公司当然同币种，它在每一对真实重复记录中都相同，
    但同国的所有供应商也都相同，`GROUP BY currency` 只得到 3 个大组，F1 ≈ 0。
    早前的版本只看一致率，于是把 currency 误列为泄漏字段。
    """
    agreement = field_agreement_within_true_duplicates(records, truth_of)

    single_field: dict[str, Any] = {}
    for field in COMPARED_FIELDS:
        result = trivial_baseline(records, lambda r, f=field: r.get(f), truth_of)
        single_field[field] = {
            "identical_rate_within_true_duplicates": agreement["identical_rate"][field],
            "group_by_f1": result["f1"],
            "group_by_precision": result["precision"],
            "group_by_recall": result["recall"],
        }

    leaked = sorted(
        field for field, stats in single_field.items()
        if stats["group_by_f1"] >= SOLVES_IT_F1
    )

    composite = {
        "group_by_name_norm": trivial_baseline(
            records, lambda r: normalize_name(str(r["vendor_name"]))[0], truth_of
        ),
        "group_by_street_and_created_date": trivial_baseline(
            records, lambda r: (r.get("street"), r.get("created_date")), truth_of
        ),
        "group_by_city_and_postal_code": trivial_baseline(
            records, lambda r: (r.get("city"), r.get("postal_code")), truth_of
        ),
    }

    beaten_by = sorted(
        [f"group_by_{field}" for field in leaked]
        + [
            name for name, result in composite.items()
            if result["f1"] >= splink_primary["f1"] - 1e-9
        ]
    )

    best_trivial = max(
        [stats["group_by_f1"] for stats in single_field.values()]
        + [result["f1"] for result in composite.values()]
    )

    return {
        "verdict": "not_informative" if beaten_by else "informative",
        "leak_criterion_zh": (
            "某字段单独 `GROUP BY` 的 cluster 级 F1 ≥ "
            f"{SOLVES_IT_F1} 即判定它泄漏了 ground truth。"
            "只看『组内一致率 100%』会误判——currency 满足它却毫无区分度。"
        ),
        "fields_that_leak_ground_truth": leaked,
        "splink_beaten_or_matched_by_trivial_baselines": beaten_by,
        "splink_f1": splink_primary["f1"],
        "best_trivial_baseline_f1": round(best_trivial, 4),
        "single_field_baselines": single_field,
        "composite_baselines": composite,
        "verdict_zh": (
            "⚠ 本数据集上的 precision/recall 不构成模型能力的证据："
            f"{_baseline_hint(beaten_by)} 就能取得不低于 Splink 的 F1。"
        ) if beaten_by else (
            f"指标具备区分力：没有任何单字段 `GROUP BY` 能复原 ground truth"
            f"（最佳平凡基线 F1 = {best_trivial:.4f}，Splink = {splink_primary['f1']:.4f}）。"
            "重复记录的字段已不再逐字相同，模型必须真正做概率匹配才能找回它们。"
        ),
    }


def _baseline_hint(beaten_by: list[str]) -> str:
    return "、".join(f"`{name}`" for name in beaten_by) if beaten_by else "平凡基线"


# --------------------------------------------------------------------------
# 按脏度档拆解召回率
# --------------------------------------------------------------------------
def recall_by_dirt_level(
    manifest: dict[str, dict[str, Any]],
    cluster_of: dict[str, Any],
    truth_of: dict[str, str],
) -> dict[str, Any]:
    """每条重复记录，有没有被归进它所属供应商那一组？按脏度档拆开看。

    衡量的是"这条脏记录被找回来了吗"，判定标准是它与本实体的基础记录落在同一预测簇。
    manifest 与 ground truth 一样只用于评估，不参与训练、阻断、阈值选择或聚类。
    """
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"recovered": 0, "total": 0})

    for record_id, entry in manifest.items():
        role = entry["role"]
        if role == "base":
            continue
        level = "exact_duplicate" if role == "exact_duplicate" else str(entry["dirt_level"])
        base_id = str(entry["entity_id"])
        buckets[level]["total"] += 1
        if cluster_of.get(record_id) == cluster_of.get(base_id):
            buckets[level]["recovered"] += 1

    order = ["exact_duplicate", "clean", "moderate", "dirty"]
    return {
        "definition_zh": (
            "recall = 该档中被归入其所属供应商簇的重复记录数 / 该档重复记录总数。"
            "基础记录本身不计入。"
        ),
        "by_level": {
            level: {
                **buckets[level],
                "recall": round(buckets[level]["recovered"] / buckets[level]["total"], 4)
                if buckets[level]["total"] else 0.0,
            }
            for level in order if level in buckets
        },
    }


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


def build_borderline_pairs(
    edges: pd.DataFrame,
    records_by_id: dict[str, dict[str, Any]],
    cluster_of: dict[str, Any],
    truth_of: dict[str, str],
) -> list[dict[str, Any]]:
    """模型犹豫的记录对：概率在 [BORDERLINE_MIN_PROBABILITY, MATCH_THRESHOLD) 之间，
    且两条记录最终落在不同的簇。

    这是漏配（false split）唯一能被看见的地方——被漏掉的记录成了 singleton，
    不属于任何组，没有任何组的 needs_review 会为它亮灯。

    `would_be_correct` 取自 ground truth，仅用于评估展示，不参与任何判定。
    """
    borderline = edges[edges["match_probability"] < MATCH_THRESHOLD]

    rows: list[dict[str, Any]] = []
    for edge in borderline.itertuples(index=False):
        left, right = str(edge.unique_id_l), str(edge.unique_id_r)
        if cluster_of.get(left) == cluster_of.get(right):
            continue  # 已被其它边经传递闭包并到一起，不是漏配
        rows.append({
            "record_ids": sorted([left, right]),
            "match_probability": round(float(edge.match_probability), 6),
            "vendor_names": [
                str(records_by_id[left]["vendor_name"]),
                str(records_by_id[right]["vendor_name"]),
            ],
            "name_diff": diff_tags(
                str(records_by_id[left]["vendor_name"]),
                str(records_by_id[right]["vendor_name"]),
            ),
            "would_be_correct": truth_of[left] == truth_of[right],
        })

    rows.sort(key=lambda r: (-r["match_probability"], r["record_ids"]))
    return rows[:MAX_BORDERLINE_PAIRS_IN_REPORT]


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
        for field, normalizer in CONFLICT_CRITICAL_FIELDS.items():
            normalized = {
                normalizer(r.get(field)) for r in member_records if r.get(field) is not None
            }
            if len(normalized) > 1:
                review_reasons.append(
                    f"强标识符 `{field}` 在组内取值冲突（标准化后仍不同）："
                    f"{sorted(normalized)}。一个法人实体只有一个税号。"
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

    manifest: dict[str, dict[str, Any]] = json.loads(
        MANIFEST_PATH.read_text(encoding="utf-8")
    )

    frame = build_frame(records)

    # 阻断天花板先算：它与模型无关，且给 recall 定了上限。
    blocking = {
        "note_zh": (
            "阻断阶段漏掉的记录对，后续无论模型多好都无法召回——它给 recall 定了天花板。"
            "放宽阻断能抬高天花板，代价是候选对（即打分次数）增加。二者都列在这里。"
        ),
        "baseline": blocking_coverage(frame, truth_of, BLOCKING_KEYS_BASELINE),
        "widened": blocking_coverage(frame, truth_of, BLOCKING_KEYS),
        "rejected_alternative": {
            "rules": ["country_code+legal_form"],
            "reason_zh": (
                "该规则可把天花板抬到 100%，但候选对从 204 涨到 3324（全部可能对的 13.3%），"
                "为多召回 1 对而放大 16 倍计算量，是边际收益崩溃。不采用。"
            ),
        },
        "excluded_rule_phone_zh": (
            "phone 单独可覆盖 57 对真实重复对，但这些对已全部被其它规则覆盖，"
            "加入后候选对数量不变（121 -> 121），故不纳入。"
        ),
    }

    edges, clusters, training_meta = run_splink(frame)
    cluster_of = {
        str(row.unique_id): row.cluster_id for row in clusters.itertuples(index=False)
    }

    evaluation, predicted = evaluate(clusters, truth_of)
    evaluation["blocking"] = blocking
    evaluation["recall_by_dirt_level"] = recall_by_dirt_level(
        manifest, cluster_of, truth_of
    )
    evaluation["metric_validity"] = build_metric_validity(
        records, truth_of, evaluation["cluster_level_duplicate_groups"]
    )

    # 组内统计只看真正进入聚类的边（>= MATCH_THRESHOLD）。
    matched_edges = edges[edges["match_probability"] >= MATCH_THRESHOLD]
    groups = build_groups(records_by_id, predicted, matched_edges, truth_of)
    borderline = build_borderline_pairs(edges, records_by_id, cluster_of, truth_of)

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
                "vendor_name -> name_norm / name_core / legal_form；"
                "country -> ISO 3166-1 alpha-2；tax_number -> tax_norm（去分隔符）；"
                "phone -> phone_norm（只留数字）。标准化后的列同时用于阻断、确定性规则"
                "与比较器，三者保持一致——只在比较器里标准化而阻断仍用原始列，"
                "标准化就不会作用到候选生成上。"
                "法律形式表编码的是真实世界的公司法律形式，独立于合成数据生成器。"
                "对 tax_number / phone 做标准化的依据是 data_profile 独立报出的"
                "format_consistency 告警，不是 ground truth。"
            ),
            "training": training_meta,
            "thresholds": {
                "match_probability": MATCH_THRESHOLD,
                "review_probability": REVIEW_THRESHOLD,
            },
            "sources": {
                "legacy": str(LEGACY_PATH.relative_to(PROJECT_ROOT)),
                "ground_truth": str(GROUND_TRUTH_PATH.relative_to(PROJECT_ROOT)),
                "variant_manifest": str(MANIFEST_PATH.relative_to(PROJECT_ROOT)),
            },
            "limitations_zh": [
                "【模型存在一票否决层】见 _meta.training.model_diagnostics.veto_levels。"
                "EM 对『匹配对中从未出现过不一致』的比较层把 m 估成 ~0，"
                "使该层的匹配权重达到 -52 乃至 -353 bit，而全部正证据合计仅约 +69 bit。"
                "后果是：只要一对记录在这类字段上不一致，概率立刻归零，"
                "即便税号与城市完全相同也无法挽回。m=0 编码的是『不可能』，"
                "而它实际只是『有限样本里没见过』。"
                "当前配置下【没有】真实重复对落进否决层，但安全裕度极薄："
                "真实重复对中最低的 name_core Jaro-Winkler 相似度是 0.8167，"
                "而兜底层的门槛在 0.80——只差 0.0167。这是台阶设置的运气，不是稳健性。"
                "两级台阶（0.95/0.88）的上一版正因此漏掉 2 对。"
                "正确缓解是给 m 加平滑下限；splink 4 未暴露该接口，"
                "改私有属性不是本项目愿意付的代价，因此选择如实报告。"
                "已验证『删掉 legal_form / country_code 比较器』无法消除否决"
                "——否决会转移到 name_core 与 tax_norm 上，F1 反降至 0.9505。",

                "唯一漏掉的一对（V100081 / V100081D，`Ritter Automation GmbH` vs "
                "`ritter  autornation gmbh`，OCR 的 m<->rn 混淆）匹配概率 0.9493，"
                "比聚类阈值 0.95 低 0.0007。它出现在 borderline_pairs 里。"
                "阈值 0.95 是先验选定的常用值，【没有】按 ground truth 调过——"
                "把它下调到 0.94 就能拿到满分，但那是拿答案调参，不是模型变好了。",

                "阻断阶段的 recall 天花板是 99.0%（204 个候选对中覆盖 97/98 对真实重复）。"
                "剩下 1 对两条记录同时被打坏（城市各自笔误成不同值、邮编各自错位、"
                "税号一缺一有、邮箱全缺、电话两处笔误），任何不做全量两两比较的方案都召不回它。"
                "这是数据本身的极端情况，不是为了凑高 recall 就该无限放宽阻断的理由。",
                "λ（两条随机记录匹配的先验概率）由确定性规则 + 假定召回率 "
                f"{DETERMINISTIC_RECALL} 反推。该召回率是假设，不是测量值。",
                "ground truth 与 variant_manifest 仅用于评估，"
                "不参与训练、阻断、阈值选择或聚类。",
                "合成数据的脏法由生成器设计，可能不覆盖真实世界的全部脏法。"
                "本报告演示的是方法与可解释性，指标不可直接外推到真实主数据。",
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
            "borderline_pair_count": len(borderline),
            "group_size_distribution": dict(
                sorted(Counter(g["size"] for g in groups).items())
            ),
        },
        "evaluation": evaluation,
        "duplicate_groups": groups,
        "borderline_pairs": {
            "note_zh": (
                f"匹配概率落在 [{BORDERLINE_MIN_PROBABILITY}, {MATCH_THRESHOLD}) "
                "且两条记录最终未被并到一起的候选对。needs_review 只能标出可能被错并的组"
                "（精确度风险），漏配的记录成了 singleton，不属于任何组，"
                "只能在这里被看见。"
            ),
            "threshold_used_for_clustering": MATCH_THRESHOLD,
            "scored_down_to": BORDERLINE_MIN_PROBABILITY,
            "pairs": borderline,
        },
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
    print(f"Borderline pairs     : {summary['borderline_pair_count']}"
          f"  (scored but below the {MATCH_THRESHOLD} clustering threshold)")
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

    blocking = evaluation["blocking"]
    print("\nBlocking (recall ceiling vs candidate pairs):")
    for label in ("baseline", "widened"):
        entry = blocking[label]
        print(f"  {label:<9} {entry['candidate_pairs']:>5} pairs "
              f"({entry['candidate_pairs_share_of_all']:.1%} of all)   "
              f"ceiling {entry['recall_ceiling']:.4f}   "
              f"[{', '.join(entry['rules'])}]")
    print(f"  rejected  country_code+legal_form -> ceiling 1.0000 but 3324 pairs (16x). "
          f"Marginal return collapse.")

    dirt = evaluation["recall_by_dirt_level"]["by_level"]
    print("\nRecall by dirt level (duplicate records recovered into their vendor's cluster):")
    for level, stats in dirt.items():
        print(f"  {level:<16} {stats['recovered']:>3} / {stats['total']:<3} "
              f"recall {stats['recall']:.4f}")

    diagnostics = report["_meta"]["training"]["model_diagnostics"]
    print(f"\nModel diagnostics")
    print(f"  probabilistic weighting active : {diagnostics['probabilistic_weighting_active']}")
    print(f"  untrained comparison levels    : {len(diagnostics['untrained_levels'])}")
    for level in diagnostics["untrained_levels"]:
        print(f"    - {level}")
    print(f"  degenerate levels (m = 1)      : "
          f"{len(diagnostics['degenerate_m_equals_one_levels'])}")
    for level in diagnostics["degenerate_m_equals_one_levels"]:
        expected = level not in diagnostics["unexpected_degenerate_levels"]
        tag = "expected: field never varies within an entity" if expected else "UNEXPECTED"
        print(f"    - {level}   [{tag}]")
    print(f"  max positive evidence          : "
          f"+{diagnostics['max_positive_evidence_bits']} bits (all fields agreeing)")
    print(f"  VETO levels (< {diagnostics['veto_threshold_bits']} bits)     : "
          f"{len(diagnostics['veto_levels'])}"
          f"   <- a pair landing here scores ~0 regardless of every other field")
    for veto in diagnostics["veto_levels"]:
        print(f"    - {veto['level']:<48} {veto['match_weight_bits']:>8} bits "
              f"(m={veto['m_probability']:.2g})")

    validity = evaluation["metric_validity"]
    print(f"\n{'=' * 72}")
    print(f"METRIC VALIDITY: {validity['verdict'].upper()}")
    print(f"{'=' * 72}")
    print(f"  {'field':<16}{'identical':>10}{'GROUP BY F1':>13}")
    for field, stats in validity["single_field_baselines"].items():
        leak = "  <-- LEAK" if field in validity["fields_that_leak_ground_truth"] else ""
        print(f"  {field:<16}"
              f"{stats['identical_rate_within_true_duplicates']:>9.1%}"
              f"{stats['group_by_f1']:>13.4f}{leak}")
    print("\n  Composite baselines:")
    for name, result in validity["composite_baselines"].items():
        print(f"    {name:<36} F1 {result['f1']:.4f}")
    print(f"\n  best trivial baseline F1 {validity['best_trivial_baseline_f1']:.4f}"
          f"   vs   Splink F1 {validity['splink_f1']:.4f}")
    print(f"\n{validity['verdict_zh']}")

    print(f"\nWrote report -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
