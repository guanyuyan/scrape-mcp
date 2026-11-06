"""场景 6：把 dataset JSONL 写进向量库，验证「抓取 → 检索」链路。

默认按 ~900 字符切块（重叠 120），一个块一条向量；metadata 保留 url / title /
tool / ts / status，检索命中后可以直接回链原文。同一记录块用 ``url + ts + 序号``
做稳定 ID，重复导入不会产生重复点。

用法：
    python examples/06_vector_store.py data.jsonl --dry-run          # 只看统计，不连后端
    python examples/06_vector_store.py data/ --backend chroma --persist-dir .chroma
    python examples/06_vector_store.py data/ --backend qdrant --qdrant-url http://localhost:6333

依赖（示例不随包安装）：
    pip install "mcp-scrape[vector]"   # chromadb + qdrant-client
    pip install sentence-transformers   # 想要真实语义向量时加 --embedder st
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CHUNK_CHARS = 900
CHUNK_OVERLAP = 120
HASH_DIM = 256
ST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def expand_paths(raw: list[str]) -> list[Path]:
    """把文件/目录参数展开成 JSONL 列表；目录取其中所有 ``*.jsonl``。"""
    paths: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.glob("*.jsonl")))
        elif path.exists():
            paths.append(path)
        else:
            raise SystemExit(f"找不到输入：{path}")
    if not paths:
        raise SystemExit("没有可用的 .jsonl 文件")
    return paths


def iter_records(paths: list[Path]) -> Iterator[tuple[Path, dict[str, Any]]]:
    """逐行读记录，附带来源文件；损坏行只警告，不中断整批。"""
    for path in paths:
        with path.open(encoding="utf-8-sig") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"[警告] {path.name}:{line_no} JSON 损坏，跳过（{exc.msg}）",
                        file=sys.stderr,
                    )
                    continue
                if isinstance(record, dict):
                    yield path, record
                else:
                    print(f"[警告] {path.name}:{line_no} 不是对象，跳过", file=sys.stderr)


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按字符切块，尽量在换行处断开；相邻块保留 overlap 字符上下文。"""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def build_points(record: dict[str, Any], source: str = "") -> list[dict[str, Any]]:
    """把一条 dataset 记录切成若干待写入的点；无正文的记录跳过。"""
    content = record.get("content")
    if not isinstance(content, str) or not content.strip():
        return []
    ts = record.get("ts") or ""
    key = hashlib.sha1(f"{record.get('url')}|{ts}".encode()).hexdigest()[:16]
    metadata = {
        "url": record.get("url") or "",
        "final_url": record.get("final_url") or "",
        "title": record.get("title") or "",
        "tool": record.get("tool") or "",
        "ts": ts,
        "status": record.get("status") or 0,
        "tokens": record.get("tokens") or 0,
        "source": source,
    }
    if isinstance(record.get("data"), (dict, list)):
        metadata["data"] = json.dumps(record["data"], ensure_ascii=False)[:2000]
    points = []
    for index, chunk in enumerate(chunk_text(content)):
        points.append(
            {
                "id": f"{key}:{index}",
                "text": chunk,
                "metadata": {**metadata, "chunk": index},
            }
        )
    return points


class HashEmbedder:
    """无依赖的哈希向量：只用于跑通链路，语义质量有限。"""

    dim = HASH_DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in _TOKEN_RE.findall(text.lower()):
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dim
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(value * value for value in vector))
            vectors.append([value / norm for value in vector] if norm else vector)
        return vectors


