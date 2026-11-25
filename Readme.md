
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

先把下面的 `$PGPASSWORD` 换成你自己的强口令（别沿用仓库里历史上的示例口令，那个已经在 git 历史里，见下方「配置」节的告警）：

```
docker run -d \
  --name postgres \
  -e POSTGRES_USER=raguser \
  -e POSTGRES_PASSWORD="$PGPASSWORD" \
  -e POSTGRES_DB=rag_db \
  -v /opt/postgresql/data:/var/lib/postgresql/data \
  -p 5432:5432 \
  --restart unless-stopped \
  postgres:16
```

> 口令只给容器和本机的 `config/config.yaml`（两者必须一致），**不要写进仓库里的任何文件**。
> 不想在命令行里留明文：改用 `-e POSTGRES_PASSWORD_FILE=/run/secrets/pg_password`，
> 或 PowerShell 里 `Read-Host -AsSecureString` 临时输入。

## 初始化数据库（建表）

容器起来后执行一次 `sql/init.sql`（**幂等，可重复执行**；老库升级也在同一个文件里）：

```bash
# 方式一：本机有 psql（口令走环境变量 PGPASSWORD，命令行里不写死；变量名大小写敏感）
PGPASSWORD="$PGPASSWORD" psql -h 127.0.0.1 -U raguser -d rag_db -f sql/init.sql

# 方式二：直接用 docker 里的 psql（无需安装客户端；容器内走本地 socket，不需要密码）
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
| 5 | 老库升级（DO 块，存在性判断 + 异常兜底；新库自动跳过；含 `rag_user.token_epoch`）|
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

## 配置：真实配置不入库（`config/config.yaml`）

仓库里只放**模板** `config/config.yaml.example`；真实配置 `config/config.yaml` 已从 git 跟踪里
拿掉（`git rm --cached`，本地文件保留）并被 `.gitignore` 排除。

首次拉仓库后：

```powershell
# PowerShell：复制模板后把里面所有 CHANGE_ME 换掉
Copy-Item config/config.yaml.example config/config.yaml
```

```bash
cp config/config.yaml.example config/config.yaml
```

必改的占位值：

| 字段 | 换成什么 |
| --- | --- |
| `auth.secret_key` | **token 签名密钥**：`python -c "import secrets; print(secrets.token_hex(32))"`（至少 32 字符，建议 64） |
| `pg.password` | 你的 PG 强口令（要和 postgres 容器那个一致） |
| `qdrant.api_key` | Qdrant 的 API key（没开鉴权就删掉这行） |
| `llm.api_key` | LLM 服务的 key（Ollama 不校验，填 `ollama`） |
| 各 `host` / `*_url` | 服务地址（模板里默认写的是 `127.0.0.1`） |
| `docling_parser.docling_artifacts_dir` / `output_dir`、`embedding.model_name` 等 | 自己机器上的路径 |

三点补充：

- **没填完不会静默跑**：启动时会检查配置，值里还带 `CHANGE_ME` 就直接 `RuntimeError` 并列出具体的
  字段路径（比后面“连不上服务”好排查）；找不到配置文件也会报错并告诉你怎么复制 / 怎么用 `RAG_CONFIG`。
- **token 签名密钥就在本文件的 `auth.secret_key`**（环境变量 `RAG_SECRET_KEY` 可覆盖它，优先级更高）。
  放 yaml 的理由：多个进程（uvicorn 多 worker + celery）读同一份配置 ⇒ 不会出现“进程间密钥不同 →
  登录后随机 401”，而且和 pg/qdrant 凭据一起备份、一起迁移。启动日志会有一行
  `[auth] token 签名密钥来自 …`（只报来源与长度，不报密钥），用来确认哪个源生效。
  凭据集中到一个文件后要把权限收紧：
  ```powershell
  icacls config\config.yaml /inheritance:r /grant:r "%USERNAME%:F"   # Windows
  # Linux: chmod 600 config/config.yaml
  ```
- **可以把真实配置放到仓库外**，用 `RAG_CONFIG` 指过去（代码目录里一个凭据都不留）：
  ```powershell
  $env:RAG_CONFIG = "D:/rag_secrets/myrag.yaml"     # bash: export RAG_CONFIG=/etc/myrag/config.yaml
  ```
  相对路径按仓库根解析；**API 进程与 celery worker 必须指同一个文件**。

> ⚠️ **必须轮换凭据**：`config/config.yaml` 以前是被 git 跟踪的（`c60f6b9` first commit、
> `0971aed`），里面的 PG 口令、Qdrant API key，以及 Readme 里那个示例 PG 口令都已经在
> GitHub 历史里。停止跟踪不会抹掉历史 ⇒ 只要仓库是公开的（或以后可能变公开），这些凭据就算泄露。
> 至少要做：
> 1. 改 PG 口令、换 Qdrant API key、换 LLM key；
> 2. 确认仓库可见性（公开 ⇒ 当已泄露处理）；
> 3. 真要抹历史时用 `git filter-repo --path config/config.yaml --invert-paths`（或 BFG），
>    代价：重写所有 commit 哈希 + 需要 force push + 协作者必须重新 clone。
>    **抹历史不能替代轮换口令**（已拉过仓库的人手里已经有副本）。

## 依赖与版本锁定

两个文件，分工明确：

| 文件 | 作用 |
| --- | --- |
| `requirement.txt` | 安装清单，带**版本边界**（如 `docling>=2.0,<3.0`）。新环境从零装用这个 |
| `requirements.lock.txt` | **逐版本**锁定（当前能跑的环境）。部署 / 复现 bug 时装这个 |

生成 lock（必须在装好依赖的那个环境里跑；本机 `src/__pycache__` 是 cpython-310 ⇒ Python 3.10）：

```powershell
# 仓库根目录下执行
powershell -ExecutionPolicy Bypass -File scripts\pin_requirements.ps1
# 解释器不在 PATH 里时：
powershell -ExecutionPolicy Bypass -File scripts\pin_requirements.ps1 -Python "D:\path\to\python.exe"
```

为什么必须钉版本：P0 #4 的页码链路靠 docling 导出时的页边界行为，D7-a 的切块结果也跟 docling
输出的 markdown 形状直接相关——docling 一升级就可能变。
（代码里已有兜底：docling 太旧、`export_to_markdown` 不认 `page_break_placeholder` 时只关页码，
不影响解析；但版本漂导致的切块差异不会报错，只能靠 lock 防。）

装完建议先做一次语法自检（不需要外部服务起来）：

```powershell
python -m compileall src
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

