"""加载 RecapCLI 自身的模型和搜索配置。

只读取 RecapCLI 目录下的 `.env`，环境变量优先；不读取目标工作区的配置。
"""

import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
MODELS_FILE = PROJECT_ROOT / "models.toml"
PROTOCOLS = {"chat_completions", "responses"}


class ConfigError(ValueError):
    """缺少必需配置。"""


class Config:
    def __init__(self, api_key, model, base_url=None, *, profile="default", protocol="chat_completions",
                 tools=True, context_bytes=256 * 1024, images=False, streaming=False):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.profile = profile
        self.protocol = protocol
        self.tools = tools
        self.context_bytes = context_bytes
        self.images = images
        self.streaming = streaming


def load_values():
    """只读取 RecapCLI 的 .env 和环境变量，后者优先。"""
    return {**dotenv_values(ENV_FILE), **os.environ}


def _model_profiles():
    """配置只来自 RecapCLI 自身；列举配置不要求密钥，也不发送探测请求。"""
    values = load_values()
    profiles = {"default": {
        "model": (values.get("OPENAI_MODEL") or "").strip(),
        "protocol": (values.get("OPENAI_PROTOCOL") or "chat_completions").strip(),
        "base_url": (values.get("OPENAI_BASE_URL") or "").strip() or None,
        "api_key_env": "OPENAI_API_KEY",
    }}
    document = {}
    if MODELS_FILE.is_file():
        try:
            with MODELS_FILE.open("rb") as file:
                document = tomllib.load(file)
        except (OSError, ValueError) as error:
            raise ConfigError("RecapCLI 的 models.toml 无法读取或 TOML 格式不正确") from error
        if set(document) - {"default", "models"} or not isinstance(document.get("models", {}), dict):
            raise ConfigError("models.toml 只支持 default 和 [models.名称] 配置")
        profiles.update(document.get("models", {}))
    for name, spec in profiles.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(spec, dict):
            raise ConfigError("模型配置名只能使用 1–64 个英文字母、数字、下划线或连字符")
        allowed = {"model", "protocol", "base_url", "api_key_env", "tools", "context_bytes", "images", "streaming"}
        if set(spec) - allowed:
            raise ConfigError(f"模型配置 {name} 包含不支持的字段；密钥请通过 api_key_env 引用环境变量")
        if not isinstance(spec.get("model"), str) or (name != "default" and not spec["model"].strip()):
            raise ConfigError(f"模型配置 {name} 缺少 model")
        protocol = spec.get("protocol", "chat_completions")
        if not isinstance(protocol, str) or protocol not in PROTOCOLS:
            raise ConfigError(f"模型配置 {name} 的 protocol 必须是 chat_completions 或 responses")
        key_env = spec.get("api_key_env", "OPENAI_API_KEY")
        if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ConfigError(f"模型配置 {name} 的 api_key_env 必须是环境变量名")
        for capability in ("tools", "images", "streaming"):
            if capability in spec and not isinstance(spec[capability], bool):
                raise ConfigError(f"模型配置 {name} 的 {capability} 必须是布尔值")
        size = spec.get("context_bytes", 256 * 1024)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ConfigError(f"模型配置 {name} 的 context_bytes 必须是正整数（UTF-8 JSON 字节）")
        base_url = spec.get("base_url")
        if base_url is not None:
            if not isinstance(base_url, str):
                raise ConfigError(f"模型配置 {name} 的 base_url 必须是 HTTP(S) 地址")
            try:
                parts = urlsplit(base_url)
                valid = (parts.scheme in {"http", "https"} and parts.hostname
                         and not parts.username and not parts.password and not parts.query and not parts.fragment)
                parts.port
            except ValueError:
                valid = False
            if not valid:
                raise ConfigError(f"模型配置 {name} 的 base_url 必须是无凭据、无查询参数的 HTTP(S) 根地址")
    default = document.get("default", "default")
    if not isinstance(default, str) or default not in profiles:
        raise ConfigError("models.toml 的 default 必须指向已有模型配置")
    return profiles, default, values


def list_model_profiles():
    profiles, default, values = _model_profiles()
    return [{"name": name, "model": spec["model"], "protocol": spec.get("protocol", "chat_completions"),
             "default": name == default, "configured": bool(spec["model"].strip() and
                 (values.get(spec.get("api_key_env", "OPENAI_API_KEY")) or "").strip()),
             "tools": spec.get("tools", True), "images": spec.get("images", False),
             "streaming": spec.get("streaming", False), "context_bytes": spec.get("context_bytes", 256 * 1024)}
            for name, spec in profiles.items()]


def load_config(profile=None):
    profiles, default, values = _model_profiles()
    name = default if profile is None else profile
    if name not in profiles:
        raise ConfigError("未知模型配置；使用 --models 或 /models 查看已有配置")
    spec = profiles[name]
    api_key = (values.get(spec.get("api_key_env", "OPENAI_API_KEY")) or "").strip()
    if name == "default" and (not api_key or not spec["model"].strip()):
        raise ConfigError("请在 RecapCLI 的 .env 或环境变量中设置 OPENAI_API_KEY 和 OPENAI_MODEL")
    if not api_key:
        raise ConfigError(f"模型配置 {name} 缺少 {spec.get('api_key_env', 'OPENAI_API_KEY')}；请在 RecapCLI .env 或环境变量中设置")
    return Config(api_key=api_key, model=spec["model"].strip(), base_url=spec.get("base_url"), profile=name,
                  protocol=spec.get("protocol", "chat_completions"), tools=spec.get("tools", True),
                  context_bytes=spec.get("context_bytes", 256 * 1024), images=spec.get("images", False),
                  streaming=spec.get("streaming", False))


def load_search_api_key():
    """按需读取搜索凭据；URL 读取和离线使用不要求此配置。"""
    values = load_values()
    key = (values.get("TAVILY_API_KEY") or "").strip()
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ConfigError("请在 RecapCLI 的 .env 或环境变量中设置有效的 TAVILY_API_KEY")
    return key
