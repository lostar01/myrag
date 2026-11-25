from typing import List, Dict, Optional
# from embedding_service import embedding_service
from embedding_client import embedding_client
from qdrant_store import qdrant_store
from pg_store import pg_store
from settings import settings
import logging

logger = logging.getLogger(__name__)


def retrieve(
    query: str,
    top_k: Optional[int] = None,
    score_threshold: Optional[float] = None,
) -> List[Dict]:
    """向量粗召回。

    top_k / score_threshold 不传时取 `config.yaml` 的 `retrieval` 段（P0 #6）。
    阈值 <= 0 视为「不启用过滤」（给 Qdrant 传 None）。
    低于阈值的点由 Qdrant 直接不返回 ⇒ 上层拿到 [] 就会回答"未找到"，
    不再硬塞一堆无关上下文给 LLM。
    """
    if top_k is None:
        top_k = settings.retrieval.recall_top_k
    if score_threshold is None:
        score_threshold = settings.retrieval.cosine_min_score
    threshold = score_threshold if score_threshold and score_threshold > 0 else None

    query_vec = embedding_client.embed_text(query)
    resp = qdrant_store.client.query_points(
        collection_name=qdrant_store.collection_name,
        query=query_vec,
        limit=top_k,
        with_payload=True,
        score_threshold=threshold,
    )
    if not resp.points:
        if threshold is not None:
            logger.info(
                "本次向量召回为空（可能是相似度都低于 cosine_min_score=%s）", threshold
            )
        return []

    # 批量拉取完整内容，避免 N+1 查询
    chunk_ids = [p.payload["chunk_id"] for p in resp.points]
    chunk_map = pg_store.get_chunks_by_ids(chunk_ids)

    out = []
    for point in resp.points:
        chunk_id = point.payload["chunk_id"]
        chunk_full = chunk_map.get(chunk_id)
        if not chunk_full:
            # 向量库里有但 PG 里没有（孤儿向量），跳过而不是报错
            logger.warning("chunk %s 在 Qdrant 有向量但 PG 没有记录，已跳过", chunk_id)
            continue
        out.append({
            "score": point.score,
            "doc_id": chunk_full["doc_id"],
            "chunk_id": chunk_full["chunk_id"],
            "section_path": chunk_full["section_path"],
            "title": chunk_full["title"],
            "page_no": chunk_full["page_no"],
            "is_table": chunk_full["is_table"],
            "content": chunk_full["content_md"]
        })
    return out
