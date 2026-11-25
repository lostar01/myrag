"""RAG 问答链路的单一实现：query rewrite → 召回 → 重排 → 上下文裁剪 → prompt。

背景（D7-b）：prompt 模板、query rewrite、context 组装原本在 `rag_chat.py`、
`chat_qa`、`chat_qa_stream` 里各写了一份（约 150 行重复）。改 prompt 或加参数
必须同步三处，改漏一处就是行为不一致。本模块是唯一真相：

    history_to_text()        历史消息 → "role: content\n" 文本
    rewrite_query()          多轮对话下把当前问题改写成独立可检索的问题
    build_context_lines()    参考资料行（页码为空时省略，见 #4）
    assemble_prompt()        拼最终 prompt
    retrieve_and_rerank()    向量召回 + 重排（阈值见 #6）
    prepare_answer()         上面全套串起来，返回 prompt + 引用来源

`rag_chat_qa.py` 只负责流式事件、写库、日志与自动命名；
`rag_chat.py`（eval 用）也走同一套，不再自己拼 prompt。

注意：prompt 文本与重构前**逐字一致**（历史对话段即使为空也保留标题），
只有「页码为空时省略」和「阈值」这两处是本次有意引入的差异。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import chat_window
from llm_service import llm_service
from retrieval import retrieve
from rerank_client import rerank_client
from settings import settings

# chat_window 模块里的单例实例与模块同名，这里取个不丢名的别名
window = chat_window.chat_window

# top_k 来自 config.yaml 的 retrieval 段（#6）。
# 模块级常量保留：问答日志的 meta 里要写这两个值，rag_chat / rag_chat_qa 都仍从这里取。
DEFAULT_RECALL_TOP_K = settings.retrieval.recall_top_k
DEFAULT_RERANK_TOP_K = settings.retrieval.rerank_top_k

# 检索不到内容 / 全部低于阈值时的固定回答
NO_ANSWER = "知识库中未找到相关内容"

# ⚠️ 逐字保留重构前的 prompt 模板（rag_chat_qa 版本，含空的「历史对话」段）
PROMPT_TEMPLATE = """你是专业知识库助手，请严格基于下面提供的参考资料回答用户问题。
如果参考资料里没有对应的信息，直接回答“知识库中未找到相关内容”，禁止编造、臆测。

## 历史对话
{history_text}

## 参考资料
{context}

## 用户当前问题
{query}
"""

REWRITE_PROMPT_TEMPLATE = (
    "根据对话历史，把用户当前问题改写成独立、完整、可检索的问题，"
    "只输出改写后的问题，不要任何解释：\n历史：\n{history}\n用户问题：{query}"
)


@dataclass
class PreparedAnswer:
    """一次问答「生成前」的全部产物。"""

    prompt: str
    # 重排后用于回答的 chunks（含 score / rerank_score / chunk_id / doc_id ...）
    sources: List[Dict] = field(default_factory=list)
    # 实际用于检索的问题（可能是改写后的）
    query_for_search: str = ""


def history_to_text(history: List[Dict]) -> str:
    """历史消息列表 → prompt 里的 "role: content\n" 文本。"""
    text = ""
    for h in history:
        text += f"{h['role']}: {h['content']}\n"
    return text


def rewrite_query(history: List[Dict], query: str) -> str:
    """多轮对话下把当前问题改写成独立、可检索的问题；失败就用原问题。

    没有历史（首轮）时不发这次 LLM 调用，行为与重构前一致。
    """
    if not history:
        return query
    prompt = REWRITE_PROMPT_TEMPLATE.format(history=history_to_text(history), query=query)
    try:
        rewritten = llm_service.simple_call(prompt).strip()
        return rewritten if rewritten else query
    except Exception:
        return query


def retrieve_and_rerank(
    query: str,
    recall_top_k: Optional[int] = None,
    rerank_top_k: Optional[int] = None,
    cosine_min_score: Optional[float] = None,
    rerank_min_score: Optional[float] = None,
) -> List[Dict]:
    """向量召回 + 重排（阈值默认取 config.yaml 的 retrieval 段，#6）。

    召回为空、或重排后全被阈值刷掉 ⇒ 返回 []（调用方走“未找到”分支）。
    """
    recall_chunks = retrieve(query, top_k=recall_top_k, score_threshold=cosine_min_score)
    if not recall_chunks:
        return []
    return rerank_client.rerank(
        query,
        recall_chunks,
        top_n=rerank_top_k if rerank_top_k is not None else DEFAULT_RERANK_TOP_K,
        min_score=rerank_min_score,
    )


def build_context_lines(chunks: List[Dict]) -> List[str]:
    """参考资料行：章节路径 + 页码 + 正文。

    页码为空时整段省略 `| 页码：None`（#4）——以前会把字面量 "None" 塞进 prompt。
    """
    lines = []
    for item in chunks:
        page = item.get("page_no")
        meta = f"【章节路径】{item['section_path']}"
        if page is not None:
            meta += f" | 页码：{page}"
        lines.append(f"{meta}\n{item['content']}")
    return lines


def assemble_prompt(history_text: str, context: str, query: str) -> str:
    """拼最终 prompt（模板与重构前逐字一致）。"""
    return PROMPT_TEMPLATE.format(
        history_text=history_text, context=context, query=query
    )


def trim_context(context_lines: List[str], history_text: str, query: str) -> List[str]:
    """按 token 预算裁剪参考资料，避免 prompt 超模型上限。

    与重构前同样的算法：先算「历史 + 问题 + 模板」占掉的 token，剩下的预算给资料。
    """
    reserved_tokens = window.count_tokens(
        PROMPT_TEMPLATE.format(history_text=history_text, context="", query=query)
    )
    return window.trim_context(context_lines, reserved_tokens)


def prepare_answer(
    history: List[Dict],
    query: str,
    recall_top_k: Optional[int] = None,
    rerank_top_k: Optional[int] = None,
    cosine_min_score: Optional[float] = None,
    rerank_min_score: Optional[float] = None,
) -> Optional[PreparedAnswer]:
    """生成前的完整准备：rewrite → 召回 → 重排 → 组装 prompt。

    top_k / 阈值不传就用配置默认值。
    return: PreparedAnswer；没有任何可用参考资料时返回 None（调用方回答 NO_ANSWER）。
    """
    query_for_search = rewrite_query(history, query)

    sources = retrieve_and_rerank(
        query_for_search,
        recall_top_k=recall_top_k,
        rerank_top_k=rerank_top_k,
        cosine_min_score=cosine_min_score,
        rerank_min_score=rerank_min_score,
    )
    if not sources:
        return None

    context_lines = trim_context(
        build_context_lines(sources), history_to_text(history), query
    )
    context = "\n\n".join(context_lines)

    return PreparedAnswer(
        prompt=assemble_prompt(history_to_text(history), context, query),
        sources=sources,
        query_for_search=query_for_search,
    )
