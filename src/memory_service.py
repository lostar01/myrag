"""长短记忆服务（默认全部关闭，开关见 config.yaml 的 memory 段）。

三条能力：
  1. 短期：会话内滚动摘要 —— 被窗口挤掉的更早对话压成要点，存在 `chat_session.meta`（不建表）；
  2. 长期：用户画像/偏好条目 —— PG 表 `rag_memory` 是真相，Qdrant 集 `rag_memory` 只是语义索引；
  3. 注入：Tier A 固定核心画像（按权重取前 N 条）+ Tier B 与当前问题相关的记忆（语义召回）。

硬性约束（改这个文件前先读）：
  - **只存用户本人的稳定信息**（身份/领域/职责、回答风格与范围偏好），
    **不存知识库事实** —— 长期记忆一旦装进文档结论，就会绕过检索与阈值，污染回答边界；
  - **任何一步失败都只打日志**，绝不能把问答请求带崩（`load_for_prompt` 全量 try 包住）；
  - **完全私有**：每条 SQL 都带 `user_id = %s`，没有管理员旁路（与 /sessions 的隐私原则一致）；
  - 抽取与摘要都在 Celery 任务里跑（`tasks.memory_update_task`），不在 SSE 路径调 LLM。
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from chat_store import chat_store
from chat_window import chat_window
from embedding_client import embedding_client
from llm_service import llm_service
from memory_store import memory_vectors
from pg_store import pg_store
from settings import settings

cfg = settings.memory

# 单条对话在抽取/摘要提示词里的截断长度：画像抽取只需要"用户说了什么要求"，
# 不需要整段回答，截断能显著压 LLM 成本
DIALOG_LINE_CHARS = 420
DIALOG_MAX_TURNS = 12

# 同一会话的记忆任务并发保护：租约时长（秒）。拿不到租约就本轮跳过，
# 游标没动 ⇒ 下一轮自动补上，不会丢记忆。
LEASE_SECONDS = 600


# ===================== 小工具 =====================

def enabled() -> bool:
    return bool(cfg.enable)


def _normalize(content: str) -> str:
    """归一化用于去重的键：去所有空白 + 大小写折叠。

    中文没有词形变化，够用了；语义级去重（"回答简短点" / "别啰嗦"）留到以后看数据再加。
    """
    return re.sub(r"\s+", "", str(content or "")).casefold()


def _content_hash(content: str) -> str:
    return hashlib.md5(_normalize(content).encode("utf-8")).hexdigest()


def _clip(content: str) -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    return text[: max(int(cfg.profile_max_chars), 12)]


def _to_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _iso(value: datetime) -> str:
    """存游标用的 ISO 串。

    ⚠ 不能截到秒（timespec="seconds"）：同一秒内写入的多条消息会被当成“还没处理过”，
    下一轮就把它们重新送一遍 LLM（抽取会重复跑、摘要会重复压）。保留微秒精度。
    """
    return value.isoformat()


def _after(created: Any, covered: Any) -> bool:
    created_dt, covered_dt = _to_dt(created), _to_dt(covered)
    if covered_dt is None:
        return True
    if created_dt is None:
        return True
    return created_dt > covered_dt


def _read_meta(chat_id: str) -> Dict[str, Any]:
    try:
        session = chat_store.get_session(chat_id)
    except Exception as e:
        print(f"[memory] 读会话失败 chat_id={chat_id}: {e}")
        return {}
    meta = (session or {}).get("meta") or {}
    return dict(meta) if isinstance(meta, dict) else {}


def _patch_meta(chat_id: str, **kv) -> None:
    """合并写 chat_session.meta。

    注意：`chat_store.update_session(meta=...)` 是**整列覆盖**，
    所以必须先读出来合并，否则会丢掉别人写的 key（比如自动命名之外的游标）。
    """
    meta = _read_meta(chat_id)
    meta.update(kv)
    chat_store.update_session(chat_id, meta=meta)


def _dialog_text(messages: List[Dict], line_chars: int = DIALOG_LINE_CHARS) -> str:
    picked = messages[-DIALOG_MAX_TURNS:]
    lines = []
    for msg in picked:
        content = re.sub(r"\s+", " ", str(msg.get("content") or "")).strip()
        if len(content) > line_chars:
            content = content[:line_chars] + "…"
        lines.append(f"{msg.get('role')}: {content}")
    return "\n".join(lines)


# ===================== 读：给 prompt 用 =====================

@dataclass
class MemoryContext:
    """一次问答要注入的记忆内容（全空 ⇒ prompt 里不出现任何记忆段落）。"""

    profile_lines: List[str] = field(default_factory=list)
    recall_lines: List[str] = field(default_factory=list)
    summary: str = ""
    profile_ids: List[str] = field(default_factory=list)
    recall_ids: List[str] = field(default_factory=list)
    recall_scores: List[float] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.profile_lines or self.recall_lines or self.summary)


_PROFILE_SQL = """
SELECT memory_id, kind, content, weight, hits, last_used_at, created_at, meta
FROM rag_memory
WHERE user_id = %s
  AND status = 'active'
  AND (expires_at IS NULL OR expires_at > NOW())
