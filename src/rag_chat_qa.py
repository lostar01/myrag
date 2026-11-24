from retrieval import retrieve
from rerank_client import rerank_client
from llm_service import llm_service
from qa_log_service import qa_log_service
from chat_store import chat_store
from chat_window import chat_window
from title_service import generate_title, is_placeholder_title
import time


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

def chat_qa(chat_id: str, query: str):
    # 定义召回与重排数
    top_k_recall = 6
    top_k_rerank = 3
    
    t0 = time.time()

    # 1. 读取历史 + 裁剪窗口
    raw_history = chat_store.list_messages(chat_id)
    history = chat_window.trim_history(raw_history)
    autotitle = _should_autotitle(chat_id, has_history=bool(raw_history))

    # ========== Query Rewrite：多轮对话下把问题改写成独立可检索的问题 ==========
    if history:
        history_text_for_rewrite = ""
        for h in history:
            history_text_for_rewrite += f"{h['role']}: {h['content']}\n"
        rewrite_prompt = (
            "根据对话历史，把用户当前问题改写成独立、完整、可检索的问题，"
            "只输出改写后的问题，不要任何解释：\n"
            f"历史：\n{history_text_for_rewrite}\n用户问题：{query}"
        )
        try:
            rewritten = llm_service.simple_call(rewrite_prompt).strip()
            query_for_search = rewritten if rewritten else query
        except Exception:
            query_for_search = query
    else:
        query_for_search = query
    # ==================================================

    # 2. 召回 + 重排（和你原有逻辑不变）
    recall_chunks = retrieve(query_for_search, top_k=top_k_recall)
    if not recall_chunks:
        # 空召回保护：避免后续 context 未定义 / 无意义调用 LLM
        chat_store.add_msg(chat_id, "user", query)
        chat_store.add_msg(chat_id, "assistant", "知识库中未找到相关内容")
        title = _apply_title(chat_id, query) if autotitle else None
        result = {
            "chat_id": chat_id,
            "answer": "知识库中未找到相关内容",
            "history": chat_store.list_messages(chat_id),
            "sources": []
        }
        if title:
            result["title"] = title
        return result
    rerank_chunks = rerank_client.rerank(query_for_search, recall_chunks, top_n=top_k_rerank)
    chunk_ids = [c["chunk_id"] for c in rerank_chunks]

    # 3. 构造上下文参考（按 token 预算裁剪，避免 prompt 超模型上限）
    context_lines = []
    for item in rerank_chunks:
        context_lines.append(
                f"【章节路径】{item['section_path']} | 页码：{item['page_no']}\n"
                f"{item['content']}"
            )

    # 4. 把历史对话塞进prompt
    history_text = ""
    for h in history:
        history_text += f"{h['role']}: {h['content']}\n"

    # 计算已被历史、问题、prompt 模板占用的 token，剩余预算留给参考资料
    prompt_template = """你是专业知识库助手，请严格基于下面提供的参考资料回答用户问题。
如果参考资料里没有对应的信息，直接回答“知识库中未找到相关内容”，禁止编造、臆测。

## 历史对话
{history_text}

## 参考资料
{context}

## 用户当前问题
{query}
"""
    reserved_tokens = chat_window.count_tokens(
        prompt_template.format(history_text=history_text, context="", query=query)
    )
    context_lines = chat_window.trim_context(context_lines, reserved_tokens)
    context = "\n\n".join(context_lines)

    prompt = prompt_template.format(
        history_text=history_text, context=context, query=query
    )
    answer = llm_service.chat(prompt)
    cost_ms = int((time.time() - t0)*1000)

    # 5. 保存消息
    chat_store.add_msg(chat_id, "user", query)
    chat_store.add_msg(chat_id, "assistant", answer)

    # 6. 写入qa日志（复用你原来的日志）
    qa_log_service.log_qa(
        query=query,
        answer=answer,
        chunk_ids=chunk_ids,
        rerank_scores=[c["rerank_score"] for c in rerank_chunks],
        doc_ids=[c["doc_id"] for c in rerank_chunks],
        cost_ms=cost_ms,
        meta={
            "top_k_recall":top_k_recall,
            "top_k_rerank":top_k_rerank,
            "chat_id": chat_id
        }
    )

    # 7. 新会话的第一问：自动命名（失败不影响返回）
    result = {
        "chat_id": chat_id,
        "answer": answer,
        "history": chat_store.list_messages(chat_id),
        "sources": rerank_chunks
    }
    if autotitle:
        title = _apply_title(chat_id, query)
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
    top_k_recall = 6
    top_k_rerank = 3
    t0 = time.time()

    raw_history = chat_store.list_messages(chat_id)
    history = chat_window.trim_history(raw_history)
    autotitle = _should_autotitle(chat_id, has_history=bool(raw_history))

    # Query Rewrite
    if history:
        history_text_for_rewrite = ""
        for h in history:
            history_text_for_rewrite += f"{h['role']}: {h['content']}\n"
        rewrite_prompt = (
            "根据对话历史，把用户当前问题改写成独立、完整、可检索的问题，"
            "只输出改写后的问题，不要任何解释：\n"
            f"历史：\n{history_text_for_rewrite}\n用户问题：{query}"
        )
        try:
            rewritten = llm_service.simple_call(rewrite_prompt).strip()
            query_for_search = rewritten if rewritten else query
        except Exception:
            query_for_search = query
    else:
        query_for_search = query

    recall_chunks = retrieve(query_for_search, top_k=top_k_recall)
    if not recall_chunks:
        answer = "知识库中未找到相关内容"
        chat_store.add_msg(chat_id, "user", query)
        chat_store.add_msg(chat_id, "assistant", answer)
        yield {"type": "sources", "sources": []}
        yield {"type": "delta", "content": answer}
        # 先发 done，再做自动命名：标题生成（LLM）不会拖慢回答的呈现
        yield {"type": "done", "answer": answer, "chat_id": chat_id}
        if autotitle:
            title = _apply_title(chat_id, query)
            if title:
                yield {"type": "title", "title": title, "chat_id": chat_id}
        return

    rerank_chunks = rerank_client.rerank(query_for_search, recall_chunks, top_n=top_k_rerank)
    chunk_ids = [c["chunk_id"] for c in rerank_chunks]

    context_lines = []
    for item in rerank_chunks:
        context_lines.append(
            f"【章节路径】{item['section_path']} | 页码：{item['page_no']}\n"
            f"{item['content']}"
        )

    history_text = ""
    for h in history:
        history_text += f"{h['role']}: {h['content']}\n"

    prompt_template = """你是专业知识库助手，请严格基于下面提供的参考资料回答用户问题。
如果参考资料里没有对应的信息，直接回答“知识库中未找到相关内容”，禁止编造、臆测。

## 历史对话
{history_text}

## 参考资料
{context}

## 用户当前问题
{query}
"""
    reserved_tokens = chat_window.count_tokens(
        prompt_template.format(history_text=history_text, context="", query=query)
    )
    context_lines = chat_window.trim_context(context_lines, reserved_tokens)
    context = "\n\n".join(context_lines)

    prompt = prompt_template.format(
        history_text=history_text, context=context, query=query
    )

    # 先返回引用来源
    yield {"type": "sources", "sources": rerank_chunks}

    answer_parts = []
    for delta in llm_service.chat_stream(prompt):
        answer_parts.append(delta)
        yield {"type": "delta", "content": delta}

    answer = "".join(answer_parts)
    cost_ms = int((time.time() - t0) * 1000)

    chat_store.add_msg(chat_id, "user", query)
    chat_store.add_msg(chat_id, "assistant", answer)

    qa_log_service.log_qa(
        query=query,
        answer=answer,
        chunk_ids=chunk_ids,
        rerank_scores=[c["rerank_score"] for c in rerank_chunks],
        doc_ids=[c["doc_id"] for c in rerank_chunks],
        cost_ms=cost_ms,
        meta={
            "top_k_recall": top_k_recall,
            "top_k_rerank": top_k_rerank,
            "chat_id": chat_id,
            "stream": True,
        },
    )

    # 先发 done，再自动命名标题（LLM 调用不阻塞回答呈现）
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
