# 上下文选择（Context Selection）：分组召回 + 覆盖贪心 + 预算感知

> 范围已与用户确认：**Stage 1 + Stage 2 一起做**；默认策略用推荐方案（`adaptive`）；
> 数值由实现方（本文件）自行评估；来源文档名标注 + lost-in-the-middle 排序一起上。

## Context

**现象（用户实测）**：查询命中大量文档、70+ chunk 的 `rerank_score` 都 > 0.8，相关性都很高；
但上下文只能塞十几条。全局按分数截断 ⇒ 结果集中在少数几篇文档，**丢视角、丢对比信息**。

**本机实测复现（当前 20 篇文档 / 3731 chunks）**：

| 召回方式 | 结果 | 耗时 |
|---|---|---|
| 普通 `query_points(limit=12)` | 12 个点但**只来自 2 篇文档** | 0.08s |
| 普通 `query_points(limit=40)` | 40 个点仍只来自 **3 篇文档** | 0.01s |
| `query_points_groups(group_by="doc_id", limit=12, group_size=1)` | **12 组 = 12 篇文档** | 0.09s |
| `query_points_groups(limit=16, group_size=2)` | 16 组 / ≤32 点 | 0.04s |

⇒ 文档级坍缩在**召回阶段就已经发生**，靠 rerank 之后重排无法补救（候选池里压根没有第 4 篇文档）。
好消息：Qdrant payload 已含 `doc_id` / `doc_name`，服务端 1.20.2 支持 `group_by`，**做文档级多样化召回零额外成本**。

**其余实测约束**：

1. **rerank 单请求超限风险**：TEI `/info` 为 `max_client_batch_size=32`、`max_batch_tokens=16384`；
   本库 chunk `p50=700 / max=702` token ⇒ 单请求超过 23 条就超 batch token。**必须分批**。
   rerank 实测耗时：6 条 1.0s｜12 条 1.1s｜24 条 1.9s｜40 条 3.0s（按 16 条分批）。
2. **真实模型窗口比配置小一半**：`ollama /api/ps` 显示 `context_length=4096`；"针在开头"实测
   5.5k token 仍记得、**8k token 就丢失** ⇒ 现在 `chat.max_total_tokens=8192` 超出部分被静默截断。
   ⇒ 想撑住 12 条 ×700 token（≈8.3k），**必须先把 num_ctx 提到 16384**（Modelfile `PARAMETER num_ctx`
   或 `OLLAMA_CONTEXT_LENGTH=16384`），并用本文件末尾的针测脚本复验。
3. **`trim_context()` 遇超即 break**：会静默砍掉尾部（最低分的那几篇）⇒ 选择器必须预算感知。
4. **`trim_history()` 预算 = 整个窗口**：历史一长就把 context 预算吃光 ⇒ 需要给历史单独设上限。
5. **排序无 tie-break**：分数扎堆时顺序不稳，需 `(-score, doc_id, chunk_id)`。

## Approach（Stage 1 + Stage 2，一次落地）

### 召回层：文档级分组召回（替代"扩大 limit 再靠 rerank 救"）

`retrieval.retrieve()` 增加分组模式：`query_points_groups(group_by="doc_id", limit=recall_groups, group_size=group_size)`，
展平成与现有输出同构的列表（补 `doc_name`、`group_score`、`group_rank`）。
默认 `recall_groups=16`、`group_size=2` ⇒ 最多 32 条候选、覆盖 16 篇文档，rerank 约 2.4s（2 批）。
`group_size=2` 的用途：单证据查询时，同一篇文档还能贡献 2 条；是否需要第 2 条交给选择器按预算与冗余决定。

### 选择层：冗余感知的预算贪心（设施选址覆盖）

新增 `src/context_selector.py`，纯函数、无 IO：

- 目标函数（facility-location，单调子模 ⇒ 贪心有 1−1/e 保证）：
  `f(S) = Σ_i w_i · max_{j∈S} sim(i,j)`，`w_i` = 归一化后的 `rerank_score`；
