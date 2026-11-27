"""重排客户端（bge-reranker-v2-m3 / TEI `/rerank`）。

两个层次：
  - `rerank_all()`：对**全部**候选打分并返回（不再截断）。多文档覆盖的选择逻辑要看到完整分数分布，
    所以截断交给上层（`context_selector`）。
  - `rerank()`：**旧接口，语义保持不变**（打分 + 阈值过滤 + 取 top_n），内部复用 `rerank_all()`。
    其它调用方（脚本 / 旧代码）零改动。

为什么必须分批（不是可选优化）：
  TEI `/info` 的限制是 `max_client_batch_size=32`、`max_batch_tokens=16384`。
  本库 chunk `p50≈700` token ⇒ 一个请求里超过 23 条就会超 batch token（实测 24 条 16800 token）。
  召回按文档分组后候选可达 32 条，不分批必然 413/报错。
  实测耗时（按 16 条一批）：16 条≈1.2s、24 条≈1.9s、40 条≈3.0s。
"""

from typing import Dict, List, Optional

import requests

from settings import settings

RERANK_URL = settings.rerank.rerank_url
HEALTH_URL = settings.rerank.rerank_health_url


class RerankClient:
    def health_check(self) -> bool:
        try:
            resp = requests.get(HEALTH_URL, timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _batch_size(total: int) -> int:
        size = int(getattr(settings.retrieval, "rerank_batch_size", 16) or 16)
        return max(1, min(size, total))

    def _score_batch(self, query: str, texts: List[str]) -> List[float]:
        """对一批文本打分，返回与 `texts` 顺序一致的分数列表。"""
        payload = {
            "query": query,
            "texts": texts,
            "raw_scores": False,          # sigmoid 分数（0~1），与 rerank_min_score 口径一致
            "return_text": False,
            "truncate": False,            # 交给上层控制长度，不做隐式截断
            "truncation_direction": "right",
        }
        resp = requests.post(RERANK_URL, json=payload, timeout=120)
        resp.raise_for_status()
        res = resp.json()
        # TEI 返回：[{"index": 0, "score": 0.88}, ...]（index 是**本批内**的下标）
        scores_map = {item["index"]: item["score"] for item in res}
        if len(scores_map) != len(texts):
            raise ValueError(
                f"重排服务返回数量不匹配：请求 {len(texts)} 条，返回 {len(scores_map)} 条"
            )
        out = []
        for i in range(len(texts)):
            if i not in scores_map:
                raise ValueError(f"重排服务返回缺少 index={i} 的分数")
            out.append(float(scores_map[i]))
        return out

    def rerank_all(
        self,
        query: str,
        chunks: List[Dict],
        min_score: Optional[float] = None,
    ) -> List[Dict]:
        """给全部候选打分，返回按分数降序的新 dict 列表（不截断、不改入参）。

        - `min_score` 不传时用 `retrieval.rerank_min_score`；<=0 表示不过滤；
        - 排序键带 tie-break（分数 → chunk_id），分数扎堆时结果也可复现；
        - 不原地修改调用方列表/字典（返回的是新 dict）。
        """
        if not chunks:
            return []
        if min_score is None:
            min_score = settings.retrieval.rerank_min_score

        if not self.health_check():
            raise ConnectionError("Rerank TEI服务不可用")

        size = self._batch_size(len(chunks))
        scores: List[float] = []
        for start in range(0, len(chunks), size):
            batch = chunks[start:start + size]
            scores.extend(self._score_batch(query, [c["content"] for c in batch]))

        annotated: List[Dict] = []
        for chunk, score in zip(chunks, scores):
            item = dict(chunk)
            item["rerank_score"] = score
            annotated.append(item)

        # 稳定且可复现：分数相同按 chunk_id 排（不能让并列名次随输入顺序漂移）
        annotated.sort(key=lambda x: (-x["rerank_score"], str(x.get("chunk_id", ""))))

        if min_score and min_score > 0:
            annotated = [c for c in annotated if c["rerank_score"] >= min_score]
        return annotated

    def rerank(
        self,
        query: str,
        chunks: List[Dict],
        top_n: int,
        min_score: Optional[float] = None,
    ) -> List[Dict]:
        """旧接口（保持语义）：打分 + 阈值过滤 + 取 top_n。

        多文档覆盖场景请直接用 `rerank_all()` + `context_selector`，
        否则候选会被这里提前截断，后面的分组/覆盖逻辑拿不到足够信息。
        """
        return self.rerank_all(query, chunks, min_score=min_score)[:top_n]


rerank_client = RerankClient()
