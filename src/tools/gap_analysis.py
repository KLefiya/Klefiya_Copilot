"""需求抽取 + Fit/Gap 判定（模块二核心组件）。

两个环节：
  A. 抽取：从口语化访谈笔记里抽出结构化需求点（LLM）。抽取阶段【绝不读 ground truth】。
  B. 判定：对每条需求检索知识库，由 LLM 基于检索到的标准边界判定
     Fit / Configuration / Enhancement / Development，并输出可解释的证据与理由。

同时跑一个【无 LLM 的检索基线】作为对照组，用同一批抽取结果、同一套检索。
基线只能靠表面特征（命中哪个 section、有没有命中 KB-XC-002），
因此"LLM 是否显著超过基线"就成了"LLM 是否真在推理"的可证伪判据。

判定与评估严格分离：本工具不读 ground truth。评估在 evaluate_gap_analysis.py。

【模型与参数】
  model = claude-sonnet-5，不传 temperature。
  claude-sonnet-5 已移除采样参数，非默认的 temperature/top_p/top_k 会返回 400；
  而 claude-sonnet-4-6 虽接受 temperature，却不支持结构化输出（output_config.format），
  没有结构化输出就无法强约束 category/confidence/evidence/rationale 四件套。
  可复现性不依赖 temperature（它从来就不保证逐字一致），而由磁盘缓存承担。

【磁盘缓存】
  每次 LLM 调用按请求指纹 sha256 缓存到 data/synthetic/llm_cache/（进 git）。
  首次运行需要 ANTHROPIC_API_KEY 与联网；之后重跑完全离线且字节一致。
  --offline 强制只读缓存，缓存缺失即报错，绝不静默联网。

用法：
    export ANTHROPIC_API_KEY=sk-ant-...
    python src/tools/build_knowledge_base.py      # 先建库
    python src/tools/gap_analysis.py              # 抽取 + 判定 + 基线
    python src/tools/gap_analysis.py --offline    # 只读缓存重跑
    python src/tools/gap_analysis.py --baseline-only   # 只跑基线（仍需抽取缓存）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import attach_run_info  # noqa: E402
import build_knowledge_base as kb  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = PROJECT_ROOT / "data" / "synthetic" / "interview_notes.json"
KNOWLEDGE_PATH = PROJECT_ROOT / "data" / "knowledge" / "standard_processes.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "synthetic" / "gap_analysis_report.json"
CACHE_DIR = PROJECT_ROOT / "data" / "synthetic" / "llm_cache"

MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000

RETRIEVAL_TOP_K = 4          # 判定时喂给 LLM 的知识条目数（按条目去重后）
BASELINE_FETCH = 12          # 基线检索的 chunk 数
LOW_CONFIDENCE = 0.70        # 低于此置信度 → needs_review

CATEGORIES = ("Fit", "Configuration", "Enhancement", "Development")


# --------------------------------------------------------------------------
# 结构化输出 schema
# --------------------------------------------------------------------------
class ExtractedRequirement(BaseModel):
    requirement_description: str = Field(
        description="One sentence stating the requirement, in the business's own terms."
    )
    domain: Literal["P2P", "O2C", "R2R", "master_data"]
    source_quote: str = Field(
        description="The verbatim sentence or clause from the note that carries this requirement."
    )


class ExtractionResult(BaseModel):
    requirements: list[ExtractedRequirement]


class Judgement(BaseModel):
    category: Literal["Fit", "Configuration", "Enhancement", "Development"]
    confidence: float = Field(
        description="Your confidence in this category, between 0.0 and 1.0."
    )
    evidence_entry_ids: list[str] = Field(
        description="IDs of the knowledge entries that support this category, e.g. ['KB-XC-003']."
    )
    rationale: str = Field(
        description=(
            "Why this category, citing the standard boundary from the knowledge entries. "
            "State what the standard does or does not cover, and why that places the "
            "requirement in this tier. Do not classify from surface features alone."
        )
    )


# --------------------------------------------------------------------------
# 磁盘缓存的 LLM 调用
# --------------------------------------------------------------------------
def _fingerprint(model: str, system: str, user: str, schema: dict[str, Any]) -> str:
    payload = json.dumps(
        {"model": model, "system": system, "user": user, "schema": schema},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CachedLLM:
    """按请求指纹缓存 LLM 响应。缓存命中即离线；未命中才调用 API。"""

    def __init__(self, offline: bool = False) -> None:
        self.offline = offline
        self._client = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.stats = {"hit": 0, "miss": 0}

    def _lazy_client(self):
        if self._client is None:
            import anthropic

            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "缓存未命中且未设置 ANTHROPIC_API_KEY。\n"
                    "  设置密钥后重跑：export ANTHROPIC_API_KEY=sk-ant-...\n"
                    "  或用 --offline 只读已有缓存。"
                )
            self._client = anthropic.Anthropic()
        return self._client

    def parse(self, system: str, user: str, output_format: type[BaseModel]) -> BaseModel:
        schema = output_format.model_json_schema()
        digest = _fingerprint(MODEL, system, user, schema)
        cache_file = CACHE_DIR / f"{digest}.json"

        if cache_file.exists():
            self.stats["hit"] += 1
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            return output_format.model_validate(cached["parsed"])

        if self.offline:
            raise RuntimeError(
                f"--offline 模式下缓存未命中（{digest[:16]}）。"
                f"请先在联网环境下跑一次以填充 data/synthetic/llm_cache/。"
            )

        self.stats["miss"] += 1
        # 不传 temperature：claude-sonnet-5 拒绝非默认采样参数（400）。
        response = self._lazy_client().messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
        )
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError(f"结构化输出解析失败，stop_reason={response.stop_reason}")

        cache_file.write_text(
            json.dumps(
                {
                    "_request": {"model": MODEL, "system": system, "user": user},
                    "_schema_name": output_format.__name__,
                    "parsed": parsed.model_dump(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return parsed


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------
class Retriever:
    """一次性加载 embedding 模型与 Chroma 集合，避免每条需求都重载。"""

    def __init__(self) -> None:
        import chromadb
        from sentence_transformers import SentenceTransformer

        if not kb.CHROMA_PATH.exists():
            raise RuntimeError(
                "向量库不存在。先运行：python src/tools/build_knowledge_base.py"
            )
        self.model = SentenceTransformer(kb.EMBEDDING_MODEL)
        client = chromadb.PersistentClient(path=str(kb.CHROMA_PATH))
        self.collection = client.get_collection(kb.COLLECTION_NAME)

        payload = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
        self.entries = {e["id"]: e for e in payload["entries"]}

    def query(self, text: str, n: int = BASELINE_FETCH) -> list[dict[str, Any]]:
        vector = self.model.encode([text], normalize_embeddings=True).tolist()
        raw = self.collection.query(query_embeddings=vector, n_results=n)
        return [
            {
                "entry_id": raw["metadatas"][0][i]["entry_id"],
                "section": raw["metadatas"][0][i]["section"],
                "domain": raw["metadatas"][0][i]["domain"],
                "similarity": round(1.0 - raw["distances"][0][i], 4),
            }
            for i in range(len(raw["ids"][0]))
        ]

    def top_entries(self, hits: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
        """按条目去重，保留每个条目最高分的那个 section。"""
        best: dict[str, dict[str, Any]] = {}
        for hit in hits:
            best.setdefault(hit["entry_id"], hit)
        return list(best.values())[:k]

    def hydrate(self, entry_id: str) -> dict[str, Any]:
        return self.entries[entry_id]


# --------------------------------------------------------------------------
# 环节 A：需求抽取（不读 ground truth）
# --------------------------------------------------------------------------
EXTRACTION_SYSTEM = """You extract requirements from messy ERP workshop interview notes for a carve-out project.