### 启动前必须配好 token 签名密钥（两处任选，环境变量优先）

两处都是空就 `RuntimeError` 拒绝启动。**推荐写在 yaml 里**（不入库，重启/迁移都不会变）：

```yaml
# config/config.yaml
auth:
  secret_key: "<64 个十六进制字符>"
  token_ttl: 604800        # 可选，token 有效期（秒）；环境变量 RAG_TOKEN_TTL 优先
```

也可以用环境变量覆盖（优先级更高，适合 docker `-e` / systemd `EnvironmentFile`）：

```powershell
# PowerShell
python -c "import secrets; print(secrets.token_hex(32))"   # 先拿一个随机值
$env:RAG_SECRET_KEY = '<刚生成的值>'
```

```bash
# bash
export RAG_SECRET_KEY="$(openssl rand -hex 32)"
```

> **密钥值要长期固定，不要每次启动换一个**：换一个 ⇒ 旧 token 签名全不匹配 ⇒ **所有已登录用户下次
> 请求就 401**（前端会踢回登录页）；但数据、已排队的解析任务都不受影响（celery 链路不校验 token）。
> 更坑的是“各进程值不同”（比如每个进程启动时才随机生成）：同一个 token 在别的 worker 验不过，
> 表现为“登录后时好时坏、上传到一半 401”的随机掉线，极难排查——这就是代码不允许“没配就随机生成”的原因。
>
> 想确认哪个源生效：启动日志里有一行 `[auth] token 签名密钥来自 …`（只报来源与长度）。
> 本地开发想跳过检查：`$env:RAG_ALLOW_INSECURE_SECRET = "1"`（会打醒目告警）。

