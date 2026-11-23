import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from typing import List, Optional, Dict, Any
from psycopg2.extras import Json, RealDictCursor
from markdown_preprocessor import ChunkItem
from settings import settings


class PGChunkStore:
    def __init__(self):
        self.conn_info = settings.pg
        # 连接池：minconn=1, maxconn=10，避免每次操作都新建连接
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=self.conn_info.host,
            port=self.conn_info.port,
            user=self.conn_info.user,
            password=self.conn_info.password,
            dbname=self.conn_info.dbname,
        )

    @contextmanager
    def get_conn(self):
        """从连接池借出连接，用完归还；异常时回滚并归还"""
        conn = self._pool.getconn()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    @contextmanager
    def transaction(self):
        """事务上下文：正常结束提交，异常回滚。用于需要原子性的多步写入"""
        conn = self._pool.getconn()
        try:
            with conn:  # with conn 会在正常退出时 commit，异常时 rollback
                yield conn
        finally:
            self._pool.putconn(conn)

    # ========== 新增通用方法（给 doc_manager 使用） ==========
    def execute(self, sql: str, params: tuple = ()):
        """执行增删改，无返回（insert/delete/update）"""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()

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
            conn.commit()

    def batch_insert(self, chunks: List[ChunkItem], conn=None):
        """批量插入：单连接 + executemany，避免逐条建连，性能更优。
        若传入 conn，则复用外部事务连接（不自行提交/关闭）。"""
        if not chunks:
            return
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
        rows = []
        for chunk in chunks:
            meta = chunk.extra_meta or {}
            rows.append((
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
        if conn is not None:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
        else:
            with self.get_conn() as c:
                with c.cursor() as cur:
                    cur.executemany(sql, rows)
                c.commit()

    def delete_by_doc_id(self, doc_id: str, conn=None):
        sql = "DELETE FROM rag_chunk WHERE doc_id = %s;"
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(sql, (doc_id,))
        else:
            with self.get_conn() as c:
                with c.cursor() as cur:
                    cur.execute(sql, (doc_id,))
                c.commit()

    def get_chunks_by_ids(self, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量按 chunk_id 查询，返回 {chunk_id: chunk_dict}，避免 N+1 查询"""
        if not chunk_ids:
            return {}
        sql = """
        SELECT chunk_id, doc_id, section_path, title, title_num, level, page_no, is_table, token_len, content_md
        FROM rag_chunk WHERE chunk_id = ANY(%s)
        """
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, (list(chunk_ids),))
                rows = cur.fetchall()
                return {r["chunk_id"]: dict(r) for r in rows}

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
