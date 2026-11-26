from celery_app import app
from celery_task_store import task_store
from doc_manager import doc_manager
import memory_service
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


# ---------- 记忆更新（长短记忆；总开关 memory.enable，默认 false）----------

@app.task(bind=True, name="tasks.memory_update_task", retry_backoff=3, max_retries=1)
def memory_update_task(self, user_id: str, chat_id: str):
    """回答结束后异步更新记忆：会话摘要压缩 + 用户画像抽取。

    只重试 1 次：这活儿每次都要调 LLM，429 时多次重试只会放大流量。
    这里**不 raise**：答案已经返回给用户了，记忆失败只打日志；
    抽取游标没动 ⇒ 下一轮问答会自动把这段对话补上，不会丢记忆。

    ⚠ 它与文档导入共用同一个 worker 队列：导入大文档时记忆会排队。
      要隔离就把这个任务路由到单独的队列（-Q 参数），或单独起一个 worker。
    """
    try:
        stats = memory_service.run_update(user_id=user_id, chat_id=chat_id)
        print(f"[memory_update] chat_id={chat_id} user_id={user_id} {stats}")
        return stats
    except Exception as e:
        print("===== FULL EXCEPTION STACK =====")
        traceback.print_exc()
        return {"error": _short_error(e)}