## 用户与权限

系统内置用户体系，管理端与用户端分离，默认**关闭自助注册**，账号由管理员统一创建。

### 相关环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RAG_ALLOW_REGISTER` | `false` | 是否开放自助注册（`true`/`1`/`yes` 开启） |
| `RAG_ADMIN_USERNAME` | `admin` | 首次启动引导创建的管理员用户名 |
| `RAG_ADMIN_PASSWORD` | `admin123` | 首次启动引导创建的管理员密码（请尽快修改） |
| `RAG_SECRET_KEY` | 无默认值（可改用 `config.yaml` 的 `auth.secret_key`） | Token 签名密钥。**两处都空就拒绝启动**；环境变量优先。生成：`python -c "import secrets; print(secrets.token_hex(32))"` |
| `RAG_ALLOW_INSECURE_SECRET` | 未设 | 仅本地开发：置 `1` 时允许用源码里的内置密钥（会打醒目安全告警）。**生产切勿设** |
| `RAG_TOKEN_TTL` | `604800`（可改用 `auth.token_ttl`） | Token 有效期（秒），默认 7 天；环境变量优先 |

> **签名密钥为什么必需**：以前没设时会静默使用写在源码里的默认密钥 ⇒
> 任何拿到仓库的人都能自签任意 `user_id` / `role` 的 token；而且伪造时可以把 `iat` 写成当前时间，
> **直接绕过 `token_epoch` 撤销**（“改密 / 禁用后旧 token 失效”这道防线就形同虚设）。
> 现在不配就会 `RuntimeError` 拒绝启动，错误信息里直接给出三种生成密钥的命令。
>
> 三个注意：
> - **推荐写在 `config.yaml` 的 `auth.secret_key`**：与 pg/qdrant 凭据同处备份、多进程读同一份；
>   要用环境变量覆盖就设 `RAG_SECRET_KEY`。（另：`auth` 段自己的环境变量前缀是 `RAG_AUTH_`，
>   即 `RAG_AUTH_SECRET_KEY` 也能填，但建议统一用 `RAG_SECRET_KEY`）
> - **API 进程（含多个 uvicorn worker）与 celery worker 必须读到同一个值**，否则 token 互不认账；
> - 换密钥会让所有已登录 token 失效（用户需重新登录），选个低峰时段做；不要每次启动换一个。

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

### Token 失效与重新登录

Token 本身只装身份线索（`user_id` / `role` / `iat` / `exp`），**能不能用、是什么角色，全部以数据库为准**：
每次请求都会按主键查一次 `rag_user`，并检查三件事：账号存在、`status='active'`、`iat >= token_epoch`。

`rag_user.token_epoch`（`BIGINT DEFAULT 0`）是“全量失效时间点”：把账号的 `token_epoch` 刷到当前时间，
之前签发的所有 token 全部失效（`sql/init.sql` 第 5 段会自动给老库补列，无需手工迁移）。

下列操作会**当场作废该用户已登录的所有会话**，需要重新登录：

| 操作 | 执行者 |
| --- | --- |
| 修改自己的密码 | 本人 |
| 管理员重置某用户密码 | 管理员 |
| 管理员修改角色（`role`） | 管理员 |
| 管理员启用 / 禁用账号（`status`） | 管理员 |

补充：被禁用 / 改角色的用户即使 token 还没到期，下一次请求也会被直接 401（不看 token 里的旧 `role`，
授权用的是数据库里的当前角色）。删除用户同样会让其 token 失效（查不到用户 ⇒ 401）。

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

### 会话标题自动命名

