"""生成模拟的"遗留 ECC 系统供应商主数据"（合成数据，不对应任何真实公司）。

输出的记录使用遗留系统风格的字段名（legacy_vendor_id / vendor_name / ...），
将来由映射 Agent 映射到目标端 A_BusinessPartner（A2X 命名）。

数据中被【故意】注入四类真实世界脏数据：
  a) 同一供应商的重复记录，字段【不逐字相同】：名称写法差异 + 字符级噪声、
     地址缩写与笔误、邮编错位或缺失、建档日期漂移、联系方式各异
  b) 完全重复记录（整条复制）
  c) 字段缺失（税号 / 邮箱 / 电话 / 邮编）
  d) 国家代码格式不统一（DE / Germany / Deutschland / GER）

【为什么 (a) 必须有字符级噪声——一次真实的返工】
    早先的实现构造变体时只改写 vendor_name 与 country，其余字段整条复制。
    后果是 city / street / postal_code / currency / created_date 在【每一对】
    真实重复记录中都逐字相同，于是：
      - 一句 `GROUP BY postal_code` 就能完美复原 ground truth（F1 = 1.0）；
      - Splink 的 precision/recall 也是 1.0，但那度量的是生成器的性质，不是模型；
      - 标准化之后名称已逐字相同，Jaro-Winkler / Levenshtein 层从未被观测到，
        全部比较器的 m 退化为 1，Fellegi-Sunter 的概率加权根本没被触发。
    格式变化（大小写 / 空格 / 标点 / & vs und / 后缀写法）是【可被标准化完全还原】的，
    因此它不构成难度。真正的难度来自不可逆噪声：拼写错误、字符换位、OCR 混淆、
    地址缩写、门牌与邮编笔误。现在按脏度梯度注入这些噪声，
    让平凡基线失效、让概率匹配真正承担工作、让置信度产生分布。

【脏度梯度】
    每条变体记录抽一个脏度档（clean / moderate / dirty，见 DIRT_PROFILES）。
    clean 档只有格式差异，仍可被精确匹配找回；dirty 档需要模糊匹配才可能找回。
    梯度的意义：下游的匹配置信度才会有高有低，needs_review 才有内容。

【可复现性】
    噪声走独立的 noise_rng，不消费主 rng。因此基础供应商、哪些供应商产生变体、
    country 的写法抽样全部与注入噪声前逐字一致——记录总数、实体数、国家分布不变，
    只有变体记录的字段内容变脏。这让本次改动的影响面可控且可审计。

用法：
    python src/tools/generate_legacy_vendors.py
"""

from __future__ import annotations

import json
import random
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from faker import Faker

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------
SEED = 20260709
NOISE_SEED_OFFSET = 7919  # 噪声用独立 rng，不消费主 rng（见模块 docstring）

BASE_VENDOR_COUNT = 150          # 基础唯一供应商数量（真实实体数）
VARIANT_RATE = 0.20              # 有多少比例的基础供应商会产生"拼写变体"重复
MAX_VARIANTS_PER_VENDOR = 2      # 单个供应商最多产生几条变体
EXACT_DUP_RATE = 0.05            # 有多少比例的基础供应商会产生"完全重复"记录

MISSING_RATE_TAX_NUMBER = 0.08   # 基础记录上的字段缺失概率
MISSING_RATE_EMAIL = 0.12
MISSING_RATE_PHONE = 0.10
MAX_MISSING_RATE = 0.90           # 倍数放大后的上限，避免变体记录全空

COUNTRY_WEIGHTS = {"DE": 0.45, "US": 0.35, "JP": 0.20}

CREATED_DATE_START = "1998-01-01"  # 遗留系统的建档年代
CREATED_DATE_END = "2019-12-31"

OUTPUT_PATH = Path(__file__).resolve().parents[2] / "data" / "legacy" / "legacy_vendors.json"
GROUND_TRUTH_PATH = OUTPUT_PATH.with_name("legacy_vendors_ground_truth.json")
MANIFEST_PATH = OUTPUT_PATH.with_name("legacy_vendors_variant_manifest.json")