The notes are transcribed speech: people ramble, complain, and bury real requirements inside \
side remarks. A single paragraph may contain several distinct requirements, and some paragraphs \
contain none at all.

Rules:
- Extract each distinct requirement separately. Do not merge two requirements into one sentence.
- Do not invent requirements. Complaints about the old system, scheduling chatter, and \
  small talk are not requirements.
- Restate each requirement in one clear sentence, in the business's own terms.
- source_quote must be copied verbatim from the note so a human can check the extraction.
- Do not classify the requirement. Do not say whether it is standard, configuration, or custom. \
  Classification happens in a separate step."""


def extract_requirements(llm: CachedLLM, notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    counter = 0
    for note in sorted(notes, key=lambda n: n["note_id"]):
        user = (
            f"Interview note {note['note_id']} — {note['session_title']}\n"
            f"Domain focus: {note['domain_focus']}\n"
            f"Participants: {', '.join(note['participants'])}\n\n"
            f"{note['text']}"
        )
        result: ExtractionResult = llm.parse(EXTRACTION_SYSTEM, user, ExtractionResult)
        for item in result.requirements:
            counter += 1
            extracted.append({
                "extracted_id": f"EX-{counter:03d}",
                "source_note_id": note["note_id"],
                "requirement_description": item.requirement_description,
                "domain": item.domain,
                "source_quote": item.source_quote,
            })
    return extracted


# --------------------------------------------------------------------------
# 环节 B1：LLM 判定
# --------------------------------------------------------------------------
JUDGE_SYSTEM = """You classify a single ERP requirement into exactly one of four delivery tiers, \
using only the SAP standard-process knowledge entries provided to you.

  Fit           The delivered standard already does this once baseline org structures exist.
  Configuration The delivered logic does this, driven by values maintained in customizing \
                (thresholds, code lists, org units, number ranges, determination rules). No code.
  Enhancement   The standard process still drives the flow, but a delivered object must carry an \
                extra custom field, or extra logic must run at a published extension point.
  Development   The standard provides no object, no process, and no extension point covering the \
                request; a new program, interface, or persistence must be built and maintained.

