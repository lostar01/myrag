from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid
import json
from pathlib import Path
from tasks import import_document_task
from celery_task_store import task_store
from pg_store import pg_store
from doc_manager import doc_manager
from rag_chat_qa import chat_qa_stream
from user_store import user_store, create_token, decode_token
from chat_store import chat_store

app = FastAPI(title="RAG异步文档上传接口")
UPLOAD_DIR = Path("./upload_cache")
UPLOAD_DIR.mkdir(exist_ok=True)


# ============ 用户管理相关 Pydantic 模型 ============
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    nickname: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdateUserRequest(BaseModel):
    nickname: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None


# ============ 会话管理相关 Pydantic 模型 ============
class CreateSessionRequest(BaseModel):
    title: Optional[str] = None
    meta: Optional[dict] = None


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = None
    meta: Optional[dict] = None


# ============ 鉴权依赖 ============
def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """从 Authorization: Bearer <token> 解析当前用户"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少或非法的认证头")
    token = authorization[len("Bearer "):].strip()
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return payload


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ============ 用户管理 API ============
@app.post("/users/register")
async def register(req: RegisterRequest):
    """用户注册"""
    if user_store.get_by_username(req.username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = user_store.create_user(
        username=req.username,
        password=req.password,
        nickname=req.nickname,
    )
    return {"msg": "注册成功", "user": user}


@app.post("/users/login")
async def login(req: LoginRequest):
    """用户登录，返回 token"""
    user = user_store.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误，或账号已禁用")
    token = create_token(user["user_id"], user["username"], user.get("role", "user"))
    return {"msg": "登录成功", "token": token, "user": user}


@app.get("/users/me")
async def get_me(user: dict = Depends(get_current_user)):
    """获取当前登录用户信息"""
    info = user_store.get_by_id(user["user_id"])
    if not info:
        raise HTTPException(status_code=404, detail="用户不存在")
    return info


@app.get("/users")
async def list_users(
    limit: int = 100,
    offset: int = 0,
    _admin: dict = Depends(require_admin),
):
    """用户列表（管理员）"""
    rows = pg_store.query_all(
        "SELECT user_id, username, nickname, role, status, created_at "
        "FROM rag_user ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (limit, offset),
    )
    total_row = pg_store.query_one("SELECT COUNT(*) AS cnt FROM rag_user")
    return {"total": total_row["cnt"] if total_row else 0, "users": rows}


@app.get("/users/{user_id}")
async def get_user(user_id: str, _admin: dict = Depends(require_admin)):
    """查询单个用户（管理员）"""
    info = user_store.get_by_id(user_id)
    if not info:
        raise HTTPException(status_code=404, detail="用户不存在")
    return info


@app.put("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    _admin: dict = Depends(require_admin),
):
    """更新用户信息（管理员）"""
    if not user_store.get_by_id(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")

    fields = []
    params: List = []
    if req.nickname is not None:
        fields.append("nickname = %s")
        params.append(req.nickname)
    if req.role is not None:
        if req.role not in ("user", "admin"):
            raise HTTPException(status_code=400, detail="role 只能是 user 或 admin")
        fields.append("role = %s")
        params.append(req.role)
    if req.status is not None:
        if req.status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="status 只能是 active 或 disabled")
        fields.append("status = %s")
        params.append(req.status)

    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    fields.append("updated_at = NOW()")
    params.append(user_id)
    sql = f"UPDATE rag_user SET {', '.join(fields)} WHERE user_id = %s"
    pg_store.execute(sql, tuple(params))
    return {"msg": "更新成功", "user": user_store.get_by_id(user_id)}


@app.delete("/users/{user_id}")
async def delete_user(user_id: str, _admin: dict = Depends(require_admin)):
    """删除用户（管理员）"""
    if not user_store.get_by_id(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    pg_store.execute("DELETE FROM rag_user WHERE user_id = %s", (user_id,))
    return {"user_id": user_id, "msg": "用户已删除"}


# ============ 会话管理 API ============
@app.post("/sessions")
async def create_session(req: CreateSessionRequest, user: dict = Depends(get_current_user)):
    """新建会话"""
    chat_id = chat_store.create_session(
        user_id=user["user_id"],
        title=req.title,
        meta=req.meta,
    )
    return {"msg": "会话已创建", "session": chat_store.get_session(chat_id)}


@app.get("/sessions")
async def list_sessions(
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """当前用户的会话列表（管理员可查看全部，通过 all=true）"""
    # 普通用户只能看自己的会话
    uid = None if user.get("role") == "admin" else user["user_id"]
    sessions = chat_store.list_sessions(user_id=uid, limit=limit, offset=offset)
    total = chat_store.count_sessions(user_id=uid)
    return {"total": total, "sessions": sessions}


@app.get("/sessions/{chat_id}")
async def get_session(chat_id: str, user: dict = Depends(get_current_user)):
    """查询单个会话详情"""
    session = chat_store.get_session(chat_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user.get("role") != "admin" and session["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return session


@app.get("/sessions/{chat_id}/messages")
async def get_session_messages(chat_id: str, user: dict = Depends(get_current_user)):
    """查询会话消息列表"""
    session = chat_store.get_session(chat_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user.get("role") != "admin" and session["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return {"chat_id": chat_id, "messages": chat_store.list_messages(chat_id)}


@app.put("/sessions/{chat_id}")
async def update_session(
    chat_id: str,
    req: UpdateSessionRequest,
    user: dict = Depends(get_current_user),
):
    """更新会话标题/元数据"""
    session = chat_store.get_session(chat_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user.get("role") != "admin" and session["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权修改该会话")
    if req.title is None and req.meta is None:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    updated = chat_store.update_session(chat_id, title=req.title, meta=req.meta)
    return {"msg": "更新成功", "session": updated}


@app.delete("/sessions/{chat_id}")
async def delete_session(chat_id: str, user: dict = Depends(get_current_user)):
    """删除会话（级联删除消息）"""
    session = chat_store.get_session(chat_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user.get("role") != "admin" and session["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权删除该会话")
    chat_store.delete_session(chat_id)
    return {"chat_id": chat_id, "msg": "会话已删除"}


# ============ 文档 / 任务 / 问答 API ============
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

@app.get("/chat/stream")
async def chat_stream(chat_id: str, query: str):
    """SSE 流式问答接口。

    事件格式（每行 data: <json>）：
      {"type": "sources", "sources": [...]}
      {"type": "delta", "content": "..."}
      {"type": "done", "answer": "...", "chat_id": "..."}
    """
    def event_generator():
        try:
            for event in chat_qa_stream(chat_id=chat_id, query=query):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
