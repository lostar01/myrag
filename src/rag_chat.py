"""单轮问答的薄壳：不建会话、不写 chat_message，给 eval / 脚本用。

产品链路走 `rag_chat_qa`（会话 + 历史 + 流式）。这里复用 `rag_prompt` 里同一套
query rewrite / 召回 / 重排 / prompt（D7-b），这样 `rag_eval.py` 量到的就是线上
那套 prompt，不再是另一份拷贝。

与重构前的差异（有意）：
  - prompt 多了「## 历史对话」段（单轮时为空），与线上完全一致；
  - 多了按 token 预算裁剪参考资料（以前这条路径不裁剪）；
  - 无可用资料时的固定回答统一成 rag_prompt.NO_ANSWER。
"""

import time
from typing import Optional

import memory_service
import rag_prompt
from llm_service import llm_service
from qa_log_service import qa_log_service
from rag_prompt import DEFAULT_RECALL_TOP_K, DEFAULT_RERANK_TOP_K, NO_ANSWER
from settings import settings


def rag_chat(query: str, chat_id: Optional[str] = None, user_id: Optional[str] = None):
    """单轮问答。默认**不带记忆**（与重构前同口径，eval 基线可比）。

    要做“开记忆”的 A/B：传 chat_id + user_id（两个都传才启用），
    此时会像线上一样注入画像 / 相关记忆 / 会话摘要。
    """
    t0 = time.time()

    memory_provider = None
    if chat_id and user_id and memory_service.enabled():
        memory_provider = lambda q: memory_service.load_for_prompt(user_id, chat_id, q)

    # 召回 + 重排 + 组装 prompt（阈值取 config.yaml 的 retrieval 段）
    prepared = rag_prompt.prepare_answer(history=[], query=query, memory_provider=memory_provider)
    if prepared is None:
        return {"answer": NO_ANSWER, "sources": []}

    sources = prepared.sources
    answer = llm_service.chat(prepared.prompt)
    cost_ms = int((time.time() - t0) * 1000)

    # 写入日志（与重构前同样的字段，方便 eval / 报表复用）
    qa_log_service.log_qa(
        query=query,
        answer=answer,
        chunk_ids=[item["chunk_id"] for item in sources],
        rerank_scores=[item["rerank_score"] for item in sources],
        doc_ids=list({item["doc_id"] for item in sources}),
        cost_ms=cost_ms,
        meta={
            "top_k_recall": DEFAULT_RECALL_TOP_K,
            "top_k_rerank": DEFAULT_RERANK_TOP_K,
            # 与线上日志保持一致的分数/阈值字段，方便用 eval 结果定标（#6）
            "cosine_scores": [item.get("score") for item in sources],
            "thresholds": {
                "cosine_min_score": settings.retrieval.cosine_min_score,
                "rerank_min_score": settings.retrieval.rerank_min_score,
            },
            # 开记忆 A/B 时方便对比：本次注入了哪些记忆条目
            "memory_profile_ids": list(getattr(prepared.memory_ctx, "profile_ids", []) or []),
            "memory_recall_ids": list(getattr(prepared.memory_ctx, "recall_ids", []) or []),
        },
    )

    return {
        "answer": answer,
        "sources": sources,
    }


# 测试
if __name__ == "__main__":
    result = rag_chat("社会的阶层是怎么样的？")
    print("回答：", result["answer"])
    print("引用来源：")
    for s in result["sources"]:
        print(f"- {s['section_path']} 页码{s['page_no']}")
