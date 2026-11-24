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
_SECRET_KEY = os.environ.get("RAG_SECRET_KEY", "change-me-in-production-please")
_TOKEN_TTL_SECONDS = int(os.environ.get("RAG_TOKEN_TTL", str(7 * 24 * 3600)))  # 默认 7 天


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str) -> str:
    sig = hmac.new(_SECRET_KEY.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(sig)


def create_token(user_id: str, username: str, role: str = "user") -> str:
    """生成 token：<payload_b64>.<signature>"""
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
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
        """修改密码，返回是否更新成功（用户不存在返回 False）"""
        if not self.get_by_id(user_id):
            return False
        pg_store.execute(
            "UPDATE rag_user SET password_hash = %s, updated_at = NOW() WHERE user_id = %s",
            (hash_password(new_password), user_id),
        )
        return True

    def delete_user(self, user_id: str) -> None:
        pg_store.execute("DELETE FROM rag_user WHERE user_id = %s", (user_id,))


user_store = UserStore()
