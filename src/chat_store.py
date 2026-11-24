import uuid
from typing import List, Optional, Dict
from psycopg2.extras import Json
from pg_store import pg_store


class ChatStore:
    """会话/消息数据访问层，复用 pg_store 的连接池，避免每次操作新建连接。"""

    def create_session(self, user_id: str = "default", title: str = None, meta: dict = None) -> str:
        """新建会话，返回chat_id"""
        chat_id = str(uuid.uuid4())
        sql = """
        INSERT INTO chat_session (chat_id, user_id, title, meta)
        VALUES (%s, %s, %s, %s)
        """
        pg_store.execute(sql, (chat_id, user_id, title, Json(meta or {})))
        return chat_id

    def add_msg(self, chat_id: str, role: str, content: str, cost_tokens: int = 0, meta: dict = None):
        msg_id = str(uuid.uuid4())
        sql = """
        INSERT INTO chat_message (msg_id, chat_id, role, content, cost_tokens, meta)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        # 消息插入 + 会话更新时间放在同一事务，保证原子性
        with pg_store.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (msg_id, chat_id, role, content, cost_tokens, Json(meta or {})))
                cur.execute("UPDATE chat_session SET updated_at=NOW() WHERE chat_id=%s", (chat_id,))

    def list_messages(self, chat_id: str) -> List[Dict]:
        sql = """
        SELECT role, content, cost_tokens, created_at
        FROM chat_message WHERE chat_id=%s ORDER BY created_at ASC
        """
        rows = pg_store.query_all(sql, (chat_id,))
        return [
            {"role": r["role"], "content": r["content"], "cost_tokens": r["cost_tokens"], "created_at": r["created_at"]}
            for r in rows
        ]

    def get_session(self, chat_id: str) -> Optional[Dict]:
        """查询单个会话"""
        sql = """
        SELECT chat_id, user_id, title, created_at, updated_at, meta
        FROM chat_session WHERE chat_id=%s
        """
        return pg_store.query_one(sql, (chat_id,))

    def list_sessions(self, user_id: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict]:
        """查询会话列表，可按 user_id 过滤，按更新时间倒序"""
        base = """
        SELECT chat_id, user_id, title, created_at, updated_at, meta
        FROM chat_session
        """
        params: List = []
        if user_id is not None:
            base += " WHERE user_id=%s"
            params.append(user_id)
        base += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        return pg_store.query_all(base, tuple(params))

    def count_sessions(self, user_id: Optional[str] = None) -> int:
        """统计会话数量"""
        sql = "SELECT COUNT(*) AS cnt FROM chat_session"
        params: List = []
        if user_id is not None:
            sql += " WHERE user_id=%s"
            params.append(user_id)
        row = pg_store.query_one(sql, tuple(params))
        return int(row["cnt"]) if row else 0

    def update_session(self, chat_id: str, title: Optional[str] = None, meta: Optional[dict] = None) -> Optional[Dict]:
        """更新会话标题/元数据"""
        sets = []
        params: List = []
        if title is not None:
            sets.append("title=%s")
            params.append(title)
        if meta is not None:
            sets.append("meta=%s")
            params.append(Json(meta))
        if not sets:
            return self.get_session(chat_id)
        sets.append("updated_at=NOW()")
        params.append(chat_id)
        sql = f"UPDATE chat_session SET {', '.join(sets)} WHERE chat_id=%s"
        pg_store.execute(sql, tuple(params))
        return self.get_session(chat_id)

    def delete_session(self, chat_id: str):
        """级联删除消息"""
        sql = "DELETE FROM chat_session WHERE chat_id=%s"
        pg_store.execute(sql, (chat_id,))


chat_store = ChatStore()
