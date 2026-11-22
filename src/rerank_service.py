from typing import List, Dict
from sentence_transformers import CrossEncoder
import torch
from settings import settings

class RerankService:
    def __init__(self):
        # bge-reranker-v2-m3
        self.model_name = settings.rerank.model_name
        # 自动判断设备
        if torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        self.model = CrossEncoder(self.model_name, 
                                  device=self.device, 
                                  local_files_only=settings.rerank.local_files_only
                                  )

    def rerank(self, query: str, chunk_list: List[Dict], top_n: int = 3, threshold: float = 0.0):
        """
        chunk_list 就是 retrieve 返回的列表
        threshold：低于这个分数直接丢弃
        """
        if not chunk_list:
            return []
        pairs = [[query, item["content"]] for item in chunk_list]
        scores = self.model.predict(pairs)
        # 绑定分数，排序
        for idx, item in enumerate(chunk_list):
            item["rerank_score"] = float(scores[idx])
        # 过滤 + 降序
        filtered = [i for i in chunk_list if i["rerank_score"] >= threshold]
        filtered.sort(key=lambda x: x["rerank_score"], reverse=True)
        return filtered[:top_n]

rerank_service = RerankService()
