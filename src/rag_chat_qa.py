"""会话问答入口：事件流 + 写库 + 问答日志 + 首问自动命名。

prompt / 检索 / 重排 / 上下文裁剪全部在 `rag_prompt`（D7-b），这里只关心
「一次问答怎么呈现给用户、怎么落库」。`chat_qa`（非流式）直接消费
`chat_qa_stream`，两条路径共享同一份检索与 prompt 逻辑，不会再出现行为分叉。
"""

import time

import rag_prompt
from chat_store import chat_store
from chat_window import chat_window
from llm_service import llm_service
from qa_log_service import qa_log_service
from rag_prompt import (
    DEFAULT_RECALL_TOP_K,
    DEFAULT_RERANK_TOP_K,
    NO_ANSWER,
)
from settings import settings
from title_service import generate_title, is_placeholder_title


def _should_autotitle(chat_id: str, has_history: bool) -> bool:
    """只在「新会话的第一问」且标题还是占位值时自动命名。"""
    if has_history:
        return False
    try:
        session = chat_store.get_session(chat_id)
    except Exception as e:
        print(f"[rag_chat_qa] 读取会话失败，跳过自动命名 chat_id={chat_id}: {e}")
        return False
    return bool(session) and is_placeholder_title(session.get("title"))


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
            "top_k_rerank": DEFAULT_RERANK_TOP_K,
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
        },
    )


def chat_qa(chat_id: str, query: str):
    """非流式问答：消费 chat_qa_stream，返回与重构前完全一致的字典结构。"""
    answer = ""
    sources = []
    title = None

    for event in chat_qa_stream(chat_id=chat_id, query=query):
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


def chat_qa_stream(chat_id: str, query: str):
    """流式问答：先做召回/重排/构造 prompt，再逐 token yield。

    yield 的事件为 dict：
      {"type": "sources", "sources": [...]}   # 首帧返回引用来源
      {"type": "delta", "content": "..."}      # 文本增量
      {"type": "done", "answer": "...", "chat_id": ...}  # 回答结束
      {"type": "title", "title": "...", "chat_id": ...} # 仅新会话第一问：自动命名的标题
    """
    t0 = time.time()

    # 1. 读取历史 + 裁剪窗口
    raw_history = chat_store.list_messages(chat_id)
    history = chat_window.trim_history(raw_history)
    autotitle = _should_autotitle(chat_id, has_history=bool(raw_history))

    # 2. query rewrite → 召回 → 重排 → 组装 prompt（全部在 rag_prompt）
    prepared = rag_prompt.prepare_answer(history=history, query=query)
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
             query_for_search=query_for_search)

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
