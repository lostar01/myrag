# 记忆能力：短期（会话摘要）+ 长期（用户画像/偏好，含向量召回）

## Context

现在系统只有很弱的"记忆"，且跨会话完全失忆：

- **会话内**：`src/chat_window.py::trim_history()` 按 tiktoken cl100k 估 token，超过
  `chat.max_total_tokens`（当前 4096）就从最旧的消息开始**硬切**，切掉的内容永久丢失、不做任何压缩
  ⇒ 长对话会忘掉自己前面的结论；
- **多轮**：只有 `rag_prompt.rewrite_query()` 把当前问题改写成可检索问题，模型本身并不"记得"上文；
- **跨会话**：`chat_session.meta` / `chat_message.meta` 两个 JSONB 列全是 `{}`（现成存放位没人用），
  用户每次新会话都要重新交代背景（"我是做财务的""回答简短点""只看 2024 年之后的材料"）。

目标：在**不新增任何中间件**（沿用 PG + Qdrant + Celery + TEI + OpenAI 兼容 LLM）的前提下，做出
短期滚动摘要 + 长期画像记忆（PG 为真相、Qdrant 做语义召回），并且 **默认关闭、开关化、
关闭时 prompt 与现在逐字一致、记忆任何故障都不能影响问答主链路**。

## Decisions locked（已与用户确认）

- **D1**：短期 + 长期一起做。
- **D2**：长期记忆只做**用户画像 / 偏好**（领域、角色、回答风格、时间范围偏好、常用术语），
  **不**做"会话结论/知识库事实沉淀"（那类内容由短期摘要覆盖，且长期塞回事实会污染检索边界）。
- **D3**：写入走**异步 Celery**（回答结束后抽），不在 SSE 路径同步调 LLM ⇒ 零延迟代价；
  "刚说的一句画像这一轮就用不上"是可以接受的（下一轮生效）。
- **D4**：**向量语义召回本次一起上**（新建 Qdrant collection `rag_memory`，复用 bge-m3 1024 维 COSINE）。
- **D5**：**记忆完全私有**：与 `/sessions` 同一原则 —— 管理员也只能看/删自己的记忆，
  没有任何"管理员查看他人记忆"的接口；前端用户端提供「我的记忆」列表 + 手动增删改。
- **D6**：`chat.max_total_tokens` 本次调到 **8192**。
  ⚠️ 前置确认：ollama 侧 `qwen2.5:latest` 的 `num_ctx` 必须 ≥ 8192，否则服务端会自己截断，
  表现为"答非所问/丢上下文"，比现在还难查（Readme 里要写这条）。

## Approach

### 三层注入，一个原则：PG 是真相，Qdrant 只是索引

问答时按顺序拼进 prompt（**开关关 ⇒ 整段不存在 ⇒ prompt 与现在逐字一致**）：

```
你是专业知识库助手，请严格基于下面提供的参考资料回答用户问题。
如果参考资料里没有对应的信息，直接回答“知识库中未找到相关内容”，禁止编造、臆测。

## 用户画像与偏好            ← 长期·Tier A（固定注入，PG 按权重取前 N 条）
- 用户是做财务的，常用中文提问
- 偏好简短回答，不要长篇铺垫
（仅用于理解背景与偏好，不得当作知识库事实来源）

## 相关记忆                  ← 长期·Tier B（Qdrant 按 user_id 语义召回 top-k，去掉 Tier A 已有的）

## 会话摘要                  ← 短期（被窗口挤掉的更早对话压缩要点，存 chat_session.meta）

## 历史对话                  ← 现有段落，逐字不动
{history_text}

## 参考资料
{context}

## 用户当前问题
{query}
```

Tier A / Tier B 都是同一批 `rag_memory` 条目（画像/偏好），只是取法不同：A 是"永远带上的核心画像"，
B 是"与当前问题相关、但没进核心集的记忆"。这样画像条目涨到上百条时也不用改架构。