ORDER BY weight DESC, COALESCE(last_used_at, updated_at) DESC NULLS LAST, created_at DESC
LIMIT %s
"""


def _load_profile_rows(user_id: str, limit: int) -> List[Dict]:
    if limit <= 0:
        return []
    return pg_store.query_all(_PROFILE_SQL, (user_id, int(limit)))


def _touch(ids: List[str]) -> None:
    """命中统计：一条语句搞定，不逐条 UPDATE（后面排序/归档会用到 hits / last_used_at）。"""
    if not ids:
        return
    pg_store.execute(
        "UPDATE rag_memory SET hits = hits + 1, last_used_at = NOW(), updated_at = NOW() "
        "WHERE memory_id = ANY(%s)",
        (list(ids),),
    )


def _recall(user_id: str, query: str, exclude: Tuple[str, ...]) -> List[Dict]:
    """Tier B：与当前问题相关的记忆（Qdrant 按 user_id 过滤 → 回 PG 取正文）。"""
    try:
        vector = embedding_client.embed_text(query)
    except Exception as e:
        print(f"[memory] 记忆召回的 embedding 失败，本次跳过向量召回: {e}")
        return []

    hits = memory_vectors.search(user_id, vector, int(cfg.recall_top_k), float(cfg.recall_min_score))
    if not hits:
        return []
    ids = [h["memory_id"] for h in hits if h["memory_id"] not in exclude]
    rows = pg_store.get_memories_by_ids(ids)
    out: List[Dict] = []
    for hit in hits:
        if hit["memory_id"] in exclude:
            # Tier A 已经注入过，不能再进 Tier B（静默跳过，不是异常）
            continue
        row = rows.get(hit["memory_id"])
        if not row:
            # 向量里有、PG 里没有（孤儿向量）或已软删/过期 ⇒ 跳过，不用错内容
            print(f"[memory] 向量里有但 PG 无有效记录，已跳过 memory_id={hit['memory_id']}")
            continue
        out.append({"memory_id": row["memory_id"], "content": _clip(row["content"]), "score": hit["score"]})
        if len(out) >= int(cfg.recall_top_k):
            break
    return out


def load_for_prompt(user_id: Optional[str], chat_id: Optional[str], query: str) -> MemoryContext:
    """问答前取记忆。整段兜异常：记忆挂了也不能影响回答。"""
    ctx = MemoryContext()
    if not enabled() or not user_id:
        return ctx
    try:
        if cfg.enable_summary and chat_id:
            ctx.summary = str(_read_meta(chat_id).get("summary") or "").strip()

        if cfg.enable_profile:
            rows = _load_profile_rows(user_id, int(cfg.profile_limit))
            ctx.profile_ids = [r["memory_id"] for r in rows]
            ctx.profile_lines = [_clip(r["content"]) for r in rows]

        if cfg.enable_vector and query:
            recalled = _recall(user_id, query, tuple(ctx.profile_ids))
            ctx.recall_ids = [r["memory_id"] for r in recalled]
            ctx.recall_lines = [r["content"] for r in recalled]
            ctx.recall_scores = [round(float(r["score"]), 4) for r in recalled]

        _touch(ctx.profile_ids + ctx.recall_ids)
    except Exception as e:
        print(f"[memory] 读取记忆失败，本次不带记忆回答: {e}")
        return MemoryContext()
    return ctx


def get_summary(chat_id: str) -> str:
    """只读会话摘要（问答路径用；写入在 Celery 任务里）。"""
    if not enabled() or not cfg.enable_summary:
        return ""
    return str(_read_meta(chat_id).get("summary") or "").strip()


# ===================== 写：画像条目 =====================

_UPSERT_SQL = """
INSERT INTO rag_memory(
    memory_id, user_id, kind, content, content_hash, weight,
    hits, last_used_at, expires_at, source_chat_id, meta
)
VALUES (%s, %s, %s, %s, %s, %s, 1, NOW(), %s, %s, %s::jsonb)
ON CONFLICT (user_id, content_hash) DO UPDATE
   SET hits = rag_memory.hits + 1,
       last_used_at = NOW(),
       weight = GREATEST(rag_memory.weight, EXCLUDED.weight),
       -- 重新提到同一条 ⇒ 续期（GREATEST 忽略 NULL：TTL 关掉时不会把旧值洗成 NULL）
       expires_at = GREATEST(rag_memory.expires_at, EXCLUDED.expires_at),
       -- 用户手动删过的条目 **不会被自动抽取悄悄加回来**（否则用户会觉得“删了又回来”）；
       -- 自动归档（去重 / 封顶 / 过期）的条目可以复活。
       status = CASE
                  WHEN COALESCE(rag_memory.meta->>'user_deleted', 'false') = 'true'
                  THEN rag_memory.status
                  ELSE 'active'
                END,
       updated_at = NOW()
