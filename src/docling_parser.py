import json
from pathlib import Path
from typing import List, Optional, Tuple
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat, ConversionStatus
from docling.datamodel.pipeline_options import PdfPipelineOptions
from settings import settings

# ===================== 配置区 ====================================
DOCLING_ARTIFACTS_DIR = settings.docling_parser.docling_artifacts_dir
# 页边界占位符（只在 enable_page_no=true 时用）：docling 会在页边界处插入它，
# 且作为独立 part 用 "\n\n" 拼接 ⇒ 必定独占一行，能安全做行级页码映射。
PAGE_BREAK_MARK = "<!-- rag-page-break -->"
# =================================================================

def build_converter() -> DocumentConverter:
    pipeline_opts = PdfPipelineOptions(
        artifacts_path=Path(DOCLING_ARTIFACTS_DIR),
        do_ocr=True,
        do_table_structure=True,
        generate_picture_images=True,
        images_scale=2.0,
    )

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_opts)
        }
    )
    return converter


def _content_page_sequence(doc) -> List[int]:
    """按文档序取「每个页边界之后的页号」，首个元素是正文首页。

    拷贝的是 docling 自己插页边界的规则（docling_core 序列化器里的 _iterate_items）：
      - 遍历 with_groups=True + traverse_pictures=True（与导出参数一致）；
      - included_content_layers 不传 ⇒ 默认 DEFAULT_CONTENT_LAYERS，与导出一致；
      - 只看带 prov 的 DocItem（Group 没有 prov，自然跳过）；
      - 只有页号**变大**时才算一个边界（与 docling 一致，页序乱跳不会产生额外边界）。
    这样 len(序列) 应当 == 页边界数 + 1；对不上就说明页号不可靠，调用方会宁可不写页码。
    """
    pages: List[int] = []
    prev_page = None
    for item, _level in doc.iterate_items(with_groups=True, traverse_pictures=True):
        prov = getattr(item, "prov", None)
        if not prov:
            continue
        page_no = getattr(prov[0], "page_no", None)
        if page_no is None:
            continue
        if prev_page is None or page_no > prev_page:
            pages.append(page_no)
            prev_page = page_no
    return pages


def _strip_page_marks(lines: List[str]) -> str:
    return "\n".join(ln for ln in lines if ln.strip() != PAGE_BREAK_MARK)


def export_markdown_with_pages(doc) -> Tuple[str, List[Optional[int]]]:
    """导出 markdown + 「每行属于第几页」的映射（P0 #4，开关默认关闭）。

    占位符本身不带页号，所以页号另算（见 _content_page_sequence），两边对上才写。

    return: (去掉占位符后的 md, 与 md 行号一一对齐的页号列表)
            页号不可靠时返回 (md, []) ⇒ 下游不写 page_no，不会写“看着对但其实错”的页码。
    """
    try:
        md = doc.export_to_markdown(
            page_break_placeholder=PAGE_BREAK_MARK,
            # 全页 OCR 的扫描件里正文挂在 PictureItem 下面，不 traverse 就取不到文本
            traverse_pictures=True,
        )
    except TypeError as e:
        # 旧版 docling-core 没有 page_break_placeholder / traverse_pictures 参数：
        # 只关掉页码，解析本身不受影响（requirement.txt 没钉版本，这里是防碎）
        print(
            f"[docling_parser] 当前 docling-core 版本不支持页边界导出（{e}），"
            "本次不写 page_no"
        )
        return doc.export_to_markdown(), []
    lines = md.split("\n")
    marks = sum(1 for ln in lines if ln.strip() == PAGE_BREAK_MARK)
    pages_seq = _content_page_sequence(doc)

    if not pages_seq:
        # 非 PDF（.md/.html/.txt/部分 PPT）没有 prov ⇒ 没有页码可写
        return _strip_page_marks(lines), []

    if marks + 1 != len(pages_seq):
        print(
            f"[docling_parser] 页边界占位符 {marks} 个与页号序列 {len(pages_seq)} 个对不上，"
            "为避免写错页码，本文档不写 page_no"
        )
        return _strip_page_marks(lines), []

    kept: List[str] = []
    page_of_line: List[Optional[int]] = []
    cursor = 0
    current_page = pages_seq[0]
    for ln in lines:
        if ln.strip() == PAGE_BREAK_MARK:
            cursor += 1
            current_page = pages_seq[cursor]
            continue
        kept.append(ln)
        page_of_line.append(current_page)
    return "\n".join(kept), page_of_line


def doc_parser(file_path: str, page_range: Optional[Tuple[int, int]] = None):
    """
    :param file_path: pdf路径
    :param page_range: None 全部页面；传入 (start_page, end_page) 闭区间，从1开始
    :return: doc, json字符串, table_list
    """
    converter = build_converter()
    if page_range is not None:
        result = converter.convert(file_path, page_range=page_range)
    else:
        result = converter.convert(file_path)

    if result.status != ConversionStatus.SUCCESS:
        raise RuntimeError(f"解析失败：{result.errors}")

    doc = result.document
    doc_dict = doc.export_to_dict()
    json_data = json.dumps(doc_dict, ensure_ascii=False, indent=2)

    table_list = []
    for table in doc.tables:
        df = table.export_to_dataframe(doc=doc)
        table_list.append(df)

    return doc, json_data, table_list