### 长期记忆：数据模型（新表，幂等）

```sql
CREATE TABLE IF NOT EXISTS rag_memory (
    memory_id      VARCHAR(64) PRIMARY KEY,
    user_id        VARCHAR(64) NOT NULL,
    kind           VARCHAR(16) NOT NULL,          -- profile（身份/领域） / preference（偏好/风格约束）
    content        TEXT NOT NULL,                 -- 单条 ≤ 60 字（注入时用）
    content_hash   VARCHAR(64) NOT NULL,          -- 归一化(去空白+全角标点) 后 md5 ⇒ 幂等去重
    weight         REAL DEFAULT 0.5,              -- 0~1；命中加成，低权重 + 久未用会被归档
    hits           INTEGER DEFAULT 0,
    last_used_at   TIMESTAMP,
    expires_at     TIMESTAMP,                     -- 默认 created_at + memory.default_ttl_days ⇒ 防长期污染
    status         VARCHAR(16) DEFAULT 'active',  -- active / archived（软删，Qdrant 里的点同步删）
    source_chat_id VARCHAR(64),
    meta           JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMP DEFAULT NOW(),
    updated_at     TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_memory_user
    ON rag_memory(user_id, status, weight DESC, last_used_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_memory_user_hash ON rag_memory(user_id, content_hash);
```

去重/矛盾处理（一期就做，靠 LLM + 哈希两条腿）：

- 抽取提示里带上**该用户现有条目清单**，让模型输出
  `{"add":[{"kind","content","weight"}], "remove":["<memory_id>"]}`
  ⇒ 画像不会自相矛盾（"回答要详细" 覆盖 "回答简短点" 时 remove 旧条目）；
- `content_hash` 走 `ON CONFLICT (user_id, content_hash) DO UPDATE SET hits=hits+1, last_used_at=NOW()`
  ⇒ 同一条画像说一百遍也只有一条记录（照 `uq_rag_doc_file_md5` 的部分唯一索引思路）。

`qdrant_store.py` 的方法 payload 写死了文档 chunk，所以新建 `src/memory_store.py`
（不改造旧类，避免牵连已上线的文档链路）：collection `memory.qdrant_collection_name`（默认 `rag_memory`），
point id = `memory_id`，payload 只放 `{memory_id, user_id, kind, weight}`，内容仍以 PG 为准
（与 rag_chunk / Qdrant 的关系同形）；**collection 惰性创建**（首次使用时建，避免 import 期依赖 Qdrant
——那是已记录的 P1 问题，新代码不再加重它）。

### 短期记忆：滚动摘要（零新表，存 `chat_session.meta`）

```json
{
  "summary": "已压缩的更早对话要点……",
  "summary_cover_at": "2025-11-25T20:31:07",     // 摘要已覆盖到的消息 created_at（断点）
  "summary_updated_at": "2025-11-25T20:31:09"
}
```

- `chat_window` 新增 `split_history(history) -> (kept, dropped)`；`trim_history()` 保留成薄包装
  （`rag_chat.py` / `rag_prompt` 现有调用不变）；
- 触发条件：dropped 非空 **且** dropped 未被 `summary_cover_at` 覆盖 ⇒ 才调用一次 LLM
  （旧摘要 + 新被挤掉的若干轮 → 新摘要，≤ `summary_max_tokens`）；
- `keep_recent_turns`（默认 6 轮）保证最近几轮永远是原文，不会被摘要吞掉；
- 摘要生成失败 ⇒ 保留旧摘要（截断），只打日志，**绝不影响本次回答**（照 `title_service` 的容错风格）。

### 抽取与摘要都在 Celery 里跑（D3）

`src/tasks.py` 新增：

```python
@app.task(bind=True, name="tasks.memory_update_task", max_retries=1, retry_backoff=5)
def memory_update_task(self, user_id: str, chat_id: str): ...
```

