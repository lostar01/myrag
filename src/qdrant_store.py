from typing import List, Optional
from qdrant_client import QdrantClient, models
from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
from dataclasses import asdict
from markdown_preprocessor import ChunkItem
from settings import settings

class QdrantVectorStore:
    def __init__(self):
        self.client = QdrantClient(
            host=settings.qdrant.host,
            port=settings.qdrant.port,
            https=settings.qdrant.https,
            api_key=settings.qdrant.api_key,
            timeout=10
        )
        self.collection_name = settings.qdrant.collection_name
        self.vector_size = 1024  # bge-m3 fixed dim
        self.distance = models.Distance.COSINE

        # 集合不存在自动创建
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.vector_size,
                    distance=self.distance
                ),
                shard_number=settings.qdrant.collection_shard_number,
                replication_factor=settings.qdrant.collection_replication_factor,
                write_consistency_factor=settings.qdrant.collection_write_consistency_factor,
            )

    def insert_chunk(self, chunk: ChunkItem, vector: List[float]):
        """单条写入"""
        payload = {
            "chunk_id": chunk.chunk_id,
            "section_path": chunk.section_path,
            "title": chunk.title,
            "title_num": chunk.title_num,
            "level": chunk.level,
            "page_no": chunk.page_no,
            "is_table": chunk.is_table,
            "token_len": chunk.token_len,
            **(chunk.extra_meta or {})
        }
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                models.PointStruct(
                    id=chunk.chunk_id,
                    vector=vector,
                    payload=payload
                )
            ]
        )

    def batch_insert(self, chunks: List[ChunkItem], vectors: List[List[float]]):
        """批量写入，推荐导入用，速度快很多"""
        points = []
        for ck, vec in zip(chunks, vectors):
            payload = {
                "chunk_id": ck.chunk_id,
                "section_path": ck.section_path,
                "title": ck.title,
                "title_num": ck.title_num,
                "level": ck.level,
                "page_no": ck.page_no,
                "is_table": ck.is_table,
                "token_len": ck.token_len,
                **(ck.extra_meta or {})
            }
            points.append(models.PointStruct(
                id=ck.chunk_id,
                vector=vec,
                payload=payload
            ))
        self.client.upsert(
            collection_name=self.collection_name,
            points=points
        )

    def delete_by_doc_id(self, doc_id: str):
        """按doc_id批量删除向量，payload过滤"""
        filter_obj = Filter(
            must=[
                FieldCondition(
                    key="doc_id",
                    match=MatchValue(value=doc_id)
                )
            ]
        )
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=FilterSelector(filter=filter_obj)
        )


# 全局单例
qdrant_store = QdrantVectorStore()
