from celery_app import app
from celery_task_store import task_store
from doc_manager import doc_manager
import traceback

@app.task(bind=True, name="tasks.import_document_task", retry_backoff=3, max_retries=2)
def import_document_task(self, local_file_path: str, file_name: str):
    task_id = self.request.id
    task_store.update_task(task_id, status="running", progress=5)
    try:
        res = doc_manager.add_document(file_path=local_file_path, file_name=file_name)
        doc_id = res["doc_id"]
        task_store.update_task(task_id, status="success", doc_id=doc_id, progress=100)
        return res
    except Exception as e:
        # 打印完整堆栈！
        print("===== FULL EXCEPTION STACK =====")
        traceback.print_exc()
        err_msg = f"{str(e)}\n{traceback.format_exc()}"
        task_store.update_task(task_id, status="failed", error_msg=err_msg)
        raise self.retry(exc=e)