# --------------------------------------------------------------------------
# 脏度梯度：每条变体记录抽一档。全部比例集中在此，便于调参。
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class DirtProfile:
    """一档脏度下，各类噪声的施加概率与强度。"""

    name_char_noise_rate: float      # 名称施加字符级噪声的概率
    max_name_char_edits: int         # 一次最多几处字符编辑
    street_abbreviation_rate: float  # 街道类型词缩写/展开
    house_number_typo_rate: float    # 门牌号数字笔误
    postal_typo_rate: float          # 邮编数字笔误
    postal_missing_rate: float       # 邮编整体缺失
    city_typo_rate: float            # 城市名笔误
    created_date_drift_rate: float   # 建档日期漂移（重复档案往往晚建）
    max_created_date_drift_days: int
    email_local_part_rate: float     # info@ -> sales@ 之类
    phone_typo_rate: float           # 电话数字笔误
    tax_separator_rate: float        # 税号分隔符写法不同
    missing_multiplier: float        # 变体记录更不完整


# 权重之和须为 1。clean 档只有格式差异（可被标准化完全还原），
# dirty 档必须靠模糊匹配才可能找回。梯度制造出置信度的分布。
DIRT_LEVEL_WEIGHTS: dict[str, float] = {"clean": 0.30, "moderate": 0.40, "dirty": 0.30}

DIRT_PROFILES: dict[str, DirtProfile] = {
    "clean": DirtProfile(
        name_char_noise_rate=0.0, max_name_char_edits=0,
        street_abbreviation_rate=0.0, house_number_typo_rate=0.0,
        postal_typo_rate=0.0, postal_missing_rate=0.0, city_typo_rate=0.0,
        created_date_drift_rate=0.0, max_created_date_drift_days=0,
        email_local_part_rate=0.0, phone_typo_rate=0.0, tax_separator_rate=0.0,
        missing_multiplier=1.5,
    ),
    "moderate": DirtProfile(
        name_char_noise_rate=0.60, max_name_char_edits=1,
        street_abbreviation_rate=0.60, house_number_typo_rate=0.25,
        postal_typo_rate=0.15, postal_missing_rate=0.05, city_typo_rate=0.0,
        created_date_drift_rate=0.40, max_created_date_drift_days=400,
        email_local_part_rate=0.20, phone_typo_rate=0.20, tax_separator_rate=0.30,
        missing_multiplier=2.0,
    ),
    "dirty": DirtProfile(
        name_char_noise_rate=1.0, max_name_char_edits=3,
        street_abbreviation_rate=0.90, house_number_typo_rate=0.60,
        postal_typo_rate=0.45, postal_missing_rate=0.15, city_typo_rate=0.30,
        created_date_drift_rate=0.85, max_created_date_drift_days=1500,
        email_local_part_rate=0.50, phone_typo_rate=0.60, tax_separator_rate=0.50,
        missing_multiplier=3.0,
    ),
}


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
# 脏数据 (a) 的第二层：字符级噪声
#
# 上面的 NAME_TRANSFORMS 全部是【可逆】的格式变化——下游做一次标准化就能完全还原，
# 因此它们不构成匹配难度。下面这些是【不可逆】的：删掉的字符补不回来，换位后的
# 原序推不出来。只有它们才会逼出模糊匹配（Jaro-Winkler / Levenshtein）。
# --------------------------------------------------------------------------
# QWERTY 相邻键，模拟手误
KEYBOARD_NEIGHBORS: dict[str, str] = {
    "a": "qswz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr",
    "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn",
    "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm", "o": "iklp",
    "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy",
    "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu",
    "z": "asx",
}

# 扫描件 / 传真件录入产生的经典混淆对（双向）
OCR_CONFUSIONS: tuple[tuple[str, str], ...] = (
    ("0", "O"), ("1", "l"), ("5", "S"), ("8", "B"), ("2", "Z"),
    ("rn", "m"), ("cl", "d"), ("vv", "w"),
)