RETURNING memory_id, status
"""


def _expires_at() -> Optional[str]:
    days = int(cfg.default_ttl_days or 0)
    if days <= 0:
        return None
    return (datetime.now() + timedelta(days=days)).isoformat()


def _upsert_item(user_id: str, kind: str, content: str, weight: float, source_chat_id: Optional[str]) -> Optional[Dict]:
    """写入一条画像并同步向量。返回 {memory_id, content, duplicated}；失败返回 None。

    `ON CONFLICT ... RETURNING memory_id` 会把已存在那条的 id 拿回来 ⇒
    Qdrant 的 point id 与 PG 的 memory_id 始终一致（重复写就是更新向量）。
    """
    content = _clip(content)
    kind = kind if kind in ("profile", "preference") else "preference"
    if not content:
        return None
    try:
        weight = float(weight)
    except Exception:
        weight = 0.5
    weight = min(max(weight, 0.0), 1.0)

    existing_id = pg_store.query_one(
        "SELECT memory_id FROM rag_memory WHERE user_id = %s AND content_hash = %s",
        (user_id, _content_hash(content)),
    )
    duplicated = bool(existing_id)

    row = pg_store.query_one(
        _UPSERT_SQL,
        (
            str(uuid.uuid4()), user_id, kind, content, _content_hash(content), weight,
            _expires_at(), source_chat_id,
            json.dumps({"source": "auto_extract" if source_chat_id else "manual"}, ensure_ascii=False),
        ),
    )
    if not row:
        return None
    memory_id = row["memory_id"]

    if row.get("status") != "active":
        # 用户手动删过 ⇒ 自动抽取不能把它弄回来；顺手把可能存在的向量清掉（不造孤儿向量）
        memory_vectors.delete([memory_id])
        return {"memory_id": memory_id, "content": content, "duplicated": True, "user_deleted": True}

    try:
        vector = embedding_client.embed_text(content)
        memory_vectors.upsert(memory_id, user_id, kind, weight, vector)
    except Exception as e:
        # 向量写失败不影响 PG：Tier A（固定画像）照样能注入，只是这次召回不到它
        print(f"[memory] 记忆向量写入跳过 memory_id={memory_id}: {e}")
    return {"memory_id": memory_id, "content": content, "duplicated": duplicated}


def _archive(user_id: str, memory_ids: List[str], user_deleted: bool = False) -> int:
    """软删（保留审计），同时删向量点 —— 软删条目不该再被语义召回。

    `user_deleted=True` ⇒ 在 meta 里记个标：以后抽取模型再提到同一条内容，
    `ON CONFLICT` 也不会把它复活（用户删过就是删过）。自动归档（去重/封顶）不记这个标。

    ⚠ 返回真正改到的条数（RETURNING），不返回 len(memory_ids)：
    否则“改/删别人的记忆”因为 user_id 跟不到任何行，也会向调用方报成功。
    """
    if not memory_ids:
        return 0
    mark = (
        ", meta = jsonb_set("
        "CASE WHEN jsonb_typeof(meta) = 'object' THEN meta ELSE '{}'::jsonb END, "
        "'{user_deleted}', 'true'::jsonb)"
        if user_deleted
        else ""
    )
    rows = pg_store.query_all(
        f"UPDATE rag_memory SET status = 'archived', updated_at = NOW(){mark} "
        "WHERE user_id = %s AND memory_id = ANY(%s) RETURNING memory_id",
        (user_id, list(memory_ids)),
    )
    changed = [r["memory_id"] for r in rows]
    if changed:
        memory_vectors.delete(changed)
    return len(changed)


def _enforce_cap(user_id: str) -> int:
    """条目数封顶：按 (weight, last_used_at) 保留前 N 条，其余归档。"""
    limit = int(cfg.max_items_per_user or 0)
    if limit <= 0:
        return 0
    rows = pg_store.query_all(
        """
        UPDATE rag_memory
           SET status = 'archived', updated_at = NOW()
         WHERE user_id = %s AND status = 'active'
           AND memory_id NOT IN (
               SELECT memory_id FROM rag_memory
                WHERE user_id = %s AND status = 'active'
                ORDER BY weight DESC, COALESCE(last_used_at, updated_at) DESC NULLS LAST, created_at DESC
                LIMIT %s
           )
        RETURNING memory_id
        """,
        (user_id, user_id, limit),
    )
    ids = [r["memory_id"] for r in rows]
    if ids:
        memory_vectors.delete(ids)
    return len(ids)


# ===================== 抽取与摘要（Celery 里跑）======================

_ACQUIRE_LEASE_SQL = """
UPDATE chat_session
   SET meta = jsonb_set(
           CASE WHEN jsonb_typeof(meta) = 'object' THEN meta ELSE '{}'::jsonb END,
           '{memory_lease_at}',
           to_jsonb((EXTRACT(EPOCH FROM NOW())::bigint))
       )
 WHERE chat_id = %s
   -- CASE 而不是 AND：保证不会先对非法 meta 做 ::bigint 转换（meta 是旧数据/数组时会报错）。
   -- 注意不要用 NULLIF：NULL !~ '...' 结果是 NULL，CASE 会落到 ELSE，
   -- 而 NULL::bigint < ... 也是 NULL ⇒ 没有 meta 的新会话反而永远抢不到租约。
   AND CASE
         WHEN jsonb_typeof(meta) IS DISTINCT FROM 'object' THEN true
         WHEN meta->>'memory_lease_at' IS NULL THEN true
         WHEN meta->>'memory_lease_at' !~ '^[0-9]+$' THEN true
         ELSE (meta->>'memory_lease_at')::bigint < (EXTRACT(EPOCH FROM NOW())::bigint - %s)
       END
