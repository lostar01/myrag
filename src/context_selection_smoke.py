"""上下文选择冒烟（分组召回 + 覆盖贪心 + 预算感知）。

跑法（任意目录）：
    python src/context_selection_smoke.py              # 全部
    python src/context_selection_smoke.py --offline    # 只跑纯逻辑（不连 Qdrant / rerank）

覆盖：
  A. 纯逻辑（无外部依赖）：三种策略的覆盖数、配额、预算、确定性、按比例回退、lost_in_middle 排序
  B. 真实召回：分组召回 vs 平铺召回的「文档覆盖数」差异（本库实测：40 条平铺只来自 3 篇文档）
  C. 真实端到端：rag_prompt.prepare_answer 在四种 selector 下的选片结果与统计
  D. 用户场景复现：构造「多文档 × 高分扎堆」的候选池，验证 score 截断会坍缩、adaptive 能保住多源
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OFFLINE = "--offline" in sys.argv
FAILS = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if extra == "" else f" | {extra}"))
    if not cond:
        FAILS.append(name)


def _unit_vector(seed_text, dim=8, noise=0.0):
    """确定性伪随机单位向量：用字符串哈希播种，保证每次跑结果一致。

    同文档的多个 chunk 用「同一向量 + 极小扰动」模拟（近似重复），
    不同文档之间近似正交（模拟"内容不同"）。真实语料里就是这样：
    同文档相邻块高度相似，跨文档相似度接近 0。
    """
    h = 0
    for ch in str(seed_text):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    vec = []
    for i in range(dim):
        h = (1103515245 * h + 12345) & 0xFFFFFFFF
        vec.append((h / 0xFFFFFFFF) - 0.5 + noise * (i + 1) * 0.0001)
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def make_candidates(n_docs, per_doc, mode="collapsed", with_vector=True):
    """合成候选池。

    mode="collapsed"：分数随文档序号下降 ⇒ 分数截断会集中在少数文档（用户遇到的坍缩）
    mode="clean"    ：先铺满文档再降分 ⇒ top-k 天然覆盖 k 篇文档（干净池）
    """
    out = []
    for d in range(n_docs):
        for k in range(per_doc):
            if mode == "collapsed":
                score = 0.95 - d * 0.002 - k * 0.0005
            else:
                score = 0.95 - k * 0.01 - d * 0.0005
            vec = _unit_vector(f"d{d}", noise=k)
            out.append({
                "chunk_id": f"c{d:03d}_{k}",
                "doc_id": f"d{d:03d}",
                "doc_name": f"教材{d}.pdf",
                "section_path": f"第{d}章",
                "rerank_score": score,
                "score": score - 0.3,
                "content": "正文" * 20,
                "vector": vec if with_vector else None,
            })
    return out


from context_selector import order_for_context, select  # noqa: E402

cost = lambda item: 700  # noqa: E731  每条按 chunk 上限估

# ---------------- A. 纯逻辑 ----------------
print("\n== A. 纯逻辑 ==")
docs = make_candidates(30, 2)
p, s = select(docs, slots=12, budget=12000, count_tokens=cost, policy="score", per_doc_max_chunk=2)
chk("A1 score：12 条但集中在小数文档（复现坍缩）", len(p) == 12 and s.docs_selected <= 7, f"{s.docs_selected} 篇")

p, s = select(docs, slots=12, budget=12000, count_tokens=cost, policy="doc_quota", per_doc_max_chunk=2)
chk("A2 doc_quota：30 篇候选 ⇒ 12 槽铺满 12 篇", s.docs_selected == 12 and len(p) == 12, f"{s.docs_selected} 篇")

p, s = select(make_candidates(5, 6), slots=12, budget=12000, count_tokens=cost,
              policy="doc_quota", per_doc_max_chunk=2)
chk("A3 doc_quota：文档数 < 槽位时补第 2 条（5 篇 × 2）", s.docs_selected == 5 and len(p) == 10,
    f"{s.docs_selected} 篇 / {len(p)} 条")

p, s = select(docs, slots=12, budget=12000, count_tokens=cost, policy="coverage", diversity_lambda=0.3)
chk("A4 coverage：同样候选下覆盖数不低于 score", s.docs_selected >= 8, f"{s.docs_selected} 篇")

red = [
    {"chunk_id": "a1", "doc_id": "d1", "rerank_score": 0.90, "content": "x", "vector": [1.0, 0.0]},
    {"chunk_id": "a2", "doc_id": "d1", "rerank_score": 0.89, "content": "x", "vector": [1.0, 0.001]},
    {"chunk_id": "b1", "doc_id": "d2", "rerank_score": 0.60, "content": "x", "vector": [0.0, 1.0]},
]
p, _ = select(red, slots=2, budget=5000, count_tokens=cost, policy="coverage", diversity_lambda=0.5)
chk("A5 coverage：近重复条目被跳过，改选不同方向", [x["chunk_id"] for x in p] == ["a1", "b1"],
    [x["chunk_id"] for x in p])

p, s = select(make_candidates(20, 2, mode="clean"), slots=12, budget=12000, count_tokens=cost, policy="adaptive")
chk("A6 adaptive：干净池 ⇒ 退化为 score（不对干净池做多样化）", s.effective == "score", s.note)
p, s = select(make_candidates(30, 2, mode="collapsed"), slots=12, budget=12000, count_tokens=cost, policy="adaptive")
chk("A7 adaptive：坍缩池 ⇒ 启用 coverage", s.effective == "coverage", s.note)

p, s = select(make_candidates(30, 2), slots=12, budget=700 * 5, count_tokens=cost,
              policy="doc_quota", per_doc_max_chunk=2)
chk("A8 预算：只装得下 5 条，且记录 dropped_by_budget", len(p) == 5 and s.dropped_by_budget > 0,
    f"{len(p)} 条 / 丢 {s.dropped_by_budget}")

a, _ = select(make_candidates(10, 3), slots=8, budget=9000, count_tokens=cost, policy="coverage")
b, _ = select(make_candidates(10, 3), slots=8, budget=9000, count_tokens=cost, policy="coverage")
chk("A9 确定性：同输入多次结果一致", [x["chunk_id"] for x in a] == [x["chunk_id"] for x in b])

p, s = select(make_candidates(6, 2, with_vector=False), slots=8, budget=9000, count_tokens=cost, policy="coverage")
chk("A10 无向量 ⇒ coverage 降级为 doc_quota 并注明", "退化" in s.note and len(p) > 0, s.note)

items = [{"chunk_id": f"c{i}", "doc_id": "d", "rerank_score": 1 - i * 0.1} for i in range(6)]
od = order_for_context(items, "lost_in_middle")
chk("A11 lost_in_middle：最强在首位、次强在末位", od[0]["chunk_id"] == "c0" and od[-1]["chunk_id"] == "c1",
    [x["chunk_id"] for x in od])
chk("A12 lost_in_middle：不丢不重", sorted(x["chunk_id"] for x in od) == sorted(x["chunk_id"] for x in items))

# ---------------- B. 真实召回 ----------------
if not OFFLINE:
    print("\n== B. 真实召回（分组 vs 平铺） ==")
    from retrieval import retrieve

    q = "资本论 劳动价值论 商品价值"
    flat = retrieve(q, group_by_doc=False, top_k=40)
    grouped = retrieve(q, group_by_doc=True, with_vectors=True)
    flat_docs = len({c["doc_id"] for c in flat})
    grp_docs = len({c["doc_id"] for c in grouped})
    print(f"   平铺 limit=40: {len(flat)} 条 / {flat_docs} 篇文档")
    print(f"   分组 16×2   : {len(grouped)} 条 / {grp_docs} 篇文档")
    chk("B1 分组召回的文档覆盖数 > 平铺", grp_docs > flat_docs, f"{grp_docs} vs {flat_docs}")
    chk("B2 分组候选带 doc_name / vector / group_rank",
        all(c.get("doc_name") and c.get("vector") and c.get("group_rank") for c in grouped))

    # ---------------- C. 真实端到端（四种策略） ----------------
    print("\n== C. 真实端到端 prepare_answer ==")
    import rag_prompt
    from settings import settings

    orig = settings.retrieval.selector
    try:
        for policy in ("score", "doc_quota", "coverage", "adaptive"):
            settings.retrieval.selector = policy
            r = rag_prompt.prepare_answer(history=[], query=q)
            sel = r.selection or {}
            print(f"   {policy:9} → {sel.get('selector'):9} | {len(r.sources)} 条 / "
                  f"{sel.get('docs_selected')} 篇 | {sel.get('context_tokens')}/{sel.get('context_budget')} token")
            chk(f"C {policy}: 有结果且统计齐全", bool(r.sources) and sel.get("docs_selected", 0) >= 1)
    finally:
        settings.retrieval.selector = orig

    chk("C5 参考资料行带来源文档名",
        bool(rag_prompt.prepare_answer(history=[], query=q)) and
        "【来源】" in rag_prompt.build_context_line(
            rag_prompt.prepare_answer(history=[], query=q).sources[0]))

# ---------------- D. 用户场景复现 ----------------
print("\n== D. 用户场景：多文档 × 高分扎堆 ==")
# 20 篇文档 × 3 块，全部 rerank > 0.8（就是"相关性都很高、但槽位只有 12"的场景）
scenario, score_arg = [], []
for d in range(20):
    for k in range(3):
        scenario.append({
            "chunk_id": f"s{d}_{k}", "doc_id": f"sd{d}", "doc_name": f"教材{d}.pdf",
            "section_path": f"第{k}讲", "rerank_score": 0.96 - d * 0.003 - k * 0.001,
            "content": "正文" * 20, "vector": _unit_vector(f"sd{d}", noise=k),
        })
p_score, s_score = select(scenario, 12, 12000, cost, policy="score", per_doc_max_chunk=2)
p_ada, s_ada = select(scenario, 12, 12000, cost, policy="adaptive", per_doc_max_chunk=2, diversity_lambda=0.3)
print(f"   score   : {len(p_score)} 条 / {s_score.docs_selected} 篇文档")
print(f"   adaptive: {len(p_ada)} 条 / {s_ada.docs_selected} 篇文档（{s_ada.effective}）")
chk("D1 高分扎堆时 score 截断只覆盖少数文档", s_score.docs_selected < 8, f"{s_score.docs_selected} 篇")
chk("D2 adaptive 把覆盖铺开到更多文档", s_ada.docs_selected > s_score.docs_selected,
    f"{s_ada.docs_selected} vs {s_score.docs_selected}")
chk("D3 adaptive 仍然只取高相关（分数不塌陷）",
    min(x["rerank_score"] for x in p_ada) >= 0.85, round(min(x["rerank_score"] for x in p_ada), 3))

# 单证据不吃亏
single = [{"chunk_id": f"g{i}", "doc_id": "gold", "rerank_score": 0.95 - i * 0.01,
           "content": "x", "vector": _unit_vector("gold", noise=i)} for i in range(5)]
single += [{"chunk_id": f"o{i}", "doc_id": f"other{i}", "rerank_score": 0.5,
            "content": "x", "vector": _unit_vector(f"other{i}")} for i in range(10)]
p, s = select(single, 12, 12000, cost, policy="adaptive", per_doc_max_chunk=2, diversity_lambda=0.3)
chk("D4 单证据查询：答案所在文档仍能出 2 条", sum(1 for x in p if x["doc_id"] == "gold") == 2,
    f"gold={sum(1 for x in p if x['doc_id']=='gold')} 条")

print("\n==== 结果 ====")
print("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}")