def _editable_positions(text: str) -> list[int]:
    """只在字母/数字上做编辑：动空格会退化成"空白差异"，那已由格式变形覆盖。"""
    return [i for i, char in enumerate(text) if char.isalnum()]


def _noise_transpose(text: str, rng: random.Random) -> str | None:
    spots = [i for i in _editable_positions(text)
             if i + 1 < len(text) and text[i + 1].isalnum() and text[i] != text[i + 1]]
    if not spots:
        return None
    i = rng.choice(spots)
    return text[:i] + text[i + 1] + text[i] + text[i + 2:]


def _noise_delete(text: str, rng: random.Random) -> str | None:
    spots = _editable_positions(text)
    if len(spots) < 5:  # 太短就删，会把名字毁到人也认不出
        return None
    i = rng.choice(spots)
    return text[:i] + text[i + 1:]


def _noise_insert(text: str, rng: random.Random) -> str | None:
    spots = _editable_positions(text)
    if not spots:
        return None
    i = rng.choice(spots)
    return text[:i] + text[i] + text[i:]  # 重复一个字符（打字时手抖）


def _noise_substitute(text: str, rng: random.Random) -> str | None:
    spots = _editable_positions(text)
    if not spots:
        return None
    i = rng.choice(spots)
    char = text[i]
    neighbors = KEYBOARD_NEIGHBORS.get(char.lower())
    if neighbors:
        replacement = rng.choice(neighbors)
        replacement = replacement.upper() if char.isupper() else replacement
    elif char.isdigit():
        replacement = rng.choice([d for d in "0123456789" if d != char])
    else:
        return None
    return text[:i] + replacement + text[i + 1:]


def _noise_ocr(text: str, rng: random.Random) -> str | None:
    applicable = [
        (source, target)
        for left, right in OCR_CONFUSIONS
        for source, target in ((left, right), (right, left))
        if source in text
    ]
    if not applicable:
        return None
    source, target = rng.choice(applicable)
    return text.replace(source, target, 1)


CHAR_NOISE_OPS = (
    _noise_transpose, _noise_delete, _noise_insert, _noise_substitute, _noise_ocr,
)


def apply_char_noise(text: str, edits: int, rng: random.Random) -> tuple[str, list[str]]:
    """施加至多 edits 处字符级编辑。返回 (结果, 实际生效的操作名)。"""
    result = text
    applied: list[str] = []
    for _ in range(edits):
        for operation in rng.sample(CHAR_NOISE_OPS, k=len(CHAR_NOISE_OPS)):
            candidate = operation(result, rng)
            if candidate is not None and candidate != result:
                result = candidate
                applied.append(operation.__name__.removeprefix("_noise_"))
                break  # 本轮已成功编辑一处
    return result, applied


def _digit_typo(text: str, rng: random.Random) -> str:
    """把一个数字改成另一个数字。没有数字就原样返回。"""
    spots = [i for i, char in enumerate(text) if char.isdigit()]
    if not spots:
        return text
    i = rng.choice(spots)
    replacement = rng.choice([d for d in "0123456789" if d != text[i]])
    return text[:i] + replacement + text[i + 1:]


# 街道类型词的缩写 / 展开。真实主数据里两种写法长期并存。
STREET_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    # 美国
    ("Street", "St."), ("Avenue", "Ave."), ("Boulevard", "Blvd."), ("Road", "Rd."),
    ("Drive", "Dr."), ("Lane", "Ln."), ("Court", "Ct."), ("Place", "Pl."),
    ("Parkway", "Pkwy."), ("Square", "Sq."), ("Trail", "Trl."), ("Terrace", "Ter."),
    ("Heights", "Hts."), ("Junction", "Jct."), ("Crossing", "Xing"),
    ("Suite", "Ste."), ("Apartment", "Apt."), ("Mount", "Mt."),
    # 德国
    ("straße", "str."), ("Straße", "Str."), ("strasse", "str."),
    ("weg", "wg."), ("Platz", "Pl."), ("gasse", "g."), ("allee", "al."),
)


