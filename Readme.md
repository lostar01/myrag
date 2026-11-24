
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
uvicorn main_api:app --host 0.0.0.0 --port 8000 --workers 2
```

## 用户与权限

系统内置用户体系，管理端与用户端分离，默认**关闭自助注册**，账号由管理员统一创建。

### 相关环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAG_ALLOW_REGISTER` | `false` | 是否开放自助注册（`true`/`1`/`yes` 开启） |
| `RAG_ADMIN_USERNAME` | `admin` | 首次启动引导创建的管理员用户名 |
| `RAG_ADMIN_PASSWORD` | `admin123` | 首次启动引导创建的管理员密码（请尽快修改） |
| `RAG_SECRET_KEY` | `change-me-in-production-please` | Token 签名密钥，**生产环境务必修改** |
| `RAG_TOKEN_TTL` | `604800` | Token 有效期（秒），默认 7 天 |

### 首次启动引导

服务启动时若库中不存在任何管理员，会自动用 `RAG_ADMIN_USERNAME` / `RAG_ADMIN_PASSWORD`
创建默认管理员（若该用户名已被普通用户占用，则提升为管理员）。登录后请立即修改默认密码。

### 角色说明

- `admin`：管理员，可进入「用户管理」页面，创建用户、重置密码、分配角色、启用/禁用、删除用户，并可删除文档。
  管理端只做用户与文档管理，不提供对话功能。
- `user`：普通用户，仅可使用对话、上传文档、管理自己的会话。

> 会话为个人私有：`/sessions` 系列接口只返回/操作当前登录用户自己的会话，管理员也无法查看他人对话。

### 用户管理接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/auth/config` | 公开 | 返回是否开放注册，供前端决定是否显示注册入口 |
| POST | `/users/register` | 公开（需开启注册） | 自助注册 |
| POST | `/users/login` | 公开 | 登录，返回 token |
| GET | `/users/me` | 登录 | 当前用户信息 |
| PUT | `/users/me/password` | 登录 | 修改自己的密码（需旧密码） |
| GET | `/users` | 管理员 | 用户列表 |
| POST | `/users` | 管理员 | 创建用户 |
| GET | `/users/{user_id}` | 管理员 | 查询单个用户 |
| PUT | `/users/{user_id}` | 管理员 | 修改昵称/角色/状态 |
| PUT | `/users/{user_id}/password` | 管理员 | 重置指定用户密码 |
| DELETE | `/users/{user_id}` | 管理员 | 删除用户 |

> 安全约束：管理员不能修改自己的角色、不能禁用自己的账号、不能删除自己的账号，避免系统失去管理入口。

### 前端

前端位于 `web/` 目录，已拆分为**用户端**与**管理端**：

| 入口 | 说明 |
| --- | --- |
| `web/index.html` | **登录入口**（无身份选择）：登录后默认进入用户端 |
| `web/user/index.html` | 用户端：对话、文件上传、修改自己的密码；管理员侧边栏额外显示「🛠 管理端」入口 |
| `web/admin/index.html` | 管理控制台（ChatGPT 风格布局）：用户创建/角色授权/启用禁用/重置密码/删除、文档上传/列表/删除；不含对话功能 |

打开方式：直接用浏览器打开 `web/index.html` 即可（默认请求 `http://127.0.0.1:8000`，
可在 `web/common.js` 顶部 `API_BASE` 修改）。

登录与跳转规则：

- 登录（或注册）成功后**统一先进入用户端** `web/user/index.html`
- 管理员可在用户端侧边栏点「🛠 管理端」切换到管理控制台；普通用户看不到该入口
- 已登录（本地存在有效 token）时打开 `web/index.html` 直接进入用户端
- 未登录直接访问 `web/user/` 或 `web/admin/` 会被重定向回 `web/index.html`；
  非管理员访问管理端会看到「无权限访问」提示
- 注册入口需服务端开启 `RAG_ALLOW_REGISTER`

- 两端共用 `web/style.css` 与 `web/common.js`，共用 `localStorage["rag_token"]`，登录一次即可切换端。
- `web/app.js` 为拆分前的旧单页逻辑，已不再被任何页面引用（legacy，保留备查）。

## 上传文件入库
```
curl -X POST "http://127.0.0.1:8000/upload" -H "Authorization: Bearer <token>" -F "file=@C:\Users\lostar\Downloads\中国社会各阶层分析 (梁晓声) (Z-Library)-1.pdf"
```
