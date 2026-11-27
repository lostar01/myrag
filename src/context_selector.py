"""上下文选择：从「按分数取前 k」改为「按覆盖/多样性选子集」。

要解决的问题（多文档、多 chunk、分数扎堆）：
  候选里 70+ 条 `rerank_score` 都 > 0.8 时，全局截断会只保留 2~3 篇文档的内容，
  其余几十篇文档直接无缘上下文 ⇒ 丢视角、丢对比信息。这不是噪声问题（噪声由阈值负责），
  是「名额怎么分配」的问题。

三种策略（`retrieval.selector`，可随时回退）：
  - `score`     ：旧行为，按分数截断。回退基线。
  - `doc_quota` ：按文档配额两遍填充（先每文档 1 条铺满，再补第 2 条）。
  - `coverage`  ：冗余感知贪心（MMR 式惩罚 `w_e - λ·max_sim(e,S)`），配合 token 预算与单文档上限。
  - `adaptive`  ：候选里「有效不同文档数 ≥ 槽位」⇒ 走 `score`；否则走 `coverage`。
    依据：多样化在“干净候选池”上反而会降低相关性/覆盖率（arXiv 2610.09412），
    只有当候选池本身冗余、任务需要多条证据时才有收益。adaptive 会自动规避这种伤害。

本模块只用现成数据（`rerank_score` + 召回时带回的 chunk 向量），**不调用任何外部服务**，
便于单测与复现。相似度矩阵来自 `retrieval.retrieve(with_vectors=True)`。
"""

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# 打分键：分数降序 → doc_id → chunk_id（分数扎堆时也要有确定顺序，否则每次结果不同）
def sort_key(item: Dict) -> Tuple[float, str, str]:
    return (
        -float(item.get("rerank_score") or 0.0),
        str(item.get("doc_id") or ""),
        str(item.get("chunk_id") or ""),
    )


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（召回向量已是归一化向量，这里仍做一次保护性归一）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


@dataclass
class SelectionStats:
    """一次选择的统计（写进 QA 日志，用来判断配额/预算是否过紧）。"""

    requested: str = "score"          # 配置里要的策略
    effective: str = "score"          # 实际生效的策略（adaptive 会二选一）
    slots: int = 0
    candidates: int = 0
    docs_total: int = 0
    docs_selected: int = 0
    chunks_per_doc: Dict[str, int] = field(default_factory=dict)
    dropped_by_budget: int = 0
    dropped_by_quota: int = 0
    tokens_used: int = 0
    budget: int = 0
    note: str = ""

    def as_dict(self) -> Dict:
        return {
            "selector": self.effective,
            "selector_requested": self.requested,
            "slots": self.slots,
            "candidates": self.candidates,
            "docs_total": self.docs_total,
            "docs_selected": self.docs_selected,
            "chunks_per_doc": dict(self.chunks_per_doc),
            "dropped_by_budget": self.dropped_by_budget,
            "dropped_by_quota": self.dropped_by_quota,
            "context_tokens": self.tokens_used,
            "context_budget": self.budget,
            **({"note": self.note} if self.note else {}),
        }

    def summary(self) -> str:
        return (
            f"{self.effective}: {self.docs_selected}/{self.docs_total} 篇文档 "
            f"× {self.slots} 槽位 | {self.tokens_used}/{self.budget} token "
            f"| 因预算丢 {self.dropped_by_budget}、因配额丢 {self.dropped_by_quota}"
        )


def _select_score(
    candidates: List[Dict],
    slots: int,
    budget: int,
    count_tokens: Callable[[Dict], int],
    stats: SelectionStats,
    per_doc_max_chunk: int,
) -> List[Dict]:
    """旧行为：按分数顺序尽量多装（预算感知、受单文档上限保护）。"""
    picked: List[Dict] = []
    used = 0
    per_doc: Dict[str, int] = {}
    for item in sorted(candidates, key=sort_key):
        if len(picked) >= slots:
            break
        doc = str(item.get("doc_id") or "")
        if per_doc_max_chunk > 0 and per_doc.get(doc, 0) >= per_doc_max_chunk:
            stats.dropped_by_quota += 1
            continue
        cost = count_tokens(item)
        if used + cost > budget:
            stats.dropped_by_budget += 1
            continue
        picked.append(item)
        used += cost
        per_doc[doc] = per_doc.get(doc, 0) + 1
    stats.tokens_used, stats.chunks_per_doc, stats.docs_selected = used, per_doc, len(per_doc)
    return picked


