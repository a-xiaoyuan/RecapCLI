"""多个 MCP 服务的启动、工具合并与路由。"""

from __future__ import annotations

from pathlib import Path

from .config import load_mcp_config
from .provider import MCPToolProvider


class MCPManager:
    def __init__(self, providers=()):
        self.providers = tuple(providers)
        prefixes = [provider.tool_prefix for provider in self.providers]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("MCP 服务名前缀重复，请修改 prefix")
        self.definitions = tuple(
            definition for provider in self.providers for definition in provider.definitions
        )

    @classmethod
    def from_options(cls, workspace, *, repo=False, config_path=None, cwd=None):
        workspace = Path(workspace).resolve()
        providers = []
        try:
            if repo:
                providers.append(MCPToolProvider(workspace, cwd=cwd))
            configs = load_mcp_config(config_path, workspace=workspace) if config_path is not None else ()
            for name, config in configs:
                server_cwd = config["cwd"]
                if server_cwd is None:
                    server_cwd = config["config_dir"]
                elif not Path(server_cwd).is_absolute():
                    server_cwd = config["config_dir"] / server_cwd
                providers.append(MCPToolProvider(
                    workspace=workspace,
                    command=config["command"], cwd=server_cwd, prefix=config["prefix"],
                    timeout=config["timeout"], max_result_bytes=config["max_result_bytes"],
                    env=config["env"],
                    tool_permissions=config["tool_permissions"],
                ))
            return cls(providers)
        except BaseException:
            for provider in providers:
                provider.close()
            raise

    def call(self, name, arguments, **context):
        matches = [provider for provider in self.providers if provider.owns(name)]
        if not matches:
            raise ValueError("未知的 MCP 工具")
        provider = max(matches, key=lambda item: len(item.tool_prefix))
        return provider.call(name, arguments, **context)

    def close(self):
        for provider in self.providers:
            try:
                provider.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
