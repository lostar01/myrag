from celery_app import app
from celery_task_store import task_store
from doc_manager import doc_manager
import traceback

# error_msg 会展示在「上传记录」列表里（鼠标悬停可见），
# 所以只存简短的异常描述（截断 500 字），完整堆栈仍打印到 worker 日志。
MAX_ERROR_MSG_LEN = 500


def _short_error(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    return msg[:MAX_ERROR_MSG_LEN]


@app.task(bind=True, name="tasks.import_document_task", retry_backoff=3, max_retries=2)
def import_document_task(self, local_file_path: str, file_name: str, doc_id: str = None):
    task_id = self.request.id
    task_store.update_task(task_id, status="running", doc_id=doc_id, progress=5)
    try:
        res = doc_manager.add_document(
            file_path=local_file_path,
            file_name=file_name,
            doc_id=doc_id,
        )
        doc_id = res["doc_id"] or doc_id
        task_store.update_task(task_id, status="success", doc_id=doc_id, progress=100)
        return res
    except Exception as e:
        # 打印完整堆栈！
        print("===== FULL EXCEPTION STACK =====")
        traceback.print_exc()
        # doc_manager 会把 doc_id 挂在异常上；回写后列表可按 doc_id 级联 rag_doc 取文件名
        task_store.update_task(
            task_id,
            status="failed",
            doc_id=getattr(e, "doc_id", None) or doc_id,
            error_msg=_short_error(e),
        )
        raise self.retry(exc=e)
