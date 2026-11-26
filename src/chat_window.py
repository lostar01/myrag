from typing import List, Dict, Tuple
import tiktoken
from settings import settings

class ChatWindowManager:
    def __init__(self):
        # 从配置读取
        self.max_total_tokens = settings.chat.max_total_tokens  # 比如 8192
        self.encoding = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text: str) -> int:
        return len(self.encoding.encode(text))

    def fit_history(self, history: List[Dict], budget: int) -> List[Dict]:
        """从最新往回累加，不超 budget 就保留（与原先 trim_history 同一算法，只是预算可传）。"""
        total = 0
        keep = []
        for msg in reversed(history):
            cnt = self.count_tokens(msg["content"])
            if total + cnt <= budget:
                keep.append(msg)
                total += cnt
            else:
                break
        return list(reversed(keep))

    def trim_history(self, history: List[Dict]) -> List[Dict]:
        """history: [{"role":"user","content":"xxx"}, ...]
        从头部开始删，直到总token不超限
        """
        return self.fit_history(history, self.max_total_tokens)

    def split_history(
        self,
        history: List[Dict],
        budget: int = None,
        keep_recent_turns: int = 0,
    ) -> Tuple[List[Dict], List[Dict]]:
        """按预算切历史，返回 (kept 正序, dropped 正序)。

        记忆摘要要用这个：进了 kept 的继续当原文，被挤到 dropped 的送去压缩成摘要，
        而不是像以前那样直接丢。

        keep_recent_turns：最近 N 轮（= 2N 条消息）无条件留在 kept。
        原文比摘要可靠，摘要只兜“更早的部分”；为此超一点预算也值得，
        真不够用时由 trim_context 那边去收缩参考资料。
        """
        effective_budget = self.max_total_tokens if budget is None else budget
        kept = self.fit_history(history, effective_budget)
        cut = len(history) - len(kept)
        dropped = history[:max(cut, 0)]

        floor = max(int(keep_recent_turns), 0) * 2
        if floor and len(kept) < len(history):
            need = floor - len(kept)
            if need > 0:
                moved = dropped[-need:] if need <= len(dropped) else dropped[:]
                dropped = dropped[: len(dropped) - len(moved)]
                kept = moved + kept
        return kept, dropped

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