- 冗余惩罚用 MMR 式项：`gain(e|S) = w_e − λ · max_{j∈S} sim(e,j)`（λ 小，保持近似单调）；
- **预算约束**：每步同时检查 `token_len`，超预算即停（等价 AdaGReS 的做法），并统计 `dropped_by_budget`；
- **文档上限** `per_doc_max_chunk`（默认 2）作为硬约束，防止极端情况下又被单文档占满；
- 相似度矩阵来源：召回时 `with_vectors=True`（已验证 `ScoredPoint.vector` 可用）⇒ **零额外 embedding 成本**；
- 确定性：所有比较键带 `(-score, doc_id, chunk_id)`。

### 三种策略（一个开关切换，可随时回退）

| `retrieval.selector` | 行为 | 用途 |
|---|---|---|
| `score` | 现状：`rerank_all` 后按分数取前 N | 基线 / 一键回退 |
| `doc_quota` | 两遍填充：先每文档 1 条铺满，再补第 2 条 | 对照实验（用户最初提的方案） |
| **`adaptive`（默认）** | 候选里"有效不同文档数 ≥ 槽位" ⇒ 走 `score`；否则走 `coverage`（含 doc 上限） | 生产默认 |

`adaptive` 的依据：论文 *Finding the Right Balance*（arXiv 2610.09412）证明**多样化在干净候选池上有害、
只在候选池冗余且任务需要多条证据时有益**，并给出"按 top-k 里有效不同文档数决定是否多样化"的规则。

### 装配层：预算切分 + 排序

- `chat_window` 增加 `history_ratio`（默认 0.25）：历史先按该比例裁剪，剩下的留给参考资料，
  不再让历史吃满整个窗口；`trim_context` 保留作最后兜底；
- `context_order: lost_in_middle`：选出后按分数把最强证据放**首位与末位**，其余居中（缓解 lost-in-the-middle）；
- 参考资料行加**来源文档名**：`【来源】xx教材.pdf | 【章节路径】… | 页码：3`（`doc_name` 直接取召回 payload，无需额外查询）。

## 数值（自行评估结论，均基于上面实测）

| 配置项 | 取值 | 依据 |
|---|---|---|
| `retrieval.recall_groups` | **16** | 槽位 12 + 4 组余量，让 rerank 有淘汰空间 |
| `retrieval.group_size` | **2** | 兼顾"单证据查询该文档能出第 2 条"与候选规模（≤32） |
| `retrieval.rerank_batch_size` | **16** | 16×700=11200 < TEI `max_batch_tokens=16384`；24 条就会超 |
| `retrieval.rerank_top_k`（槽位） | **12** | 12×700≈8.3k，在 16k 窗口下能全装下，且 rerank 延迟≈1.9s |
| `retrieval.cosine_min_score` | 0.35（不变） | 实测无关 0.40~0.43、相关 0.56~0.58，阈值仍在正确位置 |
| `retrieval.rerank_min_score` | 0.10（不变） | 同上 |
| `retrieval.per_doc_max_chunk` | **2** | 防止被单文档占满；单证据查询仍可出 2 条 |
| `retrieval.diversity_lambda` | **0.3** | 冗余惩罚权重；过大退化成纯多样性（会伤相关性） |
| `retrieval.selector` | **adaptive** | 见上 |
| `retrieval.context_order` | **lost_in_middle** | 零成本，位置偏差有明确文献支持 |
| `chat.max_total_tokens` | **16384** | 12×700 + 历史 4k + 模板/问题/记忆 ≈1k + 生成预留 ⇒ 需要 16k；且必须同步把 ollama `num_ctx` 提到 16384（当前实测 4096） |
| `chat.history_ratio` | **0.25** | 历史上限 = 4096，保证参考资料有 ≈11k 预算 |
| **降级预案** | 若 num_ctx 无法提到 16384（显存/延迟不可接受）⇒ `max_total_tokens=8192`、槽位 8、`recall_groups=12` | 选择器本身预算感知，会自动少装 |

## Files to modify