新建会话默认叫「新对话」；用户发出**第一条提问**后，后端自动提炼主题作为标题（侧边栏与页头同步更新）。

- 实现：`src/title_service.py` → `generate_title(query)`：先让 LLM 提炼（≤ 16 个汉字、去标点引号），
  失败 / 超时（5s）/ 返回异常时退化为「问题原文截断」，永远有值；
- 触发时机：`rag_chat_qa` 中发现「新会话 + 首问 + 标题仍为占位值」才命名（`_should_autotitle`），
  第二问及以后不会再改标题；
- 不拖慢回答：标题在 `done` 事件**之后**才生成，前端收到 `done` 立即恢复输入框；
  生成成功时流式接口额外下发一帧 `{"type":"title","title":"...","chat_id":"..."}`，
  前端直接更新侧边栏与页头，无需额外请求（失败时下次刷新列表也会拿到已写入 DB 的标题）。

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
| GET | `/documents` | 登录 | 旧接口：已入库文档列表（保留兼容；**已按权限过滤**：管理员看全部，普通用户只看自己上传的） |
| DELETE | `/documents/{doc_id}` | 管理员 | 旧接口：按 `doc_id` 删除文档（保留兼容） |

> 表结构变更（`sql/init.sql`）：
> - `rag_doc`：新增 `file_size`（文件大小）——**md5 / 大小 / 文件名都是文档属性，只存在 `rag_doc`**；
> - `rag_doc(file_md5)` 部分唯一索引（一文件一记录）；
> - `rag_task`：只保留任务侧信息（`doc_id` / `file_name` / `file_path` / `status` / `progress` /
>   `error_msg` / `user_id` / 时间）。原 `rag_task.file_md5`、`rag_task.file_size` 已删除
>   （查询前先建 `rag_doc` 占位行，查重与大小都以 `rag_doc` 为准）。
> 升级前若历史数据存在同 md5 多条记录，需先清理再建唯一索引：
> `SELECT file_md5, COUNT(*) FROM rag_doc WHERE file_md5 IS NOT NULL GROUP BY file_md5 HAVING COUNT(*) > 1;`

## 配置项（`config/config.yaml`）

> 这个文件**不入库**，模板是 `config/config.yaml.example`（见上方「配置：真实配置不入库」）。

配置里新加了三块，**都是可选的**：旧的 `config.yaml` 没有这些段也能正常启动（默认值写在代码
`src/settings.py` 的 `UploadSettings` / `RetrievalSettings` / `DoclingParserSettings` 里）。

### `upload:`：上传目录与限制

```yaml
upload:
  dir: "src/upload_cache"     # 相对路径按仓库根解析；写绝对路径（如 D:/rag_data/upload_cache）则原样用
  max_size_mb: 50             # 单个文件上限
  allowed_suffixes: [".pdf", ".docx", ".pptx", ".xlsx", ".md", ".html", ".htm", ".txt"]
```

- 上传目录**只从 config.yaml 读**（不用环境变量）。以前代码里写死 `Path("upload_cache")`，
  进程从哪个目录启动就会在哪建目录（uvicorn 与 celery 的 CWD 不同 ⇒ 文件会落到不同地方）。
  现在 `src/upload_paths.py::UPLOAD_DIR` 是绝对路径，相对路径一律按仓库根（`config/` 的上一级）解析，
  两个进程在任何目录下都算出同一个路径；启动日志会打印实际使用的上传目录。
- 删除上传文件（`/uploads/{doc_id}`、`/documents/{doc_id}`）时，旧库里存的是相对路径
  （如 `upload_cache/xxx.pdf`）：现在按 `UPLOAD_DIR` 解析后再删，**老数据也能删干净**
  （不需要迁移历史行）。
- 路径校验：所有上传文件路径都要经过 `resolve_upload_path()`，解到 `UPLOAD_DIR` 之外的路径会被拒绝。
- 挂数据卷 / 换盘：改 `upload.dir` 为绝对路径即可，无需改代码。
- **上传目录不会被提交进仓库**：`.gitignore` 已同时忽略 `src/upload_cache/` 与仓库根的 `upload_cache/`
  （历史上因为 uvicorn 与 celery 的 CWD 不同，两处都建过目录；遗留文件已挑到 `src/upload_cache/`）。

