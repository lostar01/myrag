from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
import hashlib
import os
import time
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
from user_store import user_store, create_token, decode_token, token_is_revoked
from chat_store import chat_store
import memory_service
from upload_paths import UPLOAD_DIR, ensure_upload_dir, resolve_upload_path
from settings import settings

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

# 上传相关参数统一来自 config/config.yaml 的 upload 段（D1）
# UPLOAD_DIR 已在 upload_paths 里解析成绝对路径，与进程 CWD 无关
ensure_upload_dir()

# 允许上传的文档后缀（docling 可解析的格式）
ALLOWED_SUFFIXES = settings.upload.suffix_set()
# 单文件大小上限
MAX_UPLOAD_SIZE = settings.upload.max_size_mb * 1024 * 1024
# 分块写入大小：1MB
CHUNK_SIZE = 1024 * 1024

print(
    f"[upload] 上传目录（绝对路径）：{UPLOAD_DIR}｜"
    f"大小上限：{settings.upload.max_size_mb}MB｜"
    f"允许类型：{', '.join(sorted(ALLOWED_SUFFIXES))}"
)


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


class CreateMemoryRequest(BaseModel):
    # profile = 用户是谁/什么领域；preference = 回答风格与范围偏好
    kind: str = Field("preference", pattern="^(profile|preference)$")
    content: str = Field(..., min_length=2, max_length=200)


