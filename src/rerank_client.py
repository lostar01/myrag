import requests
from typing import List, Dict
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

    def rerank(self, query: str, chunks: List[Dict], top_n: int):
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
        # 回填分数到原有chunk
        for idx, c in enumerate(chunks):
            c["rerank_score"] = float(scores_map[idx])
        # 按分数降序，取top_n
        chunks.sort(key=lambda x: x["rerank_score"], reverse=True)
        return chunks[:top_n]

rerank_client = RerankClient()