RETURNING chat_id
"""


def _acquire_lease(chat_id: str) -> bool:
    """同一会话的记忆任务串行化（单条 UPDATE 原子抢租约，不长期占用连接）。

    不用 advisory lock：那要把 LLM 调用包进同一个事务里，连接会被长时间占住。
    """
    row = pg_store.query_one(_ACQUIRE_LEASE_SQL, (chat_id, LEASE_SECONDS))
    return bool(row)


_RELEASE_LEASE_SQL = """
UPDATE chat_session
   SET meta = jsonb_set(
           CASE WHEN jsonb_typeof(meta) = 'object' THEN meta ELSE '{}'::jsonb END,
           '{memory_lease_at}', '0'::jsonb)
 WHERE chat_id = %s
RETURNING chat_id
"""


def _release_lease(chat_id: str) -> None:
    """任务结束立刻放弃租约（失败也要放），否则后续轮次会一直 skip。"""
    try:
        pg_store.query_one(_RELEASE_LEASE_SQL, (chat_id,))
    except Exception as e:
        print(f"[memory] 释放租约失败 chat_id={chat_id}（等租期自然过期）: {e}")


_EXTRACT_PROMPT = """你在维护一个「用户画像 / 偏好」记忆库，只记录用户本人稳定的信息。

现有条目（memory_id: 内容）：
{existing}

最近对话：
{dialog}

只输出严格 JSON（不要 markdown、不要解释）：
{{"add": [{{"kind": "profile 或 preference", "content": "不超过{max_chars}字的中文短句", "weight": 0.0到1.0}}],
  "remove": ["要作废的现有 memory_id"]}}

