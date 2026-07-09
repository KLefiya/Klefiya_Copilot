"""生成模拟的"遗留 ECC 系统供应商主数据"（合成数据，不对应任何真实公司）。

输出的记录使用遗留系统风格的字段名（legacy_vendor_id / vendor_name / ...），
将来由映射 Agent 映射到目标端 A_BusinessPartner（A2X 命名）。

数据中被【故意】注入四类真实世界脏数据：
  a) 同一供应商的名称拼写变体重复（大小写 / 后缀写法 / 空格 / & vs and）
  b) 完全重复记录
  c) 字段缺失（税号 / 邮箱 / 电话）
  d) 国家代码格式不统一（DE / Germany / Deutschland / GER）

用法：
    python src/tools/generate_legacy_vendors.py
"""

from __future__ import annotations

import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from faker import Faker

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------
SEED = 20260709

BASE_VENDOR_COUNT = 150          # 基础唯一供应商数量（真实实体数）
VARIANT_RATE = 0.20              # 有多少比例的基础供应商会产生"拼写变体"重复
MAX_VARIANTS_PER_VENDOR = 2      # 单个供应商最多产生几条变体
EXACT_DUP_RATE = 0.05            # 有多少比例的基础供应商会产生"完全重复"记录

MISSING_RATE_TAX_NUMBER = 0.08   # 基础记录上的字段缺失概率
MISSING_RATE_EMAIL = 0.12
MISSING_RATE_PHONE = 0.10
VARIANT_MISSING_MULTIPLIER = 2.5  # 变体记录的缺失概率倍数（脏数据往往更不完整）
MAX_MISSING_RATE = 0.90           # 倍数放大后的上限，避免变体记录全空

COUNTRY_WEIGHTS = {"DE": 0.45, "US": 0.35, "JP": 0.20}

CREATED_DATE_START = "1998-01-01"  # 遗留系统的建档年代
CREATED_DATE_END = "2019-12-31"

OUTPUT_PATH = Path(__file__).resolve().parents[2] / "data" / "legacy" / "legacy_vendors.json"
GROUND_TRUTH_PATH = OUTPUT_PATH.with_name("legacy_vendors_ground_truth.json")


# --------------------------------------------------------------------------
# 国家画像：locale、法律后缀、货币、税号格式全部按国家绑定
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CountryProfile:
    code: str
    locale: str
    currency: str
    legal_suffixes: tuple[str, ...]
    industry_words: tuple[str, ...]
    email_tlds: tuple[str, ...]
    country_raw_spellings: tuple[str, ...]  # 脏数据 (d)：同一国家的多种写法
    tax_number_fn: Callable[[random.Random], str]
    street_fn: Callable[[Faker], str]
    name_core_fn: Callable[[Faker], str]


def _de_tax(rng: random.Random) -> str:
    return "DE" + "".join(rng.choices("0123456789", k=9))


def _us_tax(rng: random.Random) -> str:
    return f"{rng.randint(10, 99)}-{rng.randint(1000000, 9999999)}"


def _jp_tax(rng: random.Random) -> str:
    return "".join(rng.choices("0123456789", k=13))


def _jp_street(fake: Faker) -> str:
    # ja_JP 的 street_address() 会返回 "177 桜 Street" 这类半英半日的值，不可用。
    return f"{fake.town()}{fake.chome()}{fake.ban()}{fake.gou()}"


def _jp_name_core(fake: Faker) -> str:
    # ja_JP 没有 last_name_romanized；romanized_name() 返回 "Hiroshi Kondo"，取姓氏部分。
    return fake.romanized_name().split()[-1]


COUNTRY_PROFILES: dict[str, CountryProfile] = {
    "DE": CountryProfile(
        code="DE",
        locale="de_DE",
        currency="EUR",
        legal_suffixes=("GmbH", "GmbH & Co. KG", "AG"),
        industry_words=(
            "Werkzeugbau", "Logistik", "Maschinenbau", "Elektrotechnik",
            "Chemie", "Kunststoff", "Stahlhandel", "Automation",
        ),
        email_tlds=("de", "com"),
        country_raw_spellings=("DE", "Germany", "Deutschland", "GER", "de"),
        tax_number_fn=_de_tax,
        street_fn=lambda f: f.street_address(),
        name_core_fn=lambda f: f.last_name(),
    ),
    "US": CountryProfile(
        code="US",
        locale="en_US",
        currency="USD",
        legal_suffixes=("Inc.", "LLC", "Corp."),
        industry_words=(
            "Industrial", "Manufacturing", "Supply", "Components",
            "Materials", "Fabrication", "Technologies", "Solutions",
        ),
        email_tlds=("com", "net"),
        country_raw_spellings=("US", "USA", "United States", "U.S.A.", "us"),
        tax_number_fn=_us_tax,
        street_fn=lambda f: f.street_address(),
        name_core_fn=lambda f: f.last_name(),
    ),
    "JP": CountryProfile(
        code="JP",
        locale="ja_JP",
        currency="JPY",
        legal_suffixes=("K.K.", "Co., Ltd."),
        industry_words=(
            "Seiki", "Denki", "Kogyo", "Shoji",
            "Kikai", "Kagaku", "Seizo", "Buhin",
        ),
        email_tlds=("co.jp", "jp"),
        country_raw_spellings=("JP", "Japan", "JPN", "JAPAN"),
        tax_number_fn=_jp_tax,
        street_fn=_jp_street,
        name_core_fn=_jp_name_core,
    ),
}


