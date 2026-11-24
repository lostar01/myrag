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
        """根据md5判断文档是否已经**成功**入库，存在返回doc信息。

        注意：只有 status='success' 的记录才参与幂等判断。
        历史失败记录（status='failed'）必须允许重新入库，否则 Celery 重试时
        会命中失败记录直接返回 skipped，导致任务被误标记为 success 而实际无数据。
        """
        sql = "SELECT * FROM rag_doc WHERE file_md5 = %s AND status = 'success' LIMIT 1;"
        return self.pg.query_one(sql, (file_md5,))

    def _cleanup_doc_data(self, doc_id: str):
        """尽力清理某个 doc_id 的向量与 chunk，不抛异常（清理本身不应掩盖原始错误）。"""
        try:
            self.qdrant.delete_by_doc_id(doc_id)
        except Exception as e:
            print(f"[doc_manager] 清理 Qdrant 向量失败 doc_id={doc_id}: {e}")
        try:
            self.pg.delete_by_doc_id(doc_id)
        except Exception as e:
            print(f"[doc_manager] 清理 PG chunk 失败 doc_id={doc_id}: {e}")

    def _purge_failed_attempts(self, file_md5: str):
        """清理同一 md5 的历史失败记录及其残留数据，保证同一文件只保留一条失败记录。"""
        try:
            rows = self.pg.query_all(
                "SELECT doc_id FROM rag_doc WHERE file_md5 = %s AND status = 'failed'",
                (file_md5,),
            )
            for row in rows:
                self._cleanup_doc_data(row["doc_id"])
                self.pg.execute("DELETE FROM rag_doc WHERE doc_id = %s", (row["doc_id"],))
        except Exception as e:
            # 清理失败不应阻断本次重新入库
            print(f"[doc_manager] 清理历史失败记录失败 md5={file_md5}: {e}")

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

        # 重新入库前清理同一文件的失败残留（失败记录 + 可能残留的 chunk/向量），
        # 否则重试会不断堆积 failed 记录与孤儿数据。
        self._purge_failed_attempts(file_md5)

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

            # 先算 embedding（外部调用，放在事务外，避免长事务占用连接）
            embeddings = []
            if chunks:
                text_list = [ck.content_md for ck in chunks]
                embeddings = embedding_client.embed_texts(text_list)

            sql_insert_doc = """
            INSERT INTO rag_doc(doc_id, doc_name, file_path, file_md5, page_count, meta, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """

            # PG 写入放在同一事务：chunk + doc 原子提交，任一失败整体回滚
            with self.pg.transaction() as conn:
                if chunks:
                    self.pg.batch_insert(chunks, conn=conn)
                with conn.cursor() as cur:
                    cur.execute(sql_insert_doc, (
                        doc_id, doc_name, file_path, file_md5, page_count,
                        Json({"table_count": len(tables)}), "success"
                    ))

            # PG 事务提交成功后再写 Qdrant（向量库无事务，放最后降低不一致概率）
            if chunks:
                self.qdrant.batch_insert(chunks, embeddings)

            return {
                "doc_id": doc_id,
                "status": "success",
                "chunks": len(chunks),
                "msg": f"文档 {doc_name} 入库完成"
            }
        except Exception as e:
            # 失败清理：向量库无事务；且“PG 已提交、Qdrant 写入失败”这一分支下
            # chunk 已经落库，只删向量会留下孤儿 chunk。因此这里必须把本次 doc_id 的
            # 向量与 PG chunk 一并清理，再写一条 failed 记录（不参与幂等判断，重试时会被清掉）。
            self._cleanup_doc_data(doc_id)
            try:
                sql_insert_doc = """
                INSERT INTO rag_doc(doc_id, doc_name, file_path, file_md5, page_count, meta, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(doc_id) DO UPDATE SET
                    status    = 'failed',
                    page_count = 0,
                    meta      = EXCLUDED.meta,
                    updated_at = NOW()
                """
                self.pg.execute(sql_insert_doc, (
                    doc_id, doc_name, file_path, file_md5, 0, Json({"error": str(e)}), "failed"
                ))
            except Exception as log_err:
                # 失败记录写不进去不应吞掉原始异常
                print(f"[doc_manager] 写入失败状态出错 doc_id={doc_id}: {log_err}")
            raise

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