def _vary_street(street: str, profile: DirtProfile, rng: random.Random) -> tuple[str, list[str]]:
    """街道地址的真实脏法：类型词缩写/展开 + 门牌号笔误。"""
    result = street
    applied: list[str] = []

    if rng.random() < profile.street_abbreviation_rate:
        # 长词优先，否则 "straße" 会先被 "gasse" 之类的短规则漏掉边界
        candidates = [
            (full, short) for full, short in STREET_ABBREVIATIONS
            if full in result or short in result
        ]
        if candidates:
            full, short = rng.choice(candidates)
            if full in result:
                result = result.replace(full, short, 1)   # 展开 -> 缩写
            else:
                result = result.replace(short, full, 1)    # 缩写 -> 展开
            applied.append("street_abbreviation")

    if rng.random() < profile.house_number_typo_rate:
        typo = _digit_typo(result, rng)
        if typo != result:
            result = typo
            applied.append("house_number_typo")

    return result, applied


EMAIL_LOCAL_PARTS = ("sales", "contact", "kontakt", "vertrieb", "office", "einkauf")


def _vary_email(email: str, rng: random.Random) -> str:
    """同一家公司，不同人建的档案往往留了不同的联系邮箱。"""
    local, _, domain = email.partition("@")
    replacement = rng.choice([p for p in EMAIL_LOCAL_PARTS if p != local])
    return f"{replacement}@{domain}"


def _vary_tax_separator(tax_number: str, rng: random.Random) -> str:
    """税号分隔符写法：DE123456789 / DE 123456789；12-3456789 / 123456789。"""
    if "-" in tax_number:
        return tax_number.replace("-", "") if rng.random() < 0.5 else tax_number.replace("-", " ")
    if tax_number.startswith("DE"):
        return "DE " + tax_number[2:]
    if len(tax_number) == 13 and tax_number.isdigit():  # JP
        return f"{tax_number[:4]}-{tax_number[4:]}"
    return tax_number


def _drift_created_date(created: str, profile: DirtProfile, rng: random.Random) -> str:
    """重复档案通常是后来才被重新建的，建档日期不该与原档案相同。"""
    base = date(int(created[:4]), int(created[4:6]), int(created[6:8]))
    drift = rng.randint(1, profile.max_created_date_drift_days)
    return (base + timedelta(days=drift)).strftime("%Y%m%d")


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


def _split_legal_suffix(name: str, country: str) -> tuple[str, str]:
    """把 "核心名 + 法律后缀" 切开。字符噪声只打核心名，不毁掉法律形式。"""
    for suffix in sorted(COUNTRY_PROFILES[country].legal_suffixes, key=len, reverse=True):
        if name.endswith(suffix):
            return name[: -len(suffix)].rstrip(), suffix
    return name, ""


