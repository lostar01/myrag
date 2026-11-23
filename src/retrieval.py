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
    out = []
    for point in resp.points:
        payload = point.payload
        chunk_id = payload["chunk_id"]
        # 用chunk_id去PG拉完整内容
        chunk_full = pg_store.get_chunk_by_id(chunk_id)
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