### `retrieval:`：召回与重排阈值

```yaml
retrieval:
  recall_top_k: 6        # 向量召回条数
  rerank_top_k: 3        # 重排后送给 LLM 的条数
  cosine_min_score: 0.35 # 余弦下限（0 = 关闭过滤）
  rerank_min_score: 0.10 # 重排分下限（0 = 关闭过滤）
```

- 修复前**没有任何分数阈值**：向量相似度再低也算召回、重排分再差也塞进 prompt，
  “未找到相关资料”几乎不会触发，幻觉主要来自这里。
- 默认值依据：`bge-reranker-v2-m3` 官方模型卡的实例分数里，相关对 ≈ 0.99，不相关对 ≈ 0.0003~0.004
  ⇒ `0.10` 在“不相关带”之上、临界带之下；`bge-m3` 的余弦分布很压缩（不相关的中文长块常见
  0.30~0.40，相关长块 0.45~0.65）⇒ `0.35` 只剪“明显无关”。
  （网上常见的 `similarity=0.70` 是按 OpenAI 系 embedding 标定的，用在 bge-m3 上会把召回卡死，没采用。）
- 调法：两个值都是“下限”，调成 `0` 即关掉过滤；调高则更多提问回答 `NO_ANSWER`。
  事后从日志表看真实分布再定标：`rag_qa_log.meta.cosine_scores`（召回分数）、
  `meta.thresholds`（当时生效的阈值）、`meta.no_answer`（未命中比例）。
- 接口没变：`/chat`、`/chat/stream`、`/qa`、`eval/rag_eval.py` 都不用改；
  阈值只影响“要不要把这段资料交给 LLM”。

### `docling_parser.enable_page_no`：页码（默认关闭）

```yaml
docling_parser:
  enable_page_no: false
```

- 默认 `false`：连页边界占位符都不用，切块行为与修复前**逐字一致**；`rag_chunk.page_no` 仍为 `NULL`，
  prompt 里也不会出现页码行。
- 置 `true`：docling 导出时插入页边界占位符 `<!-- rag-page-break -->`（独占一行），每个 chunk 写入
  `page_no`（所在页），跳页的 section 另在 `rag_chunk.extra_meta.page_span` 写页范围（`[start, end]`）。
  只对带页码的源（PDF/PPTX）有效，`.md/.txt/.html` 依旧为空。
- 宁缺勿错：页边界数量与文档页号序列对不上、或 docling 版本不支持页边界导出时，会打日志并**不写页码**，
  避免写“看着对但其实错”的页号。
- prompt 里的参考资料行：有页码才写 `| 页码：3`，没有就整段省略（以前会写成字面量 `页码：None`）。
- 滑动窗口切出的多个 chunk 共用所在 section 的页范围（精确到 chunk 级需要 block 级映射，本次未做）。

### 切块行为修正：首标题前的正文不再丢弃

`src/markdown_preprocessor.py` 以前把第一个标题之前的正文直接 `clear()` 掉 ⇒
整篇没有标题的文档（纯 `.md` / `.txt` / 部分 PPT 导出）会切出 **0 个 chunk**：
上传显示 success、`page_count` 正常，但永远检索不到，也没有任何报错。

现在这部分正文挂成一个隐式 section（标题用文档名，无文档名时用「前言」），
走与其它 section 完全相同的切块 / 清洗 / 入库链路。已入库的老文档行为不变，重传后即可享受修正。

## 上传文件入库
```
curl -X POST "http://127.0.0.1:8000/upload" -H "Authorization: Bearer <token>" -F "file=@C:\Users\lostar\Downloads\中国社会各阶层分析 (梁晓声) (Z-Library)-1.pdf"
```