def build_name_variant_record(
    base: dict[str, object],
    country: str,
    suffix_id: str,
    rng: random.Random,
    noise_rng: random.Random,
) -> tuple[dict[str, object], str, list[str]]:
    """脏数据 (a)：同一实体的另一条记录。返回 (记录, 脏度档, 施加的噪声清单)。

    【rng 与 noise_rng 的分工】
    rng 承担与注入噪声前完全相同的调用序列（名称格式变形、country 写法、三次
    缺失抽样），noise_rng 承担全部新增的噪声抽样。因此基础供应商、变体/重复的
    抽样结果、国家分布都与改动前逐字一致，本次改动的影响面被限制在变体的字段值上。
    """
    profile = COUNTRY_PROFILES[country]
    level = noise_rng.choices(
        list(DIRT_LEVEL_WEIGHTS), weights=list(DIRT_LEVEL_WEIGHTS.values()), k=1
    )[0]
    dirt = DIRT_PROFILES[level]
    applied: list[str] = []

    record = dict(base)
    record["legacy_vendor_id"] = f"{base['legacy_vendor_id']}{suffix_id}"

    # ---- 名称：先打不可逆的字符噪声，再叠可逆的格式变形 ----
    name = str(base["vendor_name"])
    if noise_rng.random() < dirt.name_char_noise_rate and dirt.max_name_char_edits:
        core, legal_suffix = _split_legal_suffix(name, country)
        edits = noise_rng.randint(1, dirt.max_name_char_edits)
        noised_core, operations = apply_char_noise(core, edits, noise_rng)
        if operations:
            name = f"{noised_core} {legal_suffix}".strip()
            applied.extend(f"name_{op}" for op in operations)

    record["vendor_name"], _ = make_name_variant(name, rng)
    record["country"] = rng.choice(profile.country_raw_spellings)

    # ---- 地址 ----
    street, street_ops = _vary_street(str(base["street"]), dirt, noise_rng)
    record["street"] = street
    applied.extend(street_ops)

    if noise_rng.random() < dirt.city_typo_rate:
        city, city_ops = apply_char_noise(str(base["city"]), 1, noise_rng)
        if city_ops:
            record["city"] = city
            applied.append("city_typo")

    if noise_rng.random() < dirt.postal_missing_rate:
        record["postal_code"] = None
        applied.append("postal_missing")
    elif noise_rng.random() < dirt.postal_typo_rate:
        postal = _digit_typo(str(base["postal_code"]), noise_rng)
        if postal != base["postal_code"]:
            record["postal_code"] = postal
            applied.append("postal_typo")

    # ---- 建档日期：重复档案是后来才建的 ----
    if noise_rng.random() < dirt.created_date_drift_rate:
        record["created_date"] = _drift_created_date(
            str(base["created_date"]), dirt, noise_rng
        )
        applied.append("created_date_drift")

    # ---- 联系方式与税号 ----
    if record["email"] is not None and noise_rng.random() < dirt.email_local_part_rate:
        record["email"] = _vary_email(str(record["email"]), noise_rng)
        applied.append("email_local_part")

    if record["phone"] is not None and noise_rng.random() < dirt.phone_typo_rate:
        phone = _digit_typo(str(record["phone"]), noise_rng)
        if phone != record["phone"]:
            record["phone"] = phone
            applied.append("phone_typo")

    if record["tax_number"] is not None and noise_rng.random() < dirt.tax_separator_rate:
        tax = _vary_tax_separator(str(record["tax_number"]), noise_rng)
        if tax != record["tax_number"]:
            record["tax_number"] = tax
            applied.append("tax_separator")

    # ---- 缺失：脏数据往往更不完整。调用序列与改动前一致（每字段恰好一次 rng）----
    for field, rate in (
        ("tax_number", MISSING_RATE_TAX_NUMBER),
        ("email", MISSING_RATE_EMAIL),
        ("phone", MISSING_RATE_PHONE),
    ):
        inflated = min(rate * dirt.missing_multiplier, MAX_MISSING_RATE)
        if record[field] is not None:
            before = record[field]
            record[field] = _maybe_missing(str(record[field]), inflated, rng)
            if record[field] is None and before is not None:
                applied.append(f"{field}_missing")

    return record, level, sorted(set(applied))


def build_exact_duplicate(base: dict[str, object]) -> dict[str, object]:
    """脏数据 (b)：整条复制，只有 id 带 X 后缀。"""
    record = dict(base)
    record["legacy_vendor_id"] = f"{base['legacy_vendor_id']}X"
    return record


