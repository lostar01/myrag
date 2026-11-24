from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
import os
import uuid
import json
import queue
import threading
from pathlib import Path
from tasks import import_document_task
from celery_task_store import task_store
from pg_store import pg_store
from doc_manager import doc_manager
from rag_chat_qa import chat_qa_stream
from user_store import user_store, create_token, decode_token
from chat_store import chat_store

app = FastAPI(title="RAG异步文档上传接口")

# ============ CORS 配置 ============
# 前端与后端分离部署（如 file:// 或不同端口）时，浏览器会因跨域拦截请求，
# 表现为 fetch 报 "Failed to fetch"。这里放开跨域，允许前端携带 Authorization 头。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 生产环境建议改为具体前端域名
    allow_credentials=False,      # allow_origins=["*"] 时必须为 False
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

UPLOAD_DIR = Path("./upload_cache")
UPLOAD_DIR.mkdir(exist_ok=True)

# 允许上传的文档后缀（docling 可解析的格式）
ALLOWED_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".html", ".htm", ".txt"}
# 单文件大小上限：50MB
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
# 分块写入大小：1MB
CHUNK_SIZE = 1024 * 1024


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


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    nickname: Optional[str] = None
    role: str = "user"


class ChangePasswordRequest(BaseModel):
    old_password: Optional[str] = None
    new_password: str = Field(..., min_length=6, max_length=128)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)


# 是否开放自助注册（默认关闭，生产环境建议由管理员创建账号）
ALLOW_REGISTER = os.environ.get("RAG_ALLOW_REGISTER", "false").lower() in ("1", "true", "yes")

# 初始管理员账号（首次启动且库中无管理员时自动创建，用于引导系统）
BOOTSTRAP_ADMIN_USERNAME = os.environ.get("RAG_ADMIN_USERNAME", "admin")
BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("RAG_ADMIN_PASSWORD", "admin123")


@app.on_event("startup")
def bootstrap_admin():
    """首次启动引导：若系统中不存在任何管理员，则用环境变量创建默认管理员。

    这样在关闭自助注册（默认）的情况下，仍有一个可用的管理入口。
    创建后请尽快登录并修改默认密码。
    """
    try:
        row = pg_store.query_one(
            "SELECT COUNT(*) AS cnt FROM rag_user WHERE role = 'admin'"
        )
        if row and row["cnt"] > 0:
            return
        if user_store.get_by_username(BOOTSTRAP_ADMIN_USERNAME):
            # 用户名已被占用（可能是普通用户），提升为管理员
            existing = user_store.get_by_username(BOOTSTRAP_ADMIN_USERNAME)
            pg_store.execute(
                "UPDATE rag_user SET role = 'admin', status = 'active', updated_at = NOW() "
                "WHERE user_id = %s",
                (existing["user_id"],),
            )
            print(f"[bootstrap] 已将用户 {BOOTSTRAP_ADMIN_USERNAME} 提升为管理员")
            return
        user_store.create_user(
            username=BOOTSTRAP_ADMIN_USERNAME,
            password=BOOTSTRAP_ADMIN_PASSWORD,
            nickname="系统管理员",
            role="admin",
        )
        print(
            f"[bootstrap] 已创建默认管理员：{BOOTSTRAP_ADMIN_USERNAME} / "
            f"{BOOTSTRAP_ADMIN_PASSWORD}（请尽快登录修改密码）"
        )
    except Exception as e:
        # 引导失败不应阻断服务启动（例如数据库尚未就绪）
        print(f"[bootstrap] 初始化管理员失败：{e}")


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
@app.get("/auth/config")
async def auth_config():
    """公开的认证配置，供前端决定是否展示注册入口"""
    return {"allow_register": ALLOW_REGISTER}


@app.post("/users/register")
async def register(req: RegisterRequest):
    """用户注册（需服务端开启 RAG_ALLOW_REGISTER）"""
    if not ALLOW_REGISTER:
        raise HTTPException(status_code=403, detail="未开放注册，请联系管理员创建账号")
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


@app.put("/users/me/password")
async def change_my_password(
    req: ChangePasswordRequest,
    user: dict = Depends(get_current_user),
):
    """当前用户修改自己的密码（需校验旧密码）"""
    if not req.old_password:
        raise HTTPException(status_code=400, detail="请提供旧密码")
    if not user_store.authenticate(user["username"], req.old_password):
        raise HTTPException(status_code=400, detail="旧密码不正确")
    if req.old_password == req.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
    user_store.change_password(user["user_id"], req.new_password)
    return {"msg": "密码修改成功，请重新登录"}


