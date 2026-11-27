"""会话问答入口：事件流 + 写库 + 问答日志 + 首问自动命名。

prompt / 检索 / 重排 / 上下文裁剪全部在 `rag_prompt`（D7-b），这里只关心
「一次问答怎么呈现给用户、怎么落库」。`chat_qa`（非流式）直接消费
`chat_qa_stream`，两条路径共享同一份检索与 prompt 逻辑，不会再出现行为分叉。
"""

import time
from typing import Optional

import memory_service
import rag_prompt
from chat_store import chat_store
from chat_window import chat_window
from llm_service import llm_service
from qa_log_service import qa_log_service
from rag_prompt import (
    DEFAULT_RECALL_TOP_K,
    DEFAULT_RERANK_TOP_N,
    NO_ANSWER,
)
from settings import settings
from title_service import generate_title, is_placeholder_title


def _should_autotitle(session: Optional[dict], has_history: bool) -> bool:
    """只在「新会话的第一问」且标题还是占位值时自动命名。

    会话行由调用方取一次传进来（记忆也要用同一行），不要查两遍。
    """
    if has_history or not session:
        return False
    return is_placeholder_title(session.get("title"))


def _dispatch_memory_task(user_id: Optional[str], chat_id: str) -> None:
    """回答结束后异步更新记忆（会话摘要 + 画像抽取）。

    延迟 import：eval（rag_chat / rag_eval）不该被 Celery 与 docling 链路拖起来；
    提交失败只打日志 —— 记忆是附属能力，不能影响已经返回的答案。
    """
    if not memory_service.enabled() or not user_id:
        return
    try:
        from tasks import memory_update_task

        memory_update_task.apply_async(kwargs={"user_id": user_id, "chat_id": chat_id})
    except Exception as e:
        print(f"[rag_chat_qa] 记忆任务提交失败（本次不更新记忆）chat_id={chat_id}: {e}")


def _apply_title(chat_id: str, query: str):
    """生成并写入会话标题，返回标题；任何异常不影响问答主流程。"""
    try:
        title = generate_title(query)
        chat_store.update_session(chat_id, title=title)
        return title
    except Exception as e:
        print(f"[rag_chat_qa] 自动命名会话失败 chat_id={chat_id}: {e}")
        return None


def _log_qa(
    query: str,
    answer: str,
    sources,
    cost_ms: int,
    chat_id: str,
    stream: bool,
    query_for_search: str = None,
    no_answer: bool = False,
    memory_ctx=None,
):
    """写问答日志（chunk_ids / rerank 分数 / doc_ids + meta）。

    meta 里额外记 cosine 分数与当前阈值（#6）：事后用 SQL 看分布就能定标，
    不需要单独的探针脚本：
      SELECT meta->'cosine_scores', rerank_scores FROM rag_qa_log ORDER BY created_at DESC LIMIT 50;
    """
    qa_log_service.log_qa(
        query=query,
        answer=answer,
        chunk_ids=[c["chunk_id"] for c in sources],
        rerank_scores=[c["rerank_score"] for c in sources],
        doc_ids=[c["doc_id"] for c in sources],
        cost_ms=cost_ms,
        meta={
            "top_k_recall": DEFAULT_RECALL_TOP_K,
            "top_k_rerank": DEFAULT_RERANK_TOP_N,
            "chat_id": chat_id,
            "stream": stream,
            # 实际用于检索的问题（多轮时可能是改写后的），方便事后排查召回问题
            "query_for_search": query_for_search or query,
            # 召回相似度分数（与 rerank_scores 对照着看，用来调阈值）
            "cosine_scores": [c.get("score") for c in sources],
            "thresholds": {
                "cosine_min_score": settings.retrieval.cosine_min_score,
                "rerank_min_score": settings.retrieval.rerank_min_score,
            },
            # True = 本次没有任何可用参考资料（召回为空或全被阈值刷掉）
            "no_answer": no_answer,
            # 记忆：用了哪些条目、向量召回分数、是否用到了会话摘要
            # （事后用 SQL 看分布就能调 memory.recall_min_score / profile_limit）
            "memory_profile_ids": list(getattr(memory_ctx, "profile_ids", []) or []),
            "memory_recall_ids": list(getattr(memory_ctx, "recall_ids", []) or []),
            "memory_scores": list(getattr(memory_ctx, "recall_scores", []) or []),
            "summary_used": bool(getattr(memory_ctx, "summary", "")),
        },
    )