规则：
1) profile = 用户是谁 / 做什么领域 / 什么职责（例：用户是做财务的）；
   preference = 回答风格、范围、格式偏好（例：回答要简短；只看 2024 年之后的资料）；
2) 不要记录：一次性的提问、文档里的知识内容、数字与日期等易变信息、助手自己的话；
   尤其不要从一次性提问里推断偏好 —— 用户问“2026 年第三季度北京 GDP 是多少”
   **不等于**“只看 2026 年的资料”，不要记；
3) 语义与现有条目相同 ⇒ 不要重复添加；与现有条目冲突 ⇒ 把旧的 memory_id 放进 remove；
4) 没有值得记的就输出 {{"add": [], "remove": []}}；
5) 最多 {max_items} 条新增；
6) 若只是要把某条旧记忆换个说法：把旧 memory_id 放 remove，**同时给出新的 content**；
   千万不要 remove 和 add 完全相同的同一句话（那等于把这条记忆删掉，用户下次就什么都看不到了）；
7) remove 只能用于**本次对话里用户明确改口 / 纠正 / 要求忘掉**的那一条，且它的主题必须
   与本次对话有关；与本次对话无关的条目一律不要动（宁可不删，也不要删错）。"""

_SUMMARY_PROMPT = """把下面的对话压缩成要点摘要，供后续对话参考（中文，条目式，不超过 {max_tokens} token）。

已有摘要：
{old_summary}

需要并入的新对话：
{new_dialog}

