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

    def create_task(
        self,
        task_id: str,
        file_name: str,
        file_path: str,
        user_id: str = None,
        status: str = "pending",
        doc_id: str = None,
    ):
        """上传时先落一条完整的上传记录（文件名 / 上传人 / doc_id）。

        doc_id 已由上传接口预留（rag_doc 已建 processing 占位行），
        这样列表可按 doc_id 直接级联；md5 / 文件大小属于文档属性，存在 rag_doc。
        """
        sql = """
        INSERT INTO rag_task(
            task_id, doc_id, file_name, file_path, user_id,
            status, progress, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, 0, NOW(), NOW())
        ON CONFLICT(task_id) DO UPDATE SET
            doc_id    = EXCLUDED.doc_id,
            file_name = EXCLUDED.file_name,
            file_path = EXCLUDED.file_path,
            user_id   = EXCLUDED.user_id,
            status    = EXCLUDED.status,
            updated_at = NOW();
        """
        with self.get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (task_id, doc_id, file_name, file_path, user_id, status))
            conn.commit()

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
