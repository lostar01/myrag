# 切块结构修复：正文不再被压平 + 大表按行切并重复表头

## 背景（用户提问触发）

「国民经济地区生产总值.xlsx 只切出 1 个 chunk」→ 该表 442 token < `chunk_size=700`，1 块正常。
但用户追问「大表是否存在问题」→ 实测发现问题不在粒度，而在**切块阶段破坏结构**。

## 实测证据（修复前）

| 现象 | 数据 |
|---|---|
| 长章节正文被压平成一行 | 全库 3169 个 chunk 中 **2688 个（85%）完全没有换行** |
| 表格字符被压平 | 41 个含 `\|` 的块里 **24 个**被压成单行 |
| 大表丢表头 / 切断行 | 300 行大表切 8 块，第 2 块起无表头；块首块尾都是半行 |
| `is_table` 判错 | 旧启发式 `"\|" in t and "\n\|" in t` 在压平后恒为 False |
| 列表被黏成一坨 | `parse_ast_build_tree` 用 `"".join(tok.content)`，列表项/段落边界全丢 |

根因两条：
1. `sliding_split` 用 `tokenizer.encode → decode` 重建文本，bge-m3 的 XLM-R 分词器带 `nmt_nfkc`
   归一化 ⇒ 换行→空格、全角标点→半角；
2. `parse_ast_build_tree` 拼接正文时不补块分隔。

## 修复

### P1：按原文切片（`markdown_preprocessor._token_offsets` / `_slice_text`）
`tokenizer(text, return_offsets_mapping=True)` 拿 token→字符区间，`text[cs:ce]` 直接切原文。
- offset 数与 token 数不一致时自动退回旧 decode 方式（防御非 fast 分词器）；
- 单块 token 数按切片后的实际编码重算，保证不超预算。

### P2：表格按行组切 + 每块重复表头（`_split_segments` / `_table_header` / `_table_chunks`）
- 识别：连续以 `|` 开头的行 ≥2 且（前 3 行有 `|---|` 或行数 ≥4）⇒ 表格块；
- 表头：第一个 6 行内**最后一个分隔行**及其之前全部算表头（docling 的多层表头）；
- 分组：按 token 预算装**整行**，绝不切断行；单行超预算才让该行独块；
- 写入 `is_table=True`、`extra_meta.row_span=[a,b]`、`extra_meta.table_index`（跳页时叠加 `page_span`）；
- 正文段仍走滑窗（P1 切片），表格段与正文段分开切。

### P3（顺带）：解析阶段保留段落/列表边界
块结束 token（`paragraph_close` / `list_item_close` / fence / code_block / hr）后补 `\n`，
列表项前补 `- ` / `1. ` 标记；列表内段落不再重复补换行（避免白烧 token）。

## 验证（真实环境：ragpy + 真实 PG/Qdrant/embedding）

- 300 行合成大表：**300/300 行无重复无遗漏**，8 块，`row_span` 连续 `[1,40][41,80]…[285,300]`，
  每块都有表头，全部行完整（首尾都以 `|` 结尾）。
- 真实小表（国民经济 xlsx）：仍 1 块，且**内容与旧库里的 chunk 逐字相同**（零回归）。
- 真实 PDF（Agent AI with LangGraph, 19 块）：含换行 14/19 → **19/19**；表格块正确识别；
  `process_markdown_to_chunks` 全链路（过滤/去重/attach meta）正常。
- 边界用例：表格紧跟正文（无空行）仍能取到表头；含 `|` 的普通文本不误判为表格；
  混排（正文+表）分段正确，表格行 120/120 不丢不重。

## 影响与操作

- 只影响**入库时**的切块：老 chunk 不会自动变，需要**重传**文档（file_md5 相同会被新切块替换）。
- 建议优先重传：xlsx / pptx / 含表格的 PDF。
- 未做：`docling` 的 TableItem → DataFrame 结构化切块（单元格对齐更准），留待有真实大表需求时再上。

## 后续可做（不在本次范围）

- 表格「行转句」渲染（`北京市：40468 亿元`）再入向量，中文检索通常更好命中；
- 列表/表格的 `section_path` 细化（现在多块共享同一 section 路径，引用展示区分度低）。
