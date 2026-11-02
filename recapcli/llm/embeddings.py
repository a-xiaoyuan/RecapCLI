"""可选 OpenAI 兼容 Embedding 接口；仅显式配置后启用，不复用聊天密钥。"""

import hashlib
import math

from openai import OpenAI

from ..config import load_values


class EmbeddingClient:
    def __init__(self, model, base_url, api_key="local", timeout=20):
        self.model, self.base_url, self.api_key, self.timeout = model, base_url, api_key, timeout
        self.identity = hashlib.sha256((base_url.rstrip("/") + "\n" + model).encode()).hexdigest()

    @classmethod
    def configured(cls, timeout=20):
        values = load_values()
        model = (values.get("RECAPCLI_EMBEDDING_MODEL") or "").strip()
        base_url = (values.get("RECAPCLI_EMBEDDING_BASE_URL") or "").strip()
        if not model:
            return None
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Embedding 需要配置 RECAPCLI_EMBEDDING_BASE_URL（包含 /v1）")
        return cls(model, base_url, values.get("RECAPCLI_EMBEDDING_API_KEY") or "local", timeout)

    def embed(self, texts):
        with OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout, max_retries=0) as client:
            response = client.embeddings.create(model=self.model, input=texts, encoding_format="float")
        items = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(texts))):
            raise ValueError("Embedding 返回数量或序号不完整")
        vectors = []
        for item in items:
            vector = item.embedding
            if not vector or not all(math.isfinite(value) for value in vector):
                raise ValueError("Embedding 返回无效向量")
            norm = math.sqrt(sum(value * value for value in vector))
            if not norm:
                raise ValueError("Embedding 返回零向量")
            vectors.append([value / norm for value in vector])
        if len({len(vector) for vector in vectors}) > 1:
            raise ValueError("Embedding 向量维度不一致")
        return vectors
