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

    @staticmethod
    def _is_healthy(conn) -> bool:
        """连接是否可用：closed == 0 表示正常；1 = 已关闭，2 = 状态异常（被服务器断开等）"""
        return getattr(conn, "closed", 1) == 0

    def _borrow(self):
        """借出一个健康连接。

        PG / 容器重启后，池里可能残留已断的连接，拿出来就用就会报错：
        先探一次状态，坏了就 putconn(close=True) 丢弃再借一次，
        避免“报错一直持续到进程重启”。
        重试只发生在“还没执行任何 SQL”的取连接阶段，不会重复写。
        """
        conn = self._pool.getconn()
        if self._is_healthy(conn):
            return conn
        try:
            self._pool.putconn(conn, close=True)
        except Exception as e:
            print(f"[pg_store] 丢弃坏连接失败：{e}")
        conn = self._pool.getconn()
        if not self._is_healthy(conn):
            # 把第二个坏连接也归还（关闭），否则它会永久卡在“已借出”，池会越用越小
            try:
                self._pool.putconn(conn, close=True)
            except Exception:
                pass
            raise psycopg2.OperationalError(
                "数据库连接池里没有可用连接（数据库可能未启动或已关闭）"
            )
        return conn

    @contextmanager
    def get_conn(self):
        """从连接池借出连接，用完归还。

        - 正常退出也 commit：读查询同样会开事务，不结束就会把连接以
          `idle in transaction` 状态归还池（阻碍 vacuum、可能长期持锁）。
          psycopg2 在无事务时 commit 是 no-op，所以重复 commit 没有副作用。
        - 异常时回滚；如果错因是连接本身挂了，就把这条连接从池里丢弃，
          下一条请求能拿到新连接（不自动重放 SQL，避免重复写）。
        """
        conn = self._borrow()
        broken = False
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass  # 连接已挂时 rollback 也会报错，不能掩盖原始异常
            broken = getattr(conn, "closed", 1) != 0
            raise
        finally:
            self._pool.putconn(conn, close=broken)

    @contextmanager
    def transaction(self):
        """事务上下文：正常结束提交，异常回滚。用于需要原子性的多步写入

        同样先探连接健康度；连接挂了的异常会把该连接从池里丢弃。
        """
        conn = self._borrow()
        broken = False
        try:
            with conn:  # with conn 会在正常退出时 commit，异常时 rollback
                yield conn
        except Exception:
            broken = getattr(conn, "closed", 1) != 0
            raise
        finally:
            self._pool.putconn(conn, close=broken)

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