- 提交点：`chat_qa_stream` 的 `done` 事件之后（与自动命名同一位置）
  `memory_update_task.apply_async(kwargs={"user_id":…, "chat_id":…})`，
  提交失败只打日志（照 `/upload` 里 `apply_async` 的兜底写法，但不影响回答）；
- 任务内部：
  1. `pg_try_advisory_lock(hashtext('rag_mem:' || user_id))` 拿不到就本轮跳过
     （游标没动 ⇒ 下一轮自动补上，避免同会话并发重复抽取）；
  2. 取 `chat_message` 里 `created_at > meta.memory_extract_cursor` 的新消息 → 画像抽取
     （`llm_service.simple_call`，容错解析 JSON；`max_new_items` 上限）；
  3. 逐条 `embedding_client.embed_texts` → upsert PG + `memory_store` 写向量；`remove` 列表软删 +
     删向量点；超过 `max_items_per_user` 按 (weight, last_used_at) 归档最旧的；
  4. 顺带做本会话摘要压缩（同一次任务里，省一次唤醒）；
  5. 游标写回 `chat_session.meta.memory_extract_cursor`。

### 注入与 token 预算（关键，做不好就撑爆模型）

`rag_prompt.prepare_answer()` 签名扩展（保持向后兼容的关键字参数）：

```python
prepare_answer(history, query, ..., memory_lines=None, summary_text=None)
```

- **reserved 必须按"真实拼好的模板"算**：现在 `trim_context()` 用
  `PROMPT_TEMPLATE.format(history_text=..., context="", query=query)` 估占用；
  新增段落存在时要一并计入，否则参考资料会把窗口挤爆、直接吃模型 400；
  实现方式：由 `rag_prompt` 生成「含记忆段落的实际模板」再 `format(context="")` 量一次，保证口径一致；
- 预算上限固定写死在配置里，便于换算：8192 窗口下
  `Tier A ≤ 12 条 × 60 字`、`Tier B ≤ 4 条 × 60 字`、`summary ≤ 400 token`
  ⇒ 记忆相关合计 ≤ ~1.4k，参考资料仍能拿到约 3~5k（`recall_top_k=6` 个 chunk 放不下时由现有
  `trim_context` 自动裁，行为不变）；
- 召回到的记忆条目做一次 `UPDATE rag_memory SET hits=hits+1, last_used_at=NOW() WHERE memory_id = ANY(%s)`
  （一条语句，不逐条 UPDATE），作为后续排序/归档的依据。

`rag_chat_qa._log_qa()` 的 meta 里追加：`memory_ids`、`memory_scores`（向量召回分数）、
`summary_used: true/false`、`memory_kinds` —— 不新增表，事后用 SQL 校准 `recall_min_score`：

```sql
SELECT meta->'memory_scores', meta->'memory_ids' FROM rag_qa_log ORDER BY created_at DESC LIMIT 50;
```

（记忆条目是短文本，余弦分布和文档块不一样，`retrieval.cosine_min_score=0.35` 不能直接套用 ⇒
默认 `memory.recall_min_score: 0.45`，上线后按上面的分布再调。）

### 接口与前端（完全私有，D5）

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/memories` | 登录 | 只看自己的：`limit/offset/kind`，返回 total；管理员同样只看自己的 |
| POST | `/memories` | 登录 | 手动加一条画像/偏好（`kind` + `content`，≤60 字，走同一个 `content_hash` 幂等） |
| PUT | `/memories/{id}` | 登录 | 改内容 / 停用（`status=archived` 会同步删向量点） |
| DELETE | `/memories/{id}` | 登录 | 删一条（软删 + 删 Qdrant 点） |
| DELETE | `/memories` | 登录 | 清空我的记忆（误抽一堆时自救） |

全部用现成的 `get_current_user`（P0 #7 那套 DB 校验），**没有任何管理员旁路**。

前端：用户端用户菜单（`web/user/index.html` 的 `user-menu-pop`，现有「🔑 修改密码」旁边）加
「🧠 我的记忆」→ 用 `common.js::openModal/closeModal` 弹窗，里面是条目列表（kind 标签 + 内容 + 权重 +
时间 + 删除）、一个「手动添加」输入框、一个「清空我的记忆」危险按钮 —— 完全照
`web/user/app.js::openChangePasswordModal()` 的现成模式写，不新增视图、不动路由。

### 配置（默认全关，照 `enable_page_no` 的路子）

```yaml
chat:
  max_total_tokens: 8192          # D6

