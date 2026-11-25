from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from markdown_it import MarkdownIt
from markdown_it.token import Token
from transformers import AutoTokenizer
from settings import settings
import re
import uuid
import hashlib
import time

# ===================== 配置区 =====================
CHUNK_SIZE = settings.markdown_preprocessor.chunk_size
OVERLAP = settings.markdown_preprocessor.overlap
MIN_CHUNK_TOKEN = settings.markdown_preprocessor.min_chunk_token
EMBEDDING_MODEL_NAME = settings.markdown_preprocessor.embedding_model_name
LOCAL_FILES_ONLY = settings.markdown_preprocessor.local_files_only
ENABLE_LOG = settings.markdown_preprocessor.enable_log
SKIP_TITLE_ONLY_SECTION = settings.markdown_preprocessor.skip_title_only_section
# =================================================

# 第一个标题之前的正文（或整篇无标题文档）挂到隐式 section 上时的默认标题
PREAMBLE_TITLE = "前言"

@dataclass
class Node:
    title: str
    title_num: str
    md_level: int
    parent: Optional["Node"] = None
    children: List["Node"] = field(default_factory=list)
    content: str = ""
    # 页码（只在 docling_parser.enable_page_no=true 时有值，P0 #4）：
    # page_start = 该 section 起始页；page_end = 下一个标题的起始页（或全文末页）
    page_start: Optional[int] = None
    page_end: Optional[int] = None

    @property
    def path(self) -> str:
        if self.parent is None:
            return self.title
        return f"{self.parent.path} > {self.title}"

    def add_child(self, child: "Node"):
        child.parent = self
        self.children.append(child)


@dataclass
class ChunkItem:
    chunk_id: str
    content_md: str
    section_path: str
    title_num: str
    title: str
    level: int
    page_no: Optional[int] = None
    is_table: bool = False
    token_len: int = 0
    extra_meta: Optional[Dict] = None


def _page_span_meta(page_start: Optional[int], page_end: Optional[int]) -> Optional[Dict]:
    """跳页时在 extra_meta 里记页范围（同页 / 无页码时不写，避免噪音）。

    注意：滑动窗口切出的多个 chunk 带的是同一个 span —— 那是所在 section 的页范围，
    不是这个 chunk 自己的精确页范围（精确到 chunk 级需要 block 级映射，本次不做）。
    rag_chunk.extra_meta 是现成的 JSONB，不需要改表。
    """
    if page_start is None or page_end is None or page_start == page_end:
        return None
    return {"page_span": [page_start, page_end]}


