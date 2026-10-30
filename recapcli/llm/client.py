"""模型配置的客户端生命周期；切换只改下一轮，不发送探测或重试请求。"""

from contextlib import ExitStack
import hashlib
import json

from openai import OpenAI, APITimeoutError, APIConnectionError, APIStatusError

from ..config import load_config
from .protocols import complete


def api_error_message(error, model="模型"):
    if isinstance(error, APITimeoutError):
        return f"等待 {model} 返回数据超时；可增大 --request-timeout，或用 /models 查看其他配置。"
    if isinstance(error, APIConnectionError):
        return f"无法连接 {model} 的模型服务，请检查网络和 OPENAI_BASE_URL。"
    if isinstance(error, APIStatusError):
        hints = {400: "请求格式不兼容，请核对模型与协议", 401: "密钥无效或已失效，请检查当前配置引用的密钥",
                 403: "当前密钥没有此模型的访问权限", 404: "模型或接口地址不存在，请核对模型名称和端点",
                 429: "请求被限流，请稍后重试或降低请求频率"}
        hint = hints.get(error.status_code, "模型服务暂时异常，请稍后重试" if error.status_code >= 500 else "请核对服务配置")
        if getattr(error, "code", None) in {"insufficient_quota", "organization_usage_limit_exceeded", "billing_hard_limit_reached"}:
            hint = "模型服务额度不足，请检查提供商账户额度"
        return f"{model} 返回 HTTP {error.status_code}：{hint}。"
    return f"模型请求失败（{type(error).__name__}），请核对协议和模型配置。"


class ModelClient:
    def __init__(self, config, *, client_factory):
        self.config = config
        self._factory = client_factory
        self._stack = ExitStack()
        self._sdk = None
        self._clients = {}

    def __enter__(self):
        try:
            self._sdk = self._open(self.config)
        except BaseException:
            self._stack.close()
            raise
        return self

    def __exit__(self, *exception):
        try:
            return self._stack.__exit__(*exception)
        finally:
            self._clients.clear()
            self._sdk = None

    def _open(self, config):
        identity = (getattr(config, "profile", "default"), config.model, config.base_url,
                    getattr(config, "protocol", "chat_completions"), config.api_key)
        if identity not in self._clients:
            self._clients[identity] = self._stack.enter_context(
                self._factory(api_key=config.api_key, base_url=config.base_url, timeout=30.0, max_retries=0))
        return self._clients[identity]

    @property
    def base_url(self):
        return self._sdk.base_url

    @property
    def model(self):
        return self.config.model

    @property
    def origin(self):
        # 不保存密钥或地址；隔离其他模型、端点或凭据签发的协议私有状态。
        source = [getattr(self.config, "profile", "default"), self.config.model,
                  getattr(self.config, "protocol", "chat_completions"), str(self.base_url), self.config.api_key]
        return hashlib.sha256(json.dumps(source).encode("utf-8")).hexdigest()

    def metadata(self):
        return {"profile": getattr(self.config, "profile", "default"),
                "protocol": getattr(self.config, "protocol", "chat_completions"), "model": self.model,
                "capabilities": {"tools": getattr(self.config, "tools", True),
                                 "images": getattr(self.config, "images", False),
                                 "streaming": getattr(self.config, "streaming", False)},
                "context_bytes": getattr(self.config, "context_bytes", 256 * 1024)}

    def switch(self, profile):
        candidate = load_config(profile)
        sdk = self._open(candidate)
        self.config, self._sdk = candidate, sdk

    def complete(self, *, model, messages, tools=None, tools_allowed=True, timeout=30, context_bytes=None,
                 workspace=None, on_delta=None, check=None):
        if model != self.model:
            raise ValueError("请求模型与当前配置不一致；请通过模型配置切换，避免混用协议私有状态")
        if tools_allowed and tools and not getattr(self.config, "tools", True):
            raise ValueError("当前模型配置 tools=false，不支持工具调用；请切换模型配置")
        if any(message.get("attachments") for message in messages) and not getattr(self.config, "images", False):
            raise ValueError("当前模型配置 images=false；请确认模型支持视觉后开启 images 或切换视觉模型")
        if not getattr(self.config, "streaming", False):
            on_delta = None
        maximum = getattr(self.config, "context_bytes", 256 * 1024)
        if context_bytes is not None:
            maximum = min(maximum, context_bytes)
        return complete(self._sdk, getattr(self.config, "protocol", "chat_completions"),
                        model=model, messages=messages, tools=tools, tools_allowed=tools_allowed,
                        timeout=timeout, origin=self.origin, context_bytes=maximum,
                        workspace=workspace, on_delta=on_delta, check=check)


def request_completion(client, *, model, messages, tools=None, tools_allowed=True, timeout=30, context_bytes=None,
                       workspace=None, on_delta=None, check=None):
    if isinstance(client, ModelClient):
        return client.complete(model=model, messages=messages, tools=tools, tools_allowed=tools_allowed,
                               timeout=timeout, context_bytes=context_bytes, workspace=workspace, on_delta=on_delta, check=check)
    # 保留 Python API 接收现有 OpenAI 客户端的兼容性。
    if isinstance(client, OpenAI):
        # 请求重试由 Agent 统一计数和取消，不叠加调用方 SDK 的默认重试。
        client = client.with_options(max_retries=0)
    return complete(client, "chat_completions", model=model, messages=messages, tools=tools,
                    tools_allowed=tools_allowed, timeout=timeout, context_bytes=context_bytes,
                    workspace=workspace, on_delta=on_delta, check=check)
