"""把 SAP 标准流程知识条目切分、编码，存进本地 Chroma 向量库。

切分策略：每条知识条目按 section 切成 4 个 chunk（overview / capabilities /
configuration / beyond_standard），而不是整条一个 chunk。原因是判定组件的 query
往往只打中某一个 section（"approval threshold" 打中 configuration，
"external system interface" 打中 beyond_standard），整条编码会把信号稀释掉。

embedding 复用模块一的 all-MiniLM-L6-v2，显式编码后传给 Chroma，
避免 Chroma 默认 embedding function 去下载它自己的 ONNX 模型。
Chroma 是纯本地持久化，不外联。

用法：
    python src/tools/build_knowledge_base.py            # 建库 + 跑检索测试
    python src/tools/build_knowledge_base.py --query "purchase order approval threshold"
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from data_profile import attach_run_info  # noqa: E402  复用模块一的可复现机制

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_PATH = PROJECT_ROOT / "data" / "knowledge" / "standard_processes.json"
CHROMA_PATH = PROJECT_ROOT / "data" / "knowledge" / "chroma"
MANIFEST_PATH = PROJECT_ROOT / "data" / "knowledge" / "knowledge_index_manifest.json"

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
COLLECTION_NAME = "sap_standard_processes"
TOP_K = 3

# 按 entry 去重时的过取倍数：同一条目的 4 个 section 会互相挤占名次，
# 不过取就拿不到第二、第三个不同的条目。
OVERFETCH = 4

# 每个 section 一个 chunk：(section 名, 条目里的字段名, 给 embedding 的前缀)
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("overview", "standard_process", "Standard process overview"),
    ("capabilities", "standard_capabilities", "Capabilities covered by the standard"),
    ("configuration", "common_configuration_points", "Common configuration points"),
    ("beyond_standard", "typically_beyond_standard", "Typically beyond the standard"),
)

SMOKE_QUERIES: tuple[str, ...] = (
    "purchase order approval threshold",
    "custom external system interface",
    "normalize country values into ISO codes",
    "require a mandatory custom field before releasing a document",
    "standard sales order to delivery to billing flow",
    "credit limit per customer group",
)


def load_entries() -> list[dict[str, Any]]:
    payload = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    return payload["entries"]


def build_chunks(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把条目切成 (entry, section) 粒度的 chunk。id 稳定可复现。"""
    chunks: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda e: e["id"]):
        for section, field, prefix in SECTIONS:
            value = entry[field]
            body = value if isinstance(value, str) else " ".join(value)
            # 标题一并编码：section 单独看往往缺少主题词
            document = f"{entry['title']}. {prefix}: {body}"
            chunks.append({
                "id": f"{entry['id']}::{section}",
                "document": document,
                "metadata": {
                    "entry_id": entry["id"],
                    "domain": entry["domain"],
                    "title": entry["title"],
                    "section": section,
                    "authorship": entry["authorship"],
                },
            })
    return chunks


def index(chunks: list[dict[str, Any]]) -> None:
    import chromadb
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL)
    embeddings = model.encode(
        [c["document"] for c in chunks], normalize_embeddings=True
    ).tolist()

    # 重建集合，保证多次运行结果一致而不是不断追加
    if CHROMA_PATH.exists():
        shutil.rmtree(CHROMA_PATH)
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    collection = client.create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )
    collection.add(
        ids=[c["id"] for c in chunks],
        documents=[c["document"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
        embeddings=embeddings,
    )


def search(
    queries: list[str], top_k: int = TOP_K, dedupe_by_entry: bool = True
) -> list[dict[str, Any]]:
    """检索。

    dedupe_by_entry 默认开启：同一知识条目只保留得分最高的那个 section。
    不去重时，一条条目的 4 个 section 会包揽 top-3，把其它相关条目挤出去——
    实测 "purchase order approval threshold" 的 top-3 全是 KB-P2P-002，
    而判定真正需要的 KB-XC-003（阈值属于配置内容）根本进不来。
    """
    import chromadb
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBEDDING_MODEL)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    collection = client.get_collection(name=COLLECTION_NAME)

    fetch = top_k * OVERFETCH if dedupe_by_entry else top_k
    vectors = model.encode(queries, normalize_embeddings=True).tolist()
    raw = collection.query(query_embeddings=vectors, n_results=fetch)

    results = []
    for position, query in enumerate(queries):
        hits: list[dict[str, Any]] = []
        seen_entries: set[str] = set()
        for rank in range(len(raw["ids"][position])):
            metadata = raw["metadatas"][position][rank]
            if dedupe_by_entry and metadata["entry_id"] in seen_entries:
                continue
            seen_entries.add(metadata["entry_id"])
            hits.append({
                "chunk_id": raw["ids"][position][rank],
                "entry_id": metadata["entry_id"],
                "domain": metadata["domain"],
                "section": metadata["section"],
                "title": metadata["title"],
                # Chroma 的 cosine space 返回距离，转成相似度更直观
                "similarity": round(1.0 - raw["distances"][position][rank], 4),
            })
            if len(hits) == top_k:
                break
        results.append({"query": query, "hits": hits})
    return results


def write_manifest(entries: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    by_domain: dict[str, int] = {}
    for entry in entries:
        by_domain[entry["domain"]] = by_domain.get(entry["domain"], 0) + 1

    manifest = attach_run_info({
        "_meta": {
            "source": str(KNOWLEDGE_PATH.relative_to(PROJECT_ROOT)),
            "chroma_path": str(CHROMA_PATH.relative_to(PROJECT_ROOT)),
            "chroma_note_zh": "向量库是派生物，不进 git（见 .gitignore）；知识条目源文件进 git。",
            "embedding_model": EMBEDDING_MODEL,
            "collection": COLLECTION_NAME,
            "entry_count": len(entries),
            "chunk_count": len(chunks),
            "entries_by_domain": dict(sorted(by_domain.items())),
            "sections": [section for section, _, _ in SECTIONS],
            "authorship": dict(sorted(
                (a, sum(1 for e in entries if e["authorship"] == a))
                for a in {e["authorship"] for e in entries}
            )),
        },
        "chunk_ids": [c["id"] for c in chunks],
    })
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="额外的检索测试 query，可重复")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--no-rebuild", action="store_true", help="跳过建库，只检索")
    parser.add_argument(
        "--no-dedupe", action="store_true", help="关闭按条目去重（演示 section 挤占名次）"
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    entries = load_entries()
    chunks = build_chunks(entries)

    if not args.no_rebuild:
        index(chunks)
        manifest = write_manifest(entries, chunks)
        meta = manifest["_meta"]
        print(f"Entries : {meta['entry_count']}  ({meta['entries_by_domain']})")
        print(f"Chunks  : {meta['chunk_count']}  (= entries x {len(SECTIONS)} sections)")
        print(f"Authors : {meta['authorship']}")
        print(f"Content : sha256 {manifest['_run_info']['content_sha256'][:16]}")
        print(f"Chroma  : {CHROMA_PATH}\n")

    queries = list(SMOKE_QUERIES) + (args.query or [])
    for result in search(queries, args.top_k, dedupe_by_entry=not args.no_dedupe):
        print(f"Q: {result['query']}")
        for rank, hit in enumerate(result["hits"], 1):
            print(
                f"   {rank}. {hit['similarity']:.3f}  {hit['entry_id']:<10} "
                f"{hit['section']:<15} [{hit['domain']}]  {hit['title'][:52]}"
            )
        print()


if __name__ == "__main__":
    main()
