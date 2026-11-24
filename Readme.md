
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

## 初始化数据库（建表）

容器起来后执行一次 `sql/init.sql`（**幂等，可重复执行**；老库升级也在同一个文件里）：

```bash
# 方式一：本机有 psql
psql "postgresql://raguser:Rag%23123456@127.0.0.1:5432/rag_db" -f sql/init.sql

# 方式二：直接用 docker 里的 psql（无需安装客户端）
docker exec -i postgres psql -U raguser -d rag_db < sql/init.sql

# 方式三：Windows PowerShell
get-content sql/init.sql | docker exec -i postgres psql -U raguser -d rag_db
```

脚本内容（按依赖顺序，所有建表/索引都是 `IF NOT EXISTS`）：

| 段 | 内容 |
| --- | --- |
| 1 | `rag_user` / `chat_session` / `chat_message` |
| 2 | `rag_doc`（文档权威表：文件名 / md5 / 大小 / 页数 / 状态）、`rag_chunk`（含 `doc_id` 外键） |
| 3 | `rag_task`（每次解析任务，`doc_id` 关联 `rag_doc`） |
| 4 | `rag_qa_log`（问答日志 / 反馈） |
| 5 | 老库升级（DO 块，存在性判断 + 异常兜底；新库自动跳过） |
| 6 | 表和字段注释 |

> 注意：`rag_doc(file_md5)` 有部分唯一索引（同一文件只允许一条记录）。若老库里已有重复 md5，
> 建索引会失败——脚本会只告警不中断，并提示排查语句：
> `SELECT file_md5, COUNT(*) FROM rag_doc WHERE file_md5 IS NOT NULL GROUP BY file_md5 HAVING COUNT(*) > 1;`

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

## 上传记录

文档列表已改为「上传记录」：**以 `rag_doc` 为准**（一条 = 一篇文档），
其余信息（状态 / 进度 / 失败原因 / 文件大小 / 上传人 / 时间）全部通过 `doc_id` 级联 `rag_task` 获取
（同一 `doc_id` 有多条任务时取最新一条）。上传时即插入 `rag_doc(status='processing')` 占位行，
所以等待/解析中的记录也能立刻看到。

- 状态：`pending`（等待解析）/ `running`（解析中）/ `processing`（排队中）/ `success`（成功）/ `failed`（失败，悬停状态看失败原因）
  —— 有任务行时取 `rag_task.status`，没有任务行的历史文档取 `rag_doc.status`
- 列表字段：文件名（`rag_doc.doc_name`）、页数（`rag_doc.page_count`）、状态、上传时间
  （管理端另加「操作/删除」列）。接口仍返回 `file_size`（`rag_doc.file_size`）与 `file_md5`
  （`rag_doc.file_md5`），但**不在列表展示**。
- 文件名：直接取 `rag_doc.doc_name`（上传时写入，权威值）；为空时才退回 `rag_task.file_name`，仍为空则显示「（未知文件名）」并在悬停提示里给出 `doc_id`，**不会用 uuid 当文件名**。
- 列宽提示：状态徽标已加 `white-space: nowrap`，窄屏不会再被拆成“成 / 功”两行。
- 可见范围：**用户端只显示自己的上传记录**；管理端显示全部记录并可删除
- 失败记录**保留**在列表中（不再自动清理）
- 重传规则：上传时先按 md5 查 `rag_doc`，
  - 已入库成功 → 提示「文件已存在，无需重复上传」
  - 上次解析失败 → 提示「请管理员在「上传记录」中删除后重传」
  - 同一文件正在解析中 → 提示「正在解析中，请稍后再试」
  以上三种情况都**不提交 Celery 任务**；只有管理员在「上传记录」中删除该记录（按 `doc_id`
  同时清理 `rag_task` / `rag_chunk` / Qdrant 向量 / `rag_doc` 与 `upload_cache` 源文件）后，才能重新上传。
- 解析中的记录不可删除（避免后台任务写回脏数据），完成后才能删。
- 卡死兼容：若某条记录超过 **30 分钟**仍停在 `pending` / `running` / `processing`（worker 被强杀、容器重启等），
  则视为已卡死，解锁管理员的删除操作（时间阈值见 `src/doc_manager.py` 的 `STALE_ACTIVE_SQL`）。
  注意：重传依旧受“记录存在即不可传”的约束（`rag_doc` 已有该 md5），仍需先删除记录。
- 惰性自愈（阈值 **2 小时**）：每次读取 `/uploads`（最多 1 分钟先执行一次）会把「超过 2 小时仍停在
  `pending` / `running`」的任务置为 `failed`，并写入原因「任务超时未完成（worker 未运行、崩溃或代码版本不匹配），
  请删除记录后重传」；对应的 `rag_doc` 若还是 `processing` 也一并改为 `failed`（若无其他存活任务）。
  实现：`doc_manager.heal_stale_tasks()`（阈值常量 `STALE_HEAL_SQL`），调用点 `main_api._heal_stale_if_due()`
  （节流 60s，异常不影响列表）。
- 状态优先级：`rag_doc` 已是 `success` / `failed` 时以文档为准，否则取 `rag_task.status`。
  这样遗留的卡死任务行被治愈后，已成入库的文档不会在列表里错误显示为失败。

### 上传记录接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/upload` | 登录 | 上传（md5 查重，重复直接 409） |
| GET | `/task/{task_id}` | 登录 | 查询单个任务状态 |
| GET | `/uploads` | 登录 | 上传记录列表（用户只返回自己的，管理员返回全部） |
| DELETE | `/uploads/{record_id}` | 管理员 | 删除上传记录，`record_id` 就是 `doc_id` |
| GET | `/documents` | 登录 | 旧接口：已入库文档列表（保留兼容） |
| DELETE | `/documents/{doc_id}` | 管理员 | 旧接口：按 doc_id 删除文档（保留兼容） |

> 表结构变更（`sql/init.sql`）：
> - `rag_doc`：新增 `file_size`（文件大小）——**md5 / 大小 / 文件名都是文档属性，只存在 `rag_doc`**；
> - `rag_doc(file_md5)` 部分唯一索引（一文件一记录）；
> - `rag_task`：只保留任务侧信息（`doc_id` / `file_name` / `file_path` / `status` / `progress` /
>   `error_msg` / `user_id` / 时间）。原 `rag_task.file_md5`、`rag_task.file_size` 已删除
>   （查询前先建 `rag_doc` 占位行，查重与大小都以 `rag_doc` 为准）。
> 升级前若历史数据存在同 md5 多条记录，需先清理再建唯一索引：
> `SELECT file_md5, COUNT(*) FROM rag_doc WHERE file_md5 IS NOT NULL GROUP BY file_md5 HAVING COUNT(*) > 1;`

## 上传文件入库
```
curl -X POST "http://127.0.0.1:8000/upload" -H "Authorization: Bearer <token>" -F "file=@C:\Users\lostar\Downloads\中国社会各阶层分析 (梁晓声) (Z-Library)-1.pdf"
```
