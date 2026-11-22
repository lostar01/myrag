import json
import time
from typing import List, Dict
from dataclasses import dataclass
from rag_chat import rag_chat
from llm_service import llm_service

# ===================== 配置 =====================
TEST_SET_PATH = "../eval/test_set.json"
REPORT_PATH = "../eval/report.json"
# 评估打分prompt 重点：JSON的{}写成{{}}
EVAL_PROMPT_TPL = """
你是RAG评估器，严格判断【模型回答】是否基于【参考上下文】，同时对比【预期标准答案】。
输出JSON，只返回这三个字段：
{{
    "is_correct": 0或1,
    "has_hallucination": 0或1,
    "reason": "简短理由"
}}
规则：
1. is_correct=1：回答核心信息和标准答案一致，没有错误
2. has_hallucination=1：回答存在上下文不存在的信息、编造内容
3. 只输出json，不要多余文字

【预期标准答案】
{expected_answer}

【召回参考上下文】
{context_text}

【模型回答】
{model_answer}
"""
# ================================================

@dataclass
class EvalItem:
    question: str
    expected_answer: str
    required_chunk_ids: List[str]  # 这个问题理论上必须召回的chunk_id，用来算召回率

def load_testset() -> List[EvalItem]:
    with open(TEST_SET_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items = []
    for d in raw:
        items.append(EvalItem(
            question=d["question"],
            expected_answer=d["expected_answer"],
            required_chunk_ids=d.get("required_chunk_ids", [])
        ))
    return items

def run_eval():
    test_items = load_testset()
    total = len(test_items)
    hit_required_chunk = 0
    correct_cnt = 0
    hallucination_cnt = 0
    detail_list = []

    for idx, item in enumerate(test_items):
        print(f"\n===== [{idx+1}/{total}] 评估问题：{item.question}")
        t0 = time.time()
        res = rag_chat(item.question)
        model_ans = res["answer"]
        sources = res["sources"]
        retrieved_chunk_ids = [s["chunk_id"] for s in sources]
        context_text = "\n".join([s["content"] for s in sources])

        # 计算召回率：所有required_chunk_ids是否全部在本次召回结果里
        all_required_hit = all(cid in retrieved_chunk_ids for cid in item.required_chunk_ids)
        if all_required_hit:
            hit_required_chunk += 1

        # 调用LLM评估是否正确、是否幻觉
        eval_prompt = EVAL_PROMPT_TPL.format(
            expected_answer=item.expected_answer,
            context_text=context_text,
            model_answer=model_ans
        )
        eval_raw = llm_service.chat(eval_prompt)
        # 清洗，提取json
        eval_raw = eval_raw.strip()
        import json as jparse
        try:
            eval_res = jparse.loads(eval_raw)
        except Exception as e:
            print(f"评估解析失败: {eval_raw}")
            eval_res = {"is_correct":0, "has_hallucination":1, "reason":"评估结果解析异常"}

        if eval_res["is_correct"] == 1:
            correct_cnt +=1
        if eval_res["has_hallucination"] ==1:
            hallucination_cnt +=1

        row = {
            "question": item.question,
            "expected_answer": item.expected_answer,
            "model_answer": model_ans,
            "retrieved_chunk_ids": retrieved_chunk_ids,
            "required_chunk_ids": item.required_chunk_ids,
            "all_required_hit": all_required_hit,
            "is_correct": eval_res["is_correct"],
            "has_hallucination": eval_res["has_hallucination"],
            "reason": eval_res["reason"]
        }
        detail_list.append(row)
        print(f"✅ 召回命中要求：{all_required_hit} | 正确：{eval_res['is_correct']} | 幻觉：{eval_res['has_hallucination']}")

    # 汇总指标
    recall = hit_required_chunk / total if total >0 else 0
    acc = correct_cnt / total if total >0 else 0
    hallucination_rate = hallucination_cnt / total if total >0 else 0

    report = {
        "meta":{
            "top_k_recall":6,
            "top_k_rerank":3,
            "chunk_size":700,
            "overlap":120
        },
        "metrics":{
            "sample_count": total,
            "chunk_recall": round(recall,4),
            "answer_accuracy": round(acc,4),
            "hallucination_rate": round(hallucination_rate,4)
        },
        "details": detail_list
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n========== 评估汇总报告 ==========")
    print(f"样本总数：{total}")
    print(f"Chunk召回率：{recall:.2%}")
    print(f"答案准确率：{acc:.2%}")
    print(f"幻觉率：{hallucination_rate:.2%}")
    print(f"报告输出至：{REPORT_PATH}")

if __name__ == "__main__":
    run_eval()
