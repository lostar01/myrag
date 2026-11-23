import requests
import time
from typing import List
from settings import settings

EMBED_URL = settings.embedding.embed_url
HEALTH_URL = settings.embedding.embed_health_url
DEFAULT_BATCH_SIZE = 16
MAX_RETRY = 2  # 重试次数


class EmbeddingClient:
    def health_check(self) -> bool:
        try:
            resp = requests.get(HEALTH_URL, timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def embed_texts(self, texts: List[str], batch_size: int = DEFAULT_BATCH_SIZE) -> List[List[float]]:
        if not texts:
            return []

        all_embeddings: List[List[float]] = []
        for start_idx in range(0, len(texts), batch_size):
            batch_texts = texts[start_idx: start_idx + batch_size]
            payload = {
                "inputs": batch_texts,
                "normalize": True,
                "truncate": True
            }

            batch_emb = None
            for retry in range(MAX_RETRY + 1):
                try:
                    resp = requests.post(
                        EMBED_URL,
                        json=payload,
                        timeout=60
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    if isinstance(data, list):
                        # TEI原生 /embed 接口
                        batch_emb = data
                    elif isinstance(data, dict):
                        if "embeddings" in data:
                            batch_emb = data["embeddings"]
                        elif "data" in data:
                            # OpenAI兼容接口
                            batch_emb = [item["embedding"] for item in data["data"]]
                        else:
                            raise ValueError(f"未知响应结构，keys: {list(data.keys())}")
                    else:
                        raise ValueError(f"非法响应类型 {type(data)}")

                    if len(batch_emb) != len(batch_texts):
                        raise ValueError(
                            f"返回数量不匹配，请求{len(batch_texts)}条，返回{len(batch_emb)}条"
                        )
                    break
                except Exception as e:
                    if retry < MAX_RETRY:
                        sleep_time = 0.5 * (2 ** retry)
                        print(f"[WARN] batch embedding failed, retry {retry+1}/{MAX_RETRY}, sleep {sleep_time}s, err: {e}")
                        time.sleep(sleep_time)
                    else:
                        raise RuntimeError(f"批次嵌入失败，已重试{MAX_RETRY}次: {e}") from e

            all_embeddings.extend(batch_emb)
        return all_embeddings

    def embed_text(self, text: str) -> List[float]:
        vecs = self.embed_texts([text])
        return vecs[0]


embedding_client = EmbeddingClient()
