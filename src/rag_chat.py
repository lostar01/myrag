from retrieval import retrieve
from rerank_service import rerank_service
from llm_service import llm_service
from qa_log_service import qa_log_service
import time

def build_context_prompt(query: str, chunks):
    context_lines = []
    for item in chunks:
        context_lines.append(
            f"【章节路径】{item['section_path']} | 页码：{item['page_no']}\n"
            f"{item['content']}"
        )
    context = "\n\n".join(context_lines)

    prompt = f"""你是专业知识库助手，请严格基于下面提供的参考资料回答用户问题。
如果参考资料里没有对应的信息，直接回答“知识库中未找到相关内容”，禁止编造、臆测。

## 参考资料
{context}

## 用户问题
{query}
"""
    return prompt

def rag_chat(query: str):
    t0 = time.time()

    top_k_recall = 6
    top_k_rerank = 3

    # 1.向量粗召回
    chunks = retrieve(query, top_k=top_k_recall)
    if not chunks:
        return {"answer": "知识库未找到相关内容", "sources": []}
    # 2.重排
    chunks = rerank_service.rerank(query, chunks, top_n=top_k_rerank)

    # 获取 chunk_ids 和 doc_ids
    chunk_ids = [item["chunk_id"] for item in chunks]
    doc_ids = list({item["doc_id"] for item in chunks})
    rerank_scores = [item["rerank_score"] for item in chunks]

    if not chunks:
        return {"answer": "知识库未找到相关内容", "sources": []}
    # 3.构造prompt
    prompt = build_context_prompt(query, chunks)
    # 4.LLM生成
    answer = llm_service.chat(prompt)

    # 5.计时
    cost_ms = int((time.time() - t0) * 1000)

    # 6.写入日志
    log_id = qa_log_service.log_qa(
        query=query,
        answer=answer,
        chunk_ids=chunk_ids,
        rerank_scores=rerank_scores,
        doc_ids=doc_ids,
        cost_ms=cost_ms,
        meta={
            "top_k_recall": top_k_recall,
            "top_k_rerank": top_k_rerank
        }
    )

    return {
        "answer": answer,
        "sources": chunks
    }

# 测试
if __name__ == "__main__":
    result = rag_chat("经济的要素是什么？")
    print("回答：", result["answer"])
    print("引用来源：")
    for s in result["sources"]:
        print(f"- {s['section_path']} 页码{s['page_no']}")
