from typing import List, Dict
# from embedding_service import embedding_service
from embedding_client import embedding_client
from qdrant_store import qdrant_store
from pg_store import pg_store
import logging

logger = logging.getLogger(__name__)

def retrieve(query: str, top_k: int = 5) -> List[Dict]:
    query_vec = embedding_client.embed_text(query)
    resp = qdrant_store.client.query_points(
        collection_name=qdrant_store.collection_name,
        query=query_vec,
        limit=top_k,
        with_payload=True
    )
    # 批量拉取完整内容，避免 N+1 查询
    chunk_ids = [p.payload["chunk_id"] for p in resp.points]
    chunk_map = pg_store.get_chunks_by_ids(chunk_ids)

    out = []
    for point in resp.points:
        chunk_id = point.payload["chunk_id"]
        chunk_full = chunk_map.get(chunk_id)
        if not chunk_full:
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