def _parse_date(iso: str) -> date:
    return date.fromisoformat(iso)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def generate() -> tuple[
    list[dict[str, object]], dict[str, str], dict[str, dict[str, object]], dict[str, int]
]:
    rng = random.Random(SEED)
    # 噪声独立取种。它绝不能消费 rng，否则基础数据会随噪声参数一起漂移，
    # 每次调脏度都要重跑并 diff 下游全部报告。
    noise_rng = random.Random(SEED + NOISE_SEED_OFFSET)

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
    manifest: dict[str, dict[str, object]] = {}  # record_id -> 角色 / 脏度 / 施加的噪声
    stats = {"base": 0, "variant": 0, "exact_dup": 0}

    for index in range(1, BASE_VENDOR_COUNT + 1):
        country = rng.choices(countries, weights=weights, k=1)[0]
        base = build_base_vendor(index, country, fakers[country], rng)
        cluster_id = str(base["legacy_vendor_id"])

        records.append(base)
        ground_truth[cluster_id] = cluster_id
        manifest[cluster_id] = {
            "entity_id": cluster_id, "role": "base",
            "dirt_level": None, "noise_applied": [],
        }
        stats["base"] += 1

        if rng.random() < VARIANT_RATE:
            for n in range(1, rng.randint(1, MAX_VARIANTS_PER_VENDOR) + 1):
                suffix_id = "D" if n == 1 else f"D{n}"
                variant, level, applied = build_name_variant_record(
                    base, country, suffix_id, rng, noise_rng
                )
                record_id = str(variant["legacy_vendor_id"])
                records.append(variant)
                ground_truth[record_id] = cluster_id
                manifest[record_id] = {
                    "entity_id": cluster_id, "role": "name_variant",
                    "dirt_level": level, "noise_applied": applied,
                }
                stats["variant"] += 1

        if rng.random() < EXACT_DUP_RATE:
            dup = build_exact_duplicate(base)
            record_id = str(dup["legacy_vendor_id"])
            records.append(dup)
            ground_truth[record_id] = cluster_id
            manifest[record_id] = {
                "entity_id": cluster_id, "role": "exact_duplicate",
                "dirt_level": None, "noise_applied": [],
            }
            stats["exact_dup"] += 1

    rng.shuffle(records)  # 打散，避免重复记录相邻导致下游"作弊"
    return records, ground_truth, manifest, stats


DUPLICATE_SIGNAL_FIELDS = (
    "vendor_name", "country", "city", "street", "postal_code",
    "tax_number", "email", "phone", "currency", "created_date",
)


SOLVES_IT_F1 = 0.99  # 单字段 GROUP BY 达到此 F1 即视为该字段泄漏了 ground truth


def _baseline_f1(
    records: list[dict[str, object]],
    truth_groups: set[frozenset[str]],
    key: Callable[[dict[str, object]], object],
) -> tuple[float, float, float]:
    """`GROUP BY key` 当作去重结果，与真实重复组做 cluster 级精确匹配。"""
    groups: dict[object, list[str]] = {}
    for record in records:
        value = key(record)
        # 键缺失的记录各自成组，不能因为"都是 NULL"而并到一起
        bucket = ("__null__", record["legacy_vendor_id"]) if value is None else value
        groups.setdefault(bucket, []).append(str(record["legacy_vendor_id"]))

    predicted = {frozenset(v) for v in groups.values() if len(v) > 1}
    hits = len(predicted & truth_groups)
    precision = hits / len(predicted) if predicted else 0.0
    recall = hits / len(truth_groups) if truth_groups else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _leakage_report(
    records: list[dict[str, object]], ground_truth: dict[str, str]
) -> None:
    """数据自检：ground truth 是否被某个字段直接泄漏。

    【判据是 GROUP BY 的 F1，不是"组内一致率"】
    组内 100% 一致只是必要条件，不是充分条件——它还必须【跨实体有区分度】。
    反例就在本表里：`currency` 在每一对真实重复记录中都相同（同一家公司当然同币种），
    但它对同国的所有供应商都相同，`GROUP BY currency` 只会得到 3 个大组，F1 ≈ 0。
    只有当 `GROUP BY 该字段` 真的能复原 ground truth 时，指标才是空的。

    这个检查放在生成器里，是为了让泄漏在数据产出的那一刻就暴露，
    而不是等下游评估时才被发现。
    """
    from itertools import combinations

    by_entity: dict[str, list[dict[str, object]]] = {}
    for record in records:
        by_entity.setdefault(ground_truth[str(record["legacy_vendor_id"])], []).append(record)

    true_pairs = [
        (left, right)
        for members in by_entity.values()
        for left, right in combinations(members, 2)
    ]
    truth_groups = {
        frozenset(str(r["legacy_vendor_id"]) for r in members)
        for members in by_entity.values() if len(members) > 1
    }

    print(f"\nTrue duplicate pairs   : {len(true_pairs)}")
    print("Per-field leakage check:")
    print("  identical = share of true duplicate pairs where the field matches byte-for-byte")
    print("  F1        = cluster-level F1 of `GROUP BY <field>` alone")
    print(f"  {'field':<14} {'identical':>10} {'GROUP BY F1':>13}")
    leaked: list[str] = []
    for field in DUPLICATE_SIGNAL_FIELDS:
        identical = sum(
            1 for left, right in true_pairs
            if left[field] is not None and left[field] == right[field]
        )
        rate = identical / len(true_pairs) if true_pairs else 0.0
        _, _, f1 = _baseline_f1(records, truth_groups, lambda r, f=field: r[f])
        marker = ""
        if f1 >= SOLVES_IT_F1:
            marker = "  <-- LEAK: this field alone reproduces ground truth"
            leaked.append(field)
        print(f"  {field:<14} {rate:>9.1%} {f1:>13.4f}{marker}")

    print("\nComposite trivial baselines (cluster-level, duplicate groups only):")
    for label, key in (
        ("GROUP BY street+created_date", lambda r: (r["street"], r["created_date"])),
        ("GROUP BY city+created_date", lambda r: (r["city"], r["created_date"])),
        ("GROUP BY city+postal_code", lambda r: (r["city"], r["postal_code"])),
    ):
        precision, recall, f1 = _baseline_f1(records, truth_groups, key)
        flag = "  <-- SOLVES IT" if f1 >= SOLVES_IT_F1 else ""
        print(f"  {label:<30} P {precision:.4f}  R {recall:.4f}  F1 {f1:.4f}{flag}")

    print()
    if leaked:
        print(f"VERDICT: ground truth is LEAKED by {leaked}. "
              f"Downstream precision/recall will not measure model ability.")
    else:
        print("VERDICT: no single field reproduces ground truth. "
              "Downstream precision/recall is meaningful.")


