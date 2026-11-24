"""会话标题生成：用户在新会话里发出第一个问题时，提炼一个简短主题作为对话标题。

策略：
1. 优先让 LLM 把问题提炼成标题（不超过 16 个汉字、无标点引号）；
2. LLM 不可用 / 超时 / 返回异常时，退化为「问题原文截断」，保证标题永远有值；
3. 生成失败也不应影响问答主流程（任何异常都在内部吃掉）。
"""

import re
import threading

from llm_service import llm_service

# 标题最大长度（CJK 字符按 1 个算）
MAX_TITLE_LEN = 16
# LLM 生成标题的超时（秒）：超时立刻用问题截断兜底，不拖慢问答
LLM_TITLE_TIMEOUT = 5.0
# 视为“还没标题”的占位值（前端新建会话时写的就是「新对话」）
PLACEHOLDER_TITLES = {None, "", "新对话", "无标题", "undefined", "null"}

_TITLE_PROMPT = """请为下面这段用户提问生成一个简短的对话标题，要求：
1. 概括问题的主题/意图，不要简单重复原句，也不要加入原问题里没有的引申；
2. 如果只是寒暄问候（比如“你好”），标题写「日常问候」这类描述；
3. 不超过 16 个汉字；
4. 不要任何标点符号、引号、书名号，也不要“标题：”之类前缀；
5. 只输出标题本身，不要解释。

用户提问：{query}
"""

# 需要从标题里去掉的字符：空白、引号、书名号、常见标点
_STRIP_RE = re.compile(r"[\s\"'“”‘’《》「」『』【】()（）\[\]{}<>:：;；,，.。!！?？~～\-—_]+")


def _clean(raw: str) -> str:
    """清洗标题：去引号/标点/换行，截断到 MAX_TITLE_LEN。"""
    text = str(raw or "").strip()
    # 只取第一行（LLM 偶尔会多输出一行解释）
    text = text.splitlines()[0] if text else ""
    text = _STRIP_RE.sub("", text)
    if len(text) > MAX_TITLE_LEN:
        text = text[:MAX_TITLE_LEN]
    return text


def fallback_title(query: str) -> str:
    """规则兜底：直接用问题原文（压掉换行与首尾标点）截断。"""
    title = _clean(query)
    return title or "新对话"


def is_placeholder_title(title) -> bool:
    """判断标题是否还是占位值（None / 空 / 「新对话」）。"""
    return (title or "").strip() in PLACEHOLDER_TITLES


def generate_title(query: str, timeout: float = LLM_TITLE_TIMEOUT) -> str:
    """根据用户首个问题生成会话标题。

    LLM 失败 / 超时 / 返回异常时退回规则截断（永远返回非空）。
    """
    query = (query or "").strip()
    if not query:
        return "新对话"

    result = {}

    def _work():
        try:
            result["title"] = _clean(
                llm_service.simple_call(_TITLE_PROMPT.format(query=query[:500]), max_tokens=32)
            )
        except Exception as e:  # noqa: BLE001 - 任何异常都退化为规则截断
            print(f"[title_service] LLM 生成标题失败，改用问题截断：{e}")

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"[title_service] LLM 生成标题超时（>{timeout}s），改用问题截断")

    title = result.get("title") or ""
    if title and len(title) >= 2:
        return title
    return fallback_title(query)