class UpdateMemoryRequest(BaseModel):
    content: Optional[str] = Field(None, min_length=2, max_length=200)
    # archived = 用户手动“删除”（软删，不注入也不再被语义召回）
    status: Optional[str] = Field(None, pattern="^(active|archived)$")


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
    """从 Authorization: Bearer <token> 解析当前用户，并做**可撤销**校验（P0 #7）。

    token 自身只能证明“签名有效且未到 exp”；账号状态、角色、token_epoch
    全部以库里那一行为准，所以：
      - 管理员禁用 / 删除账号、重置密码、改角色后，旧 token 立刻失效；
      - 代价是每次请求多一次 rag_user 的 PK 单行 SELECT（~1ms）。
        不加进程内缓存（D6）：跨进程（uvicorn 多 worker + celery）无需考虑缓存一致性，
        撤销也是立即生效。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少或非法的认证头")
    token = authorization[len("Bearer "):].strip()
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="token 无效或已过期")

    account = user_store.get_auth_context(payload.get("user_id"))
    if not account:
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")
    if account.get("status") != "active":
        raise HTTPException(status_code=401, detail="账号已被禁用")
    if token_is_revoked(payload, account.get("token_epoch")):
        raise HTTPException(
            status_code=401,
            detail="token 已失效（密码或权限已变更），请重新登录",
        )

    # 角色以库为准：管理员被降权后，手里的旧 token 也立刻失去管理员权限
    return {
        "user_id": account["user_id"],
        "username": account["username"],
        "nickname": account.get("nickname"),
        "role": account.get("role") or "user",
    }


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

    # 角色或状态变了 ⇒ 该账号已有的 token 全部作废（P0 #7）。
    # 只改昵称不 bump，避免管理员编资料把用户踢下线。
    if req.role is not None or req.status is not None:
        fields.append("token_epoch = %s")
        params.append(int(time.time()))

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
    # 先清记忆：隐私上“人删了、画像与偏好还留在库里”说不通
    purged = memory_service.purge_user(user_id)
    user_store.delete_user(user_id)
    # 已知缺口（本次不改）：该用户的 chat_session / chat_message 仍会留在库里（历史行为）。
    # 要一并删就需要在用户删除时级联删会话，已记在 Readme 的待办里。
    return {"user_id": user_id, "msg": f"用户已删除（同时清除记忆 {purged} 条）"}


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


# ============ 记忆 API（用户本人可见；管理员也没有旁路）============
# 注意：这里的接口不受 memory.enable 限制 —— 关掉功能后，用户仍然要能看到并清掉旧记忆。
# 开关只影响两件事：回答时是否注入记忆、回答后是否自动抽取。

@app.get("/memories")
async def list_memories(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    kind: Optional[str] = Query(None, description="profile | preference"),
    user: dict = Depends(get_current_user),
):
    """列出我自己的记忆（按权重/最近使用排序）。"""
    items, total = memory_service.list_for_user(user["user_id"], limit=limit, offset=offset, kind=kind)
    return {
        "total": total,
        "items": items,
        # 已归档（自动去重/封顶/过期，或用户自己删掉的）条数。
        # 不列明细（用户主动删的东西不该再摆回他面前），但要说清“不是 0 条、是都归档了”。
        "archived_total": memory_service.count_archived(user["user_id"]),
        # 前端用这个值提示“当前只展示历史记忆，功能已关闭”
        "memory_enabled": settings.memory.enable,
    }


@app.post("/memories")
async def create_memory(req: CreateMemoryRequest, user: dict = Depends(get_current_user)):
    """手动加一条记忆（与自动抽取走同一套去重，重复内容不会堆条目）。"""
    row = memory_service.create_manual(user["user_id"], req.kind, req.content)
    if not row:
        raise HTTPException(status_code=400, detail="记忆内容为空或过长")
    return {"msg": "已保存", "memory": row}


@app.put("/memories/{memory_id}")
async def update_memory(
    memory_id: str,
    req: UpdateMemoryRequest,
    user: dict = Depends(get_current_user),
):
    """改内容或改状态。

    改内容会归档旧条目 + 新建一条（保留审计轨迹，也不会出现 hash 与内容不一致）；
    所以返回的 memory 可能带着新 id，前端改完后重新拉列表即可。
    """
    if req.content is None and req.status is None:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    row = memory_service.update_memory(
        user["user_id"], memory_id, content=req.content, status=req.status
    )
    if not row:
        raise HTTPException(status_code=404, detail="记忆不存在或不属于当前用户")
    return {"msg": "更新成功", "memory": row}


@app.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str, user: dict = Depends(get_current_user)):
    """删除一条记忆（软删 + 删向量点）。"""
    if not memory_service.delete_memory(user["user_id"], memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在或不属于当前用户")
    return {"memory_id": memory_id, "msg": "已删除"}


@app.delete("/memories")
async def clear_memories(user: dict = Depends(get_current_user)):
    """清空我的全部记忆（隐私出口，必须一钮可用）。"""
    deleted = memory_service.clear_all(user["user_id"])
    return {"deleted": deleted, "msg": f"已清空 {deleted} 条记忆"}


# ============ 文档 / 任务 / 问答 API ============
def _duplicate_message(conflict: dict) -> str:
    """按已存在记录的状态生成重复上传提示文案。"""
    uploader = conflict.get("uploader") or "历史数据"
    status = conflict.get("status")
    if status in ("processing", "pending", "running"):
        return f"该文件正在解析中（上传人：{uploader}），请稍后再试"
    if status == "failed":
        return (
            f"该文件上次解析失败（上传人：{uploader}），"
            "请管理员在「上传记录」中删除后重传"
        )
    return f"文件已存在（上传人：{uploader}），无需重复上传"


# 惰性自愈节流：最多每 60 秒执行一次，避免每次列表请求都打 UPDATE
_last_heal_at = 0.0
HEAL_MIN_INTERVAL = 60.0


def _heal_stale_if_due():
    """超过 HEAL_MIN_INTERVAL 才跑一次 heal_stale_tasks，异常不影响列表查询。"""
    global _last_heal_at
    now = time.monotonic()
    if now - _last_heal_at < HEAL_MIN_INTERVAL:
        return
    _last_heal_at = now
    try:
        healed = doc_manager.heal_stale_tasks()
        if healed:
            print(f"[uploads] 惰性自愈：{healed} 条超时任务已标记为 failed")
    except Exception as e:
        print(f"[uploads] 惰性自愈失败（不影响列表）：{e}")


def _safe_unlink_upload(file_path: Optional[str]):
    """只允许删除上传目录下的源文件，避免路径穿越误删。

    路径解析统一走 upload_paths.resolve_upload_path：新数据是绝对路径，
    老数据里的相对路径也能按同一规则定位到当前上传目录。
    """
    target = resolve_upload_path(file_path)
    if target is None:
        return
    try:
        target.unlink(missing_ok=True)
    except Exception as e:
        print(f"[upload] 删除源文件失败 path={file_path}: {e}")


@app.post("/upload")
async def upload_file(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """上传文档：校验后缀/大小 → 分块落盘（边写边算 md5）→ md5 查重 → 写上传记录 → 提交 Celery 任务。

    rag_doc 已有该 md5（success / failed）或同 md5 正在解析时不提交任务，
    需管理员在「上传记录」中删除记录后才能重新上传。
    """
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

    # 2. 分块落盘，避免大文件一次性读入内存，同时限制总大小；
    #    边写边算 md5（不额外读一遍文件），供上传前查重使用
    save_name = f"{uuid.uuid4().hex}{suffix}"
    save_path = UPLOAD_DIR / save_name
    total = 0
    md5 = hashlib.md5()
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
                md5.update(chunk)
                f.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传的文件为空")
        file_md5 = md5.hexdigest()
    except HTTPException:
        save_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"文件保存失败：{e}")
    finally:
        await file.close()

    # 3. 上传前 md5 查重：rag_doc 已有该 md5 记录（任意状态）、或同 md5 正在解析，
    #    都不提交任务。只有管理员在「上传记录」里删除记录后才能重新上传。
    conflict = doc_manager.find_upload_conflict(file_md5)
    if conflict:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=_duplicate_message(conflict))

    # 4. 先占位 rag_doc（status=processing）+ 写上传记录（pending），列表立刻可见；
    #    doc_id 在上传时确定，列表以 rag_doc 为准、其余信息由 doc_id 级联 rag_task，
    #    Celery 任务 ID 与记录 ID 一致，worker 的回写才能落回同一行
    doc_id = str(uuid.uuid4())
    task_id = uuid.uuid4().hex
    try:
        doc_manager.reserve_document(
            doc_id=doc_id,
            doc_name=original_name,
            file_path=str(save_path),
            file_md5=file_md5,
            file_size=total,
        )
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"上传记录创建失败：{e}")

    try:
        task_store.create_task(
            task_id=task_id,
            doc_id=doc_id,
            file_name=original_name,
            file_path=str(save_path),
            user_id=user["user_id"],
            status="pending",
        )
    except Exception as e:
        save_path.unlink(missing_ok=True)
        doc_manager.mark_document_failed(doc_id, f"上传记录创建失败：{e}")
        raise HTTPException(status_code=500, detail=f"上传记录创建失败：{e}")

    try:
        import_document_task.apply_async(
            kwargs={
                "local_file_path": str(save_path),
                "file_name": original_name,
                "doc_id": doc_id,
            },
            task_id=task_id,
        )
    except Exception as e:
        # 提交失败也保留记录（失败保留），并清理临时文件
        task_store.update_task(
            task_id,
            status="failed",
            doc_id=doc_id,
            error_msg=f"任务提交失败：{e}"[:500],
            progress=0,
        )
        doc_manager.mark_document_failed(doc_id, f"任务提交失败：{e}")
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail=f"任务提交失败，请稍后重试：{e}")

    return {
        "task_id": task_id,
        "doc_id": doc_id,
        "msg": "任务已提交，后台解析入库",
        "file_name": original_name,
        "size": total,
        "file_md5": file_md5,
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

@app.get("/uploads")
async def list_upload_records(
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """上传记录列表：以 rag_doc 为准（一条 = 一篇文档），其余信息由 doc_id 级联 rag_task。

    普通用户只看自己的，管理员看全部（含上传人与删除操作）。
    """
    uid = None if user.get("role") == "admin" else user["user_id"]
    rows, total = doc_manager.list_upload_records(user_id=uid, limit=limit, offset=offset)
    return {"total": total, "records": rows}


@app.delete("/uploads/{record_id}")
async def delete_upload_record(record_id: str, _admin: dict = Depends(require_admin)):
    """删除上传记录（管理员）：以 rag_doc 为准，按 doc_id 清 chunk + 向量 + rag_doc + rag_task + 源文件。

    删除后同一文件才能重新上传。record_id 就是 doc_id（兼容旧的 legacy:<doc_id> 写法）。
    解析中（且未超时）的记录不允许删除。
    """
    doc_id = record_id[len("legacy:"):] if record_id.startswith("legacy:") else record_id

    record = doc_manager.get_upload_record(doc_id)
    if not record:
        raise HTTPException(status_code=404, detail="上传记录不存在")
    # 解析中（且未超时）不允许删除，避免后台任务写回脏数据；卡死的记录（>30 分钟）可删
    if record.get("is_active"):
        raise HTTPException(status_code=409, detail="该记录正在解析中，完成后可删除")

    doc_row = pg_store.query_one("SELECT file_path FROM rag_doc WHERE doc_id = %s", (doc_id,))
    task_row = pg_store.query_one(
        "SELECT file_path FROM rag_task WHERE doc_id = %s ORDER BY created_at DESC LIMIT 1",
        (doc_id,),
    )
    file_path = (doc_row or {}).get("file_path") or (task_row or {}).get("file_path")

    # 1. 先删任务行（否则 add_document 重试时还会回写已删的 doc_id）
    pg_store.execute("DELETE FROM rag_task WHERE doc_id = %s", (doc_id,))
    # 2. 再删 chunk / 向量 / rag_doc（复用已有的双删逻辑）
    doc_manager.delete_document(doc_id)
    # 3. 最后清 upload_cache 源文件
    _safe_unlink_upload(file_path)
    return {"doc_id": doc_id, "msg": "上传记录已删除，可重新上传"}


@app.get("/documents")
async def list_documents(
    limit: int = 100,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """文档列表（旧接口，保留兼容）：已按登录身份过滤（P0 #5）。

    普通用户只能看到自己上传的文档，管理员看全部。
    直接复用「上传记录」已有的按用户过滤 SQL（doc_manager.list_upload_records），
    不再单独写一份没有权限过滤的 SQL。
    字段名保持旧接口的 doc_name（值就是权威文件名 rag_doc.doc_name）。
    """
    uid = None if user.get("role") == "admin" else user["user_id"]
    rows, total = doc_manager.list_upload_records(user_id=uid, limit=limit, offset=offset)
    documents = [
        {
            "doc_id": r["doc_id"],
            "doc_name": r.get("file_name"),
            "page_count": r.get("page_count"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        }
        for r in rows
    ]
    return {"total": total, "documents": documents}

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
      {"type": "title", "title": "...", "chat_id": "..."}  # 仅新会话第一问：自动命名的标题（done 之后下发）
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
                # user_id 从 token 传进去：记忆必须用可信身份（会话已校验归属，两处一致）
                for event in chat_qa_stream(chat_id=chat_id, query=query, user_id=user["user_id"]):
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
