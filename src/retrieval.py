"""向量粗召回。

两种模式：
  1. `group_by_doc=True`（默认）：按**文档分组**召回（Qdrant `query_points_groups`）。
     动机（实测）：普通召回 `limit=40` 时 40 个点只来自 **3 篇文档** —— 文档级坍缩在召回阶段就发生了，
     rerank 之后再怎么排都救不回来（候选池里根本没有第 4 篇文档）。
     分组召回用同样的耗时（0.04~0.09s）拿到 12~16 篇不同文档的各 1~2 条，从源头保证多源覆盖。
  2. `group_by_doc=False`：旧的平铺召回（保留给对照实验 / 回退）。

阈值语义不变：低于 `cosine_min_score` 的点由 Qdrant 直接不返回 ⇒ 上层拿到 [] 就回答"未找到"。
"""

import logging
from typing import Dict, List, Optional

from embedding_client import embedding_client
from pg_store import pg_store
from qdrant_store import qdrant_store
from settings import settings

logger = logging.getLogger(__name__)


def _point_to_candidate(point, group_score: Optional[float] = None,
                        group_rank: Optional[int] = None, with_vector: bool = False) -> Dict:
    """Qdrant 点 → 上层统一的候选结构。

    `doc_id` / `doc_name` / `token_len` 都在 payload 里（入库时 `extra_meta` 全量写入），
    所以分组、来源标注、预算估算都不需要额外查库。
    """
    payload = point.payload or {}
    item = {
        "score": point.score,                      # 向量余弦相似度
        "chunk_id": payload.get("chunk_id") or str(point.id),
        "doc_id": payload.get("doc_id"),
        "doc_name": payload.get("doc_name"),
        "section_path": payload.get("section_path"),
        "title": payload.get("title"),
        "page_no": payload.get("page_no"),
        "is_table": payload.get("is_table"),
        "token_len": payload.get("token_len"),
        "content": None,                           # 由 PG 回填（Qdrant 不存正文）
    }
    if group_score is not None:
        item["group_score"] = group_score          # 该文档组内最高分
    if group_rank is not None:
        item["group_rank"] = group_rank            # 文档组排名（1 = 最相关的那篇文档）
    if with_vector:
        item["vector"] = point.vector              # 供覆盖贪心算 chunk 间相似度，无需重新 embedding
    return item


def _fill_content(items: List[Dict]) -> List[Dict]:
    """批量从 PG 回填正文（一次 `= ANY(%s)`，避免 N+1），并剔除孤儿向量。"""
    if not items:
        return []
    chunk_ids = [it["chunk_id"] for it in items]
    chunk_map = pg_store.get_chunks_by_ids(chunk_ids)
    out: List[Dict] = []
    for it in items:
        full = chunk_map.get(it["chunk_id"])
        if not full:
            logger.warning("chunk %s 在 Qdrant 有向量但 PG 没有记录，已跳过", it["chunk_id"])
            continue
        it["content"] = full["content_md"]
        # 以 PG 为准（payload 可能是旧数据/字段缺失）
        it["doc_id"] = it.get("doc_id") or full["doc_id"]
        it["section_path"] = it.get("section_path") or full["section_path"]
        it["is_table"] = full["is_table"] if it.get("is_table") is None else it["is_table"]
        out.append(it)
    return out


def retrieve(
    query: str,
    top_k: Optional[int] = None,
    score_threshold: Optional[float] = None,
    group_by_doc: Optional[bool] = None,
    groups: Optional[int] = None,
    group_size: Optional[int] = None,
    with_vectors: Optional[bool] = None,
) -> List[Dict]:
    """向量召回。返回候选列表（字段见 `_point_to_candidate`）。

    - `group_by_doc`：按文档分组召回；不传取 `retrieval.group_by_doc`
    - `groups` / `group_size`：分组模式下的组数与每组条数（不传取配置）
    - `with_vectors`：是否带回 chunk 向量（覆盖贪心用；会多传 ~1024 float/条）
    """
    if score_threshold is None:
        score_threshold = settings.retrieval.cosine_min_score
    threshold = score_threshold if score_threshold and score_threshold > 0 else None
    if group_by_doc is None:
        group_by_doc = bool(settings.retrieval.group_by_doc)
    if with_vectors is None:
        with_vectors = False

    query_vec = embedding_client.embed_text(query)

    if group_by_doc:
        n_groups = int(groups if groups is not None else settings.retrieval.recall_groups)
        n_per = int(group_size if group_size is not None else settings.retrieval.group_size)
        try:
            resp = qdrant_store.client.query_points_groups(
                collection_name=qdrant_store.collection_name,
                query=query_vec,
                group_by="doc_id",
                limit=max(n_groups, 1),
                group_size=max(n_per, 1),
                with_payload=True,
                with_vectors=with_vectors,
                score_threshold=threshold,
            )
        except Exception as e:
            # 服务端不支持 group_by / payload 里没有 doc_id ⇒ 降级为平铺召回，不影响问答
            logger.warning("分组召回失败，降级为普通召回：%s", e)
            group_by_doc = False
        else:
            items: List[Dict] = []
            for rank, group in enumerate(resp.groups, start=1):
                best = max((h.score for h in group.hits), default=None)
                for hit in group.hits:
                    items.append(
                        _point_to_candidate(hit, group_score=best, group_rank=rank,
                                            with_vector=with_vectors)
                    )
            if items:
                return _fill_content(items)
            if threshold is not None:
                logger.info("分组召回为空（可能相似度都低于 cosine_min_score=%s）", threshold)
            return []

    # 平铺召回（旧行为）
    n = int(top_k if top_k is not None else settings.retrieval.recall_top_k)
    resp = qdrant_store.client.query_points(
        collection_name=qdrant_store.collection_name,
        query=query_vec,
        limit=max(n, 1),
        with_payload=True,
        with_vectors=with_vectors,
        score_threshold=threshold,
    )
    if not resp.points:
        if threshold is not None:
            logger.info("本次向量召回为空（可能相似度都低于 cosine_min_score=%s）", threshold)
        return []
    return _fill_content([_point_to_candidate(p, with_vector=with_vectors) for p in resp.points])