def _summarize(
    records: list[dict[str, object]],
    ground_truth: dict[str, str],
    manifest: dict[str, dict[str, object]],
    stats: dict[str, int],
) -> None:
    total = len(records)
    print(f"Total records          : {total}")
    print(f"Base unique vendors    : {stats['base']}")
    print(f"  + name variants      : {stats['variant']}")
    print(f"  + exact duplicates   : {stats['exact_dup']}")
    print(f"Duplicate rate         : {(total - stats['base']) / total:.1%}")

    levels: dict[str, int] = {}
    operations: dict[str, int] = {}
    for entry in manifest.values():
        if entry["role"] != "name_variant":
            continue
        levels[str(entry["dirt_level"])] = levels.get(str(entry["dirt_level"]), 0) + 1
        for operation in entry["noise_applied"]:  # type: ignore[union-attr]
            operations[str(operation)] = operations.get(str(operation), 0) + 1

    print("\nVariant dirt levels:")
    for level in DIRT_LEVEL_WEIGHTS:
        count = levels.get(level, 0)
        share = count / stats["variant"] if stats["variant"] else 0.0
        print(f"  {level:<10} {count:>3}  ({share:.1%}, target {DIRT_LEVEL_WEIGHTS[level]:.0%})")

    print("\nNoise operations applied (across all variants):")
    for operation, count in sorted(operations.items(), key=lambda kv: -kv[1]):
        print(f"  {operation:<24} {count:>3}")

    _leakage_report(records, ground_truth)

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

    records, ground_truth, manifest, stats = generate()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    GROUND_TRUTH_PATH.write_text(
        json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 变体清单只用于【评估与展示】：按脏度档拆解召回率，说明模型在哪一档开始失手。
    # 与 ground truth 一样，判定组件在结构上被禁止读取它。
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _summarize(records, ground_truth, manifest, stats)
    print(f"\nWrote {len(records)} records -> {OUTPUT_PATH}")
    print(f"Wrote ground truth        -> {GROUND_TRUTH_PATH}")
    print(f"Wrote variant manifest    -> {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
