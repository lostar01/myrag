import uuid
from typing import List, Optional, Dict
import psycopg2
from psycopg2.extras import Json
from settings import settings

class ChatStore:
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

    def create_session(self, user_id: str = "default", title: str = None, meta: dict = None) -> str:
        """新建会话，返回chat_id"""
        chat_id = str(uuid.uuid4())
        sql = """
        INSERT INTO chat_session (chat_id, user_id, title, meta)
        VALUES (%s, %s, %s, %s)
        """
        with self.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (chat_id, user_id, title, Json(meta or {})))
        return chat_id

    def add_msg(self, chat_id: str, role: str, content: str, cost_tokens: int = 0, meta: dict = None):
        msg_id = str(uuid.uuid4())
        sql = """
        INSERT INTO chat_message (msg_id, chat_id, role, content, cost_tokens, meta)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        with self.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (msg_id, chat_id, role, content, cost_tokens, Json(meta or {})))
            # 更新会话更新时间
            cur.execute("UPDATE chat_session SET updated_at=NOW() WHERE chat_id=%s", (chat_id,))

    def list_messages(self, chat_id: str) -> List[Dict]:
        sql = """
        SELECT role, content, cost_tokens, created_at
        FROM chat_message WHERE chat_id=%s ORDER BY created_at ASC
        """
        with self.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (chat_id,))
            rows = cur.fetchall()
        return [{"role": r[0], "content": r[1], "cost_tokens": r[2], "created_at": r[3]} for r in rows]

    def delete_session(self, chat_id: str):
        """级联删除消息"""
        sql = "DELETE FROM chat_session WHERE chat_id=%s"
        with self.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (chat_id,))

chat_store = ChatStore()