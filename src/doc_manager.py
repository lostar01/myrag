import hashlib
import os
import uuid
from pathlib import Path
from typing import List, Optional, Tuple
from psycopg2.extras import Json
from pg_store import pg_store
from qdrant_store import qdrant_store
from docling_parser import doc_parser, export_markdown_with_pages
from markdown_preprocessor import process_markdown_to_chunks, ChunkItem
# from embedding_service import embedding_service
from embedding_client import embedding_client
from upload_paths import resolve_upload_path
from settings import settings


# 超过该时长仍处于 pending / running 的记录视为“卡死”（worker 被强杀、容器重启等）：
# 此时不再阻止重传，也允许管理员删除，避免某文件被永久锁死。
STALE_ACTIVE_SQL = "INTERVAL '30 minutes'"

# 惰性自愈：超过该时长仍无任何进展的 pending / running 任务视为**已死**
# （worker 未启动、崩溃、或 worker 与 API 代码版本不匹配等），
# 列表读取时自动置为 failed，避免界面永远停在“等待解析”。
STALE_HEAL_SQL = "INTERVAL '2 hours'"
STALE_HEAL_MESSAGE = "任务超时未完成（worker 未运行、崩溃或代码版本不匹配），请删除记录后重传"


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

        注意：只有 status='success' 的记录才算“已入库”。
        “同一 md5 只允许一条记录 / 失败保留 / 管理员删除后才能重传”的完整判定
        在 `find_by_md5` + `find_upload_conflict` 里。
        """
        sql = "SELECT * FROM rag_doc WHERE file_md5 = %s AND status = 'success' LIMIT 1;"
        return self.pg.query_one(sql, (file_md5,))

    def find_by_md5(self, file_md5: str) -> Optional[dict]:
        """按 md5 查任意状态的文档记录（success / processing / failed 都算占位）。"""
        sql = "SELECT * FROM rag_doc WHERE file_md5 = %s LIMIT 1;"
        return self.pg.query_one(sql, (file_md5,))

    def get_document(self, doc_id: str) -> Optional[dict]:
        """按 doc_id 查文档记录。"""
        return self.pg.query_one("SELECT * FROM rag_doc WHERE doc_id = %s", (doc_id,))

    def find_upload_conflict(self, file_md5: str) -> Optional[dict]:
        """上传前查重，命中则不允许提交任务。

        md5 只存在 rag_doc（权威表）：上传时会先插一条 status='processing' 的占位行，
        所以“同文件正在解析中”也会被这里的 rag_doc 查询拦住，无需再查 rag_task。

        返回 {doc_id, doc_name, status, uploader}；None 表示无冲突。
        """
        doc = self.pg.query_one(
            "SELECT doc_id, doc_name, status FROM rag_doc WHERE file_md5 = %s LIMIT 1",
            (file_md5,),
        )
        if not doc:
            return None
        uploader = self.pg.query_one(
            "SELECT u.nickname, u.username FROM rag_task t "
            "LEFT JOIN rag_user u ON u.user_id = t.user_id "
            "WHERE t.doc_id = %s AND t.user_id IS NOT NULL "
            "ORDER BY t.created_at DESC LIMIT 1",
            (doc["doc_id"],),
        )
        return {
            "doc_id": doc["doc_id"],
            "doc_name": doc["doc_name"],
            "status": doc["status"],
            "uploader": self._format_uploader(uploader),
        }

    @staticmethod
    def _format_uploader(row: Optional[dict]) -> str:
        """上传人展示名：昵称优先，其次用户名，都没有则为“历史数据”。"""
        if not row:
            return "历史数据"
        return row.get("nickname") or row.get("username") or "历史数据"

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

    # 「上传记录」的唯一数据源：**以 rag_doc 为准**（一篇文档 = 一条记录），
    # 状态 / 进度 / 失败原因 / 大小 / 上传人 等全部通过 doc_id 级联 rag_task 获取。
    # 同一 doc_id 可能有多次任务（重试 / 旧数据），用 LATERAL 只取最新一条，避免行数爆炸。
    _UPLOAD_RECORD_SQL = """
        SELECT d.doc_id                          AS doc_id,
               t.task_id                         AS task_id,
               COALESCE(NULLIF(d.doc_name, ''), NULLIF(t.file_name, '')) AS file_name,
               -- 文档已是终态（success / failed）时以文档为准，
               -- 否则（解析中）取任务状态；这样遗留的卡死任务行不会让已入库文档显成失败
               CASE
                   WHEN d.status IN ('success', 'failed') THEN d.status
                   ELSE COALESCE(t.status, d.status, 'success')
               END                               AS status,
               t.progress                        AS progress,
               t.error_msg                       AS error_msg,
               d.file_md5                        AS file_md5,
               d.file_size                       AS file_size,
               d.page_count                      AS page_count,
               t.user_id                         AS user_id,
               u.username                        AS username,
               u.nickname                        AS nickname,
               COALESCE(t.created_at, d.created_at) AS created_at,
               COALESCE(t.updated_at, d.updated_at) AS updated_at,
               (
                   COALESCE(t.status, d.status) IN ('pending', 'running', 'processing')
                   AND COALESCE(t.updated_at, d.updated_at) > NOW() - {stale}
               ) AS is_active
        FROM rag_doc d
        LEFT JOIN LATERAL (
            SELECT * FROM rag_task t2
            WHERE t2.doc_id = d.doc_id
            ORDER BY t2.created_at DESC
            LIMIT 1
        ) t ON TRUE
        LEFT JOIN rag_user u ON u.user_id = t.user_id
    """.format(stale=STALE_ACTIVE_SQL)

    def list_upload_records(self, user_id: Optional[str] = None, limit: int = 100, offset: int = 0):
        """「上传记录」列表：一条 rag_doc = 一条记录，其余信息由 doc_id 级联 rag_task。

        - 文件名 / md5 / 页数 来自 rag_doc（权威数据源）；
        - 状态 / 进度 / 失败原因 / 文件大小 / 上传人 / 时间 来自 rag_task；
        - user_id 为 None 时返回全部（管理员）；否则只返回该用户自己的记录
          （没有任务行的历史文档 user_id 为 NULL，只会出现在管理员视图）。
        return: (rows, total)
        """
        sub_sql = "SELECT * FROM (" + self._UPLOAD_RECORD_SQL + ") r"
        where_sql = " WHERE (%s::text IS NULL OR r.user_id = %s)"
        rows = self.pg.query_all(
            sub_sql + where_sql + " ORDER BY r.created_at DESC NULLS LAST LIMIT %s OFFSET %s",
            (user_id, user_id, limit, offset),
        )
        total_row = self.pg.query_one(
            "SELECT COUNT(*) AS cnt FROM (" + self._UPLOAD_RECORD_SQL + ") r" + where_sql,
            (user_id, user_id),
        )
        return rows, (total_row["cnt"] if total_row else 0)

    def get_upload_record(self, doc_id: str) -> Optional[dict]:
        """单条上传记录（删除接口用）：rag_doc 为准 + doc_id 级联 rag_task。"""
        return self.pg.query_one(
            "SELECT * FROM (" + self._UPLOAD_RECORD_SQL + ") r WHERE r.doc_id = %s",
            (doc_id,),
        )

    def heal_stale_tasks(self) -> int:
        """惰性自愈：把超过 2 小时仍停在 pending / running 的任务及其文档标为 failed。

        - 任务行：status 置 failed，error_msg 补充原因（已有错误信息不覆盖）；
        - 文档行：仅当仍为 processing 且没有任何存活任务时才置 failed；
        - 无任务行、停在 processing 超时的文档同样兼底。
        返回被自愈的任务行数。

        注意：写操作统一走 pg_store.execute（显式提交）；pg_store.query_* 是读接口，
        虽然连接层现在会在归还前结束事务（不再留 idle in transaction），
        但不要拿 query_* 去跑 UPDATE —— 写语义应当只看 execute。
        """
        targets = self.pg.query_all(
            "SELECT task_id, doc_id FROM rag_task "
            "WHERE status IN ('pending', 'running') "
            "  AND updated_at < NOW() - " + STALE_HEAL_SQL
        )
        if not targets:
            return 0

        task_ids = [t["task_id"] for t in targets]
        self.pg.execute(
            "UPDATE rag_task "
            "SET status = 'failed', "
            "    error_msg = COALESCE(NULLIF(error_msg, ''), %s), "
            "    updated_at = NOW() "
            "WHERE task_id = ANY(%s)",
            (STALE_HEAL_MESSAGE, task_ids),
        )

        doc_ids = [t["doc_id"] for t in targets if t.get("doc_id")]
        if doc_ids:
            self.pg.execute(
                "UPDATE rag_doc d SET status = 'failed', updated_at = NOW() "
                "WHERE d.status = 'processing' AND d.doc_id = ANY(%s) "
                "  AND NOT EXISTS (SELECT 1 FROM rag_task t WHERE t.doc_id = d.doc_id "
                "                  AND t.status IN ('pending', 'running'))",
                (doc_ids,),
            )
        # 兼底：没有任务行、停在 processing 超时的文档
        self.pg.execute(
            "UPDATE rag_doc d SET status = 'failed', updated_at = NOW() "
            "WHERE d.status = 'processing' "
            "  AND d.updated_at < NOW() - " + STALE_HEAL_SQL + " "
            "  AND NOT EXISTS (SELECT 1 FROM rag_task t WHERE t.doc_id = d.doc_id "
            "                  AND t.status IN ('pending', 'running'))"
        )
        return len(task_ids)

    def reserve_document(self, doc_id: str, doc_name: str, file_path: str, file_md5: str,
                         file_size: Optional[int] = None):
        """上传时先占位一条 rag_doc（status='processing'）。

        这样「上传记录」以 rag_doc 为唯一数据源时，等待/解析中的记录也能倢刻等到；
        解析成功 / 失败后由 add_document 原地更新同一条记录。
        md5 与文件大小都是文档属性，只存在 rag_doc。
        """
        self.pg.execute(
            "INSERT INTO rag_doc(doc_id, doc_name, file_path, file_md5, file_size, page_count, meta, status) "
            "VALUES (%s, %s, %s, %s, %s, NULL, %s, 'processing')",
            (doc_id, doc_name, file_path, file_md5, file_size, Json({"stage": "queued"})),
        )

    def mark_document_failed(self, doc_id: str, error: str):
        """把占位记录改为 failed（如任务派发失败 / 重试耗尽）。"""
        self.pg.execute(
            "UPDATE rag_doc SET status = 'failed', page_count = 0, meta = %s, updated_at = NOW() "
            "WHERE doc_id = %s",
            (Json({"error": error[:500]}), doc_id),
        )

    def add_document(self, file_path: str, file_name: str, page_range: Optional[Tuple[int, int]] = None,
                     doc_id: Optional[str] = None) -> dict:
        """
        入库入口：同一文件（md5）只保留一条 rag_doc 记录。

        - doc_id 由上传接口预留（rag_doc 已有 status='processing' 占位行），直接复用；
        - 已存在 success 记录：返回 skipped（上传接口已拦截，这里是安全网）；
        - 占位/失败记录：复用该 doc_id 重新解析，成功后原地 UPDATE 为 success；
        - 没有记录（旧调用方）：按 md5 兜底 / 新建 doc_id。

        return: {doc_id, status, chunks, msg}
        """
        # 路径兜底解析（P0 #2）：新上传落库的是绝对路径；库里老数据可还是
        # ./upload_cache/xxx 这种相对路径，这里按同一规则解析成绝对路径，
        # 后面的 md5 / getsize / doc_parser 全部用解析后的路径，
        # 与 worker 的 CWD 无关。
        resolved = resolve_upload_path(file_path)
        if resolved is None or not resolved.exists():
            raise FileNotFoundError(
                f"待解析文件不存在：{file_path}（解析后：{resolved}，"
                "可能是上传目录配置变了或源文件已被清理）"
            )
        file_path = str(resolved)

        file_md5 = calc_file_md5(file_path)
        doc_name = file_name
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            file_size = None

        exist_doc = self.get_document(doc_id) if doc_id else None
        if not exist_doc:
            exist_doc = self.find_by_md5(file_md5)
        if exist_doc and exist_doc.get("status") == "success":
            return {
                "doc_id": exist_doc["doc_id"],
                "status": "skipped",
                "chunks": 0,
                "msg": f"文件 {doc_name} 已存在，md5:{file_md5}"
            }

        # 复用失败记录的 doc_id（Celery 重试），并清理上一次可能残留的 chunk / 向量；
        # 失败记录本身保留，不删除。
        if exist_doc:
            doc_id = exist_doc["doc_id"]
            self._cleanup_doc_data(doc_id)
        else:
            doc_id = str(uuid.uuid4())

        try:
            doc, json_out, tables = doc_parser(file_path, page_range=page_range)
            page_count = len(doc.pages)

            # 页码链路（P0 #4）：默认关闭（docling_parser.enable_page_no: false）。
            # 关掉时走原来的 doc.export_to_markdown()，行为与修复前逐字一致；
            # 开启时带页边界占位符，产出与行号对齐的 page_of_line。
            if settings.docling_parser.enable_page_no:
                md_text, page_of_line = export_markdown_with_pages(doc)
            else:
                md_text = doc.export_to_markdown()
                page_of_line = []

            # ✅ 补齐全部入参
            chunks: List[ChunkItem] = process_markdown_to_chunks(
                md_text=md_text,
                page_no=None,
                doc_id=doc_id,
                doc_name=doc_name,
                doc_type="pdf",
                source_path=str(file_path),
                # D7-a：首标题前的正文挂到以文档名命名的隐式 section 上
                preamble_title=doc_name,
                # #4：页号（enable_page_no=false 时为空列表 ⇒ 不写页码）
                page_of_line=page_of_line,
            )

            # 先算 embedding（外部调用，放在事务外，避免长事务占用连接）
            embeddings = []
            if chunks:
                text_list = [ck.content_md for ck in chunks]
                embeddings = embedding_client.embed_texts(text_list)

            sql_insert_doc = """
            INSERT INTO rag_doc(doc_id, doc_name, file_path, file_md5, file_size, page_count, meta, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(doc_id) DO UPDATE SET
                doc_name   = EXCLUDED.doc_name,
                file_path  = EXCLUDED.file_path,
                file_md5   = EXCLUDED.file_md5,
                file_size  = COALESCE(EXCLUDED.file_size, rag_doc.file_size),
                page_count = EXCLUDED.page_count,
                meta       = EXCLUDED.meta,
                status     = EXCLUDED.status,
                updated_at = NOW()
            """

            # PG 写入放在同一事务：doc + chunk 原子提交，任一失败整体回滚。
            # 顺序必须是 doc 在前：rag_chunk.doc_id 有外键指向 rag_doc（fk_rag_chunk_doc），
            # 没有占位行的场景下先插 chunk 会直接违反外键。
            with self.pg.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql_insert_doc, (
                        doc_id, doc_name, file_path, file_md5, file_size, page_count,
                        Json({"table_count": len(tables)}), "success"
                    ))
                if chunks:
                    self.pg.batch_insert(chunks, conn=conn)

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
            # 向量与 PG chunk 一并清理，再写一条 failed 记录。
            # 该 failed 记录会一直保留在「上传记录」里，必须由管理员删除后才能重传。
            self._cleanup_doc_data(doc_id)
            try:
                sql_insert_doc = """
                INSERT INTO rag_doc(doc_id, doc_name, file_path, file_md5, page_count, meta, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(doc_id) DO UPDATE SET
                    doc_name   = EXCLUDED.doc_name,
                    file_path  = EXCLUDED.file_path,
                    file_md5   = EXCLUDED.file_md5,
                    status     = 'failed',
                    page_count = 0,
                    meta       = EXCLUDED.meta,
                    updated_at = NOW()
                """
                self.pg.execute(sql_insert_doc, (
                    doc_id, doc_name, file_path, file_md5, 0, Json({"error": str(e)}), "failed"
                ))
            except Exception as log_err:
                # 失败记录写不进去不应吞掉原始异常
                print(f"[doc_manager] 写入失败状态出错 doc_id={doc_id}: {log_err}")
            # 把 doc_id 挂到异常上，worker 才能写回 rag_task.doc_id，
            # 这样失败记录在「上传记录」里也能通过 doc_id 级联到 rag_doc 取文件名
            try:
                e.doc_id = doc_id
            except Exception:
                pass
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
        """全库文档列表（原始 rag_doc行）。

        ⚠︎ 这个查询**没有归属人过滤**，不要直接接副HTTP接口：
        对外接口一律用 list_upload_records(user_id=...)（P0 #5 修的正是这个）。
        目前只给脚本 / 运维排查用。
        """
        sql = "SELECT * FROM rag_doc ORDER BY updated_at DESC LIMIT %s OFFSET %s;"
        return self.pg.query_all(sql, (limit, offset))


doc_manager = DocManager()