class SentenceTransformerEmbedder:
    """真实语义向量；首次运行会下载模型权重。"""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)
        self.dim = int(self.model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [vector.tolist() for vector in vectors]


def build_embedder(kind: str, model_name: str) -> Any:
    if kind == "hash":
        return HashEmbedder()
    try:
        return SentenceTransformerEmbedder(model_name)
    except ImportError:
        if kind == "st":
            raise SystemExit(
                "缺少 sentence-transformers：pip install sentence-transformers，"
                "或改用 --embedder hash 跑通链路"
            ) from None
        print(
            "[提示] 未安装 sentence-transformers，退化为 hash 向量（仅供链路验证）；"
            "需要语义检索请装它并加 --embedder st",
            file=sys.stderr,
        )
        return HashEmbedder()


def ingest_chroma(
    points: list[dict[str, Any]], vectors: list[list[float]], args: argparse.Namespace
):
    import chromadb

    client = chromadb.PersistentClient(path=args.persist_dir)
    collection = client.get_or_create_collection(args.collection, metadata={"hnsw:space": "cosine"})
    for start in range(0, len(points), args.batch):
        batch = points[start : start + args.batch]
        collection.upsert(
            ids=[point["id"] for point in batch],
            documents=[point["text"] for point in batch],
            metadatas=[point["metadata"] for point in batch],
            embeddings=vectors[start : start + args.batch],
        )
    return collection


def ingest_qdrant(
    points: list[dict[str, Any]], vectors: list[list[float]], args: argparse.Namespace
):
    from qdrant_client import QdrantClient, models

    client = QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key or None)
    if not client.collection_exists(args.collection):
        client.create_collection(
            args.collection,
            vectors_config=models.VectorParams(
                size=len(vectors[0]), distance=models.Distance.COSINE
            ),
        )
    for start in range(0, len(points), args.batch):
        batch = points[start : start + args.batch]
        client.upsert(
            collection_name=args.collection,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, point["id"])),
                    vector=vector,
                    payload={**point["metadata"], "text": point["text"], "id": point["id"]},
                )
                for point, vector in zip(batch, vectors[start : start + args.batch], strict=True)
            ],
        )
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description="把 dataset JSONL 写入 Chroma / Qdrant")
    parser.add_argument("paths", nargs="+", help=".jsonl 文件或包含 .jsonl 的目录")
    parser.add_argument("--backend", choices=["chroma", "qdrant"], default="chroma")
    parser.add_argument("--collection", default="scrape_docs")
    parser.add_argument("--persist-dir", default=".chroma", help="Chroma 持久化目录")
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--qdrant-api-key", default="", help="也可用环境变量 QDRANT_API_KEY")
    parser.add_argument("--embedder", choices=["auto", "hash", "st"], default="auto")
    parser.add_argument("--model", default=ST_MODEL, help="sentence-transformers 模型")
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--limit", type=int, default=0, help="最多读取多少条记录（0 不限）")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计与样块，不连后端")
    args = parser.parse_args()

    started = time.perf_counter()
    paths = expand_paths(args.paths)
    points: list[dict[str, Any]] = []
    records = 0
    empty = 0
    for path, record in iter_records(paths):
        records += 1
        built = build_points(record, source=path.name)
        if not built:
            empty += 1
        points.extend(built)
        if args.limit and records >= args.limit:
            break
    if not points:
        print("没有可写入的正文（检查 content 字段或 --limit）", file=sys.stderr)
        return 1

    pages = len({point["id"].split(":")[0] for point in points})
    print(f"输入     : {len(paths)} 个文件，{records} 条记录（跳过无正文 {empty} 条）")
    print(
        f"切块     : {len(points)} 个（页面 {pages} 个，"
        f"块均 {sum(len(point['text']) for point in points) // len(points)} 字符）"
    )

    if args.dry_run:
        embedder = build_embedder(args.embedder, args.model)
        sample = embedder.encode([points[0]["text"]])[0]
        print(f"向量     : dim={len(sample)}（--embedder {args.embedder}）")
        print(f"样块 ID  : {points[0]['id']}")
        print(f"样块     : {points[0]['text'][:120].replace(chr(10), ' ')}…")
        print(f"[预览] 未连接 {args.backend}，耗时 {time.perf_counter() - started:.1f}s")
        return 0

    embedder = build_embedder(args.embedder, args.model)
    vectors: list[list[float]] = []
    for start in range(0, len(points), args.batch):
        batch = [point["text"] for point in points[start : start + args.batch]]
        vectors.extend(embedder.encode(batch))
        print(f"\r向量化 {min(start + args.batch, len(points))}/{len(points)}", end="", flush=True)
    print()

    try:
        if args.backend == "chroma":
            ingest_chroma(points, vectors, args)
            target = f"chroma://{args.persist_dir}/{args.collection}"
        else:
            ingest_qdrant(points, vectors, args)
            target = f"{args.qdrant_url}/{args.collection}"
    except ImportError as exc:
        raise SystemExit(
            f'缺少 {args.backend} 客户端：pip install "mcp-scrape[vector]"（{exc}）'
        ) from None
    print(
        f"[完成] {len(points)} 个向量 → {target}"
        f"（dim={len(vectors[0])}，耗时 {time.perf_counter() - started:.1f}s）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