class MarkdownSectionSplitter:
    def __init__(self):
        if ENABLE_LOG:
            print("[Splitter init] 开始加载tokenizer")
        self.md = MarkdownIt("commonmark")
        # ✅ 离线关键参数 local_files_only
        self.tokenizer = AutoTokenizer.from_pretrained(
            EMBEDDING_MODEL_NAME,
            local_files_only=LOCAL_FILES_ONLY,
            trust_remote_code=False
        )
        if ENABLE_LOG:
            print("[Splitter init] tokenizer加载完成")

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    @staticmethod
    def extract_title_number(title_text: str) -> tuple[Optional[str], str]:
        re_title_num = re.compile(r"^([0-9]+(?:\.[0-9]+)*)\s+(.*)")
        m = re_title_num.match(title_text)
        if m:
            num_part = m.group(1)
            pure_text = m.group(2).strip()
            return num_part, pure_text
        return None, title_text.strip()

    def parse_ast_build_tree(
        self,
        md_text: str,
        preamble_title: Optional[str] = None,
        heading_pages: Optional[List[Optional[int]]] = None,
        first_page: Optional[int] = None,
        last_page: Optional[int] = None,
    ) -> List[Node]:
        """把 markdown 解析成标题树（返回根节点列表）。

        D7-a：第一个标题之前的正文（以及整篇没有标题的文档）以前会被直接丢弃
        （缓冲区被 clear()），导致无标题文档（纯 .md / .txt / 部分 PPT 导出）
        切出 0 个 chunk：入库显示 success，但永远检索不到。
        现在把这部分正文挂成一个隐式根 section 保留下来。
        """
        if ENABLE_LOG:
            print("[parse] 进入解析，md文本长度：", len(md_text))
        tokens = self.md.parse(md_text)
        if ENABLE_LOG:
            print(f"[parse] 解析得到token数量：{len(tokens)}")

        root_nodes: List[Node] = []
        md_stack: List[Node] = []
        current_content_buf: List[str] = []
        # 首标题前的正文（此刻还没有归属 node）
        preamble_buf: List[str] = []
        current_heading_node: Optional[Node] = None
        # 页码：标题按文档序消费 heading_pages（markdown_it 遇到标题的顺序
        # == 原始 markdown 里标题行的顺序，所以能够一一对齐）
        page_iter = iter(heading_pages or [])
        heading_seen = 0
        nodes_in_order: List[Node] = []

        while tokens:
            tok = tokens.pop(0)
            if tok.type == "heading_open":
                if current_heading_node is not None:
                    current_heading_node.content = "".join(current_content_buf).strip()
                    if ENABLE_LOG:
                        print(f"[parse] 归档node：{current_heading_node.path}")
                elif current_content_buf:
                    # 碰到首个标题：此前缓冲的内容全部属于「首标题前的正文」
                    preamble_buf.extend(current_content_buf)
                current_content_buf.clear()

                md_level = int(tok.tag[1])
                inline_tok = tokens.pop(0)
                raw_title = inline_tok.content.strip()
                close_tok = tokens.pop(0)
                if ENABLE_LOG:
                    print(f"[parse] heading md_level={md_level}, raw_title='{raw_title}'")

                # 每个标题都消费一个页号（空标题也不例外），否则后面的页号会集体错位
                node_page = next(page_iter, None)
                heading_seen += 1

                if not raw_title:
                    current_heading_node = None
                    continue

                extracted_num, _ = self.extract_title_number(raw_title)
                use_num = extracted_num if extracted_num is not None else ""

                new_node = Node(title=raw_title, title_num=use_num, md_level=md_level)
                new_node.page_start = node_page
                new_node.page_end = node_page
                nodes_in_order.append(new_node)

                while md_stack and md_stack[-1].md_level >= md_level:
                    md_stack.pop()
                if md_stack:
                    parent_node = md_stack[-1]
                    parent_node.add_child(new_node)
                else:
                    root_nodes.append(new_node)

                md_stack.append(new_node)
                current_heading_node = new_node
            else:
                current_content_buf.append(tok.content)

        if current_heading_node is not None:
            current_heading_node.content = "".join(current_content_buf).strip()
            if ENABLE_LOG:
                print(f"[parse] 收尾归档node：{current_heading_node.path}")
        else:
            # 整篇没有标题：全部正文都归隐式 section
            preamble_buf.extend(current_content_buf)

        # 页码校验（#4）：行扫描得到的标题页号数必须等于真正遇到的标题数。
        # 对不上（setext 标题、代码围栏里的 # 等）就整体不写页码，
        # 宁可不写也不写“看着对但其实错”的页号。
        pages_trusted = bool(heading_pages) and heading_seen == len(heading_pages)
        if heading_pages and not pages_trusted:
            print(
                f"[parse] 标题数({heading_seen}) 与行扫描页号数({len(heading_pages)}) 不一致，"
                "本次不写页码"
            )
        if pages_trusted:
            self._assign_page_spans(nodes_in_order, last_page)

        # D7-a：首标题前的正文（或无标题文档的全文）作为隐式根 section 保留，
        # 之后走与其它 section 完全相同的切块 / 清洗 / 入库链路。
        preamble_text = "".join(preamble_buf).strip()
        if preamble_text:
            implicit = Node(
                title=(preamble_title or PREAMBLE_TITLE),
                title_num="",
                md_level=1,
            )
            implicit.content = preamble_text
            if pages_trusted:
                implicit.page_start = first_page
                next_page = nodes_in_order[0].page_start if nodes_in_order else None
                implicit.page_end = (
                    next_page if next_page is not None else (last_page or first_page)
                )
            root_nodes.insert(0, implicit)
            if ENABLE_LOG:
                print(f"[parse] 保留首标题前正文：{implicit.path}，长度：{len(preamble_text)}")

        return root_nodes

    @staticmethod
    def _heading_pages_from_line_map(
        md_text: str,
        page_of_line: List[Optional[int]],
    ) -> Tuple[List[Optional[int]], Optional[int], Optional[int]]:
        """扫原始 markdown 行，返回（按文档序的标题页号, 首行页号, 末页页号）。

        只认 ATX 标题（docling 导出的 md 就是 ATX），代码围栏内的 # 不算标题。
        标题行数与 parse_ast_build_tree 实际遇到的标题数对不上时，调用方会不写页码。
        """
        heading_pages: List[Optional[int]] = []
        first_page: Optional[int] = None
        last_page: Optional[int] = None
        in_fence = False
        for i, line in enumerate(md_text.split("\n")):
            page = page_of_line[i] if i < len(page_of_line) else None
            if page is not None:
                if first_page is None:
                    first_page = page
                last_page = page
            stripped = line.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if not in_fence and re.match(r"^ {0,3}#{1,6}\s+\S", line):
                heading_pages.append(page)
        return heading_pages, first_page, last_page

    @staticmethod
    def _assign_page_spans(nodes_in_order: List[Node], last_page: Optional[int]):
        """给每个 section 算页范围：起始页 → 下一个标题的起始页（或全文末页）。

        section 的正文一直延续到下一个标题为止，所以按文档序的“下一个标题”就是右边界。
        """
        for i, node in enumerate(nodes_in_order):
            nxt = nodes_in_order[i + 1] if i + 1 < len(nodes_in_order) else None
            if nxt is not None and nxt.page_start is not None:
                node.page_end = nxt.page_start
            elif last_page is not None:
                node.page_end = last_page
            else:
                node.page_end = node.page_start

    def dfs_collect_sections(self, node: Node, out: List[Dict]):
        content = node.content.strip()
        if not (SKIP_TITLE_ONLY_SECTION and len(content.splitlines()) <= 1):
            sec_md = f"{'#'*node.md_level} {node.title}\n{node.content}".strip()
            out.append({
                "title_num": node.title_num,
                "title": node.title,
                "md_level": node.md_level,
                "section_path": node.path,
                "content": sec_md,
                # 页码（无页码时为 None）
                "page_start": node.page_start,
                "page_end": node.page_end,
            })
        for child in node.children:
            self.dfs_collect_sections(child, out)

    def sliding_split(self, sec_item: Dict, page_no: int = None) -> List[ChunkItem]:
        text = sec_item["content"]
        section_path = sec_item["section_path"]
        title_num = sec_item["title_num"]
        title = sec_item["title"]
        md_level = sec_item["md_level"]
        # 页码：窗口级别的精确页算不出来，统一用 section 起始页 + section 页范围
        sec_page_start = sec_item.get("page_start")
        sec_page_end = sec_item.get("page_end")
        eff_page_no = page_no if page_no is not None else sec_page_start
        span_meta = _page_span_meta(sec_page_start, sec_page_end)
        chunks = []
        tokens = self.tokenizer.encode(text, add_special_tokens=False)
        start = 0
        while start < len(tokens):
            end = min(start + CHUNK_SIZE, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = self.tokenizer.decode(chunk_tokens, skip_special_tokens=True)
            chunk_len = len(chunk_tokens)
            if chunk_len >= MIN_CHUNK_TOKEN:
                chunks.append(ChunkItem(
                    chunk_id=str(uuid.uuid4()),
                    content_md=chunk_text,
                    section_path=section_path,
                    title_num=title_num,
                    title=title,
                    level=md_level,
                    page_no=eff_page_no,
                    is_table="|" in chunk_text and "\n|" in chunk_text,
                    token_len=chunk_len,
                    # 每个 chunk 拷一份 meta，后面 attach_doc_global_meta 会 update 而不是覆盖
                    extra_meta=dict(span_meta) if span_meta else None,
                ))
            start = start + CHUNK_SIZE - OVERLAP
        return chunks

    def split_markdown(
        self,
        md_text: str,
        page_no: Optional[int] = None,
        preamble_title: Optional[str] = None,
        page_of_line: Optional[List[Optional[int]]] = None,
    ) -> List[ChunkItem]:
        if ENABLE_LOG:
            print("[split_markdown] 开始")
        # 页码链路（#4）：page_of_line 只在 docling_parser.enable_page_no=true 时才有
        heading_pages: Optional[List[Optional[int]]] = None
        first_page: Optional[int] = None
        last_page: Optional[int] = None
        if page_of_line:
            heading_pages, first_page, last_page = self._heading_pages_from_line_map(
                md_text, page_of_line
            )
        root_nodes = self.parse_ast_build_tree(
            md_text,
            preamble_title=preamble_title,
            heading_pages=heading_pages,
            first_page=first_page,
            last_page=last_page,
        )
        sections: List[Dict] = []
        for r in root_nodes:
            self.dfs_collect_sections(r, sections)
        if ENABLE_LOG:
            print(f"[parse] 解析结束，一共生成 {len(sections)} 个section")

        result_chunks = []
        for sec in sections:
            sec_token_cnt = self.count_tokens(sec["content"])
            if ENABLE_LOG:
                print(f"[split_markdown] 处理section path={sec['section_path']}, token={sec_token_cnt}")
            if sec_token_cnt <= CHUNK_SIZE:
                result_chunks.append(ChunkItem(
                    chunk_id=str(uuid.uuid4()),
                    content_md=sec["content"],
                    section_path=sec["section_path"],
                    title_num=sec["title_num"],
                    title=sec["title"],
                    level=sec["md_level"],
                    page_no=page_no if page_no is not None else sec.get("page_start"),
                    is_table="|" in sec["content"] and "\n|" in sec["content"],
                    token_len=sec_token_cnt,
                    extra_meta=_page_span_meta(sec.get("page_start"), sec.get("page_end")),
                ))
            else:
                sub_chunks = self.sliding_split(sec, page_no)
                result_chunks.extend(sub_chunks)

        if ENABLE_LOG:
            print(f"[split_markdown] 过滤前chunk数量：{len(result_chunks)}")
        final = [c for c in result_chunks if c.token_len >= MIN_CHUNK_TOKEN]
        if ENABLE_LOG:
            print(f"[split_markdown] 过滤后chunk数量：{len(final)}")
        return final

    def print_tree(self, node: Node, indent: str = ""):
        print(f"{indent}- {node.title} 【{node.path}】")
        for child in node.children:
            self.print_tree(child, indent + "  ")


# ===================== Chunk 清洗、校验工具函数（新增） =====================
def simple_clean_md_text(text: str) -> str:
    """
    保守轻清洗：只压缩连续空行，不破坏markdown结构、表格、标题、图片
    """
    lines = text.splitlines()
    stripped_lines = [line.rstrip() for line in lines]
    new_lines = []
    prev_empty = False
    for l in stripped_lines:
        if l == "":
            if not prev_empty:
                new_lines.append(l)
                prev_empty = True
        else:
            new_lines.append(l)
            prev_empty = False
    return "\n".join(new_lines).strip()


def filter_invalid_chunks(chunk_list: List[ChunkItem], min_token: int) -> List[ChunkItem]:
    """过滤无效块：过短、纯空、纯分割线、仅图片链接"""
    valid = []
    img_pattern = re.compile(r"^!\[.*?\]\(.*?\)\s*$")
    for ck in chunk_list:
        if ck.token_len < min_token:
            continue
        clean_txt = simple_clean_md_text(ck.content_md)
        if not clean_txt:
            continue
        if clean_txt.strip() in ("---", "***", "-"):
            continue
        if img_pattern.fullmatch(clean_txt.strip()):
            continue
        valid.append(ck)
    return valid


def dedup_chunks(chunk_list: List[ChunkItem]) -> List[ChunkItem]:
    """同文档内基于清洗后文本MD5去重，消除滑动窗口带来完全重复chunk"""
    seen = set()
    out = []
    for ck in chunk_list:
        clean_txt = simple_clean_md_text(ck.content_md)
        h = hashlib.md5(clean_txt.encode("utf-8")).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append(ck)
    return out


def attach_doc_global_meta(
    chunk_list: List[ChunkItem],
    doc_id: str,
    doc_name: str,
    doc_type: str,
    source_path: str
) -> List[ChunkItem]:
    """挂载文档全局元数据到 extra_meta，后续入库统一放到向量库meta和PG"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for ck in chunk_list:
        base_meta = {
            "doc_id": doc_id,
            "doc_name": doc_name,
            "doc_type": doc_type,
            "source_path": source_path,
            "import_time": now
        }
        if ck.extra_meta is None:
            ck.extra_meta = {}
        ck.extra_meta.update(base_meta)
    return chunk_list


def run_chunk_clean_pipeline(
    chunk_list: List[ChunkItem],
    doc_id: str,
    doc_name: str,
    doc_type: str,
    source_path: str,
    min_token: int
) -> List[ChunkItem]:
    """清洗流水线：过滤 → 去重 → 挂载元数据"""
    chunks = filter_invalid_chunks(chunk_list, min_token)
    chunks = dedup_chunks(chunks)
    chunks = attach_doc_global_meta(chunks, doc_id, doc_name, doc_type, source_path)
    return chunks


# ===================== 对外统一入口函数（推荐上层调用这个） =====================
splitter = MarkdownSectionSplitter()

def process_markdown_to_chunks(
    md_text: str,
    doc_id: str,
    doc_name: str,
    doc_type: str,
    source_path: str,
    page_no: Optional[int] = None,
    preamble_title: Optional[str] = None,
    page_of_line: Optional[List[Optional[int]]] = None,
) -> List[ChunkItem]:
    """
    一站式入口：md原文 → 分块 → 清洗校验 → 带完整元数据的chunk列表

    preamble_title：首标题前正文（或无标题全文）隐式 section 的标题，
    一般传文档名（不传则用「前言」）。
    page_of_line：与 md_text 行号一一对齐的页号列表（#4，由 docling 导出时产出）；
    不传则不写页码。
    """
    raw_chunks = splitter.split_markdown(
        md_text,
        page_no=page_no,
        preamble_title=preamble_title or doc_name,
        page_of_line=page_of_line,
    )
    final_chunks = run_chunk_clean_pipeline(
        raw_chunks,
        doc_id=doc_id,
        doc_name=doc_name,
        doc_type=doc_type,
        source_path=source_path,
        min_token=MIN_CHUNK_TOKEN
    )
    return final_chunks