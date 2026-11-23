from fastapi import FastAPI, UploadFile, File, HTTPException
import uuid
from pathlib import Path
from tasks import import_document_task
from celery_task_store import task_store
from pg_store import pg_store
from doc_manager import doc_manager

app = FastAPI(title="RAG异步文档上传接口")
UPLOAD_DIR = Path("./upload_cache")
UPLOAD_DIR.mkdir(exist_ok=True)

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    # 1. 前端文件先临时落盘
    suffix = Path(file.filename).suffix
    save_name = f"{uuid.uuid4()}{suffix}"
    save_path = UPLOAD_DIR / save_name
    with open(save_path, "wb") as f:
        f.write(await file.read())

    # 2. 提交celery任务，立刻返回task_id，不阻塞！
    task = import_document_task.delay(
        local_file_path=str(save_path),
        file_name=file.filename
    )
    task_store.update_task(task.id, status="pending")
    return {
        "task_id": task.id,
        "msg": "任务已提交，后台解析入库",
        "file_name": file.filename
    }

@app.get("/task/{task_id}")
async def get_task_info(task_id: str):
    # 用字典查询，避免 SELECT * 的列顺序依赖导致下标错位
    row = pg_store.query_one(
        "SELECT task_id, doc_id, status, error_msg, progress FROM rag_task WHERE task_id = %s",
        (task_id,)
    )
    if not row:
        return {"code": 404, "msg": "task not found"}
    return {
        "task_id": row["task_id"],
        "doc_id": row["doc_id"],
        "status": row["status"],
        "error_msg": row["error_msg"],
        "progress": row["progress"]
    }

@app.get("/documents")
async def list_documents(limit: int = 100, offset: int = 0):
    """文档列表查询"""
    docs = doc_manager.list_documents(limit=limit, offset=offset)
    return {"total": len(docs), "documents": docs}

@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    """删除文档：同时清理 PG chunk、Qdrant 向量、rag_doc 主记录"""
    row = pg_store.query_one("SELECT doc_id FROM rag_doc WHERE doc_id = %s", (doc_id,))
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    doc_manager.delete_document(doc_id)
    return {"doc_id": doc_id, "msg": "文档已删除"}
