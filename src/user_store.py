import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from pg_store import pg_store
from settings import settings


# ========== 密码哈希（PBKDF2-HMAC-SHA256，无额外依赖） ==========
_PBKDF2_ITERATIONS = 200_000
_PBKDF2_ALGO = "sha256"


def hash_password(password: str) -> str:
    """生成密码哈希，格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(dk).decode("ascii")
    return f"pbkdf2_{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码，使用常量时间比较"""
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if not algo.startswith("pbkdf2_"):
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac(algo[len("pbkdf2_"):], password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ========== Token（HMAC-SHA256 签名，无额外依赖） ==========
# 签名密钥必须显式配置：原先的默认值写在源码里（change-me-in-production-please），
# 没配密钥时任何拿到仓库的人都能自签任意 user_id / role 的 token；而且伪造时可以把
# iat 写成当前时间，直接绕过 token_epoch 撤销（P0 #7 的防线形同虚设）。
#
# 取值优先级（靠前的优先；两边都没填 ⇒ 拒绝启动）：
#   1) 环境变量 RAG_SECRET_KEY         ← 适合 docker -e / systemd EnvironmentFile 注入
#   2) config.yaml 里的 auth.secret_key ← 日常推荐：和 pg/qdrant 凭据同一份文件，
#      而且 API 与各 worker 读的是同一份 ⇒ 不会出现「进程间密钥不同 → 登录后随机 401」
#
# 重要：密钥值要固定，不要每次启动换一个。换一个 = 全员立即重新登录；
# 而各进程值不同 = 同一个 token 在别的 worker 验不过，表现为时好时坏的随机掉线。
DEFAULT_SECRET_KEY = "change-me-in-production-please"
MIN_SECRET_LEN = 32          # 至少 32 字符；建议直接用 token_hex(32) = 64 字符
_INSECURE_ON = ("1", "true", "yes", "on")

_KEY_HELP = """生成一个随机密钥（三选一）：
  python -c "import secrets; print(secrets.token_hex(32))"
  bash:   openssl rand -hex 32
  node:   node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"

放哪里（二选一，环境变量优先）：
  A) 推荐：在 config/config.yaml 里加两行（这个文件已被 .gitignore 排除，只存本地）
       auth:
         secret_key: '<刚生成的值>'
     收一下文件权限：Windows icacls config\\config.yaml /inheritance:r /grant:r "%USERNAME%:F"
                    Linux  chmod 600 config/config.yaml
  B) 环境变量：PowerShell  $env:RAG_SECRET_KEY = '<刚生成的值>'
              bash        export RAG_SECRET_KEY='<刚生成的值>'

注意：
  1) 所有进程（多个 uvicorn worker + celery worker）必须读同一份配置或同一个环境变量值，
     否则 token 在不同进程间互不认账，表现为登录后随机掉线。
  2) 换密钥会让所有已登录 token 立即失效（用户需重新登录），挑低峰时段做。
  3) 本地开发想跳过检查：显式设 RAG_ALLOW_INSECURE_SECRET=1（会打醒目安全告警）。"""


def _yaml_secret_key() -> str:
    """读 config.yaml 的 auth.secret_key（旧 yaml 没有 auth 段时返回空串）"""
    auth = getattr(settings, "auth", None)
    return str(getattr(auth, "secret_key", "") or "").strip()


def _load_secret_key() -> str:
    sources = [
        ("环境变量 RAG_SECRET_KEY", os.environ.get("RAG_SECRET_KEY", "").strip()),
        ("config.yaml 的 auth.secret_key", _yaml_secret_key()),
    ]
    for source, key in sources:
        if not key:
            continue
        if key == DEFAULT_SECRET_KEY:
            raise RuntimeError(
                source + " 用的还是内置默认密钥（它写在源码里，等于公开），必须换成随机值。\n" + _KEY_HELP
            )
        if len(key) < MIN_SECRET_LEN:
            raise RuntimeError(
                f"{source} 太短（{len(key)} 字符，要求至少 {MIN_SECRET_LEN}）。"
                "建议 token_hex(32) ⇒ 64 个十六进制字符。\n" + _KEY_HELP
            )
        # 只报来源与长度，绝不打印密钥本身
        print(f"[auth] token 签名密钥来自 {source}（长度 {len(key)}）")
        return key

    if os.environ.get("RAG_ALLOW_INSECURE_SECRET", "").strip().lower() in _INSECURE_ON:
        print(
            "\n" + "=" * 78 + "\n"
            "[SECURITY WARNING] 未配置 token 签名密钥，已按「本地开发」使用内置默认密钥。\n"
            "该密钥写在源码里 ⇒ 任何人都能伪造 token（含绕过 token_epoch 撤销）。\n"
            "切勿在生产环境设 RAG_ALLOW_INSECURE_SECRET。\n" + "=" * 78
        )
        return DEFAULT_SECRET_KEY

    raise RuntimeError(
        "未配置 token 签名密钥：环境变量 RAG_SECRET_KEY 和 config.yaml 的 auth.secret_key 都是空，"
        "服务拒绝启动。\n" + _KEY_HELP
    )


def _load_token_ttl() -> int:
    """token 有效期（秒）：环境变量 RAG_TOKEN_TTL 优先，其次 config.yaml 的 auth.token_ttl。"""
    auth = getattr(settings, "auth", None)
    yaml_ttl = getattr(auth, "token_ttl", 604800)
    raw = os.environ.get("RAG_TOKEN_TTL", "").strip() or str(yaml_ttl)
    try:
        ttl = int(raw)
    except ValueError:
        raise RuntimeError(f"token 有效期必须是整数秒，当前值是 {raw!r}")
    if ttl < 60:
        raise RuntimeError(f"token 有效期太短（{ttl}s），至少 60 秒，否则用户会频繁被踢下线")
    return ttl


_SECRET_KEY = _load_secret_key()
_TOKEN_TTL_SECONDS = _load_token_ttl()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str) -> str:
    sig = hmac.new(_SECRET_KEY.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(sig)


def create_token(user_id: str, username: str, role: str = "user") -> str:
    """生成 token：<payload_b64>.<signature>

    payload 里带 `iat`（签发时间，秒）：服务端拿它和 rag_user.token_epoch 比较，
    改密 / 改角色 / 禁用后把这些账号已有的旧 token 一并作废（P0 #7）。
    升级前签发的 token 没有 iat，按 0 处理：不会被默认的 token_epoch=0 撤销
    （升级当天不踢人），但管理员一旦 bump token_epoch 就会失效。
    """
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + _TOKEN_TTL_SECONDS,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """校验并解析 token，失败或过期返回 None"""
    try:
        payload_b64, sig = token.split(".")
        if not hmac.compare_digest(_sign(payload_b64), sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def token_is_revoked(payload: Dict[str, Any], token_epoch: Any) -> bool:
    """token 是否已被撤销：签发时间早于账号的 token_epoch 即作废。

    两边的单位都是 epoch 秒（所以 rag_user.token_epoch 用 BIGINT 而不是 TIMESTAMP）。
    没有 iat 的旧 token 按 0 处理：token_epoch=0（默认值）时仍然有效，
    一旦账号被 bump（改密/改角色/禁用）就失效。
    """
    try:
        iat = int(payload.get("iat") or 0)
        epoch = int(token_epoch or 0)
    except (TypeError, ValueError):
        return True
    return iat < epoch


# ========== 用户 CRUD（对齐 rag_user 表结构） ==========
_USER_PUBLIC_COLS = "user_id, username, nickname, role, status, created_at"


class UserStore:
    """用户数据访问层，对齐 rag_user 表结构。"""

    def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        return pg_store.query_one(
            f"SELECT {_USER_PUBLIC_COLS}, password_hash FROM rag_user WHERE username = %s",
            (username,),
        )

    def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        return pg_store.query_one(
            f"SELECT {_USER_PUBLIC_COLS} FROM rag_user WHERE user_id = %s",
            (user_id,),
        )

    def get_auth_context(self, user_id: str) -> Optional[Dict[str, Any]]:
        """鉴权用的最小字段集（含 token_epoch）。

        token_epoch 不放进 _USER_PUBLIC_COLS（不对外返回），只给
        get_current_user 每次请求校验用：账号状态 / 角色 / 撤销标记
        都以库里的行为准，不信 token 里写的内容。
        """
        if not user_id:
            return None
        return pg_store.query_one(
            "SELECT user_id, username, nickname, role, status, token_epoch "
            "FROM rag_user WHERE user_id = %s",
            (user_id,),
        )

    def create_user(
        self,
        username: str,
        password: str,
        nickname: Optional[str] = None,
        role: str = "user",
    ) -> Dict[str, Any]:
        """创建用户，返回不含密码哈希的用户信息。用户名重复会抛异常。"""
        user_id = uuid.uuid4().hex
        password_hash = hash_password(password)
        # 注意：query_one 只用于 SELECT，内部不会 commit。
        # INSERT ... RETURNING 必须放在会提交的事务里，否则数据不会真正落库。
        with pg_store.transaction() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    INSERT INTO rag_user (user_id, username, password_hash, nickname, role)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING {_USER_PUBLIC_COLS}
                    """,
                    (user_id, username, password_hash, nickname or username, role),
                )
                row = cur.fetchone()
        return dict(row) if row else None

    def authenticate(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """校验用户名密码，成功返回用户信息（不含密码哈希），失败返回 None"""
        user = self.get_by_username(username)
        if not user or user.get("status") != "active":
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        return {
            "user_id": user["user_id"],
            "username": user["username"],
            "nickname": user["nickname"],
            "role": user["role"],
            "status": user["status"],
            "created_at": user["created_at"],
        }

    def list_users(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        return pg_store.query_all(
            f"SELECT {_USER_PUBLIC_COLS} FROM rag_user ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )

    def count(self) -> int:
        row = pg_store.query_one("SELECT COUNT(*) AS cnt FROM rag_user")
        return int(row["cnt"]) if row else 0

    def update_user(self, user_id: str, **fields) -> Optional[Dict[str, Any]]:
        """按需更新字段（nickname/role/status），返回更新后的用户信息"""
        allowed = {"nickname", "role", "status"}
        sets = []
        params: List[Any] = []
        for key, value in fields.items():
            if key in allowed and value is not None:
                sets.append(f"{key} = %s")
                params.append(value)
        # 角色 / 状态变了就作废该账号已有的 token（与 /users/{id} 接口行为一致，P0 #7）
        if any(fields.get(k) is not None for k in ("role", "status")):
            sets.append("token_epoch = %s")
            params.append(int(time.time()))
        if not sets:
            return self.get_by_id(user_id)
        sets.append("updated_at = NOW()")
        params.append(user_id)
        pg_store.execute(
            f"UPDATE rag_user SET {', '.join(sets)} WHERE user_id = %s",
            tuple(params),
        )
        return self.get_by_id(user_id)

    def change_password(self, user_id: str, new_password: str) -> bool:
        """修改密码，并顺便撤销该用户已有的 token（P0 #7）。

        写密码和 bump token_epoch 是同一条 UPDATE，不会出现
        “密码改了但旧 token 还能用”的空窗。
        """
        if not self.get_by_id(user_id):
            return False
        pg_store.execute(
            "UPDATE rag_user SET password_hash = %s, token_epoch = %s, updated_at = NOW() "
            "WHERE user_id = %s",
            (hash_password(new_password), int(time.time()), user_id),
        )
        return True

    def revoke_tokens(self, user_id: str) -> int:
        """把该用户在此刻之前签发的所有 token 作废（改角色 / 禁用 / 启用后调用）。

        用 epoch 秒写 BIGINT，不用 TIMESTAMP：库里的 created_at 是
        TIMESTAMP WITHOUT TIME ZONE + NOW()（服务器本地时区），
        跟 time.time() 的 UTC epoch 直接比会差一个时区偏移。
        """
        if not self.get_by_id(user_id):
            return 0
        pg_store.execute(
            "UPDATE rag_user SET token_epoch = %s, updated_at = NOW() WHERE user_id = %s",
            (int(time.time()), user_id),
        )
        return 1

    def delete_user(self, user_id: str) -> None:
        pg_store.execute("DELETE FROM rag_user WHERE user_id = %s", (user_id,))


user_store = UserStore()
