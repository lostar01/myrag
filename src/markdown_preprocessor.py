from typing import List, Dict, Optional
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

@dataclass
class Node:
    title: str
    title_num: str
    md_level: int
    parent: Optional["Node"] = None
    children: List["Node"] = field(default_factory=list)
    content: str = ""

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

    def parse_ast_build_tree(self, md_text: str) -> List[Node]:
        if ENABLE_LOG:
            print("[parse] 进入解析，md文本长度：", len(md_text))
        tokens = self.md.parse(md_text)
        if ENABLE_LOG:
            print(f"[parse] 解析得到token数量：{len(tokens)}")

        root_nodes: List[Node] = []
        md_stack: List[Node] = []
        current_content_buf: List[str] = []
        current_heading_node: Optional[Node] = None

        while tokens:
            tok = tokens.pop(0)
            if tok.type == "heading_open":
                if current_heading_node is not None:
                    current_heading_node.content = "".join(current_content_buf).strip()
                    if ENABLE_LOG:
                        print(f"[parse] 归档node：{current_heading_node.path}")
                current_content_buf.clear()

                md_level = int(tok.tag[1])
                inline_tok = tokens.pop(0)
                raw_title = inline_tok.content.strip()
                close_tok = tokens.pop(0)
                if ENABLE_LOG:
                    print(f"[parse] heading md_level={md_level}, raw_title='{raw_title}'")

                if not raw_title:
                    current_heading_node = None
                    continue

                extracted_num, _ = self.extract_title_number(raw_title)
                use_num = extracted_num if extracted_num is not None else ""

                new_node = Node(title=raw_title, title_num=use_num, md_level=md_level)

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

        return root_nodes

    def dfs_collect_sections(self, node: Node, out: List[Dict]):
        content = node.content.strip()
        if not (SKIP_TITLE_ONLY_SECTION and len(content.splitlines()) <= 1):
            sec_md = f"{'#'*node.md_level} {node.title}\n{node.content}".strip()
            out.append({
                "title_num": node.title_num,
                "title": node.title,
                "md_level": node.md_level,
                "section_path": node.path,
                "content": sec_md
            })
        for child in node.children:
            self.dfs_collect_sections(child, out)

    def sliding_split(self, sec_item: Dict, page_no: int = None) -> List[ChunkItem]:
        text = sec_item["content"]
        section_path = sec_item["section_path"]
        title_num = sec_item["title_num"]
        title = sec_item["title"]
        md_level = sec_item["md_level"]
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
                    page_no=page_no,
                    is_table="|" in chunk_text and "\n|" in chunk_text,
                    token_len=chunk_len
                ))
            start = start + CHUNK_SIZE - OVERLAP
        return chunks

    def split_markdown(self, md_text: str, page_no: Optional[int] = None) -> List[ChunkItem]:
        if ENABLE_LOG:
            print("[split_markdown] 开始")
        root_nodes = self.parse_ast_build_tree(md_text)
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
                    page_no=page_no,
                    is_table="|" in sec["content"] and "\n|" in sec["content"],
                    token_len=sec_token_cnt
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
    page_no: Optional[int] = None
) -> List[ChunkItem]:
    """
    一站式入口：md原文 → 分块 → 清洗校验 → 带完整元数据的chunk列表
    """
    raw_chunks = splitter.split_markdown(md_text, page_no=page_no)
    final_chunks = run_chunk_clean_pipeline(
        raw_chunks,
        doc_id=doc_id,
        doc_name=doc_name,
        doc_type=doc_type,
        source_path=source_path,
        min_token=MIN_CHUNK_TOKEN
    )
    return final_chunks