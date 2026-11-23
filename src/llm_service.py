from openai import OpenAI
from settings import settings

class LLMService:
    def __init__(self):
        self.client = OpenAI(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
        )
        self.model = settings.llm.model_name

    def chat(self, prompt: str, temperature: float = 0.1):
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return resp.choices[0].message.content

    def simple_call(self, prompt: str, temperature: float = 0.0):
        """轻量单轮调用，用于 Query Rewrite 等辅助场景"""
        return self.chat(prompt, temperature=temperature)

llm_service = LLMService()
