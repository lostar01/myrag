import psycopg2
from typing import List, Optional, Dict, Any
from psycopg2.extras import Json, RealDictCursor
from markdown_preprocessor import ChunkItem
from settings import settings

class PGChunkStore:
    def __init__(self):
        self.conn_info = settings.pg

    def get_conn(self):
        return psycopg2.connect(
            host=self.conn_info.host,
            port=self.conn_info.port,
            user=self.conn_info.user,
            password=self.conn_info.password,
            dbname=self.conn_info.dbname
        )

    # ========== 新增通用方法（给 doc_manager 使用） ==========
    def execute(self, sql: str, params: tuple = ()):
        """执行增删改，无返回（insert/delete/update）"""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def query_one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        """查单条，返回字典"""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None

    def query_all(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """查多条，返回字典列表"""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [dict(r) for r in rows]
    # ======================================================

    def insert_chunk(self, chunk: ChunkItem):
        sql = """
        INSERT INTO rag_chunk (
            chunk_id, doc_id, doc_name, doc_type, source_path, import_time,
            section_path, title, title_num, level, page_no, is_table, token_len, content_md, extra_meta
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (chunk_id) DO UPDATE SET
            doc_id = EXCLUDED.doc_id,
            doc_name = EXCLUDED.doc_name,
            section_path = EXCLUDED.section_path,
            content_md = EXCLUDED.content_md,
            extra_meta = EXCLUDED.extra_meta,
            updated_at = CURRENT_TIMESTAMP;
        """
        meta = chunk.extra_meta or {}
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    chunk.chunk_id,
                    meta["doc_id"],
                    meta["doc_name"],
                    meta["doc_type"],
                    meta["source_path"],
                    meta["import_time"],
                    chunk.section_path,
                    chunk.title,
                    chunk.title_num,
                    chunk.level,
                    chunk.page_no,
                    chunk.is_table,
                    chunk.token_len,
                    chunk.content_md,
                    Json(meta)
                ))

    def batch_insert(self, chunks: List[ChunkItem]):
        for c in chunks:
            self.insert_chunk(c)

    def delete_by_doc_id(self, doc_id: str):
        sql = "DELETE FROM rag_chunk WHERE doc_id = %s;"
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (doc_id,))

    def get_chunk_by_id(self, chunk_id: str):
        sql = """
        SELECT chunk_id, doc_id, section_path, title, title_num, level, page_no, is_table, token_len, content_md
        FROM rag_chunk WHERE chunk_id = %s
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (chunk_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "chunk_id": row[0],
                    "doc_id": row[1],
                    "section_path": row[2],
                    "title": row[3],
                    "title_num": row[4],
                    "level": row[5],
                    "page_no": row[6],
                    "is_table": row[7],
                    "token_len": row[8],
                    "content_md": row[9]
                }

pg_store = PGChunkStore()