Method:
- Reason from the knowledge entries' stated boundaries: what the standard covers, what is a \
  configuration point, and what is stated to be beyond the standard.
- Do NOT classify from surface features. "It mentions an external system" is not a reason. \
  The reason must be that the standard provides no object, process, or extension point for it, \
  and that the transport, mapping, scheduling, and error handling are project deliverables.
- A requirement to make an already-delivered field required is field-status configuration. \
  A requirement to enforce entry in a field the delivered object does not have, or to gate a \
  process on a condition the standard data model does not carry, is an enhancement.
- In rationale, cite the specific entry IDs whose boundary decided the call, and say what that \
  boundary is. Cite only entries you were given.
- Set confidence below 0.7 when the entries conflict or none of them squarely covers the request."""


def judge_with_llm(
    llm: CachedLLM, retriever: Retriever, requirement: dict[str, Any]
) -> tuple[Judgement, list[dict[str, Any]]]:
    hits = retriever.query(requirement["requirement_description"])
    top = retriever.top_entries(hits, RETRIEVAL_TOP_K)

    blocks = []
    for hit in top:
        entry = retriever.hydrate(hit["entry_id"])
        blocks.append(
            f"### {entry['id']} — {entry['title']} [{entry['domain']}]\n"
            f"Standard process: {entry['standard_process']}\n"
            f"Capabilities covered by the standard: {'; '.join(entry['standard_capabilities'])}\n"
            f"Common configuration points: {'; '.join(entry['common_configuration_points'])}\n"
            f"Typically beyond the standard: {'; '.join(entry['typically_beyond_standard'])}"
        )

    user = (
        f"Requirement ({requirement['domain']}): {requirement['requirement_description']}\n\n"
        f"Verbatim from the workshop: \"{requirement['source_quote']}\"\n\n"
        f"Knowledge entries retrieved for this requirement:\n\n"
        + "\n\n".join(blocks)
    )
    judgement: Judgement = llm.parse(JUDGE_SYSTEM, user, Judgement)
    return judgement, top


# --------------------------------------------------------------------------
# 环节 B2：无 LLM 的检索基线（对照组）
# --------------------------------------------------------------------------
BASELINE_RULES_ZH = [
    "top-3 条目中出现 KB-XC-002（外部系统集成）→ Development",
    "否则 top-1 chunk 的 section 为 beyond_standard → Enhancement",
    "否则 top-1 chunk 的 section 为 configuration → Configuration",
    "否则 → Fit",
]


def judge_baseline(retriever: Retriever, requirement: dict[str, Any]) -> dict[str, Any]:
    """纯检索基线：只能读到"命中了哪个条目、哪个 section"，读不懂内容。

    这正是"靠表面特征"的形式化版本——它是对照组，不是候选方案。
    """
    hits = retriever.query(requirement["requirement_description"])
    top_entry_ids = [h["entry_id"] for h in retriever.top_entries(hits, 3)]
    top_chunk = hits[0]

    if "KB-XC-002" in top_entry_ids:
        category, rule = "Development", BASELINE_RULES_ZH[0]
    elif top_chunk["section"] == "beyond_standard":
        category, rule = "Enhancement", BASELINE_RULES_ZH[1]
    elif top_chunk["section"] == "configuration":
        category, rule = "Configuration", BASELINE_RULES_ZH[2]
    else:
        category, rule = "Fit", BASELINE_RULES_ZH[3]

    return {
        "category": category,
        "rule_fired_zh": rule,
        "top_entry_ids": top_entry_ids,
        "top_chunk": {
            "entry_id": top_chunk["entry_id"],
            "section": top_chunk["section"],
            "similarity": top_chunk["similarity"],
        },
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def build_report(llm: CachedLLM, baseline_only: bool) -> dict[str, Any]:
    notes = json.loads(NOTES_PATH.read_text(encoding="utf-8"))["sessions"]
    retriever = Retriever()

    extracted = extract_requirements(llm, notes)

    results: list[dict[str, Any]] = []
    for requirement in extracted:
        baseline = judge_baseline(retriever, requirement)
        entry: dict[str, Any] = {**requirement, "baseline": baseline}

        if not baseline_only:
            judgement, retrieved = judge_with_llm(llm, retriever, requirement)
            retrieved_ids = {hit["entry_id"] for hit in retrieved}
            hallucinated = sorted(set(judgement.evidence_entry_ids) - retrieved_ids)
            needs_review = (
                judgement.confidence < LOW_CONFIDENCE
                or not judgement.evidence_entry_ids
                or bool(hallucinated)
            )
            entry["llm"] = {
                "category": judgement.category,
                "confidence": round(judgement.confidence, 4),
                "evidence": judgement.evidence_entry_ids,
                "rationale": judgement.rationale,
                "needs_review": needs_review,
                "needs_review_reasons": [
                    reason
                    for reason, fired in (
                        (f"置信度 {judgement.confidence:.2f} < {LOW_CONFIDENCE}",
                         judgement.confidence < LOW_CONFIDENCE),
                        ("未给出任何证据条目", not judgement.evidence_entry_ids),
                        (f"引用了未被检索到的条目 {hallucinated}", bool(hallucinated)),
                    )
                    if fired
                ],
                "retrieved_entry_ids": sorted(retrieved_ids),
            }
        results.append(entry)

    dev_backlog = [
        {
            "backlog_id": f"DEV-{index:03d}",
            "requirement_id": r["extracted_id"],
            "source_note_id": r["source_note_id"],
            "description": r["requirement_description"],
            "domain": r["domain"],
            "rationale": r["llm"]["rationale"],
            "evidence": r["llm"]["evidence"],
            "confidence": r["llm"]["confidence"],
            "needs_review": r["llm"]["needs_review"],
        }
        for index, r in enumerate(
            (x for x in results if "llm" in x and x["llm"]["category"] == "Development"), 1
        )
    ]

    return {
        "_meta": {
            "module": "module_2_fit_to_standard_gap_analysis",
            "component": "requirement_extraction_and_fit_gap_judgement",
            "model": MODEL,
            "sampling_note_zh": (
                "未传 temperature：claude-sonnet-5 已移除采样参数，非默认值返回 400。"
                "可复现性由磁盘缓存承担，而非 temperature=0（后者从不保证逐字一致）。"
            ),
            "separation_note_zh": (
                "抽取阶段不读 ground truth；判定阶段不读 ground truth；"
                "评估在 evaluate_gap_analysis.py 中独立进行。"
            ),
            "knowledge_base": str(KNOWLEDGE_PATH.relative_to(PROJECT_ROOT)),
            "retrieval_top_k": RETRIEVAL_TOP_K,
            "low_confidence_threshold": LOW_CONFIDENCE,
            "baseline_rules_zh": BASELINE_RULES_ZH,
            "baseline_purpose_zh": (
                "无 LLM 的检索基线是【对照组】：它只能读到命中了哪个条目/哪个 section，"
                "读不懂条目内容，因此只能靠表面特征。若 LLM 准确率与基线相当，"
                "说明 LLM 也在靠表面特征；显著超出才说明推理带来了价值。"
            ),
            "llm_cache": {
                "dir": str(CACHE_DIR.relative_to(PROJECT_ROOT)),
                "hits": llm.stats["hit"],
                "misses": llm.stats["miss"],
                "note_zh": "缓存进 git；填充后重跑完全离线且字节一致。",
            },
            "extracted_requirement_count": len(extracted),
        },
        "requirements": results,
        "dev_backlog": dev_backlog,
        "dev_backlog_note_zh": (
            "本表是给模块三 RAID 治理消费的接口：判定为 Development 的需求，"
            "每条带需求 id、描述、领域、判定理由与证据。模块三直接读本字段。"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="只读缓存，缺失即报错")
    parser.add_argument("--baseline-only", action="store_true", help="跳过 LLM 判定，只跑基线")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    llm = CachedLLM(offline=args.offline)
    report = attach_run_info(build_report(llm, args.baseline_only))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    meta = report["_meta"]
    print(f"Model            : {meta['model']}")
    print(f"Extracted reqs   : {meta['extracted_requirement_count']}")
    print(f"LLM cache        : {llm.stats['hit']} hit / {llm.stats['miss']} miss")
    print(f"Content          : sha256 {report['_run_info']['content_sha256'][:16]}\n")

    from collections import Counter

    baseline_counts = Counter(r["baseline"]["category"] for r in report["requirements"])
    print("Baseline (no LLM) categories:")
    for category in CATEGORIES:
        print(f"  {category:<14} {baseline_counts.get(category, 0)}")

    if not args.baseline_only:
        llm_counts = Counter(r["llm"]["category"] for r in report["requirements"])
        review = sum(1 for r in report["requirements"] if r["llm"]["needs_review"])
        print("\nLLM categories:")
        for category in CATEGORIES:
            print(f"  {category:<14} {llm_counts.get(category, 0)}")
        print(f"\nneeds_review     : {review}")
        print(f"dev_backlog      : {len(report['dev_backlog'])}")

    print(f"\nWrote report -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