- `src/context_selector.py`（新）— `score` / `doc_quota` / `coverage` 三种选法 + 预算贪心 + 统计。
- `src/retrieval.py` — 分组召回（`query_points_groups`）、`with_vectors`、`doc_name` / `group_*` 字段。
- `src/rerank_client.py` — 新增 `rerank_all()`（分批请求、返回全部候选）；`rerank()` 语义不变（内部复用）。
- `src/rag_prompt.py` — `prepare_answer` 编排：召回 → rerank_all → 预算 → 选择 → 排序 → 组装；来源行加文档名。
- `src/chat_window.py` — `history_ratio` 预算切分（`trim_history` 保持兼容）。
- `src/settings.py` — `RetrievalSettings` 新字段 + `ChatSettings.history_ratio`（全部带默认值，旧 config 可加载）。
- `config/config.yaml.example`（+ 本地 `config/config.yaml`）、`Readme.md`。
- `src/rag_chat_qa.py` / `src/rag_chat.py` — QA 日志 meta 增补覆盖度指标。
- `src/context_selection_smoke.py`（新）— 选择器与召回分组的冒烟脚本。

## Reuse（已核对存在）

- `src/retrieval.py::retrieve()` 已返回 `doc_id/chunk_id/score/section_path/title/page_no/is_table/content`；
  Qdrant payload 已含 `doc_id`/`doc_name`/`token_len`（`chunk.extra_meta` 全量写入）⇒ 分组与来源名都不缺数据。
- `src/rerank_client.py::RerankClient.rerank()` 已有 `min_score` 过滤与"返回新 dict 不改调用方"。
- `qdrant_client.query_points_groups(group_by=...)`（服务端 1.20.2 已验证可用）、`with_vectors=True` + `ScoredPoint.vector`。
- `src/chat_window.py::count_tokens()` / `trim_context()`；`src/rag_prompt.py::build_context_lines()`。
- `src/rag_eval.py` 现成的 `all_required_hit` / `is_correct` / `has_hallucination`。

## Steps（已全部完成）

- [x] 0 把 ollama `num_ctx` 提到 16384 并复验：
      `ollama create qwen2.5-16k -f Modelfile`（`FROM qwen2.5:latest` + `PARAMETER num_ctx 16384`），
      `/api/ps` 显示 `context_length: 16384`；「针在开头」实测 5.5k / 8k / 12k / **15k** token 全部答出暗号
      （改造前 8k 就丢）。`config.yaml` 的 `llm.model_name` 改为 `qwen2.5-16k`。
- [x] 1 `RetrievalSettings` 新增 `group_by_doc / recall_groups / group_size / rerank_batch_size / selector /
      per_doc_max_chunk / diversity_lambda / context_order`；`ChatSettings.history_ratio`；
      `max_total_tokens=16384`（本地 config + example 均已更新）
- [x] 2 `rerank_client.rerank_all()`：按 `rerank_batch_size=16` 分批（跨批下标对齐），
      带 tie-break 排序、不改入参；`rerank()` 保留旧语义（内部复用）。
      实测 24 条 = 2 批 / 2.19s，两次调用顺序一致。
