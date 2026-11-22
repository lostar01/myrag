from fastapi import FastAPI, UploadFile, File
import uuid
from pathlib import Path
from tasks import import_document_task
from celery_task_store import task_store

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
    row = task_store.get_task(task_id)
    if not row:
        return {"code":404, "msg":"task not found"}
    return {
        "task_id": row[0],
        "doc_id": row[1],
        "status": row[3],
        "error_msg": row[4],
        "progress": row[5]
    }
