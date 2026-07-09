"""预下载 field_mapping 所需的 embedding 模型，供离线环境准备阶段使用。

一次性联网把 all-MiniLM-L6-v2（约 80MB）拉进本地 HuggingFace 缓存；
之后所有工具都可在完全断网的环境下运行。

用法：
    python scripts/prefetch_model.py            # 下载并缓存
    python scripts/prefetch_model.py --check    # 只检查本地缓存是否可离线加载
"""

from __future__ import annotations

import argparse
import os
import sys

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def check_offline() -> int:
    """在强制离线模式下尝试加载模型，验证缓存是否完整。"""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(MODEL_NAME)
        vector = model.encode(["offline check"], normalize_embeddings=True)
    except Exception as error:  # noqa: BLE001 — 这里就是要把任何失败都当成"缓存不可用"
        print(f"[FAIL] 无法离线加载 {MODEL_NAME}：{type(error).__name__}: {error}")
        print("       请先联网运行一次：python scripts/prefetch_model.py")
        return 1
    print(f"[OK] 已可离线加载 {MODEL_NAME}，embedding 维度 {vector.shape[1]}")
    return 0


def download() -> int:
    from sentence_transformers import SentenceTransformer

    print(f"正在下载 {MODEL_NAME} …（约 80MB，仅首次需要）")
    model = SentenceTransformer(MODEL_NAME)
    vector = model.encode(["warmup"], normalize_embeddings=True)
    cache = os.environ.get("HF_HOME") or "~/.cache/huggingface"
    print(f"[OK] 下载完成，embedding 维度 {vector.shape[1]}，缓存目录 {cache}")
    print("此后本项目可在完全断网的环境下运行。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="只检查本地缓存能否离线加载，不下载"
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    return check_offline() if args.check else download()


if __name__ == "__main__":
    raise SystemExit(main())
