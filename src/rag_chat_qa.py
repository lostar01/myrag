from retrieval import retrieve
from rerank_service import rerank_service
from llm_service import llm_service
from qa_log_service import qa_log_service
from chat_store import chat_store
from chat_window import chat_window
import time

def chat_qa(chat_id: str, query: str):
    # 定义召回与重排数
    top_k_recall = 6
    top_k_rerank = 3
    
    t0 = time.time()
    
    # 1. 读取历史 + 裁剪窗口
    raw_history = chat_store.list_messages(chat_id)
    history = chat_window.trim_history(raw_history)

    # ========== 可选：Query Rewrite（推荐加上）==========
    # rewrite_prompt = f"根据对话历史，把用户问题改写成独立可检索的问题，只输出改写后的问题：\n历史：{history}\n用户问题：{query}"
    # query_for_search = llm_service.simple_call(rewrite_prompt)
    query_for_search = query
    # ==================================================

    # 2. 召回 + 重排（和你原有逻辑不变）
    recall_chunks = retrieve(query_for_search, top_k=top_k_recall)
    rerank_chunks = rerank_service.rerank(query_for_search, recall_chunks, top_n=top_k_rerank)
    chunk_ids = [c["chunk_id"] for c in rerank_chunks]

    # 3. 构造上下文参考
    context_lines = []
    for item in rerank_chunks:
        context_lines.append(
                f"【章节路径】{item['section_path']} | 页码：{item['page_no']}\n"
                f"{item['content']}"
            )
        context = "\n\n".join(context_lines)

    # 4. 把历史对话塞进prompt
    history_text = ""
    for h in history:
        history_text += f"{h['role']}: {h['content']}\n"

    prompt = f"""你是专业知识库助手，请严格基于下面提供的参考资料回答用户问题。
如果参考资料里没有对应的信息，直接回答“知识库中未找到相关内容”，禁止编造、臆测。

## 历史对话
{history_text}

## 参考资料
{context}

## 用户当前问题
{query}
"""
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
        rerank_scores=[c["score"] for c in rerank_chunks],
        doc_ids=[c["doc_id"] for c in rerank_chunks],
        cost_ms=cost_ms,
        meta={
            "top_k_recall":top_k_recall,
            "top_k_rerank":top_k_rerank,
            "chat_id": chat_id
        }
    )
    return {
        "chat_id": chat_id,
        "answer": answer,
        "history": chat_store.list_messages(chat_id),
        "sources": rerank_chunks
    }

if __name__ == "__main__":
    chat_id = chat_store.create_session()
    result = chat_qa(chat_id=chat_id, query="经济的要素是什么？")
    print("回答：", result["answer"])
    print("引用来源：")
    for s in result["sources"]:
        print(f"- {s['section_path']} 页码{s['page_no']}")