@app.post("/users")
async def create_user(req: CreateUserRequest, _admin: dict = Depends(require_admin)):
    """创建用户（管理员）"""
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role 只能是 user 或 admin")
    if user_store.get_by_username(req.username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = user_store.create_user(
        username=req.username,
        password=req.password,
        nickname=req.nickname,
        role=req.role,
    )
    return {"msg": "创建成功", "user": user}


@app.put("/users/{user_id}/password")
async def reset_user_password(
    user_id: str,
    req: ResetPasswordRequest,
    _admin: dict = Depends(require_admin),
):
    """重置指定用户密码（管理员）"""
    if not user_store.change_password(user_id, req.new_password):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"msg": "密码已重置", "user_id": user_id}


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
    # 防止管理员把自己降级或禁用，导致系统失去管理入口
    if user_id == _admin["user_id"]:
        if req.role is not None and req.role != "admin":
            raise HTTPException(status_code=400, detail="不能修改自己的管理员角色")
        if req.status is not None and req.status != "active":
            raise HTTPException(status_code=400, detail="不能禁用自己的账号")

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
    if user_id == _admin["user_id"]:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    if not user_store.get_by_id(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    user_store.delete_user(user_id)
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
    """当前用户自己的会话列表（会话为个人私有，管理员同样只能看自己的）"""
    uid = user["user_id"]
    sessions = chat_store.list_sessions(user_id=uid, limit=limit, offset=offset)
    total = chat_store.count_sessions(user_id=uid)
    return {"total": total, "sessions": sessions}


@app.get("/sessions/{chat_id}")
async def get_session(chat_id: str, user: dict = Depends(get_current_user)):
    """查询单个会话详情"""
    session = chat_store.get_session(chat_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 会话为个人私有：管理员也不能查看/操作他人对话（管理端只做用户与文档管理）
    if session["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return session


@app.get("/sessions/{chat_id}/messages")
async def get_session_messages(chat_id: str, user: dict = Depends(get_current_user)):
    """查询会话消息列表"""
    session = chat_store.get_session(chat_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 会话为个人私有：管理员也不能查看/操作他人对话（管理端只做用户与文档管理）
    if session["user_id"] != user["user_id"]:
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
    # 会话为个人私有：管理员也不能查看/操作他人对话（管理端只做用户与文档管理）
    if session["user_id"] != user["user_id"]:
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
    # 会话为个人私有：管理员也不能查看/操作他人对话（管理端只做用户与文档管理）
    if session["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权删除该会话")
    chat_store.delete_session(chat_id)
    return {"chat_id": chat_id, "msg": "会话已删除"}


# ============ 文档 / 任务 / 问答 API ============
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """上传文档：校验后缀/大小 → 分块落盘 → 提交 Celery 异步解析任务。"""
    # 1. 校验文件名与后缀
    original_name = (file.filename or "").strip()
    if not original_name:
        raise HTTPException(status_code=400, detail="缺少文件名")

    suffix = Path(original_name).suffix.lower()
    if suffix == ".doc":
        raise HTTPException(
            status_code=400,
            detail="不支持旧版 .doc 格式，请另存为 .docx 或 .pdf 后再上传",
        )
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {suffix or '(无后缀)'}，"
                   f"仅支持：{', '.join(sorted(ALLOWED_SUFFIXES))}",
        )

    # 2. 分块落盘，避免大文件一次性读入内存，同时限制总大小
    save_name = f"{uuid.uuid4().hex}{suffix}"
    save_path = UPLOAD_DIR / save_name
    total = 0
    try:
        with open(save_path, "wb") as f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小上限 {MAX_UPLOAD_SIZE // (1024 * 1024)}MB",
                    )
                f.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传的文件为空")
    except HTTPException:
        save_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"文件保存失败：{e}")
    finally:
        await file.close()

    # 3. 提交 Celery 任务；失败则清理临时文件，避免残留
    try:
        task = import_document_task.delay(
            local_file_path=str(save_path),
            file_name=original_name,
        )
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail=f"任务提交失败，请稍后重试：{e}")

    task_store.update_task(task.id, status="pending")
    return {
        "task_id": task.id,
        "msg": "任务已提交，后台解析入库",
        "file_name": original_name,
        "size": total,
    }

@app.get("/task/{task_id}")
async def get_task_info(task_id: str, user: dict = Depends(get_current_user)):
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
async def list_documents(
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """文档列表查询"""
    docs = doc_manager.list_documents(limit=limit, offset=offset)
    return {"total": len(docs), "documents": docs}

@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, _admin: dict = Depends(require_admin)):
    """删除文档（管理员）：同时清理 PG chunk、Qdrant 向量、rag_doc 主记录"""
    row = pg_store.query_one("SELECT doc_id FROM rag_doc WHERE doc_id = %s", (doc_id,))
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    doc_manager.delete_document(doc_id)
    return {"doc_id": doc_id, "msg": "文档已删除"}

@app.get("/chat/stream")
async def chat_stream(chat_id: str, query: str, user: dict = Depends(get_current_user)):
    """SSE 流式问答接口（需登录，且只能访问自己的会话）。

    事件格式（每行 data: <json>）：
      {"type": "sources", "sources": [...]}
      {"type": "delta", "content": "..."}
      {"type": "done", "answer": "...", "chat_id": "..."}
    """
    # 校验会话存在且归属当前用户（管理员可访问全部）
    session = chat_store.get_session(chat_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 会话为个人私有：管理员也不能查看/操作他人对话（管理端只做用户与文档管理）
    if session["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    HEARTBEAT_INTERVAL = 15  # 秒

    def event_generator():
        # 用后台线程跑阻塞的生成器，主线程按间隔插入心跳，避免“思考”阶段长时间无数据被断开
        q: "queue.Queue" = queue.Queue()
        _SENTINEL = object()

        def worker():
            try:
                for event in chat_qa_stream(chat_id=chat_id, query=query):
                    q.put(event)
            except Exception as e:  # 生成器内部异常转成 error 事件
                q.put({"type": "error", "message": str(e)})
            finally:
                q.put(_SENTINEL)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            try:
                event = q.get(timeout=HEARTBEAT_INTERVAL)
            except queue.Empty:
                # 超时未拿到新事件，发送 SSE 注释行保活（前端按 data: 行解析，会安全忽略）
                yield ": ping\n\n"
                continue
            if event is _SENTINEL:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
