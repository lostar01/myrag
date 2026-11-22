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

chat_window = ChatWindowManager()
