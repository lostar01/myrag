import requests
from typing import List, Dict, Optional
from settings import settings

RERANK_URL = settings.rerank.rerank_url
HEALTH_URL = settings.rerank.rerank_health_url

class RerankClient:
    def health_check(self) -> bool:
        try:
            resp = requests.get(HEALTH_URL, timeout=3)
            print(resp)
            return resp.status_code == 200
        except Exception:
            return False

    def rerank(
        self,
        query: str,
        chunks: List[Dict],
        top_n: int,
        min_score: Optional[float] = None,
    ) -> List[Dict]:
        """重排 + 取 top_n（P0 #6）。

        - min_score 不传时用 `config.yaml` 的 `retrieval.rerank_min_score`；
          阈值 <= 0 表示不过滤。
        - 不再原地 sort 调用方传进来的列表（以前会改到调用方的数据），
          也不原地往原 chunk 里写分数：返回的是新 dict 列表。
        - 全部低于阈值 ⇒ 返回 []，上层回答“知识库中未找到相关内容”。
        """
        if not chunks:
            return []
        if min_score is None:
            min_score = settings.retrieval.rerank_min_score

        if not self.health_check():
            raise ConnectionError("Rerank TEI服务不可用")

        passages = [c["content"] for c in chunks]
        payload = {
            "query": query,
            "texts": passages,
            "raw_scores": False,
            "return_text": False,
            "truncate": False,
            "truncation_direction": "right"
        }
        resp = requests.post(
            RERANK_URL,
            json=payload,
            timeout=60
        )
        resp.raise_for_status()
        res = resp.json()
        # TEI 返回结构：[{"index": 0, "score": 0.88}, ...]
        scores_map = {item["index"]: item["score"] for item in res}
        # 回填分数到新 dict（不动原列表 / 原 dict）
        annotated: List[Dict] = []
        for idx, c in enumerate(chunks):
            if idx not in scores_map:
                raise ValueError(
                    f"重排服务返回缺少 index={idx} 的分数（请求 {len(chunks)} 条，返回 {len(scores_map)} 条）"
                )
            item = dict(c)
            item["rerank_score"] = float(scores_map[idx])
            annotated.append(item)
        # 按分数降序（在本地副本上排，不影响调用方）
        annotated.sort(key=lambda x: x["rerank_score"], reverse=True)
        # 分数下限：低于阈值的一律剔掉，不送进 prompt
        if min_score and min_score > 0:
            annotated = [c for c in annotated if c["rerank_score"] >= min_score]
        return annotated[:top_n]

rerank_client = RerankClient()