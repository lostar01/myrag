import psycopg2
from psycopg2.extras import Json
from settings import settings

class CeleryTaskStore:
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

    def update_task(self, task_id: str, status: str, doc_id=None, error_msg=None, progress=0):
        sql = """
        INSERT INTO rag_task(task_id, doc_id, status, error_msg, progress, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT(task_id) DO UPDATE SET
            status = EXCLUDED.status,
            doc_id = EXCLUDED.doc_id,
            error_msg = EXCLUDED.error_msg,
            progress = EXCLUDED.progress,
            updated_at = NOW();
        """
        with self.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (task_id, doc_id, status, error_msg, progress))
            conn.commit()

    def get_task(self, task_id: str):
        sql = "SELECT * FROM rag_task WHERE task_id = %s"
        with self.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (task_id,))
            return cur.fetchone()

task_store = CeleryTaskStore()