# --------------------------------------------------------------------------
# 脏数据 (a)：名称拼写变体
# --------------------------------------------------------------------------
SUFFIX_VARIANTS: dict[str, tuple[str, ...]] = {
    "GmbH & Co. KG": ("GmbH & Co KG", "GmbH und Co. KG", "GmbH&Co.KG"),
    "GmbH": ("GMBH", "GmbH.", "mbH"),
    "AG": ("A.G.", "AG."),
    "Inc.": ("Inc", "INC", "Incorporated"),
    "LLC": ("L.L.C.", "L.L.C"),
    "Corp.": ("Corp", "Corporation"),
    "K.K.": ("KK", "Kabushiki Kaisha"),
    "Co., Ltd.": ("Co Ltd", "Co., Ltd", "Co.,Ltd.", "Company Limited"),
}


def _rewrite_suffix(name: str, rng: random.Random) -> str:
    # 最长后缀优先，否则 "GmbH & Co. KG" 会被 "GmbH" 规则先匹配掉。
    for suffix in sorted(SUFFIX_VARIANTS, key=len, reverse=True):
        if name.endswith(suffix):
            replacement = rng.choice(SUFFIX_VARIANTS[suffix])
            return name[: -len(suffix)] + replacement
    return name


NAME_TRANSFORMS: dict[str, Callable[[str, random.Random], str]] = {
    "uppercase": lambda n, _: n.upper(),
    "lowercase": lambda n, _: n.lower(),
    "suffix_rewrite": _rewrite_suffix,
    "trailing_space": lambda n, r: n + " " * r.randint(1, 3),
    "leading_space": lambda n, r: " " * r.randint(1, 2) + n,
    "ampersand_to_and": lambda n, _: n.replace("&", "and"),
    "double_space": lambda n, _: n.replace(" ", "  ", 1),
    "strip_punctuation": lambda n, _: n.replace(".", "").replace(",", ""),
}


def make_name_variant(name: str, rng: random.Random) -> tuple[str, list[str]]:
    """对公司名施加 1~3 个变形，返回变体名和所用变形的名称列表。"""
    candidates = [
        key for key in NAME_TRANSFORMS
        if key != "ampersand_to_and" or "&" in name
    ]
    chosen = rng.sample(candidates, k=rng.randint(1, 3))
    variant = name
    for key in chosen:
        variant = NAME_TRANSFORMS[key](variant, rng)
    # 全部变形都可能碰巧退化成原名（如 strip_punctuation 遇到无标点的名字）
    if variant == name:
        variant = name.upper()
        chosen = ["uppercase"]
    return variant, chosen


# --------------------------------------------------------------------------
# 记录构造
# --------------------------------------------------------------------------
UMLAUT_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                            "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def _email_slug(name_core: str) -> str:
    slug = name_core.translate(UMLAUT_MAP).lower()
    slug = re.sub(r"[^a-z0-9]", "", slug)
    return slug or "vendor"


def _maybe_missing(value: str, rate: float, rng: random.Random) -> str | None:
    """脏数据 (c)：按概率把字段打成缺失。"""
    return None if rng.random() < rate else value


def build_base_vendor(
    index: int,
    country: str,
    fake: Faker,
    rng: random.Random,
) -> dict[str, object]:
    profile = COUNTRY_PROFILES[country]

    name_core = profile.name_core_fn(fake)
    suffix = rng.choice(profile.legal_suffixes)
    industry = rng.choice(profile.industry_words)
    vendor_name = f"{name_core} {industry} {suffix}"

    slug = _email_slug(name_core)
    email = f"info@{slug}-{_email_slug(industry)}.{rng.choice(profile.email_tlds)}"

    created = fake.date_between_dates(
        date_start=_parse_date(CREATED_DATE_START),
        date_end=_parse_date(CREATED_DATE_END),
    )

    return {
        "legacy_vendor_id": f"V{100000 + index}",
        "vendor_name": vendor_name,
        "country": rng.choice(profile.country_raw_spellings),  # 脏数据 (d)
        "city": fake.city(),
        "street": profile.street_fn(fake),
        "postal_code": fake.postcode(),
        "tax_number": _maybe_missing(
            profile.tax_number_fn(rng), MISSING_RATE_TAX_NUMBER, rng
        ),
        "email": _maybe_missing(email, MISSING_RATE_EMAIL, rng),
        "phone": _maybe_missing(fake.phone_number(), MISSING_RATE_PHONE, rng),
        "currency": profile.currency,
        "created_date": created.strftime("%Y%m%d"),  # ECC 风格的 YYYYMMDD 字符串
    }


