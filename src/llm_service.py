from openai import OpenAI
from settings import settings

class LLMService:
    def __init__(self):
        self.client = OpenAI(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
        )
        self.model = settings.llm.model_name

    def chat(self, prompt: str, temperature: float = 0.1, max_tokens: int = None):
        kwargs = {}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            **kwargs,
        )
        return resp.choices[0].message.content

    def simple_call(self, prompt: str, temperature: float = 0.0, max_tokens: int = None):
        """轻量单轮调用，用于 Query Rewrite / 标题生成等辅助场景"""
        return self.chat(prompt, temperature=temperature, max_tokens=max_tokens)

    def chat_stream(self, prompt: str, temperature: float = 0.1):
        """流式调用，逐 token yield 文本增量，用于 SSE 流式输出"""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content

llm_service = LLMService()
