import json
from pathlib import Path
from typing import Optional, Tuple
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat, ConversionStatus
from docling.datamodel.pipeline_options import PdfPipelineOptions
from settings import settings

# ===================== 配置区 ====================================
DOCLING_ARTIFACTS_DIR = settings.docling_parser.docling_artifacts_dir
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