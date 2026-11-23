from typing import List, Dict
import tiktoken
from settings import settings

class ChatWindowManager:
    def __init__(self):
        # 从配置读取
        self.max_total_tokens = settings.chat.max_total_tokens  # 比如 4096
        self.encoding = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text: str) -> int:
        return len(self.encoding.encode(text))

    def trim_history(self, history: List[Dict]) -> List[Dict]:
        """history: [{"role":"user","content":"xxx"}, ...]
        从头部开始删，直到总token不超限
        """
        total = 0
        # 倒序累加，优先保留最新
        keep = []
        for msg in reversed(history):
            cnt = self.count_tokens(msg["content"])
            if total + cnt <= self.max_total_tokens:
                keep.append(msg)
                total += cnt
            else:
                break
        # 恢复正序
        return list(reversed(keep))

    def trim_context(self, context_items: List[str], reserved_tokens: int) -> List[str]:
        """按 token 预算裁剪参考资料，避免 prompt 总长超模型上限。
        reserved_tokens: 已被历史对话、问题、prompt 模板占用的 token 数。
        返回按原顺序保留的 context 片段（从前往后保留，超出预算即停止）。
        """
        budget = self.max_total_tokens - reserved_tokens
        if budget <= 0:
            return []
        kept = []
        used = 0
        for item in context_items:
            cnt = self.count_tokens(item)
            if used + cnt <= budget:
                kept.append(item)
                used += cnt
            else:
                break
        return kept

chat_window = ChatWindowManager()