只输出摘要正文。要保留：用户明确要求过的约束、已达成的结论、正在讨论的主题；
不要编造，不要加"以下是摘要"这类套话。"""


# “用户明确要求忘掉”的信号词：命中这些就不再看词面重叠，直接允许 remove
_FORGET_HINTS = ("忘掉", "忘了吧", "别再记", "不要记", "不用记", "删掉这条", "取消刚才", "不再需要记住")

# “这是长期偏好”的信号词（用户自己强调过，才是真偏好）
_PERSIST_CUES = ("以后", "今后", "后续", "一直", "总是", "每次", "都得", "都要", "统一", "一律", "记住", "长期")

# “只看/仅看 + 年份”这类范围限定偏好：很容易从一次性提问里误推断
_YEAR_SCOPE_RE = re.compile(r"(只看|仅看|只关注|只考虑|只要求|只需看).{0,8}(\d{4})")


def _bigrams(text: str) -> set:
    """归一化后的字符二元组集合（中文用二元组就够了）。"""
    s = re.sub(r"[\s\W_]+", "", str(text or "").casefold())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _remove_supported(content: str, dialog: str) -> bool:
    """remove 的依据校验：宁可少删，不能删错。

    实测（本地 qwen2.5:7b）：问两个 GDP 数字，模型就会把“用户是做财务的”放进 remove。
    小模型的 remove 不可信，所以这里加一道廉价的事实依据：
      (a) 对话里出现“忘掉 / 别再记”这类明确要求 ⇒ 直接放行；
      (b) 否则要求被删条目的文字与本次对话有词面重叠（说明真的在谈这件事）。
    例：'回答要简短' vs “以后回答都简短点” ⇒ 重叠命中 '回答' ⇒ 放行；
        '用户是做财务的' vs “北京 GDP 是多少” ⇒ 无重叠 ⇒ 拒绝。
    """
    if any(hint in dialog for hint in _FORGET_HINTS):
        return True
    return bool(_bigrams(content) & _bigrams(dialog))


def _add_supported(content: str, dialog: str) -> bool:
    """add 的依据校验：挡住“从一次性提问里推断出来的偏好”。

    实测（本地 qwen2.5:7b）：用户只问了两个 GDP 数字，模型就凭空记下“只看 2024 年之后的资料”。
    这类时间范围限定最容易误推断，所以只对它加一道确定性校验：
    若内容是“只看/仅看… + 年份”，则本次对话必须出现“以后/一直/总是…”这类长期信号，
    否则不写。其它类型条目不拦（避免误杀真正有用的画像）。
    """
    if not _YEAR_SCOPE_RE.search(str(content or "")):
        return True
    return any(cue in dialog for cue in _PERSIST_CUES)


def _parse_json_object(raw: str) -> Dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_profile(user_id: str, chat_id: str) -> Dict[str, int]:
    meta = _read_meta(chat_id)
    cursor = meta.get("memory_extract_cursor")
    messages = chat_store.list_messages(chat_id)
    fresh = [m for m in messages if _after(m.get("created_at"), cursor)]
    if not fresh:
        return {"added": 0, "removed": 0, "seen": 0}

    existing = _load_profile_rows(user_id, 40)
    existing_text = "\n".join(
        f"{r['memory_id']}: {r['content']}" for r in existing
    ) or "（还没有任何记忆条目）"

    prompt = _EXTRACT_PROMPT.format(
        existing=existing_text,
        dialog=_dialog_text(fresh),
        max_chars=max(int(cfg.profile_max_chars), 12),
        max_items=int(cfg.max_new_items),
    )
    raw = llm_service.chat(prompt, temperature=0.0)
    data = _parse_json_object(raw)
    if not data:
        print(f"[memory] 画像抽取返回不可解析（已跳过）chat_id={chat_id}: {str(raw)[:200]}")

    added = 0
    # 只允许作废“自动抽取”的条目：用户手动加的条目只能用户自己删，
    # 不能让抽取模型“觉得重复”就把人手工记的东西掉到归档里。
    valid_ids = {
        r["memory_id"] for r in existing if (r.get("meta") or {}).get("source") != "manual"
    }

    items = [i for i in (data.get("add") or [])[: int(cfg.max_new_items)] if isinstance(i, dict)]
    add_hashes = set()
    for item in items:
        text = _clip(str(item.get("content") or ""))
        if text:
            add_hashes.add(_content_hash(text))

    hash_of = {r["memory_id"]: _content_hash(r["content"]) for r in existing}
    content_of = {r["memory_id"]: r["content"] for r in existing}
    dialog = _dialog_text(fresh)
    # remove 只认“该用户现有、且是自动抽取”的 id，避免模型乱给 id 误删别人的/手写的记忆；
    # 若 remove 的条目与本次 add 的内容完全相同，直接忽略这个 remove（否则就是“自己删自己”）；
    # 还要过一道“有没有对话依据”的校验（小模型会删与对话无关的条目）。
    removes = []
    for i in (data.get("remove") or []):
        if not isinstance(i, str) or i not in valid_ids:
            continue
        if hash_of.get(i) in add_hashes:
            continue
        if not _remove_supported(content_of.get(i, ""), dialog):
            print(f"[memory] 忽略无依据的 remove（与本次对话无关）memory_id={i} content={content_of.get(i, '')!r}")
            continue
        removes.append(i)
    # ⚠ 顺序要紧：先归档再写。模型很爱在同一轮里“add 同一句 + remove 它的 id”（7B 小模型实测必现），
    # 反过来的顺序会把刚写进去的条目又归档掉 ⇒ active 永远是 0，前端「我的记忆」一条都看不到。
    removed = _archive(user_id, removes)

    for item in items:
        text = str(item.get("content") or "")
        if not _add_supported(_clip(text), dialog):
            print(f"[memory] 忽略从一次性提问推断出的偏好内容是: {text!r}")
            continue
        res = _upsert_item(
            user_id=user_id,
            kind=str(item.get("kind") or "preference"),
            content=str(item.get("content") or ""),
            weight=item.get("weight", 0.6),
            source_chat_id=chat_id,
        )
        if res and not res["duplicated"]:
            added += 1

    capped = _enforce_cap(user_id)

    last = fresh[-1].get("created_at")
    if last is not None:
        _patch_meta(chat_id, memory_extract_cursor=_iso(_to_dt(last) or datetime.now()))
    return {"added": added, "removed": removed, "capped": capped, "seen": len(fresh)}


def _update_summary(chat_id: str) -> Optional[Dict[str, int]]:
    """把"被窗口挤掉、且还没进摘要"的那部分对话并入摘要。

    注意：摘要是**下一轮**才生效（本轮已经读过了），这是刻意的 ——
    在 SSE 路径里同步调 LLM 会把每次提问拖慢一次往返。
    """
    meta = _read_meta(chat_id)
    messages = chat_store.list_messages(chat_id)
    if not messages:
        return None

    ratio = float(cfg.summary_trigger_ratio or 1.0)
    small_budget = int(chat_window.max_total_tokens * min(max(ratio, 0.05), 1.0))
    _kept, dropped = chat_window.split_history(
        messages, budget=small_budget, keep_recent_turns=int(cfg.keep_recent_turns)
    )
    if not dropped:
        return None

    covered = meta.get("summary_cover_at")
    newly = [m for m in dropped if _after(m.get("created_at"), covered)]
    if not newly:
        return None

    prompt = _SUMMARY_PROMPT.format(
        max_tokens=int(cfg.summary_max_tokens),
        old_summary=str(meta.get("summary") or "（无）").strip(),
        new_dialog=_dialog_text(newly, line_chars=240),
    )
    text = str(llm_service.chat(prompt, temperature=0.2, max_tokens=int(cfg.summary_max_tokens)) or "").strip()
    if not text:
        print(f"[memory] 摘要生成返回空，保留旧摘要 chat_id={chat_id}")
        return None

    # 游标按"已处理的最后一条消息"推进：下一轮只压新增的部分，摘要不会无限膨胀
    cover_dt = _to_dt(dropped[-1].get("created_at")) or datetime.now()
    _patch_meta(
        chat_id,
        summary=text[:4000],
        summary_cover_at=_iso(cover_dt),
        summary_updated_at=_iso(datetime.now()),
    )
    return {"covered": len(newly)}


def run_update(user_id: str, chat_id: str) -> Dict[str, Any]:
    """Celery 任务体：先抢租约，再做摘要 + 画像抽取。任何异常由调用方兜住。"""
    if not enabled() or not user_id or not chat_id:
        return {"skipped": "memory off"}
    if not cfg.enable_summary and not cfg.enable_profile:
        # 总开关开着但子能力全关 ⇒ 不抢租约、不记日志，直接给出原因（避免“看起来没反应”）
        return {"skipped": "memory.enable=true 但 enable_summary / enable_profile 都是 false，无能力可跑"}
    if not _acquire_lease(chat_id):
        return {"skipped": "another memory task is running"}

    stats: Dict[str, Any] = {}
    try:
        if cfg.enable_summary:
            try:
                stats["summary"] = _update_summary(chat_id)
            except Exception as e:
                print(f"[memory] 会话摘要更新失败 chat_id={chat_id}: {e}")
        if cfg.enable_profile:
            try:
                stats["profile"] = _extract_profile(user_id, chat_id)
            except Exception as e:
                print(f"[memory] 画像抽取失败 user_id={user_id}: {e}")
    finally:
        # 跑完就把租约放开：10 分钟租期是给“真并发/任务猝死”兜底的，
        # 若每轮都把它占满一次，用户连续提问时后续每一轮都会被 skip（实测 1 分钟后再问就跳过了）。
        _release_lease(chat_id)
    return stats


# ===================== 给用户端接口用的读写 =====================

def list_for_user(user_id: str, limit: int = 100, offset: int = 0, kind: Optional[str] = None) -> Tuple[List[Dict], int]:
    where = "WHERE user_id = %s AND status = 'active'"
    params: List[Any] = [user_id]
    if kind in ("profile", "preference"):
        where += " AND kind = %s"
        params.append(kind)
    rows = pg_store.query_all(
        f"""
        SELECT memory_id, kind, content, weight, hits, last_used_at, expires_at, source_chat_id, created_at, updated_at
        FROM rag_memory {where}
        ORDER BY weight DESC, COALESCE(last_used_at, updated_at) DESC NULLS LAST, created_at DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params + [int(limit), int(offset)]),
    )
    total_row = pg_store.query_one(f"SELECT COUNT(*) AS cnt FROM rag_memory {where}", tuple(params))
    return rows, int(total_row["cnt"]) if total_row else 0


