
## 启动 Qdrant
```
docker run -d --name qdrant \
  -p 6333:6333 -p 6334:6334 \
  -v /opt/qdrant_storage:/qdrant/storage \
  -e QDRANT__SERVICE__API_KEY="$QDRANT_API_KEY" \
  -e QDRANT__SERVICE__READ_ONLY_API_KEY="$QDRANT_READ_KEY" \
  qdrant/qdrant:latest

```

## 启动Postgresql
```
docker run -d \
  --name postgres \
  -e POSTGRES_USER=raguser \
  -e POSTGRES_PASSWORD=Rag#123456 \
  -e POSTGRES_DB=rag_db \
  -v /opt/postgresql/data:/var/lib/postgresql/data \
  -p 5432:5432 \
  --restart unless-stopped \
  postgres:16
```

## 启动Redis
```
docker run -d --name redis -p 6379:6379 --restart unless-stopped redis
```

## 启动BGE-M3
```
#预先下载
export HF_ENDPOINT=https://hf-mirror.com
hf download BAAI/bge-m3 --cache-dir /opt/hf_cache

#启动容器
docker run -d --name bge-m3 --gpus all -p 8080:80 -v /opt/hf_cache:/data \
ghcr.io/huggingface/text-embeddings-inference:cuda-1.9 \
--model-id BAAI/bge-m3 \
--max-batch-tokens 8192
```

## 启动
```
#预先下载
export HF_ENDPOINT=https://hf-mirror.com
hf download BAAI/bge-reranker-v2-m3 --cache-dir /opt/hf_cache

#启动容器
docker run -d --name bge-ranker --gpus all -p 8081:80 -v /opt/hf_cache:/data \
ghcr.io/huggingface/text-embeddings-inference:cuda-1.9 \
--model-id BAAI/bge-reranker-v2-m3
```

## 下载 docling 模型
```
docling-tools models download -o "D:\code\rag\docling_models"
```

## 启动 celery
```
celery -A celery_app worker --loglevel=INFO --pool=solo
```

## 启动 main-api

```
uvicorn main_api:app --reload
```

## 上传文件入库
```
curl -X POST "http://127.0.0.1:8000/upload" -F "file=@C:\Users\lostar\Downloads\中国社会各阶层分析 (梁晓声) (Z-Library)-1.pdf"
```