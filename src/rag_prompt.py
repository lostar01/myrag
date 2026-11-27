"""RAG 问答链路的单一实现：query rewrite → 召回 → 重排 → 上下文裁剪 → prompt。

背景（D7-b）：prompt 模板、query rewrite、context 组装原本在 `rag_chat.py`、
`chat_qa`、`chat_qa_stream` 里各写了一份（约 150 行重复）。改 prompt 或加参数
必须同步三处，改漏一处就是行为不一致。本模块是唯一真相：

    history_to_text()        历史消息 → "role: content\n" 文本
    rewrite_query()          多轮对话下把当前问题改写成独立可检索的问题
    build_context_line()     一条参考资料（来源文档名 + 章节 + 页码 + 正文）
    build_context_lines()    参考资料行列表（页码为空时省略，见 #4）
    assemble_prompt()        拼最终 prompt
    retrieve_and_rerank()    向量召回 + 重排（阈值见 #6）；**不截断**，选择交给 context_selector
    prepare_answer()         上面全套串起来：rewrite → 记忆 → 召回 → 重排 → 选择 → 排序 → prompt

`rag_chat_qa.py` 只负责流式事件、写库、日志与自动命名；
`rag_chat.py`（eval 用）也走同一套，不再自己拼 prompt。

注意：prompt 文本与重构前**逐字一致**（历史对话段即使为空也保留标题），
只有「页码为空时省略」和「阈值」这两处是本次有意引入的差异。
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import chat_window
import context_selector
from llm_service import llm_service
from retrieval import retrieve
from rerank_client import rerank_client
from settings import settings

# chat_window 模块里的单例实例与模块同名，这里取个不丢名的别名
window = chat_window.chat_window

# top_k 来自 config.yaml 的 retrieval 段（#6）。
# 模块级常量保留：问答日志的 meta 里要写这两个值，rag_chat / rag_chat_qa 都仍从这里取。
DEFAULT_RECALL_TOP_K = settings.retrieval.recall_top_k
DEFAULT_RERANK_TOP_N = settings.retrieval.rerank_top_n

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

# 记忆段落插在「历史对话」之前（记忆为空时不插 ⇒ 拼出的 prompt 与上面这份逐字一致）
_HEAD, _TAIL = PROMPT_TEMPLATE.split("## 历史对话", 1)

PROFILE_TITLE = "## 用户画像与偏好"
PROFILE_NOTE = "（以上仅用于理解用户背景与偏好，不得当作知识库事实来源）"
RECALL_TITLE = "## 相关记忆"
SUMMARY_TITLE = "## 会话摘要"


def _brace_safe(text) -> str:
    """记忆文本可能带 `{` / `}`（用户自己写的），而下面是用 .format() 拼模板 ⇒ 必须转义。"""
    return str(text).replace("{", "{{").replace("}", "}}")


def build_memory_block(
    profile_lines=None,
    recall_lines=None,
    summary: str = "",
) -> str:
    """拼记忆段落（画像 / 相关记忆 / 会话摘要）。

    三样都空 ⇒ 返回空串 ⇒ 整个 prompt 与加记忆之前逐字一致（memory.enable=false 就是这个状态）。
    """
    parts: List[str] = []
    if profile_lines:
        body = "\n".join(f"- {line}" for line in profile_lines)
        parts.append(f"{PROFILE_TITLE}\n{body}\n{PROFILE_NOTE}")
    if recall_lines:
        body = "\n".join(f"- {line}" for line in recall_lines)
        parts.append(f"{RECALL_TITLE}\n{body}")
    if summary:
        parts.append(f"{SUMMARY_TITLE}\n{str(summary).strip()}")
    if not parts:
        return ""
    return _brace_safe("\n\n".join(parts)) + "\n\n"

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
    # 注入的记忆段落（空串 = 本次没带记忆）
    memory_block: str = ""
    # 记忆上下文（memory_service.MemoryContext 或 None），只给写日志用
    memory_ctx: object = None
    # 上下文选择的统计（覆盖了几篇文档、预算丢了多少 …），写日志用
    selection: Optional[Dict] = None


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
    with_vectors: bool = False,
) -> List[Dict]:
    """向量召回 + 重排，**不截断**（截断/选择交给 `context_selector`）。

    召回为空、或重排后全被阈值刷掉 ⇒ 返回 []（调用方走“未找到”分支）。
    `with_vectors=True` 时每条候选带 chunk 向量，供覆盖贪心算冗余度（无需重新 embedding）。
    """
    recall_chunks = retrieve(query, top_k=recall_top_k, score_threshold=cosine_min_score,
                             with_vectors=with_vectors)
    if not recall_chunks:
        return []
    return rerank_client.rerank_all(query, recall_chunks, min_score=rerank_min_score)


def build_context_line(item: Dict) -> str:
    """一条参考资料（含来源文档名 + 章节路径 + 页码）。

    - 页码为空时整段省略 `| 页码：None`（#4）；
    - 文档名让模型知道“这条出自哪本书”，多源对比/引用归因靠它（`doc_name` 来自召回 payload）。
    预算统计必须用同一个函数算 token，否则算出来的预算与最终 prompt 不一致。
    """
    meta_parts = []
    doc_name = item.get("doc_name")
    if doc_name:
        meta_parts.append(f"【来源】{doc_name}")
    meta_parts.append(f"【章节路径】{item.get('section_path')}")
    page = item.get("page_no")
    if page is not None:
        meta_parts.append(f"页码：{page}")
    return " | ".join(meta_parts) + f"\n{item.get('content') or ''}"


def build_context_lines(chunks: List[Dict]) -> List[str]:
    """参考资料行列表（保持传入顺序）。"""
    return [build_context_line(item) for item in chunks]


def assemble_prompt(
    history_text: str,
    context: str,
    query: str,
    memory_block: str = "",
) -> str:
    """拼最终 prompt。

    `memory_block=""` 时输出与重构前的 PROMPT_TEMPLATE **逐字一致**；
    记忆段落插在「历史对话」之前（历史/资料/问题三段的标题与顺序一动不动）。
    """
    template = _HEAD + memory_block + "## 历史对话" + _TAIL
    return template.format(history_text=history_text, context=context, query=query)


def trim_context(
    context_lines: List[str],
    history_text: str,
    query: str,
    memory_block: str = "",
) -> List[str]:
    """按 token 预算裁剪参考资料，避免 prompt 超模型上限。

    与重构前同样的算法：先算「记忆 + 历史 + 问题 + 模板」占掉的 token，剩下的预算给资料。
    ⚠ 记忆段落必须计入 reserved：否则它会挤爆窗口，直接吃模型 400。
    """
    reserved_tokens = window.count_tokens(
        assemble_prompt(history_text=history_text, context="", query=query, memory_block=memory_block)
    )
    return window.trim_context(context_lines, reserved_tokens)


def prepare_answer(
    history: List[Dict],
    query: str,
    recall_top_k: Optional[int] = None,
    rerank_top_k: Optional[int] = None,
    cosine_min_score: Optional[float] = None,
    rerank_min_score: Optional[float] = None,
    memory_provider: Optional[Callable[[str], object]] = None,
) -> Optional[PreparedAnswer]:
    """生成前的完整准备：rewrite → 记忆 → 召回 → 重排 → 组装 prompt。

    top_k / 阈值不传就用配置默认值。

    `memory_provider`：回调，拿到“改写后的问题”后返回记忆上下文（memory_service.MemoryContext）。
    用回调而不是在这里直接调 memory_service，两个原因：
      1. 记忆要用改写后的问题去召回，而 rewrite 发生在本函数内部；
      2. 保持 rag_prompt 不依赖记忆模块 ⇒ `rag_chat`（eval 用）可以不带记忆跑，口径与以前一致。
    回调里的异常一律吃掉 ⇒ 记忆挂了照常回答。
    return: PreparedAnswer；没有任何可用参考资料时返回 None（调用方回答 NO_ANSWER）。
    """
    query_for_search = rewrite_query(history, query)

    memory_ctx = None
    memory_block = ""
    if memory_provider is not None:
        try:
            memory_ctx = memory_provider(query_for_search)
        except Exception as e:
            print(f"[rag_prompt] 记忆读取失败，本次不带记忆回答: {e}")
            memory_ctx = None
        if memory_ctx is not None and not getattr(memory_ctx, "is_empty", lambda: True)():
            memory_block = build_memory_block(
                profile_lines=getattr(memory_ctx, "profile_lines", None),
                recall_lines=getattr(memory_ctx, "recall_lines", None),
                summary=getattr(memory_ctx, "summary", ""),
            )

    # 覆盖贪心需要 chunk 向量；score 策略不需要，可选省带宽
    policy = (settings.retrieval.selector or "score").strip().lower()
    need_vectors = policy in ("coverage", "adaptive")

    candidates = retrieve_and_rerank(
        query_for_search,
        recall_top_k=recall_top_k,
        cosine_min_score=cosine_min_score,
        rerank_min_score=rerank_min_score,
        with_vectors=need_vectors,
    )
    if not candidates:
        # 没参考资料 ⇒ 不生成：记忆段落不能代替知识库内容，否则就是编造
        return None

    history_text = history_to_text(history)
    # 预算：先算「模板 + 历史 + 问题 + 记忆段落」已占多少，剩下才是参考资料可用额度
    reserved_tokens = window.count_tokens(
        assemble_prompt(history_text=history_text, context="", query=query, memory_block=memory_block)
    )
    budget = max(window.max_total_tokens - reserved_tokens, 0)

    slots = int(rerank_top_k if rerank_top_k is not None else DEFAULT_RERANK_TOP_N)
    picked, sel_stats = context_selector.select(
        candidates,
        slots=slots,
        budget=budget,
        # 预算口径必须与最终渲染一致（含来源/章节/页码前缀）
        count_tokens=lambda item: window.count_tokens(build_context_line(item)),
        policy=policy,
        per_doc_max_chunk=int(settings.retrieval.per_doc_max_chunk),
        diversity_lambda=float(settings.retrieval.diversity_lambda),
    )
    if not picked:
        return None

    # 参考资料顺序：lost_in_middle 把最强证据放首与末，减轻中间位置被忽略
    ordered = context_selector.order_for_context(picked, settings.retrieval.context_order)
    context_lines = build_context_lines(ordered)
    # 兜底：选择器已按预算装，理论上不会丢；真超了再裁（保持原有行为）
    context_lines = trim_context(context_lines, history_text, query, memory_block)
    context = "\n\n".join(context_lines)

    return PreparedAnswer(
        prompt=assemble_prompt(history_text, context, query, memory_block),
        # 引用列表给前端/日志用，统一按分数降序（与 prompt 内部的 lost_in_middle 顺序无关）
        sources=sorted(picked, key=context_selector.sort_key),
        query_for_search=query_for_search,
        memory_block=memory_block,
        memory_ctx=memory_ctx,
        selection=sel_stats.as_dict(),
    )
