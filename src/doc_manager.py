import hashlib
import uuid
from pathlib import Path
from typing import List, Optional, Tuple
from psycopg2.extras import Json
from pg_store import pg_store
from qdrant_store import qdrant_store
from docling_parser import doc_parser
from markdown_preprocessor import process_markdown_to_chunks, ChunkItem
# from embedding_service import embedding_service
from embedding_client import embedding_client


def calc_file_md5(file_path: str) -> str:
    """计算文件md5，幂等判断"""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class DocManager:
    def __init__(self):
        self.pg = pg_store
        self.qdrant = qdrant_store

    def is_file_exist(self, file_md5: str) -> Optional[dict]:
        """根据md5判断文档是否已经入库，存在返回doc信息"""
        sql = "SELECT * FROM rag_doc WHERE file_md5 = %s LIMIT 1;"
        return self.pg.query_one(sql, (file_md5,))

    def add_document(self, file_path: str, file_name: str, page_range: Optional[Tuple[int, int]] = None) -> dict:
        """
        新增文档入口：幂等，存在则跳过
        return: {doc_id, status, chunks, msg}
        """
        file_md5 = calc_file_md5(file_path)
        doc_name = file_name

        # 幂等校验
        exist_doc = self.is_file_exist(file_md5)
        if exist_doc:
            return {
                "doc_id": exist_doc["doc_id"],
                "status": "skipped",
                "chunks": 0,
                "msg": f"文件 {doc_name} 已存在，md5:{file_md5}"
            }

        # 生成文档唯一ID
        doc_id = str(uuid.uuid4())

        try:
            doc, json_out, tables = doc_parser(file_path, page_range=page_range)
            md_text = doc.export_to_markdown()
            page_count = len(doc.pages)

            # ✅ 补齐全部入参
            chunks: List[ChunkItem] = process_markdown_to_chunks(
                md_text=md_text,
                page_no=None,
                doc_id=doc_id,
                doc_name=doc_name,
                doc_type="pdf",
                source_path=str(file_path)
            )

            if chunks:
                # embeddings = embedding_service.encode([ck.content_md for ck in chunks])
                text_list = [ck.content_md for ck in chunks]
                embeddings = embedding_client.embed_texts(text_list)
                self.pg.batch_insert(chunks)
                self.qdrant.batch_insert(chunks, embeddings)

            sql_insert_doc = """
            INSERT INTO rag_doc(doc_id, doc_name, file_path, file_md5, page_count, meta, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            self.pg.execute(sql_insert_doc, (
                doc_id, doc_name, file_path, file_md5, page_count,
                Json({"table_count": len(tables)}), "success"
            ))

            return {
                "doc_id": doc_id,
                "status": "success",
                "chunks": len(chunks),
                "msg": f"文档 {doc_name} 入库完成"
            }
        except Exception as e:
            sql_insert_doc = """
            INSERT INTO rag_doc(doc_id, doc_name, file_path, file_md5, page_count, meta, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(doc_id) DO UPDATE SET status='failed'
            """
            # ✅ 这里也包 Json
            self.pg.execute(sql_insert_doc, (
                doc_id, doc_name, file_path, file_md5, 0, Json({"error": str(e)}), "failed"
            ))
            raise e

    def delete_document(self, doc_id: str):
        """
        删除文档：双删除
        1. PG 删除rag_chunk 该doc下所有chunk
        2. Qdrant 删除该doc_id下所有向量
        3. PG 删除rag_doc记录
        """
        # 1. Qdrant 删除（用filter按doc_id过滤删除，qdrant支持payload过滤删除）
        self.qdrant.delete_by_doc_id(doc_id)
        # 2. PG 删除chunk
        self.pg.delete_by_doc_id(doc_id)
        # 3. PG 删除doc主记录
        sql_del_doc = "DELETE FROM rag_doc WHERE doc_id = %s;"
        self.pg.execute(sql_del_doc, (doc_id,))
        return True

    def update_document(self, file_path: str, file_name: str, page_range: Optional[Tuple[int, int]] = None):
        """更新文档：先删旧版本，再重新add"""
        file_md5 = calc_file_md5(file_path)
        old_doc = self.is_file_exist(file_md5)
        if old_doc:
            self.delete_document(old_doc["doc_id"])
        return self.add_document(file_path, file_name, page_range)

    def list_documents(self, limit: int = 100, offset: int = 0):
        """文档列表查询"""
        sql = "SELECT * FROM rag_doc ORDER BY updated_at DESC LIMIT %s OFFSET %s;"
        return self.pg.query_all(sql, (limit, offset))


doc_manager = DocManager()