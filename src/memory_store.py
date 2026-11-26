"""记忆向量索引（长期记忆的语义召回层）。

分工跟文档块链路完全一样：
  - PG 的 `rag_memory` 是**真相**（内容、kind、权重、过期、状态）；
  - Qdrant 集（默认 `rag_memory`）只存向量 + 最小 payload（memory_id / user_id / kind / weight），
    point id 就是 memory_id；召回拿到 id 后回 PG 取内容（`pg_store.get_memories_by_ids`）。

两个刻意的取舍：
  1. **collection 惰性创建**：不在 import 期连 Qdrant（"模块导入即连外部服务"是已记录的 P1 问题，
     新代码不再加重它）。首次用到时才建；连不上就返回 False，调用方降级（问答照常）。
  2. **不改造 `qdrant_store`**：那个类的 payload 写死了文档 chunk 字段，改它会牵连已上线的入库链路；
     这里只复用它的 `client`（同一个连接配置、同一份 api_key/timeout）。
"""

from typing import Dict, List

from qdrant_client import models

from qdrant_store import qdrant_store
from settings import settings

# bge-m3 固定 1024 维、余弦距离，跟文档集一致（同一个 embedding 服务，同一个向量口径）
VECTOR_SIZE = 1024


class MemoryVectorStore:
    def __init__(self):
        self.collection_name = settings.memory.qdrant_collection_name
        self._ready = False

    @property
    def client(self):
        return qdrant_store.client

    def ensure_collection(self) -> bool:
        """首次使用时建集；失败只告警不抛（向量召回是可降级的能力）。"""
        if self._ready:
            return True
        try:
            if not self.client.collection_exists(self.collection_name):
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=VECTOR_SIZE,
                        distance=models.Distance.COSINE,
                    ),
                )
                print(f"[memory_store] 已创建记忆向量集：{self.collection_name}")
            self._ready = True
            return True
        except Exception as e:
            print(f"[memory_store] 记忆向量集不可用（本次跳过向量读写）{self.collection_name}: {e}")
            return False

    def upsert(self, memory_id: str, user_id: str, kind: str, weight: float, vector: List[float]) -> bool:
        """写入/覆盖一条记忆的向量（point id = memory_id ⇒ 重复写就是更新）。"""
        if not self.ensure_collection():
            return False
        try:
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=memory_id,
                        vector=vector,
                        payload={
                            "memory_id": memory_id,
                            "user_id": user_id,
                            "kind": kind,
                            "weight": float(weight),
                        },
                    )
                ],
            )
            return True
        except Exception as e:
            print(f"[memory_store] 记忆向量写入失败 memory_id={memory_id}: {e}")
            return False

    def delete(self, memory_ids: List[str]) -> bool:
        """删除记忆向量（point id 就是 memory_id，不需要按 payload 过滤）。

        PG 侧是软删（status=archived），这里删干净：软删条目不该再被召回。
        """
        if not memory_ids or not self.ensure_collection():
            return False
        try:
            self.client.delete(collection_name=self.collection_name, points_selector=list(memory_ids))
            return True
        except Exception as e:
            print(f"[memory_store] 记忆向量删除失败 ids={len(memory_ids)}: {e}")
            return False

    def search(self, user_id: str, vector: List[float], top_k: int, min_score: float) -> List[Dict]:
        """按用户过滤做语义召回，返回 [{memory_id, score}]（分数降序）。

        记忆条目是短句（≤60 字），余弦分布跟文档块不一样 ⇒ 阈值单独配
        （memory.recall_min_score，默认 0.45），上线后用 rag_qa_log.meta.memory_scores 校准。
        """
        if top_k <= 0 or not self.ensure_collection():
            return []
        try:
            resp = self.client.query_points(
                collection_name=self.collection_name,
                query=vector,
                limit=top_k,
                with_payload=["memory_id", "kind", "weight"],
                score_threshold=min_score if min_score and min_score > 0 else None,
                query_filter=models.Filter(
                    must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))]
                ),
            )
        except Exception as e:
            print(f"[memory_store] 记忆向量召回失败 user_id={user_id}: {e}")
            return []

        out: List[Dict] = []
        for point in resp.points:
            payload = point.payload or {}
            memory_id = payload.get("memory_id") or point.id
            out.append({"memory_id": memory_id, "score": point.score, "kind": payload.get("kind")})
        return out


memory_vectors = MemoryVectorStore()
