import uuid
from typing import List, Optional
from pg_store import pg_store
from psycopg2.extras import Json

class QALogService:
    def log_qa(
        self,
        query: str,
        answer: str,
        chunk_ids: List[str],
        rerank_scores: List[float],
        doc_ids: List[str],
        cost_ms: int,
        feedback: Optional[int] = None,
        feedback_note: str = "",
        meta: Optional[dict] = None
    ) -> str:
        """
        写入问答日志
        :return: log_id
        """
        log_id = str(uuid.uuid4())
        sql = """
        INSERT INTO rag_qa_log(
            log_id, query, answer, retrieved_chunk_ids, rerank_scores, doc_ids,
            user_feedback, feedback_note, cost_ms, meta
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """
        pg_store.execute(sql, (
            log_id,
            query,
            answer,
            chunk_ids,
            rerank_scores,
            doc_ids,
            feedback,
            feedback_note,
            cost_ms,
            Json(meta or {})
        ))
        return log_id

    def update_feedback(self, log_id: str, user_feedback: int, feedback_note: str = ""):
        """用户提交好评/差评，更新反馈"""
        sql = """
        UPDATE rag_qa_log
        SET user_feedback = %s, feedback_note = %s
        WHERE log_id = %s;
        """
        pg_store.execute(sql, (user_feedback, feedback_note, log_id))

    def list_logs(self, limit: int = 100, offset: int = 0):
        """查询问答日志列表"""
        sql = """
        SELECT * FROM rag_qa_log ORDER BY created_at DESC LIMIT %s OFFSET %s;
        """
        return pg_store.query_all(sql, (limit, offset))

qa_log_service = QALogService()