memory:
  enable: false                   # 总开关：false ⇒ prompt 与现在逐字一致
  qdrant_collection_name: "rag_memory"
  short_term:
    enable_summary: true
    keep_recent_turns: 6          # 最近 N 轮永远原文
    summary_trigger_ratio: 0.6    # 历史估算 token 超过 0.6×max_total_tokens 才压
    summary_max_tokens: 400
  long_term:
    enable_profile: true          # Tier A + 抽取
    enable_vector: true           # Tier B（本次上）
    profile_limit: 12
    profile_max_chars: 60
    recall_top_k: 4
    recall_min_score: 0.45
    max_items_per_user: 200
    default_ttl_days: 180
    max_new_items: 5              # 单次任务最多写入几条
    extract_every_n_turns: 1      # 1=每轮抽一次；调 3 可省 LLM 调用
```

`src/settings.py` 里必须是**带默认值的可选块**（`MemorySettings = MemorySettings()`），
否则旧 `config.yaml` 缺这段会加载失败 —— 这条已经有过教训，注释里写死。

### 灰度顺序（出问题只回退一层）

`enable: false` 上线 ⇒ 开 `short_term.enable_summary`（只影响会话内，风险最低）⇒
开 `long_term.enable_profile`（PG 固定注入）⇒ 开 `long_term.enable_vector`（Qdrant 召回）。

## Files to modify

| 文件 | 改动 |
| --- | --- |
| `src/settings.py` | 新增 `MemorySettings`（带默认值）+ `AppSettings.memory` |
| `config/config.yaml` / `config/config.yaml.example` | `memory:` 块 + `chat.max_total_tokens: 8192`（真实值只改本地那份） |
| `sql/init.sql` | `rag_memory` 建表 + 索引；老库升级段加一个幂等 DO 块；字段注释 |
| `src/memory_store.py`（**新**） | Qdrant `rag_memory` collection 惰性创建、`upsert_memory`、`delete_memories`、`recall(user_id, vector, top_k, min_score)` |
| `src/memory_service.py`（**新**） | 画像抽取/去重/归档、Tier A 加载、Tier B 召回+命中回写、摘要压缩、游标与 advisory lock |
| `src/pg_store.py` | `get_memories_by_ids()`（照 `get_chunks_by_ids` 的 `= ANY(%s)` 写法） |
| `src/chat_window.py` | `split_history()`（`trim_history` 变薄包装） |
| `src/rag_prompt.py` | 记忆/摘要段落拼装 + `prepare_answer(..., memory_lines, summary_text)` + reserved 口径修正 |
| `src/rag_chat_qa.py` | 取记忆/摘要 → 注入；`done` 后提交 `memory_update_task`；日志 meta 记 memory_ids/scores/summary_used |
| `src/rag_chat.py` | 走同一套（eval 口径一致） |
| `src/tasks.py` | `memory_update_task` |
| `src/main_api.py` | `/memories` 五个端点（仅本人） |
| `web/user/index.html`、`web/user/app.js` | 「🧠 我的记忆」菜单项 + 弹窗（复用 `openModal/api/escapeHtml/formatTime`） |
| `Readme.md` | 配置说明、隐私与权限、ollama `num_ctx ≥ 8192` 前置确认、`recall_min_score` 校准 SQL、回退开关 |

## Reuse（已核对存在的现成件）

- `pg_store.query_one/query_all/execute/transaction`、`get_chunks_by_ids`（IN 查询范式，`src/pg_store.py:217`）
- `chat_store.get_session/update_session(meta=)`（`chat_session.meta` 现成 JSONB；注意 `update_session`
  是**整列覆盖** ⇒ 必须先读后改，只合并需要的 key）
- `title_service.py`：有超时 + 失败降级的 LLM 调用范式；`llm_service.simple_call`
- `embedding_client.embed_texts / embed_text`；`retrieval.py` 的 `query_points(score_threshold=)` 写法；
  `qdrant_store.delete_by_doc_id` 的 `FilterSelector` 删除范式
- `celery_app` + `tasks.py`（`@app.task(bind=True, …)`）+ `main_api` 里 `apply_async` 的提交与兜底写法
- `rag_prompt.history_to_text / trim_context / assemble_prompt / prepare_answer`
- 前端 `common.js::openModal/closeModal/api/escapeHtml/formatTime`、`user/app.js::openChangePasswordModal` 模式
- 幂等升级范式：`sql/init.sql` 第 5 段 DO 块 + 部分唯一索引

## Steps

- [x] 1 配置：`MemorySettings`（带默认值）+ `memory:` 块（example + 本地）+ `chat.max_total_tokens: 8192`；确认 ollama `num_ctx`
      → 字段全部**扁平**（`enable_summary` / `enable_profile` / `enable_vector`，没有嵌两层），
      `env_prefix="RAG_MEMORY_"`（避开 `enable` 这种短名被环境变量误顶）。本地 config 已写入并确认被 gitignore。
- [x] 2 SQL：`rag_memory` 建表/索引/注释 + 老库幂等 DO 块；在已有库上重跑验证
      → 已对当前开发库（PG 16.16）执行 `sql/init.sql` 并核对：`rag_memory` 14 列就位、
      `rag_memory_pkey` + `idx_rag_memory_user` + `uq_rag_memory_user_hash` 三个索引就位、
      `rag_user.token_epoch` 也在（P0 的迁移同时生效）。新表无需 DO 块，重跑全部 `already exists, skipping`。
      顺手修了一处在这文件头注释里的明文口令（URL-encode 形式 `Rag%23…`）⇒ 改成 `PGPASSWORD` 写法。
      → 代码已写就（`CREATE TABLE IF NOT EXISTS` + 两个索引 + 注释，新表无需 DO 块）；**待用户跑** `sql/init.sql`。
- [x] 3 `memory_store.py`：惰性建 collection、upsert/删除/按 user 过滤召回（payload 只放索引字段）
      → point id = `memory_id`，payload 仅 `memory_id/user_id/kind/weight`，正文永远从 PG 取。
- [x] 4 `pg_store.get_memories_by_ids()`（只返 active 且未过期，`= ANY(%s)` 避开 N+1）
- [x] 5 `memory_service.py`：Tier A 加载、Tier B 召回 + 命中回写、抽取提示词（带现有条目 + `remove` 列表）、
      `content_hash` upsert、TTL/归档、游标 + 并发锁、摘要压缩
      → **实现偏差（有意）**：并发锁不用 `pg_try_advisory_lock`，改成 `chat_session.meta.memory_lease_at`
      租约（单条 UPDATE 原子抢，租期 600s）：advisory lock 是事务级的，要把 LLM 调用包进同一个事务，
      会把连接池的连接长时间占住。抢不到就本轮跳过，游标不动 ⇒ 下轮自动补，不丢记忆。
      另一个偏差：没做 `extract_every_n_turns`（靠游标“无新消息就跳过”已经能控成本，少一个开关）。
- [x] 6 `chat_window.split_history()`（保持 `trim_history` 兼容）
      → 已单测：`trim_history(msgs) == split_history(msgs)[0]`，且 kept+dropped 不丢消息、保留最近 N 轮。
- [x] 7 `rag_prompt`：段落拼装 + `prepare_answer` 参数 + **reserved 按含记忆段落计算**；
      `enable=false` 时段落完全不出现（用 md5 对比确认 prompt 逐字不变）
      → 已在 ragpy 环境实跑比对：无记忆时 `assemble_prompt` 与旧 `PROMPT_TEMPLATE.format` 逐字一致
      （md5 `05a9ac15…`）；记忆文本里的 `{}` 已转义；reserved 含记忆段落（单测验证留得更少）。
      `prepare_answer` 用 `memory_provider` 回调（记忆要用改写后的问题去召回）。
- [x] 8 `rag_chat_qa` / `rag_chat` 接线 + `done` 后 `memory_update_task.apply_async` + 日志 meta
      → 任务在**消息已入库后**提交（避免 worker 读到不完整的本轮对话）；无答案分支也提交（用户问题里常带偏好）；
      `user_id` 从 token 传入；日志 meta 新增 `memory_profile_ids` / `memory_recall_ids` / `memory_scores` / `summary_used`。
      `rag_chat(query)` 默认不带记忆（eval 基线口径不变），要 A/B 就传 `chat_id` + `user_id`。
- [x] 9 `tasks.memory_update_task`（只重试 1 次、不 raise：答案已返回，失败只打日志）
- [x] 10 `/memories` 五个端点（仅本人，无管理员旁路）
      → 已确认路由注册：`/memories`、`/memories/{memory_id}`（GET/POST/PUT/DELETE + DELETE 清空）；
      这五个接口**不受 `memory.enable` 限制**（关掉功能也要能删旧记忆）。
- [x] 11 前端「我的记忆」弹窗（列表/添加/停用/删除/清空）
      → 头像菜单新增 🧠 我的记忆；复用现有 `openModal`。
      注：UI 只做“删除”（后端是软删 archived，等价于停用且可回数据）；改内容走 `PUT` 自动“归档旧 + 新建”。
- [x] 12 Readme（配置、隐私、`num_ctx`、校准 SQL、灰度与回退）
- [ ] 13 回归：`eval/rag_eval.py` 开关前后各跑一次，比对 `report.json` 的 `is_correct` / `has_hallucination`
      → 注意：`rag_eval` 走 `rag_chat(query)`，**默认不带记忆** ⇒ 它量的是基线口径；
      要量开记忆的效果，需传 `chat_id` + `user_id`（两个都传才启用）或在网页上对比。
- [ ] 14 灰度上线（按上面的四层顺序）

## Verification

无测试框架，端到端手工验证（每一步都要能单独回退）：

1. **开关关 = 零变化**：`memory.enable: false` 启动，dump 一次 `assemble_prompt(...)` 输出与改前 md5 相同；
   `/chat/stream`、`/sessions`、`/uploads` 行为不变。
2. **短期摘要**：造一个 30+ 轮的长会话 ⇒ `chat_session.meta` 里出现 `summary` 与 `summary_cover_at`，
   后续请求 prompt 里出现「## 会话摘要」，且**更早的结论能被引用到**（对照：关掉摘要时答不出来）；
   再次提问时 `summary_cover_at` 前进、摘要不重复膨胀（≤ `summary_max_tokens`）。
3. **长期画像**：新会话说"我是做财务的，回答简短点" ⇒ 等 1~2s 后「我的记忆」里出现两条
   （kind=profile / preference）；**换一个会话**提问，日志里 `meta.memory_ids` 非空、回答风格变短。
4. **去重与矛盾**：同一句偏好连说 3 次 ⇒ `SELECT content, hits FROM rag_memory WHERE user_id=…`
   只有一条、`hits` 增加；改口"以后要详细回答" ⇒ 旧条目 `status='archived'`、新条目生效。
5. **向量召回**：手工塞 40 条画像（`POST /memories`），问一个只与其中几条相关的问题 ⇒
   `meta.memory_scores` 里是相关条目；把 `recall_min_score` 调成 `1.0` ⇒ Tier B 应为空（确认阈值真接上了）。
6. **删除一致性**：删一条记忆后，Qdrant 里对应 point 也消失：
   `client.scroll(collection_name='rag_memory', limit=5)` 里查不到该 `memory_id`；「清空我的记忆」后 PG 全
   `archived`、Qdrant 该 user 的点为空。
7. **私有性**：管理员 token 访问 `/memories` 只能看到自己的；不存在按他人 `user_id` 查询的参数；
   直接猜别人的 `memory_id` 做 `PUT/DELETE` ⇒ 404/403（SQL 带 `user_id=%s` 条件）。
8. **故障隔离**：关掉 embedding 服务 / Qdrant ⇒ 问答照常返回（Tier B 空、Tier A 仍在，日志有告警）；
   关掉 celery worker ⇒ 问答照常、记忆不增长（游标不动，worker 起来后补抽）。
9. **预算**：把 `max_total_tokens` 设成 2048（小窗口压测）⇒ 记忆段落先计入 reserved，参考资料自动收缩，
   不出现模型 400 / 截断；再把 ollama `num_ctx` 设回 8192 正常跑。
10. **老库幂等**：`docker exec -i postgres psql -U raguser -d rag_db < sql/init.sql` 在已建库上重跑不报错。

## 冒烟实测结果（已执行：`python src/memory_smoke.py`，连真实 PG/Qdrant/embedding/ollama）

上面 1、2、3、4、5、6、7、9 的能力点已被自动冒烟脚本覆盖（~65 项全通过，临时用户/会话/向量跑完全部清掉）；
8（故障隔离）与 9 的 `num_ctx` 部分仍是上线时人工看。

**冒烟挖出并修掉 5 个真 bug**（纯静态 review 没发现，只有连真实服务跑才暴露）：

1. **租约 SQL 的 NULL 陷阱**：`NULLIF(meta->>'memory_lease_at','') !~ '^[0-9]+$'` 对“没有租约字段”的会话
   结果是 NULL ⇒ CASE 落到 ELSE，`NULL::bigint < …` 也是 NULL ⇒ **新会话永远抢不到租约，抽取与摘要全被跳过**。
   修成 `meta->>'…' IS NULL` 单独一个分支。
2. **游标精度截到秒**：`isoformat(timespec="seconds")` + 同一秒内写入的多条消息 ⇒ “已处理过”判断为假，
   每轮都把同一段对话重新送给 LLM（抽取重复跑、摘要重复压）。修：保留微秒精度。
3. **`_archive` 谎报成功**：原先返 `len(memory_ids)` 而不是实际改到的行数 ⇒ “删/改别人的记忆”
   因为 `user_id` 匹配不到任何行、其实什么也没做，却向调用方返成功（接口层 200）。修：`RETURNING memory_id` + 返真实条数。
4. **向量复活（孤儿向量）**：条目被归档后，`ON CONFLICT DO UPDATE` 只加 `hits` 不管 `status`，
   于是下次 upsert 同一个 point 又把向量写回去 ⇒ 召回命中但 PG 无有效记录（日志已能观察到）。
   修：`ON CONFLICT` 里带 `status` 复活规则 + 非 active 时删向量而不是写向量。
5. **用户删过的记忆会被自动抽回来**：模型“觉得重复”也会把用户手写的条目放进 `remove`。
   修：`remove` 只认自动抽取的条目（`meta.source != 'manual'`）；用户主动删的贴 `meta.user_deleted=true` 且永不复活。
   同时补上：`DELETE /users/{id}` 调 `purge_user` 硬清该用户记忆（之前会残留）。

6. **抽取“自己删自己”的竞赛（用户可见：网页“我的记忆”恒为 0 条）**：小模型（本地 qwen2.5:7b 必现）
   经常在同一轮里返回 `add: [同一句]` + `remove: [这条已有的 memory_id]`。原实现先写 add（同 hash 命中
   ⇒ `hits+1`）再执行 remove ⇒ **把刚写的那条归档掉**，每轮如此 ⇒ `active` 永远为 0，而 worker 日志里
   `added` 看着正常。修法三件套：`remove` 先于 `add` 执行；`remove` 的 id 若与本次 add 内容同 hash 则忽略；
   提示词补第 6 条规则（只改说法时 remove 旧 id + 给新 content，禁止 remove 与 add 同一句）。
   同时给接口加 `archived_total`、前端空列表时提示“另有 N 条已归档”，避免下次再出现“后端有、前端 0 条”的黑箱。

7. **小模型的 remove 不可信 + 从一次性提问推断偏好**（用户现场日志触发发现）：14:18 那次任务
   （`{'added': 0, 'removed': 1}`）把「用户是做财务的」归档了，而那轮对话是在问北京/广东 GDP。
   复现确认：7B 模型在完全无关的对话里也会把现有条目放进 `remove`，还会把“问过 2026 Q3”推断成
   “只看2026年的资料”。修：`_remove_supported()`（词面重叠 / 明确“忘掉”才允许删）+
   `_add_supported()`（“只看+年份”必须有长期信号）+ 提示词第 2、7 条。
8. **租约过期 600s 导致后续轮次被 skip**：同一会话第一次任务后 1 分钟再问，日志出现
   `skipped: another memory task is running`（租约还占着）。修：`run_update` 在 `finally` 里
   `_release_lease()`，租约只用于挡真并发/任务猝死；冒烟用例改成手工占租约来验证防护仍有效。

**Celery worker 链路也已实测**（`src/memory_worker_smoke.py`，独立队列 `smoke_queue` + 临时 worker，10/10 通过）：
worker 真执行了抽取（`{'profile': {'added': 1, 'seen': 4}}`）、PG 与 Qdrant 双写一致、游标与租约写进
`chat_session.meta`、紧接的第二次被租约跳过、开关关着的 worker 直接 `skip`、测试数据自动清空。

过程中踩到一个**配置坑**（已写进 Readme）：嵌套块里 YAML 写了 key 就以 YAML 为准，
`RAG_MEMORY_*` 环境变量顶不动它（pydantic 的 init 优先于 env）⇒ 临时换开关要用 `RAG_CONFIG`。
另外本地 `config.yaml` 当时是 `enable: true` 但三个子开关全 false ⇒ `run_update` 静默空转，
已加：启动时打印一行 `[memory] enable=…｜子开关…`（全关时带 ⚠），`run_update` 也会返回明确 skip 原因。

另外两个实测得到的结论：
- 分数分布（60 字内记忆条目 + bge-m3）：无关 0.40~0.43、相关 0.56~0.58 ⇒ 默认 `recall_min_score=0.45` 合适；
- `qdrant-client` 的 `scroll()` 参数名是 `scroll_filter`（`query_points` 才是 `query_filter`）—— 脚本里踩过。

## Not in scope（本次不做）

- 会话结论/知识库事实的长期沉淀（D2 明确排除）；`rag_qa_log` 情景记忆复用（L3）；
- 记忆导出/导入、跨用户共享记忆、管理员查看他人记忆（D5）；
- 记忆自动衰减/合并的语义级去重（余弦 >0.9 合并）—— 上线后看数据再加；
- 没修、但本次新代码不再加重它的 P1：`chat_window` 用 cl10k 与真实模型/bge 口径不一致、
  settings 嵌套块的环境变量串扰、`qdrant_store`/`pg_store` 在 import 期连外部服务
  （`memory_store` 用的是惰性建集，不在 import 期连 Qdrant）。