def build_name_variant_record(
    base: dict[str, object],
    country: str,
    suffix_id: str,
    rng: random.Random,
) -> dict[str, object]:
    """脏数据 (a)：同一实体的另一条记录，名称写法不同、缺失更多。"""
    profile = COUNTRY_PROFILES[country]
    record = dict(base)
    record["legacy_vendor_id"] = f"{base['legacy_vendor_id']}{suffix_id}"
    record["vendor_name"], _ = make_name_variant(str(base["vendor_name"]), rng)
    record["country"] = rng.choice(profile.country_raw_spellings)

    for field, rate in (
        ("tax_number", MISSING_RATE_TAX_NUMBER),
        ("email", MISSING_RATE_EMAIL),
        ("phone", MISSING_RATE_PHONE),
    ):
        inflated = min(rate * VARIANT_MISSING_MULTIPLIER, MAX_MISSING_RATE)
        if record[field] is not None:
            record[field] = _maybe_missing(str(record[field]), inflated, rng)

    return record


def build_exact_duplicate(base: dict[str, object]) -> dict[str, object]:
    """脏数据 (b)：整条复制，只有 id 带 X 后缀。"""
    record = dict(base)
    record["legacy_vendor_id"] = f"{base['legacy_vendor_id']}X"
    return record


def _parse_date(iso: str):
    from datetime import date
    return date.fromisoformat(iso)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def generate() -> tuple[list[dict[str, object]], dict[str, str], dict[str, int]]:
    rng = random.Random(SEED)

    # 每个国家一个独立 locale 的 Faker 实例，各自 seed，保证可复现且互不干扰。
    fakers: dict[str, Faker] = {}
    for offset, (code, profile) in enumerate(sorted(COUNTRY_PROFILES.items())):
        fake = Faker(profile.locale)
        fake.seed_instance(SEED + offset)
        fakers[code] = fake

    countries = list(COUNTRY_WEIGHTS)
    weights = [COUNTRY_WEIGHTS[c] for c in countries]

    records: list[dict[str, object]] = []
    ground_truth: dict[str, str] = {}  # record_id -> 真实实体 id（供后续 Splink 评估）
    stats = {"base": 0, "variant": 0, "exact_dup": 0}

    for index in range(1, BASE_VENDOR_COUNT + 1):
        country = rng.choices(countries, weights=weights, k=1)[0]
        base = build_base_vendor(index, country, fakers[country], rng)
        cluster_id = str(base["legacy_vendor_id"])

        records.append(base)
        ground_truth[cluster_id] = cluster_id
        stats["base"] += 1

        if rng.random() < VARIANT_RATE:
            for n in range(1, rng.randint(1, MAX_VARIANTS_PER_VENDOR) + 1):
                suffix_id = "D" if n == 1 else f"D{n}"
                variant = build_name_variant_record(base, country, suffix_id, rng)
                records.append(variant)
                ground_truth[str(variant["legacy_vendor_id"])] = cluster_id
                stats["variant"] += 1

        if rng.random() < EXACT_DUP_RATE:
            dup = build_exact_duplicate(base)
            records.append(dup)
            ground_truth[str(dup["legacy_vendor_id"])] = cluster_id
            stats["exact_dup"] += 1

    rng.shuffle(records)  # 打散，避免重复记录相邻导致下游"作弊"
    return records, ground_truth, stats


def _summarize(records: list[dict[str, object]], stats: dict[str, int]) -> None:
    total = len(records)
    print(f"Total records          : {total}")
    print(f"Base unique vendors    : {stats['base']}")
    print(f"  + name variants      : {stats['variant']}")
    print(f"  + exact duplicates   : {stats['exact_dup']}")
    print(f"Duplicate rate         : {(total - stats['base']) / total:.1%}")

    print("\nRecords by resolved country:")
    by_country: dict[str, int] = {}
    spellings: dict[str, set[str]] = {}
    for rec in records:
        currency = str(rec["currency"])
        code = {"EUR": "DE", "USD": "US", "JPY": "JP"}[currency]
        by_country[code] = by_country.get(code, 0) + 1
        spellings.setdefault(code, set()).add(str(rec["country"]))
    for code in sorted(by_country):
        raws = ", ".join(sorted(spellings[code]))
        print(f"  {code}: {by_country[code]:>3}  (country spelled as: {raws})")

    print("\nMissing-value rates:")
    for field in ("tax_number", "email", "phone"):
        missing = sum(1 for r in records if r[field] is None)
        print(f"  {field:<12}: {missing:>3} / {total}  ({missing / total:.1%})")


def main() -> None:
    # 控制台可能是 GBK（Windows 中文），写文件始终用 UTF-8，摘要只打印 ASCII。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    records, ground_truth, stats = generate()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    GROUND_TRUTH_PATH.write_text(
        json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _summarize(records, stats)
    print(f"\nWrote {len(records)} records -> {OUTPUT_PATH}")
    print(f"Wrote ground truth        -> {GROUND_TRUTH_PATH}")


if __name__ == "__main__":
    main()
