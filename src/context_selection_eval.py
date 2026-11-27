"""选择策略对比：四种 selector 在“多源问题”上的覆盖度与幻觉率。

为什么不用 `rag_eval.py` 就够：`eval/test_set.json` 只有 2 题、而且两题都只命中 2 篇文档，
四种选择策略在这里必然同分（对策略不敏感）。本脚本用**多源问题**（需要跨书对比）来拉开差距，
并且只用「回答是否被参考资料支持」这一项判定（不需要标准答案，避免自造 ground truth）。

跑法（在 src/ 下，或任意目录）：
    python src/context_selection_eval.py
    python src/context_selection_eval.py --selectors score adaptive

输出：
  - 控制台表格（准确覆盖、chunk 数、上下文 token、是否出现无依据内容）
  - `eval/report_selection_compare.json`
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from settings import settings  # noqa: E402

# 多源问题：答案天然需要跨多本书/多篇文档，才能体现"槽位怎么分配"的差异
QUESTIONS = [
    "不同教材对劳动价值论的解释有什么差异？",
    "资本论和政治经济学对商品价值的定义有什么不同？",
    "关于矛盾论，不同资料的说法有哪些异同？",
]

JUDGE_PROMPT = """你在判断一段回答是否**超出给定参考资料**（是否编造）。

【参考资料】
{context}

【回答】
{answer}

只输出 JSON：{{"unsupported": 0或1, "reason": "简短理由"}}
判定标准：
1. unsupported=1：回答里出现参考资料中不存在的事实、数字、引文或结论；
2. unsupported=0：回答的内容都能在参考资料里找到依据（允许概括与改写）；
3. 如果回答是“知识库中未找到相关内容”，unsupported=0。
"""


def run_one(selector: str, questions):
    from llm_service import llm_service
    import rag_prompt

    settings.retrieval.selector = selector
    rows = []
    for q in questions:
        t0 = time.time()
        prepared = rag_prompt.prepare_answer(history=[], query=q)
        if prepared is None:
            rows.append({"question": q, "answer": "", "chunks": 0, "docs": 0,
                         "tokens": 0, "unsupported": 0, "note": "无参考资料", "ms": 0})
            continue
        answer = llm_service.chat(prepared.prompt)
        sel = prepared.selection or {}
        judge = ""
        try:
            raw = llm_service.chat(JUDGE_PROMPT.format(
                context="\n\n".join(rag_prompt.build_context_lines(prepared.sources)),
                answer=answer,
            ))
            data = json.loads(raw.strip().strip("`").replace("json\n", "", 1))
            judge = int(data.get("unsupported", 0))
        except Exception as e:
            print(f"    [警告] 判定解析失败：{e}")
            judge = -1
        rows.append({
            "question": q,
            "answer_len": len(answer),
            "chunks": len(prepared.sources),
            "docs": len({s.get("doc_id") for s in prepared.sources if s.get("doc_id")}),
            "tokens": sel.get("context_tokens"),
            "effective": sel.get("selector"),
            "unsupported": judge,
            "ms": int((time.time() - t0) * 1000),
        })
    return rows


def main():
    args = sys.argv[1:]
    selectors = ["score", "doc_quota", "coverage", "adaptive"]
    if "--selectors" in args:
        idx = args.index("--selectors")
        selectors = [s for s in args[idx + 1:] if not s.startswith("-")] or selectors

    orig = settings.retrieval.selector
    summary = {}
    try:
        for sel in selectors:
            print(f"\n===== selector = {sel} =====")
            rows = run_one(sel, QUESTIONS)
            for r in rows:
                print(f"  {r['chunks']:2} 条 / {r['docs']} 篇 / {r.get('tokens')} token / "
                      f"无依据={r['unsupported']} / {r['ms']}ms | {r['question'][:26]}")
            summary[sel] = {
                "avg_docs": round(sum(r["docs"] for r in rows) / len(rows), 2),
                "avg_chunks": round(sum(r["chunks"] for r in rows) / len(rows), 2),
                "avg_tokens": round(sum(r.get("tokens") or 0 for r in rows) / len(rows)),
                "unsupported_rate": round(
                    sum(1 for r in rows if r["unsupported"] == 1) / len(rows), 3),
                "avg_ms": round(sum(r["ms"] for r in rows) / len(rows)),
                "rows": rows,
            }
    finally:
        settings.retrieval.selector = orig

    print(f"\n{'selector':10} {'覆盖文档':>8} {'chunk':>6} {'上下文token':>10} {'无依据率':>8} {'耗时ms':>8}")
    for sel, s in summary.items():
        print(f"{sel:10} {s['avg_docs']:>8} {s['avg_chunks']:>6} {s['avg_tokens']:>10} "
              f"{s['unsupported_rate']:>8.0%} {s['avg_ms']:>8}")

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "eval", "report_selection_compare.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"questions": QUESTIONS, "summary": summary}, f, ensure_ascii=False, indent=2)
    print(f"\n报告输出至：{out}")


if __name__ == "__main__":
    main()