def _select_doc_quota(
    candidates: List[Dict],
    slots: int,
    budget: int,
    count_tokens: Callable[[Dict], int],
    stats: SelectionStats,
    per_doc_max_chunk: int,
) -> List[Dict]:
    """按文档配额两遍填充。

    Pass 1：每篇文档贡献它最好的那 1 条，按分数降序填槽位（目标是“尽量多文档有代表”）；
    Pass 2：槽位还没满（文档数 < 槽位）时，才允许同一文档出第 2、3 条。
    """
    ordered = sorted(candidates, key=sort_key)
    by_doc: Dict[str, List[Dict]] = {}
    for item in ordered:
        by_doc.setdefault(str(item.get("doc_id") or ""), []).append(item)

    picked: List[Dict] = []
    used = 0
    per_doc: Dict[str, int] = {}
    limit = max(per_doc_max_chunk, 1)

    def try_take(item: Dict) -> bool:
        nonlocal used
        doc = str(item.get("doc_id") or "")
        if len(picked) >= slots or per_doc.get(doc, 0) >= limit:
            return False
        cost = count_tokens(item)
        if used + cost > budget:
            stats.dropped_by_budget += 1
            return False
        picked.append(item)
        used += cost
        per_doc[doc] = per_doc.get(doc, 0) + 1
        return True

    # Pass 1
    for doc, items in sorted(by_doc.items(), key=lambda kv: sort_key(kv[1][0])):
        if len(picked) >= slots:
            break
        try_take(items[0])

    # Pass 2
    if len(picked) < slots:
        taken = {id(x) for x in picked}
        for item in ordered:
            if len(picked) >= slots:
                break
            if id(item) in taken:
                continue
            if not try_take(item):
                continue

    stats.tokens_used, stats.chunks_per_doc, stats.docs_selected = used, per_doc, len(per_doc)
    return picked


def _select_coverage(
    candidates: List[Dict],
    slots: int,
    budget: int,
    count_tokens: Callable[[Dict], int],
    stats: SelectionStats,
    per_doc_max_chunk: int,
    diversity_lambda: float,
) -> List[Dict]:
    """冗余感知贪心（MMR 式）：`gain(e|S) = w_e - λ · max_{j∈S} sim(e, j)`。

    - `w_e` 直接用 rerank 的 sigmoid 分数（TEI 返回即 0~1，与 λ 同量纲）。
      不要做 min-max 归一化：候选分数本来就都挤在高位时，归一化会把最弱那条压成 0，
      而 λ·相似度 恒为正 ⇒ 覆盖项永远赢不过它，冗余惩罚形同失效（实测踩过）；
    - 向量缺失（召回没开 `with_vectors`）时无法算相似度 ⇒ 退化为 `doc_quota`
      （纯按文档铺开，不依赖向量），并在 stats.note 里注明；
    - 预算与单文档上限都是硬约束：装不下就跳过，不越界。
    """
    vectors = {c["chunk_id"]: c.get("vector") for c in candidates if c.get("vector")}
    if len(vectors) < len(candidates):
        stats.note = "缺少 chunk 向量，coverage 退化为 doc_quota"
        return _select_doc_quota(candidates, slots, budget, count_tokens, stats, per_doc_max_chunk)

    weights = {c["chunk_id"]: float(c.get("rerank_score") or 0.0) for c in candidates}
    remaining = sorted(candidates, key=sort_key)
    picked: List[Dict] = []
    used = 0
    per_doc: Dict[str, int] = {}
    limit = max(per_doc_max_chunk, 1)

    while remaining and len(picked) < slots:
        best_item, best_gain = None, None
        for item in remaining:
            doc = str(item.get("doc_id") or "")
            if per_doc.get(doc, 0) >= limit:
                stats.dropped_by_quota += 1
                continue
            if used + count_tokens(item) > budget:
                stats.dropped_by_budget += 1
                continue
            redundancy = 0.0
            if picked:
                vi = vectors[item["chunk_id"]]
                redundancy = max(_cos(vi, vectors[p["chunk_id"]]) for p in picked)
            gain = weights.get(item["chunk_id"], 0.0) - float(diversity_lambda) * redundancy
            # tie-break：gain 相同则分数高者优先，再按 doc/chunk 保证确定性
            key = (gain, -float(item.get("rerank_score") or 0.0), str(item.get("doc_id") or ""),
                   str(item.get("chunk_id") or ""))
            if best_gain is None or key > best_gain[0]:
                best_gain, best_item = (key, item), item
        if best_item is None:
            break
        remaining = [x for x in remaining if x is not best_item]
        picked.append(best_item)
        used += count_tokens(best_item)
        doc = str(best_item.get("doc_id") or "")
        per_doc[doc] = per_doc.get(doc, 0) + 1

    stats.tokens_used, stats.chunks_per_doc, stats.docs_selected = used, per_doc, len(per_doc)
    return picked