- [x] 3 `retrieval.retrieve()` 支持分组召回（`query_points_groups(group_by="doc_id")`）+ `with_vectors`，
      展平并补 `doc_name / group_score / group_rank`；不含 `doc_id` 的旧数据/服务端不支持时自动降级为平铺召回。
      实测：平铺 limit=40 → 40 条/3 篇；分组 16×2 → 31 条/**16 篇**（耗时 39ms → 50ms）。
- [x] 4 `src/context_selector.py`：`score / doc_quota / coverage / adaptive` + 预算贪心 + 单文档上限 + 统计。
      **实现要点（踩过坑）**：`coverage` 的 `w_e` 必须用 rerank 原始 sigmoid 分数，不能做 min-max 归一化——
      候选分数本来就都挤在高位时，归一化会把最弱那条压成 0，而 `λ·相似度` 恒为正 ⇒ 冗余惩罚永远失效。
- [x] 5 `rag_prompt.prepare_answer()`：召回 → `rerank_all`（不截断）→ 算 reserved 预算 → 选择 → `lost_in_middle`
      排序 → 组装；`trim_context` 保留兜底；参考资料行加 `【来源】文档名`；`sources` 按分数降序给前端/日志。
- [x] 6 `chat_window.history_ratio=0.25`：历史预算 = 4096，参考资料固定留 ≥11k（以前历史能吃满整个窗口）。
- [x] 7 QA 日志 meta：`selection / selector / docs_selected / docs_total / dropped_by_budget / context_tokens`；
      `rag_eval` 明细加 `docs_selected / context_chunks`。
- [x] 8 `Readme.md`：新配置、四种策略对照表、`adaptive` 的论文依据、预算感知的必要性、调参 SQL、
      `num_ctx` 实测坑与提升方法、降级预案。
- [x] 9 `src/context_selection_smoke.py`：纯逻辑 16 项 + 真实召回/端到端 —— **全部通过**
      （含用户场景复现：高分扎堆时 `score` 覆盖 6 篇 → `adaptive` 覆盖 12 篇且最低分仍 ≥0.909；
      单证据查询下答案所在文档仍能出 2 条）。
- [x] 10 真实回归：`rag_eval.py` 四组（`RAG_EVAL_SELECTOR` / `RAG_EVAL_REPORT`）全部 100% 准确率、0 幻觉；
      另建 `src/context_selection_eval.py`（多源问题、只判“回答是否被参考资料支持”）跑四组对比。
      真实数据的覆盖率对照（同一查询、16 篇候选、不设阈值）：
      `score → 7 篇 | doc_quota → 12 篇 | coverage → 9 篇 | adaptive → 9 篇`。

## Verification

1. **回退一致**：`selector: score` ⇒ 输出 `chunk_id` 序列与改动前逐条一致。
2. **文档覆盖**：同一查询（"不同教材对劳动价值论的解释差异"）⇒ `docs_selected` 从实测的 2~3 提升到 ≥8（目标 12）。
3. **自适应触发**：候选本身已覆盖 ≥槽位文档时，`adaptive` 与 `score` 结果相同（不对干净池做多样化）。
4. **单证据不吃亏**：构造"答案集中在 1 篇文档连续 5 块"的用例 ⇒ 仍能取到该文档多条（`per_doc_max_chunk=2` + 冗余惩罚不误杀）。
5. **预算**：日志 `dropped_by_budget=0`（12 条能全装下）；把 `max_total_tokens` 临时改成 8192 ⇒ 自动少装且不出现模型 400/截断。
6. **批量 rerank**：`recall_groups=16` ⇒ 2 批请求、无 413、分数完整覆盖 32 条。
7. **确定性**：分数相同的候选重复 10 次，顺序一致。
8. **上下文窗口**：针测脚本在 12k token 仍能答出暗号（证明 num_ctx 生效、没有被静默截断）。
9. **端到端**：多源对比类问题 ⇒ 参考来源里出现多本书、回答呈现对比视角；`rag_qa_log.meta.docs_selected` 可观测。
10. **eval**：`src/rag_eval.py` 四组对比，`all_required_hit` 不降、幻觉率不升；若某组下降，说明该测试集偏"单证据"，
    默认值应保持 `adaptive`（它本身会在干净池上退回 `score`）。

## 风险与边界

- **收益依赖"候选冗余"**：论文证据表明多样化在干净池上有害 ⇒ 用 `adaptive` 自动规避；但若语料实际是"多版本教材"，
  `coverage` 的冗余惩罚比文档配额更合适（这也是本次直接上 Stage 2 的原因）。
- **延迟**：召回 0.1s + rerank ≈2.4s + LLM prefill（8k 约 5s）。若不可接受，降 `recall_groups` / 槽位即可。
- **token 计数口径**：`chat_window` 用 cl100k，而模型是 qwen2.5 分词器，两者对中文长度估计有偏差 ⇒
  `max_total_tokens` 只占 `num_ctx` 的 75%，留出误差与生成空间。
- **不改阈值语义**：`cosine_min_score` / `rerank_min_score` 仍负责过滤噪声（问题 A）；
  本次只改"候选之间怎么选"（问题 B），两类问题不混。