def count_archived(user_id: str) -> int:
    """已归档（自动整理 / 用户删除）的条数。

    只给前端做提示用：用户看不到这些条目，但知道“历史上有 N 条”比看到一个突兀的 0 条靠谱，
    也能区出“真的没有”和“全被自动归档了”。
    """
    row = pg_store.query_one(
        "SELECT COUNT(*) AS c FROM rag_memory WHERE user_id = %s AND status = 'archived'",
        (user_id,),
    )
    return int(row["c"]) if row else 0


def create_manual(user_id: str, kind: str, content: str) -> Optional[Dict]:
    """用户手动加一条（走同一个 content_hash，重复内容不会堆条目）。"""
    res = _upsert_item(user_id=user_id, kind=kind or "preference", content=content, weight=0.9, source_chat_id=None)
    if not res:
        return None
    _enforce_cap(user_id)
    return pg_store.query_one(
        "SELECT memory_id, kind, content, weight, hits, last_used_at, expires_at, created_at "
        "FROM rag_memory WHERE user_id = %s AND memory_id = %s",
        (user_id, res["memory_id"]),
    )


def update_memory(user_id: str, memory_id: str, content: Optional[str] = None, status: Optional[str] = None) -> Optional[Dict]:
    """改内容 ⇒ 旧的归档 + 新建一条（保留审计轨迹，也避免 hash 与内容不一致）；
    只改状态 ⇒ 直接改，并同步向量点。
    """
    row = pg_store.query_one(
        "SELECT memory_id, kind, content, weight FROM rag_memory WHERE user_id = %s AND memory_id = %s",
        (user_id, memory_id),
    )
    if not row:
        return None

    if content and _normalize(content) != _normalize(row["content"]):
        _archive(user_id, [memory_id], user_deleted=True)
        return create_manual(user_id, row["kind"], content)

    if status in ("active", "archived") and status != "active":
        _archive(user_id, [memory_id], user_deleted=True)
    elif status == "active":
        pg_store.execute(
            "UPDATE rag_memory SET status = 'active', updated_at = NOW() WHERE user_id = %s AND memory_id = %s",
            (user_id, memory_id),
        )
        try:
            vector = embedding_client.embed_text(row["content"])
            memory_vectors.upsert(memory_id, user_id, row["kind"], row["weight"], vector)
        except Exception as e:
            print(f"[memory] 重新启用时向量写入失败 memory_id={memory_id}: {e}")

    return pg_store.query_one(
        "SELECT memory_id, kind, content, weight, hits, last_used_at, expires_at, status, created_at, updated_at "
        "FROM rag_memory WHERE user_id = %s AND memory_id = %s",
        (user_id, memory_id),
    )


