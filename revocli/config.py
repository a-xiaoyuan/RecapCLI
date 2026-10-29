"""加载 RevoCLI 自身的模型配置。

只读取 RevoCLI 目录下的 `.env`，环境变量优先；不读取目标工作区的配置。
"""

import os
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class ConfigError(ValueError):
    """缺少必需配置。"""


class Config:
    def __init__(self, api_key, model, base_url=None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url


def load_values():
    """只读取 RevoCLI 的 .env 和环境变量，后者优先。"""
    return {**dotenv_values(ENV_FILE), **os.environ}


def load_config():
    values = load_values()
    api_key = (values.get("OPENAI_API_KEY") or "").strip()
    model = (values.get("OPENAI_MODEL") or "").strip()
    base_url = (values.get("OPENAI_BASE_URL") or "").strip() or None
    if not api_key or not model:
        raise ConfigError("请在 RevoCLI 的 .env 或环境变量中设置 OPENAI_API_KEY 和 OPENAI_MODEL")
    return Config(api_key=api_key, model=model, base_url=base_url)