def _distinct_docs(items: List[Dict]) -> int:
    return len({str(i.get("doc_id") or "") for i in items})


def select(
    candidates: List[Dict],
    slots: int,
    budget: int,
    count_tokens: Callable[[Dict], int],
    policy: str = "adaptive",
    per_doc_max_chunk: int = 2,
    diversity_lambda: float = 0.3,
) -> Tuple[List[Dict], SelectionStats]:
    """从候选里选出要放进 prompt 的 chunk。

    :param candidates: 已带 `rerank_score` 的候选（建议同时带 `vector`、`doc_id`）
    :param slots: 最多几条
    :param budget: 参考资料可用的 token 预算（已扣除历史/问题/模板/记忆）
    :param count_tokens: 传入单条候选 → 渲染成参考资料行后的 token 数（由调用方保证口径一致）
    :param policy: score / doc_quota / coverage / adaptive
    :return: (picked, stats)；picked 按“选择顺序”（分数或覆盖顺序）排列
    """
    stats = SelectionStats(
        requested=policy, slots=int(slots), candidates=len(candidates),
        docs_total=_distinct_docs(candidates), budget=int(budget),
    )
    if not candidates or slots <= 0 or budget <= 0:
        stats.note = "无候选或预算为 0"
        return [], stats

    policy = (policy or "score").strip().lower()
    if policy not in ("score", "doc_quota", "coverage", "adaptive"):
        stats.note = f"未知策略 {policy!r}，回退 score"
        policy = "score"

    effective = policy
    if policy == "adaptive":
        # 只对“候选 top-slots 中有效文档数不足”的查询做多样化（干净池上多样化有害）
        head = sorted(candidates, key=sort_key)[: int(slots)]
        effective = "score" if _distinct_docs(head) >= int(slots) else "coverage"
        stats.note = f"adaptive → {effective}（top{slots} 覆盖 {_distinct_docs(head)} 篇）"
    stats.effective = effective

    if effective == "score":
        picked = _select_score(candidates, slots, budget, count_tokens, stats, per_doc_max_chunk)
    elif effective == "doc_quota":
        picked = _select_doc_quota(candidates, slots, budget, count_tokens, stats, per_doc_max_chunk)
    else:
        picked = _select_coverage(candidates, slots, budget, count_tokens, stats,
                                  per_doc_max_chunk, diversity_lambda)
    return picked, stats


def order_for_context(items: List[Dict], mode: str = "lost_in_middle") -> List[Dict]:
    """决定参考资料在 prompt 里的顺序。

    `lost_in_middle`：把最强证据放**首位与末位**，较弱的放中间。
    依据：LLM 对上下文中间位置的信息利用率显著更低（Lost in the Middle, TACL 2023）。
    `score`：保持分数降序（旧行为）。
    """
    ordered = sorted(items, key=sort_key)
    if (mode or "").strip().lower() != "lost_in_middle" or len(ordered) < 3:
        return ordered

    head: List[Dict] = []
    tail: List[Dict] = []
    for idx, item in enumerate(ordered):
        # 偶数名次（0,2,4...）→ 前半段；奇数名次 → 后半段（逆序摆放，使最强留在最后）
        (head if idx % 2 == 0 else tail).append(item)
    return head + list(reversed(tail))
