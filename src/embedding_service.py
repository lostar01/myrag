from typing import List, Union
from sentence_transformers import SentenceTransformer
from settings import settings
import torch

class EmbeddingService:
    def __init__(self):
        self.model_name = settings.embedding.model_name
        self.local_files_only = settings.embedding.local_files_only
        self.device = settings.embedding.device  # "cuda" / "cpu"

        # 自动校验设备，cuda不可用自动切cpu
        if self.device == "cuda" and not torch.cuda.is_available():
            print("[Embedding] 警告：配置cuda，但CUDA不可用，自动降级到cpu")
            self.device = "cpu"
        
        # bge-m3
        self.model = SentenceTransformer(
            self.model_name,
            local_files_only=self.local_files_only,
            device=self.device
        )

    def encode(self, text: str) -> List[float]:
        """单条文本向量化"""
        vec = self.model.encode(text, normalize_embeddings=True)
        return vec.tolist()

    def batch_encode(self, texts: List[str]) -> List[List[float]]:
        """批量向量化，加速导入"""
        vecs = self.model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vecs]


# 全局单例，只加载一次
embedding_service = EmbeddingService()