def delete_memory(user_id: str, memory_id: str) -> bool:
    return _archive(user_id, [memory_id], user_deleted=True) > 0


def clear_all(user_id: str) -> int:
    rows = pg_store.query_all(
        "SELECT memory_id FROM rag_memory WHERE user_id = %s AND status = 'active'", (user_id,)
    )
    return _archive(user_id, [r["memory_id"] for r in rows], user_deleted=True)


def purge_user(user_id: str) -> int:
    """账号被删除时彻底清掉这个用户的记忆（硬删 + 删向量）。

    跟 `clear_all`（用户自助清空，软删留审计）不同：账号都没了，画像与偏好没有留存的道理。
    """
    rows = pg_store.query_all("SELECT memory_id FROM rag_memory WHERE user_id = %s", (user_id,))
    ids = [r["memory_id"] for r in rows]
    if not ids:
        return 0
    pg_store.execute("DELETE FROM rag_memory WHERE user_id = %s", (user_id,))
    memory_vectors.delete(ids)
    return len(ids)


# 会话摘要也随会话删除而消失：摘要存在 chat_session.meta 里，
# 删会话时 chat_message 有 ON DELETE CASCADE，meta 跟着会话行走 ⇒ 无需额外清理。


def log_config(prefix: str = "[memory]") -> None:
    """启动时打印生效的记忆开关（API 与 celery worker 都会走到）。

    存在的理由：`enable: true` 但子开关全 false 是个完全静默的空转，看过一次就知道配错了；
    不打印任何敏感值。
    """
    tail = ""
    if cfg.enable and not (cfg.enable_summary or cfg.enable_profile or cfg.enable_vector):
        tail = "  ⚠ enable=true 但 enable_summary/enable_profile/enable_vector 都是 false ⇒ 记忆实际不会工作"
    print(
        f"{prefix} enable={cfg.enable}｜短期滚动摘要={cfg.enable_summary}｜画像条目={cfg.enable_profile}"
        f"｜向量召回={cfg.enable_vector}（阈值 {cfg.recall_min_score}）"
        f"｜每人最多 {cfg.max_items_per_user} 条｜TTL {cfg.default_ttl_days} 天"
        f"｜窗口 {settings.chat.max_total_tokens} tokens{tail}"
    )


log_config()