def chat_qa(chat_id: str, query: str, user_id: Optional[str] = None):
    """非流式问答：消费 chat_qa_stream，返回与重构前完全一致的字典结构。"""
    answer = ""
    sources = []
    title = None

    for event in chat_qa_stream(chat_id=chat_id, query=query, user_id=user_id):
        etype = event.get("type")
        if etype == "sources":
            sources = event.get("sources") or []
        elif etype == "done":
            answer = event.get("answer", "")
        elif etype == "title":
            title = event.get("title")

    result = {
        "chat_id": chat_id,
        "answer": answer,
        "history": chat_store.list_messages(chat_id),
        "sources": sources,
    }
    if title:
        result["title"] = title
    return result


def chat_qa_stream(chat_id: str, query: str, user_id: Optional[str] = None):
    """流式问答：先做记忆/召回/重排/构造 prompt，再逐 token yield。

    `user_id`：不传则从会话行里取（记忆必须知道是谁的 ⇒ 取不到就本次不用记忆）。

    yield 的事件为 dict：
      {"type": "sources", "sources": [...]}   # 首帧返回引用来源
      {"type": "delta", "content": "..."}      # 文本增量
      {"type": "done", "answer": "...", "chat_id": ...}  # 回答结束
      {"type": "title", "title": "...", "chat_id": ...} # 仅新会话第一问：自动命名的标题
    """
    # 会话行只取一次：标题（自动命名用）与 user_id（记忆用）共用同一行
    try:
        session = chat_store.get_session(chat_id)
    except Exception as e:
        print(f"[rag_chat_qa] 读取会话失败 chat_id={chat_id}: {e}")
        session = None
    if not user_id:
        user_id = (session or {}).get("user_id")

    t0 = time.time()

    # 1. 读取历史 + 裁剪窗口
    raw_history = chat_store.list_messages(chat_id)
    history = chat_window.trim_history(raw_history)
    autotitle = _should_autotitle(session, has_history=bool(raw_history))

    # 2. query rewrite → 记忆 → 召回 → 重排 → 组装 prompt（全部在 rag_prompt）
    #    记忆要用“改写后的问题”去召回，所以做成回调，由 prepare_answer 在 rewrite 之后调
    prepared = rag_prompt.prepare_answer(
        history=history,
        query=query,
        memory_provider=(
            (lambda q: memory_service.load_for_prompt(user_id, chat_id, q))
            if memory_service.enabled()
            else None
        ),
    )
    query_for_search = prepared.query_for_search if prepared else query

    # 3. 没有任何可用参考资料（召回为空 / 全部低于阈值）：固定回答，不调 LLM
    if prepared is None:
        answer = NO_ANSWER
        chat_store.add_msg(chat_id, "user", query)
        chat_store.add_msg(chat_id, "assistant", answer)
        # 也写日志：方便统计“未找到”占比、并看阈值是不是调得太严
        _log_qa(
            query, answer, [], int((time.time() - t0) * 1000), chat_id,
            stream=True, query_for_search=query_for_search, no_answer=True,
        )
        # 没答上也要跑记忆：用户的问题里往往就带着偏好（“只看 2024 年之后的”）
        _dispatch_memory_task(user_id, chat_id)
        yield {"type": "sources", "sources": []}
        yield {"type": "delta", "content": answer}
        # 先发 done，再做自动命名：标题生成（LLM）不会拖慢回答的呈现
        yield {"type": "done", "answer": answer, "chat_id": chat_id}
        if autotitle:
            title = _apply_title(chat_id, query)
            if title:
                yield {"type": "title", "title": title, "chat_id": chat_id}
        return

    # 4. 先返回引用来源
    yield {"type": "sources", "sources": prepared.sources}

    # 5. 流式生成
    answer_parts = []
    for delta in llm_service.chat_stream(prepared.prompt):
        answer_parts.append(delta)
        yield {"type": "delta", "content": delta}

    answer = "".join(answer_parts)
    cost_ms = int((time.time() - t0) * 1000)

    # 6. 保存消息
    chat_store.add_msg(chat_id, "user", query)
    chat_store.add_msg(chat_id, "assistant", answer)

    # 7. 写入qa日志
    _log_qa(query, answer, prepared.sources, cost_ms, chat_id, stream=True,
             query_for_search=query_for_search, memory_ctx=prepared.memory_ctx)

    # 8. 异步更新记忆（摘要 + 画像）：消息已入库，任务里读到的一定是本轮完整对话
    _dispatch_memory_task(user_id, chat_id)

    # 8. 先发 done，再自动命名标题（LLM 调用不阻塞回答呈现）
    yield {"type": "done", "answer": answer, "chat_id": chat_id}
    if autotitle:
        title = _apply_title(chat_id, query)
        if title:
            yield {"type": "title", "title": title, "chat_id": chat_id}


if __name__ == "__main__":
    chat_id = chat_store.create_session()
    result = chat_qa(chat_id=chat_id, query="经济的要素是什么？")
    print("回答：", result["answer"])
    print("引用来源：")
    for s in result["sources"]:
        print(f"- {s['section_path']} 页码{s['page_no']}")
